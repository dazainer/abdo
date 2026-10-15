"""Abdo's background loop: reminders, the morning briefing, and prayer notifications.

Every 30 s it looks at the clock and the database. All state lives in Postgres, so a
restart loses nothing, and every send is claimed in the database first (a reminder row
flipped to done, or a sent_log row for daily sends). Railway's zero-downtime deploys
briefly run two instances; only the claimer delivers, so nothing is ever sent twice.
"""
import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import briefing, db, location, notify, prayer
from app.config import settings

log = logging.getLogger("abdo")

TICK_SECONDS = 30
PRAYER_WINDOW = timedelta(minutes=20)     # after that, a missed adhan is skipped, not sent late
LATE_AFTER = timedelta(hours=6)           # reminders this overdue still go out, marked "(late)"
BRIEFING_CUTOFF = time(12, 0)             # a briefing missed by noon is skipped


def _parse_hhmm(value: str) -> time:
    try:
        h, m = (int(x) for x in value.split(":"))
        return time(h, m)
    except (ValueError, AttributeError):
        return time(8, 0)


# Urgent reminders ("ضروري", "مهم جداً") go out up to URGENT_SENDS times, URGENT_EVERY apart,
# until the person taps the notification or messages Abdo (db.ack_reminders).
URGENT_SENDS = 3
URGENT_EVERY = timedelta(minutes=2)


async def send_due_reminders(now: datetime) -> int:
    sent = 0
    for rid in await db.due_reminder_ids(now):
        row = await db.claim_reminder(rid, now, URGENT_SENDS, URGENT_EVERY)
        if row is None:
            continue  # another instance took it
        member = await db.get_member(row["member_id"])
        if member is None:
            continue
        first = row["sends"] == 1
        late = "(late) " if first and now - row["due_at"] > LATE_AFTER else ""
        own = row["created_by"] in (None, row["member_id"])
        body = f"{late}{row['text']}" if own else f"{late}From {row['created_by_name']}: {row['text']}"
        title = "⏰ Reminder"
        if row["urgent"]:
            title = "⏰‼️ Important" + ("" if first else f" ({row['sends']}/{URGENT_SENDS})")
            if not row["done"]:
                body += "\nI'll remind you again in 2 minutes. Tap this or message me to stop."
        try:
            await notify.deliver(member, title, body, url="/", urgent=row["urgent"],
                                 ack=row["id"] if row["urgent"] else None)
            sent += 1
        except Exception:
            log.exception("reminder #%s delivery failed", rid)
    return sent


async def send_briefings(members, now: datetime, locations: dict) -> int:
    sent = 0
    for m in members:
        p = notify.prefs_of(m)
        if not p["briefing"]:
            continue
        # 8:00 where the member is (travel mode), not necessarily where the suite is.
        loc = locations.get(m["id"])
        local = now.astimezone(ZoneInfo(loc.tz if loc else settings.timezone))
        if not (_parse_hhmm(p["briefing_time"]) <= local.time() < BRIEFING_CUTOFF):
            continue
        if not await db.claim_daily(m["id"], "briefing", local.date()):
            continue
        try:
            title, body = await briefing.build(m, now)
            await notify.deliver(m, title, body, url="/today")
            sent += 1
        except Exception:
            log.exception("briefing for member %s failed", m["id"])
    return sent


async def send_prayers(members, now: datetime, locations: dict) -> int:
    """Members are grouped by where they are (home, or a travel place), and each group's
    prayer times are computed for that place, in that place's timezone and local day."""
    if not prayer.is_configured():
        return 0
    groups: dict = {}
    for m in members:
        loc = locations.get(m["id"])
        if loc is not None and notify.prefs_of(m)["prayers"]:
            groups.setdefault(loc, []).append(m)
    sent = 0
    for loc, group in groups.items():
        local = now.astimezone(ZoneInfo(loc.tz))
        times = prayer.times_at(local.date(), loc)
        where = "" if loc.is_home else f" in {loc.label}"
        for name in prayer.PRAYERS:
            at = times[name]
            if not (at <= local < at + PRAYER_WINDOW):
                continue
            ar, en = prayer.NAMES[name]
            for m in group:
                if not await db.claim_daily(m["id"], f"prayer:{name}", local.date()):
                    continue
                try:
                    # Fajr never makes a sound: people wake to their own alarms.
                    await notify.deliver(m, f"{en} ({ar})", f"It's {en} time{where}: {prayer.clock12(at)}",
                                         url="/today", silent=(name == "fajr"))
                    sent += 1
                except Exception:
                    log.exception("prayer %s for member %s failed", name, m["id"])
    return sent


async def tick(now: datetime | None = None) -> None:
    """One pass. Each part is isolated so one failure never blocks the others."""
    now = now or datetime.now(timezone.utc)
    try:
        await send_due_reminders(now)
    except Exception:
        log.exception("scheduler: reminders step failed")
    try:
        members = await db.all_members()
        locations = await location.all_locations(members)
    except Exception:
        log.exception("scheduler: couldn't load members/locations")
        return
    for step in (send_briefings, send_prayers):
        try:
            await step(members, now, locations)
        except Exception:
            log.exception("scheduler: %s failed", step.__name__)


async def run() -> None:
    log.info("scheduler started (every %ss)", TICK_SECONDS)
    while True:
        await tick()
        await asyncio.sleep(TICK_SECONDS)
