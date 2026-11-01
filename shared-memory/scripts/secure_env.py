"""Shared split-env loader for the framework's three long-running processes.

Separates configuration from secrets:
  - config keys -> os.environ via setdefault (operator exports win).
  - secret keys -> held only in this module's in-process dict (_secrets), read only via
    get_secret(). Never exported to os.environ, so a secret cannot appear in
    /proc/<pid>/environ or reach a child through a wholesale os.environ.copy().

Classification uses both an explicit name list and suffix patterns so unlisted
secret keys are caught automatically.

Precedence (highest first):
  1. Operator os.environ export (always wins in get_secret).
  2. $CREDENTIALS_DIRECTORY/<key, lowercased> (systemd LoadCredential=).
  3. <KEY>_FILE (Docker convention; secret read from file path).
  4. shared-memory/.env plaintext value (fallback).

This is the entire precedence: nothing below a tier is consulted once it resolves. Tiers
2-4 land in _secrets and must never reach os.environ or a child env. A known secret
already in the exec environment logs one advisory naming the key only, never the value;
it is honoured, not refused. The daemon's own AGENT_TOKEN crosses only through an
inherited pipe fd (read_daemon_token_from_fd).
"""
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import sys
import urllib.parse
from pathlib import Path

# PG_CONN embeds the Postgres password, so treating it as config would export it. AGENT_TOKEN is already secret by suffix; it is listed so the advisory can see a key that is never written to shared-memory/.env.
KNOWN_SECRET_NAMES = {
    "PG_PASSWORD",
    "NEO4J_PASSWORD",
    "TAVILY_API_KEY",
    "AGENT_TOKENS",
    "AGENT_TOKEN",
    "BACKUP_ADMIN_TOKEN",
    "PG_CONN",
}

# Suffix matches that are config, checked before the pattern. EMBED_CHARS_PER_TOKEN is a ratio; the advisory-lock keys are integers. AGENT_TOKEN matches _TOKEN and stays a secret so it is never exported.
KNOWN_CONFIG_NAMES = {
    "EMBED_CHARS_PER_TOKEN",
    "BACKUP_ADVISORY_LOCK_KEY",
    "NREM_PRIORITY_ADVISORY_LOCK_KEY",
}

# Catches provider keys the explicit list does not name. _KEY is wide enough to hit the advisory-lock ids, which is why those are excluded first.
_SECRET_SUFFIXES = (
    "_PASSWORD", "_TOKEN", "_API_KEY", "_SECRET", "_KEY",
    "_CREDENTIAL", "_CREDENTIALS",
)

# Names from token_env that match no suffix. Once seen they stay secret for the process; a reload must not forget a credential name.
_dynamic_secret_names: set[str] = set()

# A bad LLM_BACKENDS_JSON must not raise at import. This flag makes the same failure loud at the entrypoint, and a later good value clears it.
_llm_backends_json_parse_failed: bool = False

# In-process only. Populated by load_split_env(); read by get_secret(). Never
# written to os.environ and never handed to a subprocess env dict wholesale —
# see hive_mind_proxy._daemon_env(), which filters by is_secret_key() instead
# of passing this dict (or os.environ) through.
_secrets: dict[str, str] = {}

# Names already advised-on in this process, de-duplicating across repeated calls.
# Cleared only by a test harness that owns the module's lifetime.
_advised_exec_env_names: set[str] = set()

# De-duplication set for non-secret _FILE pointer warnings.
_advised_ignored_file_pointer_names: set[str] = set()

# A health probe and a save both build the header. One line per variable for the process.
_warned_non_ascii_encoder_names: set[str] = set()


def _normalize_key(name: str) -> str:
    """The one key normaliser every classification and lookup site must use: strip BOM
    (U+FEFF) and whitespace from both ends, then upper-case.

    A loop, because a fixed single-order strip handles only one of "BOM then space" and
    "space then BOM", classifying one key as secret on one side and config on the other.
    """
    s = name
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    while s and (s[-1].isspace() or s[-1] == "﻿"):
        s = s[:-1]
    return s.upper()


_EXPORT_PREFIX_RE = re.compile(r"^export\s+", re.IGNORECASE)


def _strip_export_prefix(key: str) -> str:
    """Strip an optional leading shell `export ` keyword, case-insensitively, at parse
    time.

    The literal key "export AGENT_TOKENS" matches no secret name or suffix, so a
    shell-sourceable .env would otherwise send its registry into os.environ. BOM and
    whitespace are stripped before and after the keyword.
    """
    s = name = key
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    s = _EXPORT_PREFIX_RE.sub("", s, count=1)
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    return s


