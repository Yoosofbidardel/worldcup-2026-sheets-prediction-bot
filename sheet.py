"""Google Sheets layer.

The bot only ever WRITES into the two prediction cells of a participant's
3-column block ([home, away, formula]). It never writes the formula column, so
all scoring stays exactly as defined in the sheet and recalculates live.
"""
import threading
from datetime import datetime, timezone

import gspread
from gspread.utils import rowcol_to_a1

from api_client import canon
from config import (
    EXCLUDED_NAMES,
    FIRST_SLOT_COL,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_SHEET_ID,
    KICKOFF_COL,
    KICKOFF_HEADER,
    MATCH_FIRST_ROW,
    MATCH_LAST_ROW,
    NAME_ROW,
    PREDICTIONS_ALWAYS_OPEN,
    SLOT_STRIDE,
    SPECIAL_ROWS,
    TOTAL_ROW,
    WORKSHEET_NAME,
)


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def _parse_kickoff(s) -> datetime | None:
    """Parse an ISO-8601 kickoff string into an aware UTC datetime, or None."""
    if _blank(s):
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:  # assume UTC if no offset was given
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SheetClient:
    def __init__(self):
        self._gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
        self._ws = self._gc.open_by_key(GOOGLE_SHEET_ID).worksheet(WORKSHEET_NAME)
        self._slots: list[dict] | None = None
        self._lock = threading.RLock()  # serialize gspread/requests session access

    # ── Participant slots ────────────────────────────────────────────────
    def slots(self, refresh: bool = False) -> list[dict]:
        """List of {idx, name, col, home_a1, away_a1} read from row 1.

        `idx` is a stable 1-based position used by the admin to assign people.
        `col` is the participant's first (home) prediction column.
        """
        if self._slots is not None and not refresh:
            return self._slots
        with self._lock:
            names = self._ws.row_values(NAME_ROW)  # 1-based list
        slots = []
        col = FIRST_SLOT_COL
        idx = 1
        while col <= len(names):
            name = names[col - 1] if col - 1 < len(names) else ""
            if name and name.strip() and name.strip() not in EXCLUDED_NAMES:
                slots.append(
                    {
                        "idx": idx,
                        "name": name.strip(),
                        "col": col,
                        "home_a1": rowcol_to_a1(1, col)[:-1],   # column letter only
                        "away_a1": rowcol_to_a1(1, col + 1)[:-1],
                    }
                )
                idx += 1
            col += SLOT_STRIDE
        self._slots = slots
        return slots

    def slot_by_idx(self, idx: int) -> dict | None:
        return next((s for s in self.slots() if s["idx"] == idx), None)

    def slot_by_col(self, col: int) -> dict | None:
        return next((s for s in self.slots() if s["col"] == col), None)

    # ── Matches ──────────────────────────────────────────────────────────
    def matches(self) -> list[dict]:
        """All fixtures that have both teams set, with results and kickoff lock.

        A match is `open` only while it has no result AND its kickoff time hasn't
        passed. If no kickoff is known, it falls back to result-only (open until
        a score is entered).
        """
        kcol = rowcol_to_a1(1, KICKOFF_COL)[:-1]  # column letter
        ranges = [
            f"A{MATCH_FIRST_ROW}:D{MATCH_LAST_ROW}",
            f"{kcol}{MATCH_FIRST_ROW}:{kcol}{MATCH_LAST_ROW}",
        ]
        with self._lock:
            rows, kicks = self._ws.batch_get(ranges, value_render_option="UNFORMATTED_VALUE")
        now = datetime.now(timezone.utc)
        out = []
        for i, row in enumerate(rows):
            r = MATCH_FIRST_ROW + i
            home = row[0] if len(row) > 0 else ""
            away = row[1] if len(row) > 1 else ""
            if _blank(home) or _blank(away):
                continue
            ah = row[2] if len(row) > 2 else ""
            aa = row[3] if len(row) > 3 else ""
            has_result = not _blank(ah) and not _blank(aa)
            kraw = kicks[i][0] if i < len(kicks) and kicks[i] else None
            kickoff = _parse_kickoff(kraw)
            started = kickoff is not None and now >= kickoff
            out.append(
                {
                    "row": r,
                    "home": str(home).strip(),
                    "away": str(away).strip(),
                    "actual_home": ah if has_result else None,
                    "actual_away": aa if has_result else None,
                    "kickoff": kickoff,
                    "started": started,
                    "open": True if PREDICTIONS_ALWAYS_OPEN else (not has_result and not started),
                }
            )
        return out

    def open_matches(self) -> list[dict]:
        return [m for m in self.matches() if m["open"]]

    # ── Specials ─────────────────────────────────────────────────────────
    def specials(self) -> list[dict]:
        out = []
        for row, (label, pts) in SPECIAL_ROWS.items():
            with self._lock:
                actual = self._ws.acell(
                    f"C{row}", value_render_option="UNFORMATTED_VALUE"
                ).value
            out.append(
                {
                    "row": row,
                    "label": label,
                    "points": pts,
                    "actual": None if _blank(actual) else str(actual).strip(),
                    "open": True if PREDICTIONS_ALWAYS_OPEN else _blank(actual),
                }
            )
        return out

    # ── Reading one participant's predictions ────────────────────────────
    def user_predictions(self, base_col: int) -> dict:
        """Return {'matches': {row: (home, away)}, 'specials': {row: text}}."""
        home_letter = rowcol_to_a1(1, base_col)[:-1]
        away_letter = rowcol_to_a1(1, base_col + 1)[:-1]
        last = max(MATCH_LAST_ROW, max(SPECIAL_ROWS))
        ranges = [
            f"{home_letter}{MATCH_FIRST_ROW}:{home_letter}{last}",
            f"{away_letter}{MATCH_FIRST_ROW}:{away_letter}{last}",
        ]
        with self._lock:
            homes, aways = self._ws.batch_get(ranges, value_render_option="UNFORMATTED_VALUE")

        def col_val(block, row):
            i = row - MATCH_FIRST_ROW
            if i < len(block) and block[i]:
                return block[i][0]
            return None

        result = {"matches": {}, "specials": {}}
        for m in self.matches():
            r = m["row"]
            h, a = col_val(homes, r), col_val(aways, r)
            if not _blank(h) and not _blank(a):
                result["matches"][r] = (h, a)
        for row in SPECIAL_ROWS:
            v = col_val(homes, row)
            if not _blank(v):
                result["specials"][row] = str(v).strip()
        return result

    def predicted_match_rows(self, base_col: int) -> set:
        """Set of match rows this participant has already predicted (both cells
        filled). Cheap (2 ranges) — used by the reminder job."""
        home_letter = rowcol_to_a1(1, base_col)[:-1]
        away_letter = rowcol_to_a1(1, base_col + 1)[:-1]
        ranges = [
            f"{home_letter}{MATCH_FIRST_ROW}:{home_letter}{MATCH_LAST_ROW}",
            f"{away_letter}{MATCH_FIRST_ROW}:{away_letter}{MATCH_LAST_ROW}",
        ]
        with self._lock:
            homes, aways = self._ws.batch_get(ranges, value_render_option="UNFORMATTED_VALUE")
        out = set()
        for r in range(MATCH_FIRST_ROW, MATCH_LAST_ROW + 1):
            i = r - MATCH_FIRST_ROW
            h = homes[i][0] if i < len(homes) and homes[i] else None
            a = aways[i][0] if i < len(aways) and aways[i] else None
            if not _blank(h) and not _blank(a):
                out.add(r)
        return out

    def predicted_rows_for_cols(self, cols) -> dict:
        """{base_col: set(match rows predicted)} for many participants in ONE read.

        The per-user predicted_match_rows() does 2 reads each; calling it for
        every participant blows the Sheets read-quota (60/min), so the reminder
        job uses this single bulk read of the whole prediction grid instead."""
        cols = sorted(set(cols))
        if not cols:
            return {}
        last_col = max(cols) + 1
        last_letter = rowcol_to_a1(1, last_col)[:-1]
        with self._lock:
            grid = self._ws.get(
                f"A{MATCH_FIRST_ROW}:{last_letter}{MATCH_LAST_ROW}",
                value_render_option="UNFORMATTED_VALUE",
            )
        out = {c: set() for c in cols}
        for i, rowvals in enumerate(grid):
            r = MATCH_FIRST_ROW + i
            for c in cols:
                h = rowvals[c - 1] if c - 1 < len(rowvals) else None
                a = rowvals[c] if c < len(rowvals) else None
                if not _blank(h) and not _blank(a):
                    out[c].add(r)
        return out

    def match_all_predictions(self, row: int) -> list[dict]:
        """Every participant's prediction for one match row: [{name, home, away}]
        for slots that filled both cells. Reads the whole row in one call."""
        slots = self.slots()
        if not slots:
            return []
        last_col = max(s["col"] + 1 for s in slots)
        last_letter = rowcol_to_a1(1, last_col)[:-1]
        with self._lock:
            got = self._ws.get(f"A{row}:{last_letter}{row}", value_render_option="UNFORMATTED_VALUE")
        rowvals = got[0] if got else []

        def cell(c):
            return rowvals[c - 1] if c - 1 < len(rowvals) else None

        def as_int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return v

        out = []
        for s in slots:
            h, a = cell(s["col"]), cell(s["col"] + 1)
            if not _blank(h) and not _blank(a):
                out.append({"name": s["name"], "home": as_int(h), "away": as_int(a)})
        return out

    def user_points(self, base_col: int) -> dict:
        """{row: points} the sheet's formula gave this participant per row
        (their points column is base_col+2). Blank/zero cells are skipped."""
        pts_letter = rowcol_to_a1(1, base_col + 2)[:-1]
        last = max(MATCH_LAST_ROW, max(SPECIAL_ROWS))
        with self._lock:
            vals = self._ws.get(
                f"{pts_letter}{MATCH_FIRST_ROW}:{pts_letter}{last}",
                value_render_option="UNFORMATTED_VALUE",
            )
        out = {}
        for i, cell in enumerate(vals):
            v = cell[0] if cell else None
            if _blank(v):
                continue
            try:
                out[MATCH_FIRST_ROW + i] = float(v)
            except (TypeError, ValueError):
                pass
        return out

    def match_prediction(self, base_col: int, row: int) -> tuple:
        """Current (home, away) prediction for one player+match, ints or (None, None)."""
        home_letter = rowcol_to_a1(1, base_col)[:-1]
        away_letter = rowcol_to_a1(1, base_col + 1)[:-1]
        with self._lock:
            vals = self._ws.batch_get(
                [f"{home_letter}{row}", f"{away_letter}{row}"],
                value_render_option="UNFORMATTED_VALUE",
            )
        def one(block):
            if block and block[0] and not _blank(block[0][0]):
                try:
                    return int(float(block[0][0]))
                except (TypeError, ValueError):
                    return None
            return None
        return one(vals[0]), one(vals[1])

    # ── Writing (predictions only — never the formula column) ────────────
    def set_match_prediction(self, base_col: int, row: int, home, away):
        with self._lock:
            self._ws.update_acell(rowcol_to_a1(row, base_col), home)
            self._ws.update_acell(rowcol_to_a1(row, base_col + 1), away)

    def set_special_prediction(self, base_col: int, row: int, text: str):
        with self._lock:
            self._ws.update_acell(rowcol_to_a1(row, base_col), text)

    # ── Kickoff times (filled from the API; admin can override in the sheet) ─
    def sync_kickoffs(self, kickoff_map: dict) -> dict:
        """Fill the kickoff column from {(canon_home, canon_away): iso}.

        Only writes cells that are currently EMPTY, so a manual override or an
        already-set time is never clobbered. Returns a small report.
        """
        kcol = rowcol_to_a1(1, KICKOFF_COL)[:-1]
        # make sure the column has a header so it's obvious in the sheet
        with self._lock:
            header = self._ws.acell(f"{kcol}{NAME_ROW}").value
            if _blank(header):
                self._ws.update_acell(f"{kcol}{NAME_ROW}", KICKOFF_HEADER)
            existing = self._ws.get(
                f"{kcol}{MATCH_FIRST_ROW}:{kcol}{MATCH_LAST_ROW}",
                value_render_option="UNFORMATTED_VALUE",
            )

        written, unmatched = 0, []
        for m in self.matches():
            i = m["row"] - MATCH_FIRST_ROW
            cur = existing[i][0] if i < len(existing) and existing[i] else None
            if not _blank(cur):
                continue  # respect existing / manual value
            iso = kickoff_map.get((canon(m["home"]), canon(m["away"])))
            if iso:
                with self._lock:
                    self._ws.update_acell(rowcol_to_a1(m["row"], KICKOFF_COL), iso)
                written += 1
            elif kickoff_map:
                unmatched.append(f"{m['home']} – {m['away']}")
        return {"written": written, "unmatched": unmatched}

    # ── Results (auto-filled from the API after a match finishes) ────────
    def sync_results(self, results_map: dict) -> dict:
        """Write finished-match results into columns C/D from
        {(canon_home, canon_away): (home_goals, away_goals)}.

        Only fills rows whose result is still EMPTY, so a manual entry or
        correction in the sheet is never overwritten. Returns a small report.
        """
        written, scored = 0, []
        for m in self.matches():
            if m["actual_home"] is not None:
                continue  # already has a result — leave it alone
            sc = results_map.get((canon(m["home"]), canon(m["away"])))
            if not sc:
                continue
            home_goals, away_goals = sc
            with self._lock:
                self._ws.update_acell(f"C{m['row']}", home_goals)
                self._ws.update_acell(f"D{m['row']}", away_goals)
            written += 1
            scored.append(f"{m['home']} {home_goals}-{away_goals} {m['away']}")
        return {"written": written, "scored": scored}

    # ── Leaderboard / standings (totals are computed live by the sheet) ──
    def standings(self) -> list[dict]:
        """Like leaderboard() but also carries each slot's column, so callers
        can map a name back to its assigned Telegram user."""
        with self._lock:
            totals = self._ws.row_values(TOTAL_ROW, value_render_option="UNFORMATTED_VALUE")
        board = []
        for s in self.slots():
            col = s["col"]
            total = totals[col - 1] if col - 1 < len(totals) else 0
            try:
                total = float(total)
            except (TypeError, ValueError):
                total = 0.0
            board.append({"name": s["name"], "col": col, "total": total})
        board.sort(key=lambda x: x["total"], reverse=True)
        return board

    def leaderboard(self) -> list[dict]:
        return [{"name": e["name"], "total": e["total"]} for e in self.standings()]
