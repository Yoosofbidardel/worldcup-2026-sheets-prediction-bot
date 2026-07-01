"""Team-name display map.

On this English branch it is a pass-through: the sheet and the API already use
English team names, so `fa()` returns the name unchanged. (On the Persian branch
this maps each team to its Farsi display name.)
"""


def fa(name: str) -> str:
    """Display name for a team (unchanged on the English branch)."""
    if not name:
        return name
    return str(name).strip()
