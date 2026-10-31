#!/usr/bin/env python3
"""Read-only check that LLM_BACKENDS_JSON can boot the gateway. Writes nothing.

The first update runs the previous update_framework.sh against this file, so --apply must refuse a defined backend that lacks a valid context before the schema step, and --capture-preimage must still exit 0. check_config imports the unit-credential helpers.
"""
from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import secure_env  # noqa: E402

DEFAULT_GATEWAY_UNIT = "hive-mind-gateway.service"
MANAGED_KEYS = ("EMBEDDER_URL", "RERANKER_URL", "LLM_BACKENDS_JSON")
EXIT_OK = 0
EXIT_STOP = 1

class UnitQuery:
    """status is one of: 'no_systemctl' (proceed normally), 'ok' (owned_keys
    is authoritative), 'query_failed' (ALL writes must decline)."""

    def __init__(self, status: str, owned_keys: "set[str]" = frozenset(),
                 environment: "dict[str, str]" = None, error: str = None,
                 credential_ids: "frozenset[str]" = frozenset(),
                 unit_id: "str | None" = None):
        self.status = status
        self.owned_keys = owned_keys
        self.environment = environment or {}
        self.error = error
        # Credential ids and globs declared by the unit (never values or paths).
        self.credential_ids = credential_ids
        # Set when systemctl show returned an Id. Absent means no loaded unit.
        self.unit_id = unit_id


class _EnvironmentParseError(Exception):
    """Raised when an `Environment=` line does not parse into clean
    KEY=VALUE tokens (SEC H-1) — the caller treats this as query_failed
    (decline every write) rather than silently missing an owned key."""


def _parse_systemctl_environment(raw: str) -> "dict[str, str]":
    """`Environment=KEY=VALUE KEY2=VALUE2 ...` (one line). systemd applies
    SHELL-STYLE quoting to each element when it contains whitespace, a
    quote, `$`, `\\`, or a glob character (systemd.syntax(7)) — e.g. a
    value carrying JSON with embedded double quotes (LLM_BACKENDS_JSON,
    always) is emitted as a single double-quoted, backslash-escaped token:

        Environment="LLM_BACKENDS_JSON=[{\\"url\\": \\"http://a:5000\\"}]"

    A naive `str.split()` on whitespace (the original cut) tokenizes this
    into garbage — `["LLM_BACKENDS_JSON` with a leading quote, which
    matches no MANAGED_KEYS name, so the R-C unit-ownership gate silently
    never fires for the one key it matters most for. `shlex.split()`
    (POSIX mode) tokenizes systemd's quoting correctly — verified against
    real `systemctl show -p Environment` output for both a JSON value and
    a plain space-containing one (SEC H-1 regression fixture).

    FAILS CLOSED: a line shlex cannot tokenize (unbalanced quotes) raises
    _EnvironmentParseError rather than silently returning a partial/wrong
    dict — the caller turns this into query_failed (every write declines),
    per the fix's own stated fallback ("failing that, treat 'does not
    parse into clean KEY=VALUE tokens' as query_failed")."""
    out: "dict[str, str]" = {}
    prefix = "Environment="
    for line in raw.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):].strip()
        if not rest:
            continue
        try:
            tokens = shlex.split(rest, posix=True)
        except ValueError as exc:
            raise _EnvironmentParseError(
                f"could not tokenize Environment= line ({type(exc).__name__})") from None
        for tok in tokens:
            if "=" in tok:
                k, _, v = tok.partition("=")
                out[k] = v
    return out


def _parse_systemctl_environment_files(raw: str) -> "list[tuple[str, bool]]":
    """`EnvironmentFiles=/path (ignore_errors=no)` — one or more lines.

    The second item is true when a missing file is skipped: `ignore_errors=yes`,
    or a leading `-` on the path (`EnvironmentFile=-/x`). The dash is not part of the path.
    """
    found: "list[tuple[str, bool]]" = []
    prefix = "EnvironmentFiles="
    for line in raw.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):].strip()
        if not rest:
            continue
        matched = re.match(r"^(\S+)\s*\((.*)\)\s*$", rest)
        if matched:
            path, meta = matched.group(1), matched.group(2)
        else:
            path, meta = rest.split()[0], ""
        optional = "ignore_errors=yes" in meta.replace(" ", "")
        if path.startswith("-"):
            optional = True
            path = path[1:]
        found.append((path, optional))
    return found


