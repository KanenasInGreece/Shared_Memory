"""REM prompt version, the legacy 2,000-character cut, and entry text_is.

The gateway and rem_loop both import this module (brief §0.2-10, fact:1309).
A database driver import here would pull psycopg2 into the gateway process,
which does not carry it.
"""

from __future__ import annotations

REM_PROMPT_VERSION = 2
REM_LEGACY_SUMMARY_CUT = 2000


def entry_text_is(kind, node_state, pg_content):
    """Return (text_is, text) for one record entry.

    Fact and retrospective: missing state or not rem_processed is pointer
    (Opus r2 F11). A shorter summary is kept only when a stamp is present or
    its length is not the legacy cut (brief §0.2-3). Decision, summary and
    insight branches match the 1.1.0 renderer.
    """
    body = pg_content if isinstance(pg_content, str) else ""
    if kind in ("summary", "insight"):
        return "pointer", None
    if kind == "decision":
        if isinstance(pg_content, str) and pg_content.strip():
            return "summary", pg_content
        return "full_record", body
    if kind not in ("fact", "retrospective"):
        return "full_record", body
    if not isinstance(node_state, dict) or not node_state.get("rem_processed"):
        return "pointer", None
    summary = node_state.get("rem_summary")
    stamp_present = node_state.get("rem_prompt_version") is not None
    if (isinstance(summary, str) and summary.strip()
            and len(summary) < len(body)
            and (stamp_present or len(summary) != REM_LEGACY_SUMMARY_CUT)):
        return "summary", summary
    return "full_record", body
