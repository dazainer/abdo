"""The fridge (what's in it and when it expires) and "I'm going shopping" alerts.

Expiry is a plain local date; labels ("today", "in 3 days", "expired") are computed here so
Abdo, the app, and the briefing all say the same thing.
"""
import time as _time
from datetime import date

from app import db, notify

GOING_COOLDOWN_SECONDS = 30 * 60      # one "going shopping" ping per person per half hour
_last_going: dict[int, float] = {}


class TooSoon(Exception):
    """A second 'going shopping' ping inside the cooldown."""


def expiry(expires_on: date | None, today: date) -> dict:
    """{"label": "expired 2 days ago" | "today" | "tomorrow" | "in 5 days" | None,
        "state": "expired" | "soon" | "ok" | None}  ('soon' = today or tomorrow)."""
    if expires_on is None:
        return {"label": None, "state": None}
    days = (expires_on - today).days
    if days < 0:
        return {"label": "expired yesterday" if days == -1 else f"expired {-days} days ago", "state": "expired"}
    if days == 0:
        return {"label": "today", "state": "soon"}
    if days == 1:
        return {"label": "tomorrow", "state": "soon"}
    return {"label": f"in {days} days", "state": "ok"}


def describe(rows, today: date) -> str:
    """The fridge as text for the model."""
    if not rows:
        return "The fridge list is empty."
    lines = []
    for r in rows:
        e = expiry(r["expires_on"], today)
        whose = f" ({r['owner_name']}'s)" if r["owner_name"] else " (shared)"
        when = f", expires {e['label']}" if e["label"] and e["state"] != "expired" else \
            (f", {e['label']}" if e["label"] else "")
        lines.append(f"- #{r['id']} {r['item']}{whose}{when}")
    return "\n".join(lines)


async def going_shopping(member, store: str | None) -> int:
    """Tell everyone else someone's heading out, so they can add to the list. Returns how
    many roommates were notified. Raises TooSoon inside the cooldown (no spam)."""
    last = _last_going.get(member["id"])
    if last is not None and _time.monotonic() - last < GOING_COOLDOWN_SECONDS:
        raise TooSoon()
    _last_going[member["id"]] = _time.monotonic()
    where = f" to {store.strip()}" if store and store.strip() else " shopping"
    items = await db.get_shopping_list()
    body = (f"On the list: {', '.join(i['item'] for i in items[:8])}. Need anything else? Add it now."
            if items else "The list is empty. Need anything? Add it now.")
    sent = 0
    for m in await db.all_members():
        if m["id"] == member["id"]:
            continue
        if await notify.deliver(m, f"🛒 {member['name']} is going{where}", body, url="/lists") != "none":
            sent += 1
    return sent
