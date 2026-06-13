"""football-data.org client — used only to discover each match's kickoff time.

Results are NOT taken from here (the admin enters them in the sheet); the API is
purely a convenience that fills the kickoff column so predictions can lock at
kickoff. Anything the API can't match is simply left for the admin to fill in by
hand, so a name mismatch can never silently leave a game unlocked.
"""
import logging
import unicodedata

import requests

from config import COMPETITION_CODE, FOOTBALL_API_BASE, FOOTBALL_API_KEY

logger = logging.getLogger(__name__)


# Map the many spellings of a country to one canonical token. After stripping
# accents/punctuation, both the sheet's spelling and the API's spelling should
# resolve to the same value here. Extend this if the log reports an unmatched fixture.
_ALIASES = {
    "korearepublic": "southkorea",
    "korea": "southkorea",
    "cotedivoire": "ivorycoast",
    "turkey": "turkiye",
    "caboverde": "capeverde",
    "capeverdeislands": "capeverde",
    "congodr": "drcongo",
    "usa": "unitedstates",
    "unitedstatesofamerica": "unitedstates",
    "iriran": "iran",
    "czechrepublic": "czechia",
    "bosniaherzegovina": "bosniaandherzegovina",
}


def _norm(name: str) -> str:
    """Lowercase, drop accents and any non-letter characters."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return "".join(c for c in n.lower() if c.isalnum())


def canon(name: str) -> str:
    n = _norm(name)
    return _ALIASES.get(n, n)


def fetch_kickoffs() -> dict:
    """Return {(canon_home, canon_away): utcDate_iso} for the competition.

    Returns {} on any error (the caller then just keeps whatever is already in
    the sheet — the bot degrades gracefully if the API is down).
    """
    if not FOOTBALL_API_KEY:
        logger.warning("FOOTBALL_API_KEY not set — skipping kickoff fetch.")
        return {}
    url = f"{FOOTBALL_API_BASE}/competitions/{COMPETITION_CODE}/matches"
    try:
        resp = requests.get(url, headers={"X-Auth-Token": FOOTBALL_API_KEY}, timeout=15)
        resp.raise_for_status()
        matches = resp.json().get("matches", [])
    except Exception as e:
        logger.error("Failed to fetch fixtures: %s", e)
        return {}

    out = {}
    for m in matches:
        try:
            home = m["homeTeam"]["name"]
            away = m["awayTeam"]["name"]
            utc = m["utcDate"]  # e.g. "2026-06-20T19:00:00Z"
        except (KeyError, TypeError):
            continue
        if home and away and utc:
            out[(canon(home), canon(away))] = utc
    logger.info("Fetched %d fixtures from the API.", len(out))
    return out