def _strip_balanced_quotes(value: str) -> str:
    """Strip one balanced pair of surrounding quotes; anything else is kept verbatim.

    memory_bridge.py and mcp/vector-skill.py hold their own copies, because none of the
    three may import another: change all three. Never applied to _read_secret_file,
    which strips only trailing CR/LF.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _parse_env_val(val: str) -> str:
    """Parse a .env value, stripping balanced quotes and inline comments.

    Order matters: quote protection is checked first so that hashes embedded
    in balanced quotes (e.g. KEY="foo # bar") are preserved verbatim rather
    than truncated by comment stripping. Inverting this order breaks quoted
    hash values.
    """
    val = val.strip()
    if not val:
        return ""
    if val[0] in ('"', "'"):
        q = val[0]
        closing = val.find(q, 1)
        if closing != -1:
            rest = val[closing + 1:].strip()
            if not rest or rest.startswith("#"):
                return val[1:closing]
    m = re.search(r"\s+#.*$", val)
    if m:
        val = val[:m.start()].strip()
    elif val.startswith("#"):
        return ""
    return _strip_balanced_quotes(val)


def read_env_value(
    env_path: "Path | str | None", key: str, default: "str | None" = None
) -> "str | None":
    """Return the parsed value of `key` from the .env file at `env_path`.

    Uses the single Python parser for framework environment files:
    normalises CRLF, strips balanced quotes, strips inline comments
    outside quotes, and resolves deterministically with last-definition-wins.
    Returns `default` if the file does not exist or the key is not present.
    """
    if env_path is None:
        p = _select_env_file()
    else:
        p = Path(env_path)
    if p is None or not p.is_file():
        return default

    target_canonical = _normalize_key(key)
    found = False
    result = default

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default

    for raw_line in content.splitlines():
        line = raw_line.rstrip("\r\n").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, val = line.partition("=")
        k = _strip_export_prefix(k).strip()
        if not k:
            continue
        if _normalize_key(k) == target_canonical:
            found = True
            result = _parse_env_val(val)

    return result if found else default


read_env_key = read_env_value



def is_secret_key(name: str) -> bool:
    """True if name must never be exported to os.environ or forwarded into a child env.

    KNOWN_CONFIG_NAMES is checked first and always wins, then KNOWN_SECRET_NAMES and
    token_env names, then _SECRET_SUFFIXES. name is normalised once, first, matching
    the storage side; normalising only one side inverts classification, and
    AGENT_TOKENS needs the exact match because the plural misses the _TOKEN suffix.
    """
    key_norm = _normalize_key(name)
    if key_norm in KNOWN_CONFIG_NAMES:
        return False
    if key_norm in KNOWN_SECRET_NAMES or key_norm in _dynamic_secret_names:
        return True
    return key_norm.endswith(_SECRET_SUFFIXES)


def _token_env_names(raw_json: str) -> set[str]:
    """Normalised token_env names from LLM_BACKENDS_JSON, for classification only;
    hive_mind_proxy._load_llm_backends() does the real validation.

    Must not raise. Invalid JSON or a non-list sets _llm_backends_json_parse_failed and
    returns empty. Absent or empty is not a failure and clears the flag, as does a later
    clean parse, so a corrected value un-wedges require_llm_backends_json_parses().
    """
    global _llm_backends_json_parse_failed
    names: set[str] = set()
    if not raw_json:
        _llm_backends_json_parse_failed = False
        return names
    try:
        entries = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        _llm_backends_json_parse_failed = True
        return names
    if not isinstance(entries, list):
        _llm_backends_json_parse_failed = True
        return names
    _llm_backends_json_parse_failed = False
    for entry in entries:
        if isinstance(entry, dict):
            token_env = entry.get("token_env")
            if isinstance(token_env, str) and token_env:
                names.add(_normalize_key(token_env))
    return names


def llm_backends_json_parse_failed() -> bool:
    """True if the most recent load_split_env() found a non-empty, malformed LLM_BACKENDS_JSON.
    Consulted by require_llm_backends_json_parses(), never by classification logic.
    """
    return _llm_backends_json_parse_failed


# sysexits EX_CONFIG. The unit's RestartPreventExitStatus=78 stops a refused config from crash-looping.
EX_CONFIG = 78
# Daemon identities the gateway mints itself. An operator entry would share the name with that token.
RESERVED_DAEMON_NAMES = frozenset({"rem_daemon", "consolidation"})
_LITERAL_SECRET_FIELDS = ("token", "api_key", "apikey", "secret", "key")


class ConfigExit(SystemExit):
    """Exit 78. The string is the refusal, so a traceback still names the field."""

    def __init__(self, message: str):
        super().__init__(EX_CONFIG)
        self.message = message

    def __str__(self) -> str:
        return self.message


def config_fatal(message: str) -> None:
    """Log a configuration refusal, then exit 78 so systemd does not restart it."""
    logging.getLogger("secure_env").error("%s", message)
    raise ConfigExit(message)


def require_llm_backends_json_parses(daemon_name: str) -> None:
    """Refuse a malformed LLM_BACKENDS_JSON at startup, not at import.

    Import stays quiet so check_config can still report a bad file (decision:1824 item 9; retrospective 3375: a declared backend is trusted, an untrusted one is not declared). A bad array used to fall through to a legacy pool and could copy an unclassified provider key into a daemon.
    """
    if llm_backends_json_parse_failed():
        config_fatal(
            f"FATAL ({daemon_name}): LLM_BACKENDS_JSON is set but is not "
            "valid JSON (or not a JSON array) — fix the syntax (a trailing "
            "comma or an unescaped quote is the usual cause) or unset the "
            "variable entirely."
        )


def _lookup_environ(name: str) -> "str | None":
    """Case-insensitive os.environ read. A `_FILE` pointer is config, not a secret."""
    if name in os.environ:
        return os.environ[name]
    canonical = _normalize_key(name)
    for key, value in os.environ.items():
        if _normalize_key(key) == canonical:
            return value
    return None


def _positive_int(value) -> bool:
    """True for an int >= 1. A bool is an int subclass and is refused."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _scrub_declared_url(url: str) -> str:
    """Drop userinfo and the query so a refusal can name the backend.

    Parse first. A URL with no scheme is <unparseable>: taking the text before the first :// treats a query that contains :// as the scheme and prints the secret. A credential in userinfo or the query must not reach a journal, check_config, or migrate_env. Never raises, including at import: an unparseable port used to throw from parts.port.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return "<unparseable>"
        netloc = parsed.netloc.rpartition("@")[2]
        return f"{parsed.scheme}://{netloc}{parsed.path}"
    except Exception:
        return "<unparseable>"


def _url_embeds_userinfo(url: str) -> bool:
    """True when the URL has userinfo, or cannot be parsed. Query strings are allowed."""
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return True
    return parts.username is not None or parts.password is not None


def _classify_secret_file(path: Path) -> str:
    """token_file_empty or token_refused. Never returns or logs the file's contents."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return "token_refused"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _SECRET_FILE_MAX_BYTES:
            return "token_refused"
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, _SECRET_FILE_MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _SECRET_FILE_MAX_BYTES:
                return "token_refused"
        raw = b"".join(chunks).decode("utf-8", errors="replace").rstrip("\r\n")
    except OSError:
        return "token_refused"
    finally:
        os.close(fd)
    if not raw.strip():
        return "token_file_empty"
    if _first_control_character(raw) is not None:
        return "token_refused"
    return "token_refused"