def _read_env_file_pairs(path: str) -> "dict[str, str]":
    """KEY=VALUE pairs from an EnvironmentFile= target. Raises OSError if the file cannot be read; the caller treats that as a failed query (NEW-B).

    systemd applies Environment= first, then each EnvironmentFile= in the order listed, and a later assignment wins. A secret value stays in this map until build_faithful_env drops it. Nothing here is printed.
    """
    out: "dict[str, str]" = {}
    # encoding= is explicit so a non-UTF-8 file raises UnicodeDecodeError on every locale. That is a ValueError, not OSError.
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = secure_env._strip_balanced_quotes(val.strip())
    return out


def _read_env_file_key_names(path: str) -> "set[str]":
    """The KEY names present in an EnvironmentFile= target. Raises OSError if the file cannot be read."""
    return set(_read_env_file_pairs(path))


CREDENTIAL_PROPERTIES = (
    "LoadCredential",
    "LoadCredentialEncrypted",
    "SetCredential",
    "SetCredentialEncrypted",
    "ImportCredential",
    "ImportCredentialEx",
)


def _unit_object_path(unit: str) -> str:
    """systemd bus-label escape for unit name under /org/freedesktop/systemd1/unit/."""
    raw_bytes = unit.encode("utf-8")
    out = ["/org/freedesktop/systemd1/unit/"]
    for i, b in enumerate(raw_bytes):
        is_alpha = (ord(b"a") <= b <= ord(b"z")) or (ord(b"A") <= b <= ord(b"Z"))
        is_digit = ord(b"0") <= b <= ord(b"9")
        if is_alpha or (is_digit and i > 0):
            out.append(chr(b))
        else:
            out.append(f"_{b:02x}")
    return "".join(out)


def _parse_credential_property(prop: str, stdout: str) -> "frozenset[str] | None":
    """Parses credential ids and globs from one busctl get-property JSON line.
    Returns frozenset of ids/globs, or None if unparseable/unexpected shape."""
    line = stdout.strip()
    if not line:
        return frozenset()
    ids = set()
    try:
        obj = json.loads(line)
        if not isinstance(obj, dict) or "type" not in obj or "data" not in obj:
            return None
        t = obj["type"]
        data = obj["data"]
        if not isinstance(t, str) or not isinstance(data, list):
            return None
        if t == "a(ss)":
            for item in data:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    return None
                if not isinstance(item[0], str) or not isinstance(item[1], str):
                    return None
                if prop == "ImportCredentialEx":
                    # ImportCredentialEx: when item[1] is non-empty, rename replaces prefix.
                    # Add rename + "*" if glob ends with "*", else rename. When item[1] is empty, add item[0].
                    if item[1]:
                        ids.add(item[1] + "*" if item[0].endswith("*") else item[1])
                    else:
                        ids.add(item[0])
                else:
                    ids.add(item[0])
        elif t == "a(say)":
            for item in data:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    return None
                if not isinstance(item[0], str):
                    return None
                # the payload bytes (item[1]) are never copied or printed
                ids.add(item[0])
        elif t == "as":
            for item in data:
                if not isinstance(item, str):
                    return None
                ids.add(item)
        else:
            return None
    except Exception:
        return None
    return frozenset(ids)


