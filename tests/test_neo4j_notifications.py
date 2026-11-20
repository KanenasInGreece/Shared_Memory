"""Neo4j notification flood: the scoped readiness CALL and the journal filter.

fact:3775 is the root cause and the measured counts of the flood. Change A
replaces the deprecated importing ``WITH pid`` subquery in
``insight_gate.decision_readiness_cypher()`` with ``CALL (pid) { … }``.
Change B drops the driver's "does not exist" notices for six expected-absent
names, and keeps every other notice.

Cypher is never executed here (all Neo4j is stubbed): the old and new query
texts are checked for equal rows on a live graph, by the operator.
"""
import ast
import importlib
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import insight_gate  # noqa: E402
import log_hygiene  # noqa: E402


# ── T1: scoped CALL in the readiness query ───────────────────────────────────

OLD_READINESS_CYPHER = (
    "UNWIND $ids AS pid CALL { WITH pid MATCH (s:Fact {pg_id: pid}) RETURN s "
    "UNION WITH pid MATCH (s:Decision {pg_id: pid}) RETURN s "
    "UNION WITH pid MATCH (s:Retrospective {pg_id: pid}) RETURN s "
    "UNION WITH pid RETURN null AS s } "
    "WITH pid, count(s) AS spine_nodes, count(CASE WHEN s:Decision THEN 1 END) AS decision_nodes "
    "OPTIONAL MATCH (d:Decision {pg_id: pid})-[rel]->(b) "
    "WHERE ((type(rel) IN ['CONSIDERED', 'GROUNDED_IN', 'INFORMED_BY', 'REJECTED', 'UNDER_CONDITIONS'] AND b:Fact) "
    "OR (type(rel) = 'HAD_OUTCOME' AND b:Retrospective)) "
    "AND b.pg_id IS NOT NULL AND coalesce(b.superseded, false) = false "
    "AND coalesce(b.rem_processed, false) = false AND coalesce(b.rem_invalid, false) = false "
    "WITH pid, spine_nodes, decision_nodes, collect(DISTINCT CASE "
    "WHEN b:Fact THEN 'fact:' + toString(b.pg_id) "
    "WHEN b:Retrospective THEN 'retrospective:' + toString(b.pg_id) "
    "ELSE null END) AS blocking_ids "
    "RETURN pid AS pg_id, (decision_nodes > 0 AND size(blocking_ids) = 0) AS ready, blocking_ids, spine_nodes"
)

NEW_READINESS_CYPHER = (
    "UNWIND $ids AS pid CALL (pid) { MATCH (s:Fact {pg_id: pid}) RETURN s "
    "UNION MATCH (s:Decision {pg_id: pid}) RETURN s "
    "UNION MATCH (s:Retrospective {pg_id: pid}) RETURN s "
    "UNION RETURN null AS s } "
    "WITH pid, count(s) AS spine_nodes, count(CASE WHEN s:Decision THEN 1 END) AS decision_nodes "
    "OPTIONAL MATCH (d:Decision {pg_id: pid})-[rel]->(b) "
    "WHERE ((type(rel) IN ['CONSIDERED', 'GROUNDED_IN', 'INFORMED_BY', 'REJECTED', 'UNDER_CONDITIONS'] AND b:Fact) "
    "OR (type(rel) = 'HAD_OUTCOME' AND b:Retrospective)) "
    "AND b.pg_id IS NOT NULL AND coalesce(b.superseded, false) = false "
    "AND coalesce(b.rem_processed, false) = false AND coalesce(b.rem_invalid, false) = false "
    "WITH pid, spine_nodes, decision_nodes, collect(DISTINCT CASE "
    "WHEN b:Fact THEN 'fact:' + toString(b.pg_id) "
    "WHEN b:Retrospective THEN 'retrospective:' + toString(b.pg_id) "
    "ELSE null END) AS blocking_ids "
    "RETURN pid AS pg_id, (decision_nodes > 0 AND size(blocking_ids) = 0) AS ready, blocking_ids, spine_nodes"
)


