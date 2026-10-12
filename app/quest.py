"""A deterministic parser for Quest's "My Class Schedule → List View", copied as text.

No model involved: Quest's layout is fixed, so it's parsed exactly (and for free). Handles
what real copies look like:
  - each field on its own line (mobile / some browsers) or tab-separated rows (desktop);
  - continuation rows: a second meeting of the same section printed without Class Nbr /
    Section / Component (e.g. a test with two dates) inherits them from the row above;
  - DD/MM/YYYY or MM/DD/YYYY dates, depending on the student's settings, decided from the
    whole schedule;
  - 24-hour or AM/PM times;
  - TST slots and one-day rows → proposed EXAMS, never weekly classes;
  - TBA / online rows and dropped or waitlisted courses → skipped, with a reason.
"""
import re
from collections import Counter
from datetime import date

COURSE_RE = re.compile(r"^([A-Z]{2,8})\s*(\d{1,3}[A-Z]?)\s+-\s+(.+)$")
DATE_RANGE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})$")
TIME = r"\d{1,2}:\d{2}\s*(?:[AaPp][Mm])?"
DAYS_TIMES_RE = re.compile(rf"^([A-Za-z]+)\s+({TIME})\s*-\s*({TIME})$")
DAY_CODES = {"M": "Mon", "T": "Tue", "W": "Wed", "Th": "Thu", "F": "Fri", "S": "Sat", "Su": "Sun"}
COMPONENTS = {"LEC", "TUT", "LAB", "TST", "SEM", "PRJ", "DIS", "ESS", "FLD", "CLN", "ORL",
              "PRA", "RDG", "STU", "WRK", "WSP", "OLN"}
STATUSES = {"Enrolled", "Dropped", "Waitlisted"}


class QuestFormatError(ValueError):
    """The text isn't a Quest list view we can read. The message is safe to show."""


def _days(code: str) -> list[str] | None:
    parts = re.findall(r"Th|Su|M|T|W|F|S", code)
    if "".join(parts) != code:
        return None
    return [DAY_CODES[p] for p in parts]


def _hhmm(raw: str) -> str:
    raw = raw.strip().upper().replace(" ", "")
    ampm = raw[-2:] if raw[-2:] in ("AM", "PM") else None
    h, m = (int(x) for x in (raw[:-2] if ampm else raw).split(":"))
    if ampm == "PM" and h < 12:
        h += 12
    if ampm == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{m:02d}"


def _tokens(text: str) -> list[tuple[str, list[str]]]:
    """[(course header, [field tokens...])] — fields split on tabs and lines, blanks dropped."""
    courses: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if COURSE_RE.match(stripped):
            courses.append((stripped, []))
        elif courses:
            courses[-1][1].extend(f.strip() for f in line.split("\t") if f.strip())
    return courses


def _records(tokens: list[str]) -> list[list[str]]:
    """Meeting records after the column header: each one ends at its date range."""
    try:
        start = tokens.index("Start/End Date") + 1
    except ValueError:
        return []
    records, acc = [], []
    for t in tokens[start:]:
        acc.append(t)
        if DATE_RANGE_RE.match(t):
            records.append(acc)
            acc = []
    return records


def _date_order(pairs: list[tuple[int, int]]) -> str:
    """'dmy' or 'mdy' for the whole schedule. A part over 12 decides it; otherwise pick the
    reading under which every range is in order and terms look like terms (DD/MM wins ties:
    it's what Quest shows with Canadian settings)."""
    if any(a > 12 for a, _ in pairs):
        return "dmy"
    if any(b > 12 for _, b in pairs):
        return "mdy"
    return "dmy"


def _to_date(d: int, m: int, y: int) -> date:
    return date(y, m, d)


def parse(text: str) -> dict:
    """Quest list-view text -> {"classes", "exams", "skipped", "term_start", "term_end"}.

    classes: weekly meetings in the same shape the review screen and validate() use.
    exams: one-off test slots (TST, or first day == last day), proposed for review.
    Raises QuestFormatError if no course could be read.
    """
    courses = _tokens(text or "")
    if not courses:
        raise QuestFormatError("that doesn't look like Quest's List View. In Quest: My Class "
                               "Schedule → List View, then copy the whole page")

    parsed = []           # (course code, status, record fields)
    all_pairs = []
    for header, tokens in courses:
        m = COURSE_RE.match(header)
        code = f"{m.group(1)} {m.group(2)}"
        status = next((t for t in tokens if t in STATUSES), "Enrolled")
        last_section, last_component = None, None
        for rec in _records(tokens):
            nbr = next((t for t in rec if re.fullmatch(r"\d{4,5}", t)), None)
            section = next((t for t in rec if re.fullmatch(r"\d{3}", t)), None)
            component = next((t for t in rec if t in COMPONENTS), None)
            if component is None:          # continuation row: same section, another date
                section, component = last_section, last_component
            last_section, last_component = section, component
            dt_index = next((i for i, t in enumerate(rec) if t == "TBA" or DAYS_TIMES_RE.match(t)), None)
            days_times = rec[dt_index] if dt_index is not None else "TBA"
            room = rec[dt_index + 1] if dt_index is not None and dt_index + 1 < len(rec) - 1 else ""
            dm = DATE_RANGE_RE.match(rec[-1])
            nums = [int(x) for x in dm.groups()]
            all_pairs += [(nums[0], nums[1]), (nums[3], nums[4])]
            parsed.append({"course": code, "status": status, "nbr": nbr, "section": section or "",
                           "kind": component or "", "days_times": days_times, "room": room, "nums": nums})

    order = _date_order(all_pairs)
    classes, exams, skipped = [], [], []
    for r in parsed:
        a, b, y1, c, d, y2 = r["nums"]
        try:
            start = _to_date(a, b, y1) if order == "dmy" else _to_date(b, a, y1)
            end = _to_date(c, d, y2) if order == "dmy" else _to_date(d, c, y2)
        except ValueError:
            skipped.append(f"{r['course']} {r['kind']}: unreadable dates")
            continue
        label = f"{r['course']} {r['kind']}".strip()
        if r["status"] != "Enrolled":
            skipped.append(f"{label}: {r['status'].lower()}")
            continue
        m = DAYS_TIMES_RE.match(r["days_times"])
        room = "" if r["room"].upper() in ("TBA", "") else r["room"]
        if not m:
            where = "online" if r["room"].upper().startswith("ONLN") else "no set time"
            skipped.append(f"{label}: {where}, no weekly slot")
            continue
        days = _days(m.group(1))
        if not days:
            skipped.append(f"{label}: unreadable days '{m.group(1)}'")
            continue
        s, e = _hhmm(m.group(2)), _hhmm(m.group(3))
        if r["kind"] == "TST" or start == end:
            exams.append({"course": r["course"], "date": start.isoformat(), "start": s, "end": e,
                          "room": room, "kind": "midterm"})
            continue
        classes.append({"course": r["course"], "section": r["section"], "kind": r["kind"], "days": days,
                        "start": s, "end": e, "room": room,
                        "start_date": start.isoformat(), "end_date": end.isoformat()})

    if not classes and not exams:
        raise QuestFormatError("found courses but no class times; make sure you copied the List View")
    starts = Counter(c["start_date"] for c in classes)
    ends = Counter(c["end_date"] for c in classes)
    return {"classes": classes, "exams": sorted(exams, key=lambda x: (x["date"], x["start"])),
            "skipped": skipped,
            "term_start": starts.most_common(1)[0][0] if starts else "",
            "term_end": ends.most_common(1)[0][0] if ends else ""}