def backend_credential_reason(token_env: str) -> "str | None":
    """None when the named secret is usable. Otherwise a reason, never the value.

    An empty regular file is token_file_empty. A pointer that is set but unusable is token_refused. No pointer and no value is token_missing.
    """
    value = get_secret(token_env)
    if value is not None and str(value).strip():
        return None
    pointer = _lookup_environ(f"{token_env}_FILE")
    if pointer is None or not str(pointer).strip():
        return "token_missing"
    classified = _classify_secret_file(Path(str(pointer).strip()))
    if classified == "token_file_empty":
        return "token_file_empty"
    return "token_refused"


_UNPARSED = object()


def read_env_file(env_path: "Path | None") -> "dict[str, str]":
    """Pairs from one env file. Does not write os.environ or the secret store.

    Last line wins. export and a balanced quote pair are stripped the same way as load_split_env. A missing path is an empty map.
    """
    if env_path is None or not env_path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = _strip_export_prefix(key).strip()
        if not key:
            continue
        out[key] = _strip_balanced_quotes(val.strip())
    return out


def path_for_secure_env_file(override: "str | None") -> "Path | None":
    """The file an explicit SECURE_ENV_FILE names. None loads nothing.

    An empty string loads nothing. A set path that is missing, or is not a file, loads nothing and warns. None means this process's own selection.
    """
    if override is None:
        return _select_env_file()
    override = override.strip()
    if not override:
        return None
    path = Path(override)
    if path.is_file():
        return path
    why = "does not exist" if not path.exists() else "is not a file"
    print(f"[secure_env] WARNING: SECURE_ENV_FILE={override!r} {why} "
          f"— loading NO env file (refusing to fall through to a "
          f"file you did not name)", file=sys.stderr)
    return None


def read_selected_env_file() -> "dict[str, str]":
    """Pairs the gateway loader would take from the selected env file.

    migrate_env --apply runs outside the gateway process, so a declaration that lives only in the file is invisible in os.environ. This does not write os.environ or the secret store.
    """
    return read_env_file(_select_env_file())


_ENV_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def token_env_is_variable_name(value: str) -> bool:
    """True when token_env is a variable name. A key in that field is not."""
    return _ENV_VAR_NAME.fullmatch(value) is not None


def _registry_text_from_picture(env: dict) -> str:
    """AGENT_TOKENS as this picture holds it. Does not consult the operator shell.

    A _FILE pointer is the path. The registry text is read from that file, the same tier the gateway uses, and is not returned anywhere but this check.
    """
    pointer = env.get("AGENT_TOKENS_FILE") if "AGENT_TOKENS_FILE" in env else None
    if pointer and str(pointer).strip():
        loaded = _read_secret_file(Path(str(pointer).strip()), source="AGENT_TOKENS_FILE")
        if loaded:
            return loaded
    if "AGENT_TOKENS" in env:
        return str(env.get("AGENT_TOKENS") or "")
    return ""


