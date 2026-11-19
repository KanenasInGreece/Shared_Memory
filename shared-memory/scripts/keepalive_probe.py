"""The one rule for which encoder keepalive values this platform's sockets take.

This module uses only the standard library and neither caches results nor logs
warnings, so the coordinator can apply it and check_config can report on it.
"""
import socket

# Map each setting to its socket level and option attribute names rather than
# numbers, because a platform may lack a constant. Following decision:3101 (no
# arbitrary limits: ask the platform), no ceiling is ever written down here.
KEEPALIVE_OPTIONS = {
    "ENCODER_TCP_KEEPALIVE_IDLE_S": ("IPPROTO_TCP", "TCP_KEEPIDLE"),
    "ENCODER_TCP_KEEPALIVE_INTERVAL_S": ("IPPROTO_TCP", "TCP_KEEPINTVL"),
    "ENCODER_TCP_KEEPALIVE_COUNT": ("IPPROTO_TCP", "TCP_KEEPCNT"),
}


class _NoSuchOption:
    """Returned by platform_accepts when the platform lacks the level or option name."""

    def __repr__(self):
        return "NO_SUCH_OPTION"

    def __bool__(self):
        # This sentinel evaluates to False so a truthiness check never treats a missing
        # option as accepted; callers must compare against NO_SUCH_OPTION with `is`.
        return False


NO_SUCH_OPTION = _NoSuchOption()


def try_setsockopt(level_id, option_id, value):
    """Return True if setsockopt accepts `value` on a throwaway socket, False if it
    rejects it, or None when no throwaway socket can be made (which says nothing
    about the value).
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        try:
            probe.setsockopt(level_id, option_id, value)
            return True
        except (OSError, TypeError, OverflowError):
            return False
    finally:
        probe.close()


def option_exists(level_name, option_name):
    """Return True when this platform defines both socket constants, without opening
    a socket."""
    return hasattr(socket, level_name) and hasattr(socket, option_name)


def platform_accepts(level_name, option_name, value):
    """Return True, False, or None like try_setsockopt, or NO_SUCH_OPTION when a
    socket constant is missing on this platform."""
    if not option_exists(level_name, option_name):
        return NO_SUCH_OPTION
    level_id, option_id = getattr(socket, level_name), getattr(socket, option_name)
    return try_setsockopt(level_id, option_id, value)