def test_readiness_cypher_is_the_pinned_scoped_text():
    """Kills: the deprecated importing-WITH subquery, or any other edit to the query text."""
    assert insight_gate.decision_readiness_cypher() == NEW_READINESS_CYPHER


def test_pinned_new_text_is_the_old_text_with_only_the_scope_changed():
    """The pinned new text differs from the old one by the scope clause and the four importing WITH clauses only."""
    assert OLD_READINESS_CYPHER.count("WITH pid MATCH") == 3
    assert OLD_READINESS_CYPHER.count("WITH pid RETURN null") == 1
    derived = (OLD_READINESS_CYPHER
               .replace("CALL { ", "CALL (pid) { ", 1)
               .replace("WITH pid MATCH", "MATCH")
               .replace("WITH pid RETURN null", "RETURN null"))
    assert derived == NEW_READINESS_CYPHER


def test_readiness_subquery_is_scoped_and_keeps_its_branch_order():
    """Kills: an importing WITH left inside the subquery, or the four UNION branches reordered."""
    q = insight_gate.decision_readiness_cypher()
    start = q.index("CALL (pid) { ")
    body = q[start + len("CALL (pid) { "):q.index(" } ", start)]
    assert "CALL {" not in q
    assert "WITH" not in body
    branches = body.split(" UNION ")
    assert branches == [
        "MATCH (s:Fact {pg_id: pid}) RETURN s",
        "MATCH (s:Decision {pg_id: pid}) RETURN s",
        "MATCH (s:Retrospective {pg_id: pid}) RETURN s",
        "RETURN null AS s",
    ]


# ── T2: the notification filter on real lazily formatted records ─────────────

def _description_for(kind: str, name: str) -> str:
    if kind == "relationship":
        return (f"warn: relationship type does not exist. The relationship type `{name}` "
                "does not exist. Verify that the spelling is correct.")
    return (f"warn: property key does not exist. The property `{name}` does not exist. "
            "Verify that the spelling is correct.")


class _Notice:
    """Stand-in whose repr is the driver 6.2.0 GqlStatusObject repr, field for field."""

    def __init__(self, gql_status, description, raw_classification="UNRECOGNIZED",
                 position="<SummaryInputPosition line=1, column=11, offset=10>"):
        self.gql_status = gql_status
        self.status_description = description
        self.raw_classification = raw_classification
        self.position = position

    def __repr__(self):
        cls = self.raw_classification
        return (
            f"<GqlStatusObject gql_status={self.gql_status!r}, "
            f"status_description={self.status_description!r}, "
            f"position={self.position}, "
            f"raw_classification={cls!r}, "
            f"classification=<NotificationClassification.{cls}: {cls!r}>, "
            "raw_severity='WARNING', "
            "severity=<NotificationSeverity.WARNING: 'WARNING'>, "
            f"diagnostic_record={{'_classification': {cls!r}, '_severity': 'WARNING', "
            "'_position': {'offset': 10, 'line': 1, 'column': 11}, 'OPERATION': '', "
            "'OPERATION_CODE': '0', 'CURRENT_SCHEMA': '/'}>"
        )


class _Printer:
    """Stand-in for the driver's NotificationPrinter(notification, query, one_line=True)."""

    def __init__(self, notice, query):
        self.notice = notice
        self.query = query

    def __str__(self):
        return f"{self.notice!r} for query: {self.query!r}"


DRIVER_MSG = "Received notification from DBMS server: %s"
QUERY = "MATCH (d:Decision)-[:GROUNDED_IN]->(f:Fact) RETURN d"


def _record(notice, query=QUERY, level=logging.WARNING, msg=DRIVER_MSG):
    return logging.LogRecord(
        name="neo4j.notifications", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(_Printer(notice, query),), exc_info=None)