def llm_declaration_errors(env: "dict | None" = None) -> "list[tuple[str, str]]":
    """(entry, field) for every FATAL declaration. Absent JSON and [] are not errors.

    Stdlib only, so check_config --phase-a-only and migrate_env --apply can refuse the same classes the gateway refuses. Reserved daemon names are included because an operator token would collide with the gateway-minted one.
    """
    source = os.environ if env is None else env
    errors: list[tuple[str, str]] = []
    raw = source.get("LLM_BACKENDS_JSON")
    text = "" if raw is None else str(raw).strip()
    entries = None
    if text and text != "[]":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            errors.append(("LLM_BACKENDS_JSON", "json"))
            parsed = _UNPARSED
        else:
            # null is not a list. Treating it as absent let a defined value boot.
            if not isinstance(parsed, list):
                errors.append(("LLM_BACKENDS_JSON", "type"))
                parsed = _UNPARSED
        if isinstance(parsed, list):
            entries = parsed
    if isinstance(entries, list):
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append((f"[{index}]", "entry"))
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url.strip():
                errors.append((f"[{index}]", "url"))
                continue
            shown = _scrub_declared_url(url.strip().rstrip("/"))
            if _url_embeds_userinfo(url):
                errors.append((shown, "url"))
            for field in ("n_ctx", "max_output_tokens"):
                if not _positive_int(entry.get(field)):
                    errors.append((shown, field))
            if "priority" in entry and not _positive_int(entry.get("priority")):
                errors.append((shown, "priority"))
            for secret_field in _LITERAL_SECRET_FIELDS:
                if entry.get(secret_field):
                    errors.append((shown, secret_field))
            if "extra_body" in entry and entry.get("extra_body") is not None and not isinstance(entry.get("extra_body"), dict):
                errors.append((shown, "extra_body"))
            if "max_inflight" in entry and entry.get("max_inflight") is not None and not _positive_int(entry.get("max_inflight")):
                errors.append((shown, "max_inflight"))
    # The file stores a secret in _secrets, not os.environ. An explicit map is migrate_env's file-plus-unit picture and wins, so a reserved name in that file is refused before the gateway boots. A _FILE pointer in that picture is read here: the map holds the path, and get_secret has not loaded it.
    if env is None:
        tokens = get_secret("AGENT_TOKENS") or ""
    else:
        tokens = _registry_text_from_picture(env)
    seen: set[str] = set()
    for pair in str(tokens).split(","):
        pair = pair.strip()
        if not pair:
            continue
        name = pair.split(":", 1)[0].strip()
        if name in RESERVED_DAEMON_NAMES and name not in seen:
            seen.add(name)
            errors.append((name, "agent"))
    return errors


# Hard ceiling on one secret file; a deployment with a larger secret can raise it.
# FIFOs and devices never reach it: S_ISREG refuses them first.
_SECRET_FILE_MAX_BYTES = int(
    os.environ.get("SECURE_ENV_SECRET_FILE_MAX_BYTES", str(64 * 1024))
)


def _first_control_character(value: str) -> "tuple[int, str] | None":
    """(offset, char) of the first C0 control character or DEL, else None.

    Deliberately NOT str.isprintable() or a unicodedata test: those also reject
    non-ASCII letters and non-breaking spaces a secret may contain. This class is what
    aiohttp refuses in a header value, per request rather than at load.
    """
    for offset, ch in enumerate(value):
        if ord(ch) < 0x20 or ch == "\x7f":
            return offset, ch
    return None