def query_gateway_unit(gateway_unit: str) -> UnitQuery:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return UnitQuery("no_systemctl")
    try:
        proc = subprocess.run(
            [systemctl, "--user", "show", gateway_unit, "-p", "Environment",
             "-p", "EnvironmentFiles", "-p", "Id"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        return UnitQuery("query_failed", error=f"{type(exc).__name__} running systemctl")
    if proc.returncode != 0:
        return UnitQuery("query_failed",
                          error=f"systemctl show exited {proc.returncode}")
    try:
        environment = _parse_systemctl_environment(proc.stdout)
    except _EnvironmentParseError as exc:
        return UnitQuery("query_failed", error=str(exc))
    owned = set(environment) & set(MANAGED_KEYS)
    # Environment= is already in `environment`. Each EnvironmentFile= then overrides it, later files last, which is systemd's order.
    # A missing optional file (ignore_errors=yes, or a leading -) is skipped. Any other unreadable file fails the query.
    for path, optional in _parse_systemctl_environment_files(proc.stdout):
        try:
            pairs = _read_env_file_pairs(path)
        except FileNotFoundError:
            if optional:
                continue
            return UnitQuery(
                "query_failed",
                error=f"EnvironmentFile {path!r} unreadable (FileNotFoundError)")
        except UnicodeDecodeError:
            # Not an OSError. The sentence names the path and none of the bytes.
            return UnitQuery(
                "query_failed",
                error=f"EnvironmentFile {path} is not UTF-8")
        except OSError as exc:
            return UnitQuery(
                "query_failed",
                error=f"EnvironmentFile {path!r} unreadable ({type(exc).__name__})")
        environment.update(pairs)
        owned |= set(pairs) & set(MANAGED_KEYS)

    unit_id = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("Id="):
            val = line[3:].strip()
            if val:
                unit_id = val
            break

    credential_ids: frozenset[str] = frozenset()
    busctl = shutil.which("busctl")
    if busctl is not None and unit_id:
        # An unknown answer gives the old report, never the note.
        collected_ids: set[str] = set()
        query_ok = True
        unit_path = _unit_object_path(unit_id)
        for prop in CREDENTIAL_PROPERTIES:
            try:
                b_proc = subprocess.run(
                    [busctl, "--user", "--json=short", "get-property",
                     "org.freedesktop.systemd1", unit_path,
                     "org.freedesktop.systemd1.Service", prop],
                    capture_output=True, text=True, timeout=10,
                )
                if b_proc.returncode == 0:
                    parsed = _parse_credential_property(prop, b_proc.stdout)
                    if parsed is None:
                        query_ok = False
                        break
                    collected_ids |= parsed
                elif "Unknown" in (b_proc.stderr or ""):
                    # Directive unsupported on older systemd (<257); treat as empty and continue.
                    continue
                else:
                    query_ok = False
                    break
            except Exception:
                query_ok = False
                break
        if query_ok:
            credential_ids = frozenset(collected_ids)
    return UnitQuery("ok", owned_keys=owned, environment=environment,
                     credential_ids=credential_ids, unit_id=unit_id)


def _unit_supplies_token(name: str, unit_query: UnitQuery) -> bool:
    """Matches the lowercased name, as secure_env._credentials_directory_secret reads $CREDENTIALS_DIRECTORY, against credential ids/globs or secret unit Environment keys."""
    name_lower = name.lower()
    for cid in unit_query.credential_ids:
        if name_lower == cid or fnmatch.fnmatchcase(name_lower, cid):
            return True
    if name in unit_query.environment and secure_env.is_secret_key(name):
        return True
    return False


def hidden_credentials(unit_query: UnitQuery, token_env_names: "list[str] | set[str]") -> list[str]:
    """Returns, sorted, the token_env names among unresolved token_env_names that
    the unit supplies. Status not 'ok' -> []."""
    if unit_query.status != "ok":
        return []
    hidden = [name for name in set(token_env_names) if _unit_supplies_token(name, unit_query)]
    return sorted(hidden)


# Printed when an excluded entry's reason is token_missing and the unit supplies that name.
HIDDEN_CREDENTIAL_NOTE = (
    "note: {names} {verb} supplied by the gateway's systemd unit (a credential directive or a secret "
    "Environment= value), which this check cannot see, so the entry is excluded with token_missing. "
    "The running gateway reads it from the unit; the authenticated /memory/telemetry "
    "shows the gateway's own roster, with has_credential per backend, under config.llm_backends."
)


def extract_token_env_names(raw_json_text: "str | None") -> list[str]:
    """Extracts non-empty token_env strings from raw LLM_BACKENDS_JSON text."""
    if not raw_json_text:
        return []
    entries = _load_raw_backend_entries(raw_json_text)
    if entries is None:
        return []
    names = []
    for e in entries:
        token_env = e.get("token_env")
        if isinstance(token_env, str) and token_env.strip():
            names.append(token_env.strip())
    return names


def format_hidden_credential_note(hidden: list[str]) -> str:
    """Formats the note explaining that hidden credentials are supplied by the gateway's systemd unit."""
    names_str = ", ".join(hidden)
    verb = "is" if len(hidden) == 1 else "are"
    return HIDDEN_CREDENTIAL_NOTE.format(names=names_str, verb=verb)


def _hidden_credential_line(unit_query: UnitQuery, image: "dict | None",
                            token_env_names: "list[str] | set[str]") -> "str | None":
    """Follows an excluded entry whose reason is token_missing, not a legacy fallback."""
    if image is not None:
        excluded = image.get("llm_backends_excluded") or image.get("excluded") or []
        if not any(isinstance(item, dict) and item.get("reason") == "token_missing" for item in excluded):
            return None
    hidden = hidden_credentials(unit_query, token_env_names)
    if not hidden:
        return None
    return format_hidden_credential_note(hidden)


# PATH, HOME, and the unit's own declarations only. The operator shell is excluded so the image matches systemd.
# A secret on a unit Environment= line is not copied into the child, and a LoadCredential= file is not readable by it, so both can look like a missing token. Writes still decline.

def build_faithful_env(unit_query: UnitQuery) -> "dict[str, str]":
    faithful: "dict[str, str]" = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    # A secret on the unit's Environment= line must not enter this child process's environ.
    if unit_query.status == "ok":
        for key, value in unit_query.environment.items():
            if not secure_env.is_secret_key(key):
                faithful[key] = value
    # Mechanism variable, not operator config: the unit's value wins, then this process's, so a run with no unit still finds the file.
    if "SECURE_ENV_FILE" not in faithful and "SECURE_ENV_FILE" in os.environ:
        faithful["SECURE_ENV_FILE"] = os.environ["SECURE_ENV_FILE"]
    return faithful



def _load_raw_backend_entries(raw_json_text: str) -> "list[dict] | None":
    """Objects in an LLM_BACKENDS_JSON array. None when the text is not that shape. Never raises."""
    try:
        entries = json.loads(raw_json_text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    if not all(isinstance(e, dict) for e in entries):
        return None
    return entries

_JSON_SHAPE = (
    "Each entry is an object with url, n_ctx and max_output_tokens "
    "(positive integers). priority, when set, is an integer >= 1. "
    "token_env holds the NAME of the variable, never the key. "
    "Query the URL; if it does not say, ask the operator. Never guess."
)


def _legacy_names(env: dict) -> list[str]:
    """Non-empty leftover names. An empty assignment boots degraded, same as unset."""
    found = []
    for name in ("LLM_BACKENDS", "LLM_DEFAULT_TARGET"):
        value = env.get(name)
        if value is not None and str(value).strip():
            found.append(name)
    return found


def _legacy_only(env: dict) -> bool:
    """True when a leftover CSV or default target is set and JSON declares nothing."""
    raw = env.get("LLM_BACKENDS_JSON")
    text = "" if raw is None else str(raw).strip()
    declared = bool(text) and text != "[]"
    return bool(_legacy_names(env)) and not declared


def _report_errors(errors: list, env: dict) -> str:
    if errors:
        lines = ["LLM_BACKENDS_JSON would stop the gateway:"]
    else:
        lines = ["A legacy backend line would not be read:"]
    for entry, field in errors:
        lines.append(f"  {entry} field {field}")
        if field == "agent":
            lines.append(
                f"  Rename {entry} in AGENT_TOKENS. The gateway mints that daemon itself."
            )
    for name in _legacy_names(env):
        if not _json_declares(env):
            lines.append(f"  legacy field {name}")
    lines.append(_JSON_SHAPE)
    if errors:
        lines.append("Do not pass --skip-env-migration. The gateway would refuse this file and the update would restart it.")
    else:
        lines.append(
            "The legacy line is no longer read, so this host would lose reasoning. "
            "Convert it to LLM_BACKENDS_JSON or delete the line, then re-run."
        )
    return "\n".join(lines)


def _json_declares(env: dict) -> bool:
    raw = env.get("LLM_BACKENDS_JSON")
    text = "" if raw is None else str(raw).strip()
    return bool(text) and text != "[]"


def do_capture(out_path: str, gateway_unit: str) -> int:
    """Exit 0 even when the declaration is bad. The previous updater captures before it pulls."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"capture_schema": 1, "image": {"urls": [], "note": "read-only capture; --apply validates and writes nothing"}}
    fd = os.open(out, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(out, 0o600)
    print(f"captured pre-image -> {out_path}")
    return EXIT_OK


class _EnvFileDecodeError(Exception):
    """The selected env file is not UTF-8. The message is the path, never the bytes."""

    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


def _gateway_env(unit_query: UnitQuery) -> dict:
    """What the gateway will read. The operator shell is not part of it.

    build_faithful_env already put the unit's non-secret Environment= first, including SECURE_ENV_FILE, so the unit's file wins. The file then fills keys the unit did not set, matching load_split_env's setdefault. Nothing here is printed.
    """
    merged = build_faithful_env(unit_query)
    if "SECURE_ENV_FILE" in merged:
        path = secure_env.path_for_secure_env_file(merged.get("SECURE_ENV_FILE"))
    else:
        path = secure_env.path_for_secure_env_file(None)
    try:
        pairs = secure_env.read_env_file(path)
    except UnicodeDecodeError:
        raise _EnvFileDecodeError(str(path)) from None
    for key, value in pairs.items():
        merged.setdefault(key, value)
    return merged


# Same suffixes the gateway uses for a private bearer path. A public https host is still off-host: the notice is about corpus text, not the key.
_PRIVATE_NAME_SUFFIXES = (".local", ".lan", ".internal", ".home", ".home.arpa", ".ts.net")


def _declared_address_is_self_hosted(url: str) -> bool:
    """True for a non-global IP, localhost, a bare name, or a private suffix.

    A numeric spelling inet_aton accepts and ipaddress rejects is not a bare name. Those addresses are not treated as self-hosted.
    """
    try:
        parts = urllib.parse.urlsplit(str(url).strip())
    except Exception:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return bool(ip.is_loopback or not ip.is_global)
    if host == "localhost" or "." not in host:
        try:
            socket.inet_aton(host)
            return False
        except OSError:
            return True
    return host.endswith(_PRIVATE_NAME_SUFFIXES)


def _off_host_notices(env: dict) -> list:
    """One scrubbed line per declared backend that is not self-hosted. Never raises and never prints a query or userinfo."""
    raw = env.get("LLM_BACKENDS_JSON")
    entries = _load_raw_backend_entries("" if raw is None else str(raw))
    if not entries:
        return []
    lines = []
    for entry in entries:
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        if _declared_address_is_self_hosted(url):
            continue
        shown = secure_env._scrub_declared_url(url.strip())
        lines.append(
            f"{shown} receives corpus text from both daemons; remove it if not trusted"
        )
    return lines


def do_apply(gateway_unit: str) -> int:
    """Validate the file and unit the gateway will read. Never writes and never guesses n_ctx."""
    unit_query = query_gateway_unit(gateway_unit)
    if unit_query.status == "query_failed" and (unit_query.error or "").startswith("EnvironmentFile "):
        # The required file could not be read. Say that, and do not call the empty picture usable.
        print(unit_query.error)
        return EXIT_STOP
    try:
        merged = _gateway_env(unit_query)
    except _EnvFileDecodeError as exc:
        print(f"{exc.path} is not UTF-8. Refusing.")
        return EXIT_STOP
    errors = secure_env.llm_declaration_errors(merged)
    if errors or _legacy_only(merged):
        print(_report_errors(errors, merged))
        return EXIT_STOP
    print("LLM_BACKENDS_JSON is usable. Nothing was written.")
    # The notice does not stop the update. The gateway log stays quiet when private_ok is absent.
    for line in _off_host_notices(merged):
        print(line)
    return EXIT_OK


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture-preimage", metavar="OUT_JSON")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--preimage", metavar="IN_JSON")
    ap.add_argument("--gateway-unit", default=os.environ.get("GATEWAY_UNIT", DEFAULT_GATEWAY_UNIT))
    args = ap.parse_args(argv)
    if args.capture_preimage:
        return do_capture(args.capture_preimage, args.gateway_unit)
    if args.apply or args.preimage:
        return do_apply(args.gateway_unit)
    return do_apply(args.gateway_unit)


if __name__ == "__main__":
    raise SystemExit(main())