def _absent(kind, name, **kw):
    status = "01N51" if kind == "relationship" else "01N52"
    return _record(_Notice(status, _description_for(kind, name)), **kw)


def _keeps(record) -> bool:
    return log_hygiene.NotificationFilter().filter(record)


def test_expected_absent_sets_hold_exactly_these_names():
    """Kills: a name added to or removed from either set (fact:1309: assert the value, not a second copy)."""
    assert log_hygiene.EXPECTED_ABSENT_PROPERTIES == frozenset({"rem_invalid", "rem_invalid_reason"})
    assert log_hygiene.EXPECTED_ABSENT_RELATIONSHIPS == frozenset(
        {"CONSIDERED", "REJECTED", "UNDER_CONDITIONS", "ALIASES"})
    assert log_hygiene.NOTIFICATION_LOGGER == "neo4j.notifications"


def test_the_record_carries_the_template_and_the_notice_in_args():
    """The fixture is lazy, as the driver's is: the name is only in getMessage(), never in msg."""
    record = _absent("relationship", "CONSIDERED")
    assert record.msg == DRIVER_MSG
    assert "CONSIDERED" not in record.msg
    assert "CONSIDERED" in record.getMessage()


@pytest.mark.parametrize("name", ["rem_invalid", "rem_invalid_reason"])
def test_expected_absent_property_is_dropped(name):
    assert _keeps(_absent("property", name)) is False


@pytest.mark.parametrize("name", ["CONSIDERED", "REJECTED", "UNDER_CONDITIONS", "ALIASES"])
def test_expected_absent_relationship_is_dropped(name):
    assert _keeps(_absent("relationship", name)) is False


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING])
def test_drop_does_not_depend_on_the_level(level):
    assert _keeps(_absent("relationship", "CONSIDERED", level=level)) is False


@pytest.mark.parametrize("record", [
    pytest.param(_absent("relationship", "rem_invalid"), id="property-name-in-relationship-slot"),
    pytest.param(_absent("relationship", "rem_invalid_reason"), id="property-name-2-in-relationship-slot"),
    pytest.param(_absent("property", "ALIASES"), id="relationship-name-in-property-slot"),
    pytest.param(_absent("property", "CONSIDERED"), id="relationship-name-2-in-property-slot"),
    pytest.param(_absent("property", "rem_invalidd"), id="misspelt-property"),
    pytest.param(_absent("relationship", "CONSIDERD"), id="misspelt-relationship"),
    pytest.param(_absent("relationship", "HAD_OUTCOME"), id="unexpected-relationship"),
    pytest.param(_absent("property", "superseded"), id="unexpected-property"),
])
def test_an_absent_name_outside_its_kinds_set_is_kept(record):
    """Kills: swapped kind sets, one merged set, or matching names loosely."""
    assert _keeps(record) is True


def test_expected_name_only_in_the_query_text_is_kept():
    """Kills: matching the name anywhere in the message instead of in the description."""
    query = ("MATCH (d)-[:CONSIDERED]->(f) WHERE f.rem_invalid "
             "// warn: relationship type does not exist. The relationship type `CONSIDERED` does not exist. "
             "warn: property key does not exist. The property `rem_invalid` does not exist.")
    assert _keeps(_absent("relationship", "UNEXPECTED_TYPE", query=query)) is True
    assert _keeps(_absent("property", "unexpected_key", query=query)) is True


# Single quotes only, so the printer's repr() of the query keeps them unescaped
# and the qualifying fields appear in the message verbatim.
FORGED_QUERY = (
    "RETURN '<GqlStatusObject gql_status='01N51', status_description='warn: "
    "relationship type does not exist. The relationship type `CONSIDERED` does not "
    "exist.', raw_classification='UNRECOGNIZED' gql_status='01N52' warn: property key "
    "does not exist. The property `rem_invalid` does not exist.'")