def _read_secret_file(path: Path, *, source: str) -> "str | None":
    """Read one secret value from path. Never raises: any failure WARNS and returns
    None so the caller falls through to the next tier; a mount that came and went must
    not crash a daemon's startup.

    The order matters. O_NONBLOCK stops the open blocking on a FIFO with no writer.
    fstat the OPEN fd, never re-stat the path, so check and read hit the same object.
    S_ISREG refuses a FIFO, device, directory or socket before a byte is read: a typo'd
    _FILE pointer to a FIFO or /dev/zero would otherwise hang the gateway before its
    listener, with no log line. Over-cap is decided from st_size first; the read loops
    because read(2) may return short and a short read must never pass as the whole
    secret; the length check stays as a backstop for a file whose st_size lies. An
    over-cap file is unset, never truncated.

    Deliberately NO O_NOFOLLOW: Kubernetes mounts a Secret as symlinks through a swapped
    ..data directory, so it would make every such mount unreadable, and real
    substitution defence needs an owner/parent-directory check too. Read this before
    adding it. Loose permissions WARN but do not refuse: the Docker _FILE convention
    commonly mounts 0444.

    Strips only a trailing run of CR and LF, never .strip() or .rstrip(), which would
    eat spaces that can be part of the secret. Any other control character, a trailing
    tab included, is corruption and is refused. The warning names the source, path,
    byte and character offset, never the value or its length. Left in, aiohttp rejects
    every upstream request with nothing naming the key file; refusing at load makes it
    one journal line.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        print(f"[secure_env] WARNING: {source} ({path}) not readable ({exc}) "
              f"— falling through to the next credential source",
              file=sys.stderr)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            print(f"[secure_env] WARNING: {source} ({path}) is not a regular "
                  f"file (FIFO/device/directory/socket) — refusing to read, "
                  f"falling through to the next credential source",
                  file=sys.stderr)
            return None
        loose = stat.S_IMODE(st.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        if loose:
            print(f"[secure_env] WARNING: {source} ({path}) is group/world-accessible "
                  f"(mode {oct(stat.S_IMODE(st.st_mode))}) — reading it anyway (a "
                  f"Docker secrets mount is commonly 0444 by design); tighten it "
                  f"if this is not a container mount", file=sys.stderr)
        # Check size before reading to avoid touching oversized files.
        if st.st_size > _SECRET_FILE_MAX_BYTES:
            print(f"[secure_env] WARNING: {source} ({path}) is {st.st_size} "
                  f"bytes, over the {_SECRET_FILE_MAX_BYTES}-byte cap "
                  f"(SECURE_ENV_SECRET_FILE_MAX_BYTES) — refusing to read, "
                  f"treating as unset", file=sys.stderr)
            return None
        try:
            # Loop until EOF or cap; a single read may return fewer bytes than available.
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, _SECRET_FILE_MAX_BYTES + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _SECRET_FILE_MAX_BYTES:
                    break  # backstop trip — st_size lied; stop reading now
            raw_bytes = b"".join(chunks)
        except OSError as exc:
            print(f"[secure_env] WARNING: {source} ({path}) could not be read "
                  f"({exc}) — falling through to the next credential source",
                  file=sys.stderr)
            return None
    finally:
        os.close(fd)

    # Backstop in case st_size does not reflect content (e.g. pseudo-files).
    if len(raw_bytes) > _SECRET_FILE_MAX_BYTES:
        print(f"[secure_env] WARNING: {source} ({path}) exceeds "
              f"{_SECRET_FILE_MAX_BYTES} bytes (SECURE_ENV_SECRET_FILE_MAX_BYTES) "
              f"— refusing to read, treating as unset", file=sys.stderr)
        return None

    raw = raw_bytes.decode("utf-8", errors="replace")
    # Only trailing CR and LF are write artefacts. A trailing space stays (it can be
    # part of a secret); a tab is corruption, refused below.
    raw = raw.rstrip("\r\n")
    if not raw.strip():
        print(f"[secure_env] WARNING: {source} ({path}) is empty — treating "
              f"as unset", file=sys.stderr)
        return None
    bad = _first_control_character(raw)
    if bad is not None:
        offset, ch = bad
        # No length: under the cap it is the secret's length. offset is a character index, not a byte position.
        print(f"[secure_env] WARNING: {source} ({path}) contains a control "
              f"character \\x{ord(ch):02x} at character offset {offset} "
              f"— refusing to use it, treating as "
              f"unset. A secret read from a file must not contain control "
              f"characters (they cannot appear in an HTTP header value). "
              f"Rewrite the file with: printf '%s' '<key>' > {path}",
              file=sys.stderr)
        return None
    return raw


_PRIVATE_NAME_SUFFIXES = (".local", ".lan", ".internal", ".home", ".home.arpa", ".ts.net")


def _bearer_transport_ok(backend_url: str, plaintext_ok: bool = False) -> bool:
    """Check whether bearer auth may be sent to `backend_url`.

    A key over plaintext to a public host is a key published to the path.
    Allows https, non-global IP/local host destinations (.local, .lan, .internal,
    .home, .home.arpa, .ts.net), or plaintext http with plaintext_ok true."""
    try:
        parts = urllib.parse.urlsplit(backend_url)
    except Exception:
        return False
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme == "https":
        return True
    if scheme != "http" or not host:
        return False
    if plaintext_ok:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_loopback or not ip.is_global)
    except ValueError:
        pass
    if host in ("localhost",) or "." not in host:
        # Reject numeric IPv4 spellings ipaddress.ip_address() misses but the resolver accepts.
        try:
            socket.inet_aton(host)
            return False
        except OSError:
            pass
        return True
    return host.endswith(_PRIVATE_NAME_SUFFIXES)


_ENCODER_SECRET_NAMES = {
    "embedder": "EMBEDDER_API_KEY",
    "reranker": "RERANKER_API_KEY",
}


def encoder_plaintext_ok() -> bool:
    """Encoders send a key over plain HTTP unless ENCODER_PLAINTEXT_OK is false. Unset is true."""
    raw = os.environ.get("ENCODER_PLAINTEXT_OK")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _encoder_file_pointer(name: str) -> "str | None":
    """The *_FILE path when one is set. The value stays with get_secret."""
    raw = _lookup_environ(f"{name}_FILE")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _encoder_token(kind: str) -> str:
    """The stripped key, or "" when none resolved."""
    token = get_secret(_ENCODER_SECRET_NAMES[kind])
    return "" if token is None else str(token).strip()


def encoder_key_configured(kind: str) -> bool:
    """A resolved value, or a *_FILE pointer that is set. The file itself may be missing."""
    name = _ENCODER_SECRET_NAMES[kind]
    return bool(_encoder_token(kind)) or _encoder_file_pointer(name) is not None


def encoder_auth_headers(kind: str, url: str) -> dict:
    """Authorization for one encoder, or {}. This is the only place the header is built.

    The value comes from get_secret, so a plaintext line, a *_FILE pointer and
    $CREDENTIALS_DIRECTORY all work. A missing file is the loader's warning and
    yields no header. ENCODER_PLAINTEXT_OK false withholds the key from a public
    http address; the request still goes out. A value that is not visible ASCII
    after stripping is withheld the same way. The warning names the variable
    and is printed once for that variable in the process.
    """
    value = _encoder_token(kind)
    if not value:
        return {}
    if not _bearer_transport_ok(url, encoder_plaintext_ok()):
        return {}
    # The key file keeps a trailing space, and h11's refusal text is the header value. That text is what each retry logs and what the 503 repeats.
    name = _ENCODER_SECRET_NAMES[kind]
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value):
        if name not in _warned_non_ascii_encoder_names:
            _warned_non_ascii_encoder_names.add(name)
            print(f"[secure_env] WARNING: {name} is not visible ASCII "
                  f"— withholding the encoder key", file=sys.stderr)
        return {}
    return {"Authorization": "Bearer " + value}


def encoder_auth_kwargs(kind: str, url: str) -> dict:
    """``headers=`` for one encoder call, empty when no key is sent."""
    headers = encoder_auth_headers(kind, url)
    return {"headers": headers} if headers else {}


def encoder_key_refused(kind: str, http_status: int) -> bool:
    """A configured key answered 401 or 403. With no key, those statuses keep their old meaning."""
    return http_status in (401, 403) and encoder_key_configured(kind)


def encoder_plaintext_warnings(urls: dict) -> list[str]:
    """URLs whose configured key is withheld because ENCODER_PLAINTEXT_OK is false.

    One entry per URL. The value is never included. A missing file is the loader's
    message, not this list.
    """
    if encoder_plaintext_ok():
        return []
    lines = []
    for kind, url in urls.items():
        if kind not in _ENCODER_SECRET_NAMES or not url:
            continue
        if not _encoder_token(kind):
            continue
        if not _bearer_transport_ok(str(url), False):
            lines.append(str(url))
    return lines


# token_env is an arbitrary string. Without this, `../../../home/user/.ssh/id_rsa` would be read and sent as a bearer token.
_VALID_KEY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _credentials_directory_secret(key: str) -> "str | None":
    """Tier 2: $CREDENTIALS_DIRECTORY/<key.lower()>, systemd LoadCredential=. This
    module only reads that variable, never sets it. A credential named in another case
    is a silent miss by construction.
    """
    if not _VALID_KEY_NAME.match(key):
        # The candidate is not a variable name. Printing it would publish a key parked in token_env.
        print("[secure_env] WARNING: candidate key '<not a variable name>' fails the "
              f"safe-name check ({_VALID_KEY_NAME.pattern}) — refusing to "
              f"use it as a path component under $CREDENTIALS_DIRECTORY",
              file=sys.stderr)
        return None
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not cred_dir:
        return None
    path = Path(cred_dir) / key.lower()
    if not path.exists():
        return None
    return _read_secret_file(path, source="$CREDENTIALS_DIRECTORY entry")


def _file_indirection_secret(key: str, file_values: dict) -> "str | None":
    """Tier 3: <KEY>_FILE, the Docker convention. The path follows os.environ, then the
    .env file. Only the secret is withheld from os.environ, never the path, which is
    not sensitive.
    """
    if not _VALID_KEY_NAME.match(key):
        # Same omission as the credentials-directory gate: the text is not a name.
        print("[secure_env] WARNING: candidate key '<not a variable name>' fails the "
              f"safe-name check ({_VALID_KEY_NAME.pattern}) — refusing to "
              f"resolve its _FILE pointer", file=sys.stderr)
        return None
    file_key = f"{key}_FILE"
    raw_path = (os.environ.get(file_key) or file_values.get(file_key, "")).strip()
    if not raw_path:
        return None
    return _read_secret_file(Path(raw_path), source=file_key)


def _warn_secrets_in_exec_environment(candidate_keys: set) -> None:
    """Advisory only, never a refusal: a known secret already in this process's exec
    environment is visible in /proc/<pid>/environ and inherited by a child copying
    os.environ. The value is still honoured. Logs the key NAME only, never a value, once
    per process.
    """
    for key in sorted(candidate_keys):
        if key in os.environ and key not in _advised_exec_env_names:
            _advised_exec_env_names.add(key)
            print(
                f"[secure_env] ADVISORY: {key} is set directly in this "
                f"process's environment (EnvironmentFile= or an exported "
                f"shell var) — visible via /proc/<pid>/environ and inherited "
                f"by any child that copies the full environment. Prefer "
                f"{key}_FILE or $CREDENTIALS_DIRECTORY/{key.lower()} "
                f"(systemd LoadCredential=) instead. Advisory only — the "
                f"value is still honoured.",
                file=sys.stderr,
            )


def _derive_file_pointer_candidates(file_values: dict) -> set[str]:
    """Derive K as a secret candidate from each <K>_FILE in os.environ or the parsed
    .env, only when is_secret_key(K) accepts it.

    Without this, a pointer for an unlisted key resolves to nothing with no warning. An
    operator's own `export PG_PASSWORD_FILE=...` must still work, so os.environ stays in
    the scan, but the non-secret warning fires only for a name from the .env file, once
    per process: an ambient SSL_CERT_FILE is not this framework's business. Names are
    case-folded first so `agent_tokens_file=` is not silently ignored.
    """
    candidates: set[str] = set()
    file_keys_canonical = set(file_values)   # already canonical
    for name in set(os.environ) | file_keys_canonical:
        key_candidate = _normalize_key(name)
        if not key_candidate.endswith("_FILE"):
            continue
        key = key_candidate[: -len("_FILE")]
        if not key:
            continue
        if is_secret_key(key):
            candidates.add(key)
        elif (key_candidate in file_keys_canonical
              and key_candidate not in _advised_ignored_file_pointer_names):
            _advised_ignored_file_pointer_names.add(key_candidate)
            print(f"[secure_env] WARNING: {name} is set, but {key!r} is not "
                  f"classified as a secret — its _FILE pointer is ignored "
                  f"(only a secret-classified key can be delivered this way)",
                  file=sys.stderr)
    return candidates


def _derive_credentials_directory_candidates() -> set[str]:
    """Derive a candidate from each $CREDENTIALS_DIRECTORY entry's uppercased filename,
    only when is_secret_key() accepts it. Without this, LoadCredential= for an unlisted
    key silently delivers nothing.
    """
    candidates: set[str] = set()
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not cred_dir:
        return candidates
    try:
        entries = os.listdir(cred_dir)
    except OSError:
        return candidates
    for entry in entries:
        key = entry.upper()
        if is_secret_key(key):
            candidates.add(key)
    return candidates


def _select_env_file() -> "Path | None":
    """Which .env this process loads: the one decision point.

    SECURE_ENV_FILE overrides the walk. A path names the file; a set-but-missing path
    warns and loads NOTHING, never a different file than the one named. The empty string
    loads no file: the hermeticity contract in tests/conftest.py, because setdefault
    re-adds what delenv removed. Unset: shared-memory/.env, then the repo root.
    """
    override = os.environ.get("SECURE_ENV_FILE")
    if override is not None:
        override = override.strip()
        if not override:
            return None
        p = Path(override)
        if p.exists():
            return p
        print(f"[secure_env] WARNING: SECURE_ENV_FILE={override!r} does not "
              f"exist — loading NO env file (refusing to fall through to a "
              f"file you did not name)", file=sys.stderr)
        return None
    here = Path(__file__).resolve()
    candidates = [here.parent.parent / ".env", here.parent.parent.parent / ".env"]
    return next((p for p in candidates if p.exists()), None)


def load_split_env() -> None:
    """Parse the framework .env and split it between os.environ (config, via setdefault)
    and in-process secrets.

    Parsed by hand, never with dotenv or any parser library: an env loader must not
    depend on a library that might be missing. Idempotent and additive; it never clears
    what an earlier call or an operator's export established.

    Secrets resolve $CREDENTIALS_DIRECTORY, <KEY>_FILE, then the plaintext value.
    Candidates are secret-classified file keys, KNOWN_SECRET_NAMES, token_env names, and
    each secret-classified <K>_FILE pointer and $CREDENTIALS_DIRECTORY entry, so a
    headless deployment with no plaintext .env can resolve any secret-classified key.

    File keys collapse to canonical form once, here, the last definition winning, after a
    leading `export ` is stripped. Otherwise a variant spelling of LLM_BACKENDS_JSON reads
    as absent, bypassing its malformed-JSON refusal, and case variants of one secret
    compete by set order instead of file order.
    """
    env_path = _select_env_file()

    raw_pairs: list[tuple[str, str]] = []
    if env_path is not None:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = _strip_export_prefix(key).strip()
            if not key:
                continue
            val = val.strip()
            unquoted_val = _strip_balanced_quotes(val)
            if unquoted_val != val:
                print(
                    f"[secure_env] NOTE: {key}'s value in {env_path} was "
                    f"wrapped in a balanced surrounding quote pair — the "
                    f"pair is stripped; the value is used without it.",
                    file=sys.stderr,
                )
            raw_pairs.append((key, unquoted_val))

    # One spelling per key, last line wins. Otherwise two casings race a set's iteration order.
    file_values: dict[str, str] = {}
    for key, val in raw_pairs:
        file_values[_normalize_key(key)] = val

    # An exported value wins over the file, same as get_secret(). Still computed when there is no .env.
    llm_json = os.environ.get("LLM_BACKENDS_JSON") or file_values.get("LLM_BACKENDS_JSON", "")
    _dynamic_secret_names.update(_token_env_names(llm_json))

    # Config keeps the operator's spelling in os.environ. Secrets skip this path entirely.
    for key, val in raw_pairs:
        if not is_secret_key(key):
            os.environ.setdefault(key, val)

    # Includes names that exist only as a _FILE pointer or a credentials-directory entry. Without those, a present file resolved to None.
    candidate_secret_keys = (
        {k for k in file_values if is_secret_key(k)}
        | KNOWN_SECRET_NAMES
        | _dynamic_secret_names
        | _derive_file_pointer_candidates(file_values)
        | _derive_credentials_directory_candidates()
    )
    # Store and look up the canonical spelling. A lowercase agent_tokens= line otherwise misses get_secret("AGENT_TOKENS") and auth turns off. The first tier that resolves still wins.
    for key in candidate_secret_keys:
        canonical = _normalize_key(key)
        value = _credentials_directory_secret(canonical)
        if value is None:
            value = _file_indirection_secret(canonical, file_values)
        if value is None:
            value = file_values.get(canonical)
        if value is not None:
            _secrets.setdefault(canonical, value)

    _warn_secrets_in_exec_environment(candidate_secret_keys)


def get_secret(name: str, default: "str | None" = None) -> "str | None":
    """The one way a framework process reads a secret.

    Every os.environ step runs before either _secrets step. Do not reorder them: the
    operator's own export must beat the file store at both the exact and canonical step.
      1. Exact match in os.environ.
      2. Canonical match in os.environ, a scan of its key names: POSIX os.environ is
         case-sensitive, so a second exact lookup could never find a lowercase export.
      3. Exact match in _secrets.
      4. Canonical match in _secrets, where load_split_env() stores what it resolved;
         without it get_secret("AGENT_TOKENS") misses an `agent_tokens=` line.
    Returns default if unresolved.
    """
    if name in os.environ:
        return os.environ[name]
    canonical = _normalize_key(name)
    for env_key, env_val in os.environ.items():
        if _normalize_key(env_key) == canonical:
            return env_val
    if name in _secrets:
        return _secrets[name]
    if canonical in _secrets:
        return _secrets[canonical]
    return default


def read_daemon_token_from_fd(env_var: str = "AGENT_TOKEN_FD") -> "str | None":
    """Read this daemon's AGENT_TOKEN from the pipe fd inherited at spawn.

    Only the fd number travels in env_var; the value crosses the pipe buffer alone, so
    it is in no /proc/<pid>/environ, argv or file. Never raises: returns None when unset,
    invalid or unreadable, so a daemon started directly falls back to get_secret instead
    of a silent 401.
    """
    raw_fd = os.environ.get(env_var, "").strip()
    if not raw_fd:
        return None
    try:
        fd = int(raw_fd)
    except ValueError:
        return None
    try:
        data = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    token = data.decode("utf-8", errors="replace").strip()
    return token or None


def require_db_credentials(*, pg_password: str, pg_conn: str, neo4j_password: str,
                            daemon_name: str) -> None:
    """Fail loudly when database authentication is missing.

    Must be called from a daemon's '__main__' guard only, never at module import time,
    so test collection is not interrupted.

    pg_conn must be the raw PG_CONN, not a default DSN: a constructed DSN always looks
    non-empty even when it embeds an empty password, which would defeat this check.
    """
    if not pg_password and not pg_conn:
        config_fatal(
            f"FATAL ({daemon_name}): no Postgres credential resolved — "
            "PG_PASSWORD and PG_CONN are both empty. Supply PG_PASSWORD (or "
            "a full PG_CONN) via shared-memory/.env, or the A4 file-based "
            "credential path once it ships."
        )
    if not neo4j_password:
        config_fatal(
            f"FATAL ({daemon_name}): no Neo4j credential resolved — "
            "NEO4J_PASSWORD is empty. Supply it via shared-memory/.env, or "
            "the A4 file-based credential path once it ships."
        )


def pg_dsn(password: str, *, user: str = "postgres", host: str = "localhost",
           port: str | int = 5432, dbname: str = "agent_data") -> str:
    """Build the default Postgres URL with the password percent-encoded.

    `quote(safe="")` is used because both drivers percent-decode the userinfo, and
    `quote_plus` would turn a space into a literal `+`. A supplied PG_CONN is used
    verbatim instead of passing through here.
    """
    return f"postgresql://{user}:{urllib.parse.quote(password, safe='')}@{host}:{port}/{dbname}"
