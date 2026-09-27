"""Persistent scheduling state for linked Overwatch accounts."""

from __future__ import annotations

import time


STATE_KEY = "rank_tracker"
STATUS_PENDING = "pending"
STATUS_KNOWN_GOOD = "known_good"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ARCHIVED = "archived"
DAILY_RETRY_SECONDS = 24 * 60 * 60

_ACTIVE_STATUSES = {STATUS_PENDING, STATUS_KNOWN_GOOD, STATUS_UNAVAILABLE}
_VALID_STATUSES = _ACTIVE_STATUSES | {STATUS_ARCHIVED}


def _timestamp(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def normalize_tracker_state(value, *, has_snapshot: bool = False) -> dict:
    """Return a safe copy of persisted state, including legacy-account defaults."""

    state = dict(value) if isinstance(value, dict) else {}
    status = state.get("status")
    if status not in _VALID_STATUSES:
        status = STATUS_KNOWN_GOOD if has_snapshot else STATUS_PENDING
    state["status"] = status

    for key in ("last_attempt_at", "last_success_at", "archived_at", "reactivated_at"):
        parsed = _timestamp(state.get(key))
        if parsed is None:
            state.pop(key, None)
        else:
            state[key] = parsed

    resume_status = state.get("resume_status")
    if resume_status not in _ACTIVE_STATUSES:
        state.pop("resume_status", None)
    return state


def get_tracker_state(settings, user_id: int) -> dict:
    has_snapshot = settings.get_user(user_id, "ow", "rank_snapshot") is not None
    raw = settings.get_user(user_id, "ow", STATE_KEY)
    return normalize_tracker_state(raw, has_snapshot=has_snapshot)


def check_is_due(state: dict, *, now: float | None = None) -> bool:
    """Whether an automatic check is due for a normalized tracker state."""

    now = time.time() if now is None else now
    status = state.get("status")
    if status in (STATUS_PENDING, STATUS_KNOWN_GOOD):
        return True
    if status == STATUS_UNAVAILABLE:
        last_attempt = _timestamp(state.get("last_attempt_at"))
        return last_attempt is None or now - last_attempt >= DAILY_RETRY_SECONDS
    return False


async def mark_pending(settings, user_id: int, *, now: float | None = None) -> dict:
    """Queue a newly linked or relinked account for one qualification check."""

    state = get_tracker_state(settings, user_id)
    state["status"] = STATUS_PENDING
    state["queued_at"] = time.time() if now is None else now
    for key in (
        "reason",
        "resume_status",
        "archived_at",
        "reactivated_at",
        "last_attempt_at",
        "last_success_at",
    ):
        state.pop(key, None)
    await settings.set_user(user_id, "ow", STATE_KEY, state)
    return state


async def mark_success(settings, user_id: int, *, now: float | None = None) -> dict:
    """Mark a profile as public and eligible for the normal polling cadence."""

    now = time.time() if now is None else now
    state = get_tracker_state(settings, user_id)
    state.update(
        status=STATUS_KNOWN_GOOD,
        last_attempt_at=now,
        last_success_at=now,
    )
    for key in ("reason", "resume_status", "archived_at", "reactivated_at", "queued_at"):
        state.pop(key, None)
    await settings.set_user(user_id, "ow", STATE_KEY, state)
    return state


async def mark_unavailable(settings, user_id: int, *, now: float | None = None) -> dict:
    """Back off a private or missing profile to one automatic attempt per day."""

    now = time.time() if now is None else now
    state = get_tracker_state(settings, user_id)
    state.update(
        status=STATUS_UNAVAILABLE,
        reason="private_or_not_found",
        last_attempt_at=now,
    )
    for key in ("resume_status", "archived_at", "reactivated_at", "queued_at"):
        state.pop(key, None)
    await settings.set_user(user_id, "ow", STATE_KEY, state)
    return state


async def mark_attempt(settings, user_id: int, *, now: float | None = None) -> dict:
    """Record an API attempt without changing the profile's classification."""

    state = get_tracker_state(settings, user_id)
    state["last_attempt_at"] = time.time() if now is None else now
    await settings.set_user(user_id, "ow", STATE_KEY, state)
    return state


async def archive_account(settings, user_id: int, *, now: float | None = None) -> bool:
    """Pause rank checks while preserving the Battletag and prior tracker state."""

    state = get_tracker_state(settings, user_id)
    if state["status"] == STATUS_ARCHIVED:
        return False

    state["resume_status"] = state["status"]
    state["status"] = STATUS_ARCHIVED
    state["reason"] = "no_shared_guild"
    state["archived_at"] = time.time() if now is None else now
    await settings.set_user(user_id, "ow", STATE_KEY, state)
    return True


async def reactivate_account(settings, user_id: int, *, now: float | None = None) -> bool:
    """Resume an archived connection when the user shares a guild again."""

    state = get_tracker_state(settings, user_id)
    if state["status"] != STATUS_ARCHIVED:
        return False

    state["status"] = state.get("resume_status", STATUS_PENDING)
    if state["status"] not in _ACTIVE_STATUSES:
        state["status"] = STATUS_PENDING
    state["reactivated_at"] = time.time() if now is None else now
    for key in ("resume_status", "archived_at", "reason"):
        state.pop(key, None)
    await settings.set_user(user_id, "ow", STATE_KEY, state)
    return True