def test_forged_query_text_appears_verbatim_in_the_message():
    message = _absent("relationship", "SOMETHING_ELSE", query=FORGED_QUERY).getMessage()
    assert message.count("raw_classification='UNRECOGNIZED'") == 2
    assert "gql_status='01N51'" in message.split(" for query: ", 1)[1]


def test_qualifying_fields_only_in_the_query_text_are_kept():
    """Kills: reading classification, status or description from anywhere in the message."""
    query = FORGED_QUERY
    deprecation = _Notice(
        "01N00", "warn: feature deprecated. CALL subquery without a variable scope clause is deprecated.",
        raw_classification="DEPRECATION")
    assert _keeps(_record(deprecation, query=query)) is True
    other = _Notice("01N51", _description_for("relationship", "SOMETHING_ELSE"),
                    raw_classification="PERFORMANCE")
    assert _keeps(_record(other, query=query)) is True
    assert _keeps(_absent("relationship", "SOMETHING_ELSE", query=query)) is True
    assert _keeps(_absent("property", "something_else", query=query)) is True


def test_a_classification_other_than_unrecognized_is_kept():
    """Kills: dropping the classification check."""
    for cls in ("DEPRECATION", "PERFORMANCE", "GENERIC", "HINT"):
        notice = _Notice("01N51", _description_for("relationship", "CONSIDERED"), raw_classification=cls)
        assert _keeps(_record(notice)) is True, cls


def test_a_deprecation_notice_is_kept():
    notice = _Notice(
        "01N00", "warn: feature deprecated. CALL subquery without a variable scope clause is "
        "deprecated. Use CALL (pid) { ... }", raw_classification="DEPRECATION")
    assert _keeps(_record(notice)) is True


def test_contradictory_status_and_description_are_kept():
    """Kills: ignoring gql_status, or matching a description against the other kind's code."""
    rel_status_prop_text = _Notice("01N51", _description_for("property", "rem_invalid"))
    prop_status_rel_text = _Notice("01N52", _description_for("relationship", "CONSIDERED"))
    assert _keeps(_record(rel_status_prop_text)) is True
    assert _keeps(_record(prop_status_rel_text)) is True


@pytest.mark.parametrize("status", ["01N50", "01N53", "01N42", ""])
def test_a_wrong_status_with_a_right_description_is_kept(status):
    """Kills: ignoring gql_status."""
    assert _keeps(_record(_Notice(status, _description_for("relationship", "CONSIDERED")))) is True
    assert _keeps(_record(_Notice(status, _description_for("property", "rem_invalid")))) is True


@pytest.mark.parametrize("msg,args", [
    ("Received notification from DBMS server: %s", ("garbage that is not a notice",)),
    ("Received notification from DBMS server: <GqlStatusObject gql_status='01N51'>", ()),
    ("some other driver line about `CONSIDERED` and raw_classification='UNRECOGNIZED'", ()),
    ("Received notification from DBMS server: %d", ("not a number",)),
])
def test_an_unparsable_record_is_kept(msg, args):
    record = logging.LogRecord("neo4j.notifications", logging.WARNING, __file__, 1, msg, args, None)
    assert _keeps(record) is True


def test_a_parser_exception_keeps_the_record(monkeypatch):
    """Kills: failing closed (returning False) when the parser raises."""
    def boom(_message):
        raise RuntimeError("parser broke")
    monkeypatch.setattr(log_hygiene, "_is_expected_absent_name", boom)
    assert _keeps(_absent("relationship", "CONSIDERED")) is True


def test_a_description_in_double_quotes_still_parses():
    """repr() switches to double quotes when the description holds a single quote."""
    description = _description_for("relationship", "CONSIDERED") + " It's fine."
    message = _record(_Notice("01N51", description)).getMessage()
    assert '"warn:' in message
    head = log_hygiene._NOTICE_RE.match(message)
    assert head is not None
    assert ast.literal_eval(head.group("description")) == description


