"""Class timetables and "who's free" — the code side of schedules.

Import: a member pastes Quest's list view; app/quest.py parses it deterministically and
PROPOSES classes (and test slots as exams); validate() re-checks every field; the member
reviews the preview and only then saves.

Free time is computed here, never by the model: busy intervals from classes (on their
weekday, within the term), exams, and travel ("away"), subtracted from a window.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import db, location
from app.config import settings

log = logging.getLogger("abdo")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]      # index = date.weekday()
KINDS = ("quiz", "midterm", "final")
EXAM_DEFAULT = timedelta(hours=2)        # an exam without an end time blocks 2 hours
DAY_START, DAY_END = time(8, 0), time(23, 0)
MIN_FREE = timedelta(minutes=30)

class TimetableError(ValueError):
    """Something we won't save. The message is safe to show."""


def is_one_off(item: dict) -> bool:
    """TST slots and anything whose first and last day are the same date happen once, not
    weekly (Quest lists a midterm TST slot like a class). They never become weekly classes."""
    kind = (item.get("kind") or "").strip().upper()
    start, end = (item.get("start_date") or "").strip(), (item.get("end_date") or "").strip()
    return kind == "TST" or (bool(start) and start == end)


def split_one_offs(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(weekly items, one-off items) — done in code, whatever the model returned."""
    weekly = [i for i in items if not is_one_off(i)]
    return weekly, [i for i in items if is_one_off(i)]


MIN_TERM, MAX_TERM = timedelta(days=14), timedelta(days=214)


def _hhmm(value: str) -> time:
    try:
        h, m = (int(x) for x in (value or "").strip().split(":"))
        return time(h, m)
    except (ValueError, TypeError):
        raise TimetableError(f"'{value}' isn't a time")


def _date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise TimetableError(f"'{value}' isn't a date")


def validate(items: list[dict], term_start: date | None = None,
             term_end: date | None = None) -> tuple[list[dict], list[str]]:
    """Proposed items -> (rows ready to save, one per meeting day; problems). Rows missing
    term dates take term_start/term_end (the preview lets the member set them). Every row
    is re-checked here, whatever the model or the browser sent."""
    rows, problems = [], []
    items, _ = split_one_offs(items)        # defence in depth: one-offs never save as weekly
    for i, it in enumerate(items, 1):
        label = (it.get("course") or f"row {i}").strip()
        try:
            course = (it.get("course") or "").strip().upper()
            if not course:
                raise TimetableError("no course name")
            days = [d for d in it.get("days") or [] if d in DAYS]
            if not days:
                raise TimetableError("no meeting days")
            starts, ends = _hhmm(it.get("start")), _hhmm(it.get("end"))
            if ends <= starts:
                raise TimetableError("ends before it starts")
            ts = _date(it.get("start_date")) or term_start
            te = _date(it.get("end_date")) or term_end
            if not ts or not te:
                raise TimetableError("no term dates (set them above)")
            if te < ts:
                raise TimetableError("term ends before it starts (check the day/month order)")
            if not MIN_TERM <= te - ts <= MAX_TERM:
                raise TimetableError(f"a term from {ts} to {te} looks wrong (check the day/month order)")
            for d in sorted(set(days), key=DAYS.index):
                rows.append({"course": course, "section": (it.get("section") or "").strip() or None,
                             "kind": (it.get("kind") or "").strip().upper() or None,
                             "weekday": DAYS.index(d), "starts": starts, "ends": ends,
                             "room": (it.get("room") or "").strip() or None,
                             "term_start": ts, "term_end": te})
        except TimetableError as e:
            problems.append(f"{label}: {e}")
    return rows, problems


def term_guess(items: list[dict]) -> tuple[str, str]:
    """The most common start/end dates among proposed items ('' if none), for the preview."""
    def common(key):
        vals = [it.get(key) for it in items if it.get(key)]
        return max(set(vals), key=vals.count) if vals else ""
    return common("start_date"), common("end_date")


# --- busy / free ------------------------------------------------------------------

@dataclass
class Busy:
    start: datetime
    end: datetime
    what: str


def _merge(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[list[datetime]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def free_windows(window: tuple[datetime, datetime], busy: list[tuple[datetime, datetime]],
                 min_len: timedelta = MIN_FREE) -> list[tuple[datetime, datetime]]:
    """The parts of `window` not covered by `busy`, at least min_len long."""
    start, end = window
    free, cursor = [], start
    for s, e in _merge([(max(s, start), min(e, end)) for s, e in busy if e > start and s < end]):
        if s - cursor >= min_len:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if end - cursor >= min_len:
        free.append((cursor, end))
    return free


def on_break(day: date, member_id, breaks) -> dict | None:
    """The member's break covering `day` (reading week etc.), or None."""
    return next((b for b in breaks or () if b["member_id"] == member_id
                 and b["starts_on"] <= day <= b["ends_on"]), None)


def busy_on(day: date, classes, exams, tz: ZoneInfo, breaks=()) -> list[Busy]:
    """One member's commitments on a local day: classes meeting that weekday within the
    term (unless that member is on a break that day), and exams starting that day.
    Exams still count during a break."""
    out = []
    for c in classes:
        if c["weekday"] == day.weekday() and c["term_start"] <= day <= c["term_end"] \
                and not on_break(day, c["member_id"], breaks):
            out.append(Busy(datetime.combine(day, c["starts"], tz), datetime.combine(day, c["ends"], tz),
                            f"{c['course']} {c['kind'] or ''}".strip()))
    for e in exams:
        s = e["starts_at"].astimezone(tz)
        if s.date() == day:
            end = e["ends_at"].astimezone(tz) if e["ends_at"] else s + EXAM_DEFAULT
            out.append(Busy(s, end, f"{e['course']} {e['kind']}"))
    return sorted(out, key=lambda b: b.start)


async def who_is_free(day: date, from_time: time | None = None, to_time: time | None = None,
                      now: datetime | None = None) -> dict:
    """Free time for the whole suite on a local day, computed from timetables, exams, and
    travel. Returns data the tool and the app both render."""
    tz = ZoneInfo(settings.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    start = datetime.combine(day, from_time or DAY_START, tz)
    if day == now.date() and not from_time:
        # From now, rounded up to the next quarter hour.
        q = now + timedelta(minutes=(15 - now.minute % 15) % 15)
        start = max(start, q.replace(second=0, microsecond=0))
    end = datetime.combine(day, to_time or DAY_END, tz)

    members = await db.all_members()
    classes = await db.classes_for()
    breaks = await db.class_breaks(since=day)
    day_start = datetime.combine(day, time(0, 0), tz)
    exams = await db.exams_between(day_start, day_start + timedelta(days=1))
    places = await location.all_locations(members)

    people, together_busy = [], []
    for m in members:
        loc = places.get(m["id"])
        mine_c = [c for c in classes if c["member_id"] == m["id"]]
        busy = busy_on(day, mine_c, [e for e in exams if e["member_id"] == m["id"]], tz, breaks)
        away = loc is not None and not loc.is_home
        people.append({"name": m["name"], "away": loc.label if away else None,
                       "has_timetable": bool(mine_c),
                       "busy": [{"from": b.start.strftime("%H:%M"), "to": b.end.strftime("%H:%M"),
                                 "what": b.what} for b in busy if b.end > start and b.start < end]})
        if not away:
            together_busy += [(b.start, b.end) for b in busy]
    together = free_windows((start, end), together_busy) if end > start else []
    return {"day": day.isoformat(), "label": day.strftime("%A %d %B"),
            "from": start.strftime("%H:%M"), "to": end.strftime("%H:%M"),
            "together": [{"from": a.strftime("%H:%M"), "to": b.strftime("%H:%M")} for a, b in together],
            "people": people}


def describe_free(f: dict) -> str:
    """who_is_free() as text for the model."""
    lines = [f"Free together on {f['label']} ({f['from']}–{f['to']}, everyone in town): "
             + (", ".join(f"{w['from']}–{w['to']}" for w in f["together"]) or "no common free time")]
    for p in f["people"]:
        if p["away"]:
            lines.append(f"- {p['name']}: away (in {p['away']})")
        elif p["busy"]:
            lines.append(f"- {p['name']}: busy " + ", ".join(f"{b['from']}–{b['to']} ({b['what']})" for b in p["busy"]))
        else:
            lines.append(f"- {p['name']}: free all window"
                         + ("" if p["has_timetable"] else " (no timetable on file; may not be accurate)"))
    return "\n".join(lines)
