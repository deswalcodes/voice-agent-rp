"""Compliance guardrails enforced in code (not just in the prompt)."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import config

TERMINAL_STATES = {"recovered", "do_not_call", "wrong_number", "escalated"}


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def within_calling_window(now: datetime | None = None) -> tuple[bool, str]:
    tz = ZoneInfo(config.CALL_TIMEZONE)
    now = now.astimezone(tz) if now else datetime.now(tz)
    start, end = _parse(config.CALL_WINDOW_START), _parse(config.CALL_WINDOW_END)
    ok = start <= now.time() <= end
    return ok, f"{now.strftime('%H:%M')} {config.CALL_TIMEZONE} (window {config.CALL_WINDOW_START}-{config.CALL_WINDOW_END})"


def eligibility(customer: dict, *, ignore_window: bool = False, demo: bool = False) -> tuple[bool, str]:
    """Decide whether this customer may be called right now.

    demo=True is the dashboard's role-play mode: it skips the calling window, attempt cap and
    terminal-state checks so a scenario can be replayed. Do-not-call is never skipped.
    """
    st = customer["state"]
    if st.get("do_not_call"):
        return False, "customer opted out (do-not-call)"
    if demo:
        return True, "eligible (demo mode)"
    if st.get("recovery_status") in TERMINAL_STATES:
        return False, f"already {st['recovery_status'].replace('_', ' ')}"
    if st.get("attempts", 0) >= config.MAX_ATTEMPTS:
        return False, f"max attempts reached ({config.MAX_ATTEMPTS})"
    if not ignore_window:
        ok, why = within_calling_window()
        if not ok:
            return False, f"outside calling window: {why}"
    return True, "eligible"