@pytest.mark.parametrize("kind,name", [("property", "rem_invalid"), ("relationship", "CONSIDERED")])
def test_trailing_unexpected_content_after_the_description_is_kept(kind, name):
    """Kills: match() instead of fullmatch() on the description."""
    for tail in (" Additional unexpected content.", " Verify that the spelling is correct. More.", "x"):
        bare = _description_for(kind, name).replace(" Verify that the spelling is correct.", "")
        status = "01N51" if kind == "relationship" else "01N52"
        assert _keeps(_record(_Notice(status, bare + tail))) is True, tail


@pytest.mark.parametrize("kind,name", [("property", "rem_invalid"), ("relationship", "CONSIDERED")])
def test_the_real_description_is_dropped_with_and_without_the_verify_tail(kind, name):
    status = "01N51" if kind == "relationship" else "01N52"
    full = _description_for(kind, name)
    assert full.endswith(". Verify that the spelling is correct.")
    assert _keeps(_record(_Notice(status, full))) is False
    bare = full.replace(" Verify that the spelling is correct.", "")
    assert _keeps(_record(_Notice(status, bare))) is False


def test_a_notice_with_position_none_is_parsed():
    """position=None is the driver's repr when the server sends no position."""
    expected = _Notice("01N51", _description_for("relationship", "CONSIDERED"), position=None)
    other = _Notice("01N51", _description_for("relationship", "TYPO_REL"), position=None)
    assert "position=None, " in repr(expected)
    assert _keeps(_record(expected)) is False
    assert _keeps(_record(other)) is True
    prop = _Notice("01N52", _description_for("property", "rem_invalid"), position=None)
    assert _keeps(_record(prop)) is False


@pytest.fixture
def notification_logger():
    """The driver's logger with only the owned filter and a capturing handler; restored afterwards."""
    target = logging.getLogger("neo4j.notifications")
    saved_filters = list(target.filters)
    saved_level, saved_propagate = target.level, target.propagate
    for f in saved_filters:
        target.removeFilter(f)
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = _Capture(level=logging.DEBUG)
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    target.propagate = False
    log_hygiene.install_notification_filter()
    try:
        yield target, captured
    finally:
        target.removeHandler(handler)
        for f in list(target.filters):
            target.removeFilter(f)
        for f in saved_filters:
            target.addFilter(f)
        target.setLevel(saved_level)
        target.propagate = saved_propagate


def test_delivery_dropped_records_never_reach_the_handler(notification_logger):
    """Kills: the filter installed on a different logger name."""
    target, captured = notification_logger
    target.warning(DRIVER_MSG, _Printer(_Notice("01N51", _description_for("relationship", "CONSIDERED")), QUERY))
    target.warning(DRIVER_MSG, _Printer(_Notice("01N52", _description_for("property", "rem_invalid")), QUERY))
    target.warning(DRIVER_MSG, _Printer(_Notice("01N51", _description_for("relationship", "TYPO_REL")), QUERY))
    target.warning(DRIVER_MSG, _Printer(_Notice(
        "01N00", "warn: feature deprecated.", raw_classification="DEPRECATION"), QUERY))
    messages = [r.getMessage() for r in captured]
    assert len(messages) == 2
    assert "`TYPO_REL`" in messages[0]
    assert "DEPRECATION" in messages[1]


def _real_driver_notice(neo4j_code, gql_status, description):
    from neo4j._work.summary import GqlStatusObject
    return GqlStatusObject._from_status_metadata({
        "gql_status": gql_status,
        "status_description": description,
        "neo4j_code": neo4j_code,
        "title": "",
        "diagnostic_record": {
            "_classification": "UNRECOGNIZED", "_severity": "WARNING",
            "_position": {"offset": 10, "line": 1, "column": 11},
            "OPERATION": "", "OPERATION_CODE": "0", "CURRENT_SCHEMA": "/",
        },
    })


