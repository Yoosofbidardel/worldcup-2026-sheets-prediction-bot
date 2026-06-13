"""Local persistence for the telegram-user -> sheet-slot mapping.

A slot is identified by its base column number (the participant's first
prediction column). Keying on the column rather than the display name keeps
things unambiguous even when two people share a name (e.g. two "Sina").
"""
import json
import os
import threading

from config import ASSIGNMENTS_FILE, REMINDERS_FILE

_lock = threading.Lock()
_rem_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(ASSIGNMENTS_FILE):
        return {}
    with open(ASSIGNMENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict):
    tmp = ASSIGNMENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ASSIGNMENTS_FILE)


def get_assignment(user_id: int) -> dict | None:
    """Return {'name', 'col', 'username'} for a telegram user, or None."""
    return _load().get(str(user_id))


def col_is_taken(base_col: int) -> int | None:
    """Return the telegram_id already assigned to this slot, or None."""
    for uid, info in _load().items():
        if info.get("col") == base_col:
            return int(uid)
    return None


def set_assignment(user_id: int, name: str, base_col: int, username: str = ""):
    with _lock:
        data = _load()
        # one slot per person: drop any other user holding this column
        data = {
            uid: info for uid, info in data.items() if info.get("col") != base_col
        }
        data[str(user_id)] = {"name": name, "col": base_col, "username": username}
        _save(data)


def remove_assignment(user_id: int) -> bool:
    with _lock:
        data = _load()
        if str(user_id) in data:
            del data[str(user_id)]
            _save(data)
            return True
    return False


def all_assignments() -> dict:
    """Return {user_id(int): {'name','col','username'}}."""
    return {int(uid): info for uid, info in _load().items()}


# ── Reminder de-duplication ──────────────────────────────────────────────
# Remembers which reminders were already sent so a user is never nudged twice
# for the same (match, window). Key: "user_id:row:window_hours".
def _load_reminders() -> dict:
    if not os.path.exists(REMINDERS_FILE):
        return {}
    try:
        with open(REMINDERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def _save_reminders(data: dict):
    tmp = REMINDERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REMINDERS_FILE)


def _rem_key(user_id: int, row: int, window: int) -> str:
    return f"{user_id}:{row}:{window}"


def was_reminded(user_id: int, row: int, window: int) -> bool:
    return _rem_key(user_id, row, window) in _load_reminders()


def mark_reminded(user_id: int, row: int, window: int):
    with _rem_lock:
        data = _load_reminders()
        data[_rem_key(user_id, row, window)] = True
        _save_reminders(data)
