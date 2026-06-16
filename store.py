"""Local persistence for the telegram-user -> sheet-slot mapping.

A slot is identified by its base column number (the participant's first
prediction column). Keying on the column rather than the display name keeps
things unambiguous even when two people share a name (e.g. two "Sina").
"""
import json
import os
import threading

from datetime import datetime, timezone

from config import ANNOUNCE_FILE, ASSIGNMENTS_FILE, PREDLOG_FILE, REMINDERS_FILE, SEEN_FILE

_lock = threading.Lock()
_rem_lock = threading.Lock()
_seen_lock = threading.Lock()
_ann_lock = threading.Lock()
_log_lock = threading.Lock()


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


# ── First-time guide tracking ────────────────────────────────────────────
def _load_seen() -> dict:
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def has_seen_intro(user_id: int) -> bool:
    return str(user_id) in _load_seen()


def mark_seen_intro(user_id: int):
    with _seen_lock:
        data = _load_seen()
        data[str(user_id)] = True
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SEEN_FILE)


# ── Group announcements state ────────────────────────────────────────────
def _load_announce() -> dict:
    base = {"group_id": None, "snapshot": {}, "announced": [], "started": [], "auto_leaderboard": True}
    if not os.path.exists(ANNOUNCE_FILE):
        return base
    try:
        with open(ANNOUNCE_FILE, encoding="utf-8") as f:
            base.update(json.load(f))
    except (ValueError, OSError):
        pass
    return base


def _save_announce(data: dict):
    tmp = ANNOUNCE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ANNOUNCE_FILE)


def get_group_id():
    return _load_announce().get("group_id")


def get_snapshot() -> dict:
    return _load_announce().get("snapshot", {})


def set_snapshot(snapshot: dict):
    with _ann_lock:
        data = _load_announce()
        data["snapshot"] = snapshot
        _save_announce(data)


def get_announced() -> set:
    return set(_load_announce().get("announced", []))


def set_announced(rows):
    with _ann_lock:
        data = _load_announce()
        data["announced"] = sorted(set(int(r) for r in rows))
        _save_announce(data)


def get_started() -> set:
    return set(_load_announce().get("started", []))


def set_started(rows):
    with _ann_lock:
        data = _load_announce()
        data["started"] = sorted(set(int(r) for r in rows))
        _save_announce(data)


def get_auto_leaderboard() -> bool:
    return bool(_load_announce().get("auto_leaderboard", True))


def set_auto_leaderboard(on: bool):
    with _ann_lock:
        data = _load_announce()
        data["auto_leaderboard"] = bool(on)
        _save_announce(data)


# ── Prediction audit log (append-only; never overwritten) ────────────────
def log_prediction(record: dict):
    """Append one prediction event with a UTC timestamp. Every save is kept,
    so a match predicted 3 times leaves 3 records."""
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    with _log_lock:
        with open(PREDLOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_prediction_log(limit: int | None = None) -> list:
    """All logged predictions oldest→newest (or the last `limit`)."""
    if not os.path.exists(PREDLOG_FILE):
        return []
    recs = []
    with open(PREDLOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
    return recs[-limit:] if limit else recs


def init_announce(group_id: int, snapshot: dict, announced, started):
    """Register the group and baseline already-finished + already-started matches
    (so we don't dump a backlog of past matches when the group is first set)."""
    with _ann_lock:
        _save_announce(
            {
                "group_id": group_id,
                "snapshot": snapshot,
                "announced": sorted(set(int(r) for r in announced)),
                "started": sorted(set(int(r) for r in started)),
            }
        )