def _run_driver_handle_warnings(notices, query):
    """Run the driver's own AsyncResult._handle_warnings, the code that logs every notice."""
    from neo4j._async.work.result import AsyncResult
    summary = SimpleNamespace(gql_status_objects=notices)
    fake = SimpleNamespace(
        _warn_notification_severity=None,
        _obtain_summary=lambda: summary,
        _metadata={"query": query},
        _creation_frame=False,
    )
    AsyncResult._handle_warnings(fake)


def test_driver_logs_notices_lazily_and_the_filter_reads_them(notification_logger):
    """Pins the driver's record construction: template in msg, the printer in args, the filter on getMessage()."""
    target, captured = notification_logger
    kept_filter = [f for f in target.filters if getattr(f, "shared_memory_owned", False)]
    target.removeFilter(kept_filter[0])  # first see every record the driver emits
    rel = _real_driver_notice("Neo.ClientNotification.Statement.UnknownRelationshipTypeWarning",
                              "01N51", _description_for("relationship", "CONSIDERED"))
    prop = _real_driver_notice("Neo.ClientNotification.Statement.UnknownPropertyKeyWarning",
                               "01N52", _description_for("property", "rem_invalid"))
    typo = _real_driver_notice("Neo.ClientNotification.Statement.UnknownRelationshipTypeWarning",
                               "01N51", _description_for("relationship", "CONSIDERD"))
    _run_driver_handle_warnings([rel, prop, typo], QUERY)
    assert len(captured) == 3
    for record in captured:
        assert record.name == "neo4j.notifications"
        assert record.levelno == logging.WARNING
        assert record.msg == DRIVER_MSG
        assert len(record.args) == 1
        assert type(record.args[0]).__name__ == "NotificationPrinter"
        assert record.getMessage().startswith(
            "Received notification from DBMS server: <GqlStatusObject gql_status='01N5")
        assert record.getMessage().endswith(f" for query: {QUERY!r}")
    owned = log_hygiene.NotificationFilter()
    assert [owned.filter(r) for r in captured] == [False, False, True]

    captured.clear()
    target.addFilter(kept_filter[0])
    _run_driver_handle_warnings([rel, prop, typo], QUERY)
    assert len(captured) == 1
    assert "`CONSIDERD`" in captured[0].getMessage()


# ── T3: install, idempotent, and called by each entry point ──────────────────

def _owned(target):
    return [f for f in target.filters if getattr(f, "shared_memory_owned", False)]


def test_install_attaches_one_owned_filter_and_is_idempotent(notification_logger):
    """Kills: a second install adding another filter, or the filter on another logger."""
    target = logging.getLogger("neo4j.notifications")
    first = _owned(target)
    assert len(first) == 1
    again = log_hygiene.install_notification_filter()
    assert _owned(target) == first
    assert again is first[0]


def test_a_third_party_filter_is_preserved_and_still_sees_records():
    """A filter attached before the install stays, and still sees every record, dropped or kept."""
    target = logging.getLogger("neo4j.notifications")
    saved_filters = list(target.filters)
    saved_level, saved_propagate = target.level, target.propagate
    for f in saved_filters:
        target.removeFilter(f)
    seen: list[str] = []

    class _ThirdParty(logging.Filter):
        def filter(self, record):
            seen.append(record.getMessage())
            return True

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    third = _ThirdParty()
    handler = _Capture(level=logging.DEBUG)
    target.addFilter(third)
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    target.propagate = False
    try:
        log_hygiene.install_notification_filter()
        log_hygiene.install_notification_filter()
        assert third in target.filters
        assert len(_owned(target)) == 1
        target.warning(DRIVER_MSG, _Printer(_Notice("01N51", _description_for("relationship", "CONSIDERED")), QUERY))
        target.warning(DRIVER_MSG, _Printer(_Notice("01N51", _description_for("relationship", "TYPO_REL")), QUERY))
        assert len(seen) == 2
        assert "`CONSIDERED`" in seen[0] and "`TYPO_REL`" in seen[1]
        assert [("`TYPO_REL`" in r.getMessage()) for r in captured] == [True]
    finally:
        target.removeHandler(handler)
        for f in list(target.filters):
            target.removeFilter(f)
        for f in saved_filters:
            target.addFilter(f)
        target.setLevel(saved_level)
        target.propagate = saved_propagate


ENTRY_POINTS = ["hive_mind_proxy", "rem_loop", "consolidation_loop"]


@pytest.mark.parametrize("module_name", ENTRY_POINTS)
def test_each_entry_point_installs_the_filter_on_import(module_name):
    """Kills: the install call removed from that entry point.

    The import starts clean: the owned filter is removed and the module is
    dropped from sys.modules, so an earlier install cannot mask a removed call.
    The import is made safe the way the other tests do it: conftest.py blanks
    SECURE_ENV_FILE and COORDINATOR_UDS before any daemon import, the Neo4j
    driver constructor is patched (as tests/test_logging.py does), and each
    module opens no connection at import (drivers are built in its classes,
    and its main runs only under ``__main__``). The previous sys.modules entry
    is restored afterwards so no other test sees a second copy.
    """
    target = logging.getLogger("neo4j.notifications")
    saved_filters = list(target.filters)
    for f in _owned(target):
        target.removeFilter(f)
    unrelated = logging.Filter("unrelated")
    target.addFilter(unrelated)
    saved_module = sys.modules.pop(module_name, None)
    assert _owned(target) == []
    try:
        with patch("neo4j.AsyncGraphDatabase.driver"):
            importlib.import_module(module_name)
        assert len(_owned(target)) == 1
        assert unrelated in target.filters
    finally:
        if saved_module is not None:
            sys.modules[module_name] = saved_module
        else:
            sys.modules.pop(module_name, None)
        for f in list(target.filters):
            target.removeFilter(f)
        for f in saved_filters:
            target.addFilter(f)


# ── T4: pure consumers of the readiness rows ─────────────────────────────────

def test_readiness_by_id_absent_id_row_is_kept_and_not_ready():
    """An id that exists nowhere comes back as spine_nodes 0 and ready false."""
    rows = [{"pg_id": 99, "ready": False, "blocking_ids": [], "spine_nodes": 0}]
    assert insight_gate._readiness_by_id(rows) == {99: rows[0]}


def test_admit_members_ignores_an_absent_id_not_in_the_walk():
    labels = {1: "Decision"}
    readiness = [
        {"pg_id": 1, "ready": True, "blocking_ids": [], "spine_nodes": 1},
        {"pg_id": 99, "ready": False, "blocking_ids": [], "spine_nodes": 0},
    ]
    new_labels, _c, comps, excluded = insight_gate.admit_members(labels, {1: False}, [[1]], readiness)
    assert excluded == []
    assert new_labels == {1: "Decision"}
    assert comps == [[1]]


def test_admit_members_duplicate_ids_in_the_input_rows():
    """UNWIND over a duplicated id returns its row twice: the decision is still dropped once."""
    row = {"pg_id": 5, "ready": False, "blocking_ids": ["fact:7"], "spine_nodes": 1}
    _l, _c, comps, excluded = insight_gate.admit_members(
        {5: "Decision", 6: "Retrospective"}, {5: False}, [[5, 6]], [row, dict(row)])
    assert excluded == [{"pg_id": 5, "blocking_ids": ["fact:7"]}]
    assert comps == [[6]]


def test_admit_members_keeps_a_node_with_two_labels():
    """A pg_id on two spine labels counts spine_nodes 2, so the duplicate refusal still fires."""
    readiness = [{"pg_id": 3, "ready": False, "blocking_ids": ["fact:4"], "spine_nodes": 2}]
    labels = {3: "Decision"}
    new_labels, _c, comps, excluded = insight_gate.admit_members(labels, {3: False}, [[3]], readiness)
    assert excluded == []
    assert new_labels == labels
    assert comps == [[3]]
