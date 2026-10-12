"""Web channel: the JSON API behind Abdo's web app (React PWA in web/).

Two kinds of routes:
  - Talking to Abdo (/api/chat, /api/chat/voice) goes through turn.handle_turn —
    the same entry point as Telegram, on the member's personal thread dm:<id>.
  - The shared-state screens (shopping, packages, calendar) are plain views over the
    same tables, using the same db/calendar functions as Abdo's tools. No Claude call
    for a checkbox: that would cost money for nothing, and the rows are the same ones
    Abdo reads.

Every route except /api/auth/* needs a session cookie. Every mutating route needs the
X-Abdo header: browsers won't send a custom header cross-site without CORS (which is
not enabled), so it doubles as CSRF protection alongside SameSite=Lax.
"""
import base64
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app import auth, briefing, calendar_svc, db, location, money, notify, pantry, polls, prayer, push, quest, stt, timetable, tts, turn
from app.config import settings
from app.tools import TimeError_, event_when, read_calendar, resolve_until

log = logging.getLogger("abdo")
router = APIRouter(prefix="/api")

MAX_AUDIO_BYTES = 3 * 1024 * 1024
DIDNT_HEAR = "معلش، مسمعتش كويس — ممكن تعيد؟"


# --- dependencies ----------------------------------------------------------------

async def current_member(request: Request):
    member = await auth.member_for_token(request.cookies.get(auth.COOKIE))
    if member is None:
        raise HTTPException(401, "not logged in")
    return member


def same_origin(x_abdo: str | None = Header(default=None)):
    if x_abdo != "1":
        raise HTTPException(403, "missing X-Abdo header")


def require_auth_enabled():
    if not auth.enabled():
        raise HTTPException(503, "web login isn't configured on the server")


def personal_thread(member) -> str:
    return f"dm:{member['id']}"


def public_member(member) -> dict:
    return {"id": member["id"], "name": member["name"], "arabic_name": member["arabic_name"]}


# --- auth ------------------------------------------------------------------------

class CodeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class CodeVerify(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    code: str = Field(min_length=1, max_length=12)


@router.post("/auth/request-code", dependencies=[Depends(same_origin), Depends(require_auth_enabled)])
async def request_code(body: CodeRequest):
    try:
        await auth.request_code(body.name)
    except Exception:
        # Never reveal (via a 500) whether the name matched someone.
        log.exception("request_code failed")
    return {"ok": True}


@router.post("/auth/verify", dependencies=[Depends(same_origin), Depends(require_auth_enabled)])
async def verify(body: CodeVerify, request: Request, response: Response):
    member_id = await auth.verify_code(body.name, body.code)
    if member_id is None:
        raise HTTPException(400, "wrong or expired code")
    token = await auth.create_session(member_id, request.headers.get("user-agent"))
    response.set_cookie(auth.COOKIE, token, max_age=int(auth.SESSION_TTL.total_seconds()),
                        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    return {"member": public_member(await db.get_member(member_id))}


@router.post("/auth/logout", dependencies=[Depends(same_origin)])
async def logout(request: Request, response: Response):
    await auth.end_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(member=Depends(current_member)):
    return {"member": public_member(member)}


# --- chat ------------------------------------------------------------------------

class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _message_out(r) -> dict:
    return {"id": r["id"], "role": r["role"], "content": r["content"], "channel": r["channel"],
            "modality": r["modality"], "created_at": r["created_at"].isoformat(),
            "speaker": r["speaker"]}


@router.get("/chat/history")
async def chat_history(before: int | None = Query(default=None), limit: int = Query(default=30, ge=1, le=100),
                       member=Depends(current_member)):
    rows = await db.thread_page(personal_thread(member), before, limit + 1)
    has_more = len(rows) > limit
    page = list(reversed(rows[:limit]))          # oldest first for display
    return {"messages": [_message_out(r) for r in page], "has_more": has_more}


@router.post("/chat", dependencies=[Depends(same_origin)])
async def chat(body: ChatIn, member=Depends(current_member)):
    reply = await turn.handle_turn(member=member, text=body.text.strip(), channel="web",
                                   thread=personal_thread(member))
    return {"reply": reply}


@router.post("/chat/voice", dependencies=[Depends(same_origin)])
async def chat_voice(audio: UploadFile = File(...), member=Depends(current_member)):
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty recording")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "recording too long")
    content_type = (audio.content_type or "audio/webm").split(";")[0]
    try:
        text = await stt.transcribe(data, audio.filename or "voice.webm", content_type)
    except Exception:
        log.exception("web voice STT failed for member %s", member["id"])
        text = ""
    if not text:
        return {"transcript": "", "reply": DIDNT_HEAR, "audio": None}

    reply = await turn.handle_turn(member=member, text=text, channel="web",
                                   thread=personal_thread(member), voice=True)
    audio_b64 = None
    if settings.voice_replies and settings.elevenlabs_api_key and settings.tts_voice_id:
        try:
            audio_b64 = base64.b64encode(await tts.synthesize_mp3(reply)).decode()
        except Exception:
            # The text reply still goes back; the app just doesn't play audio.
            log.exception("web voice TTS failed for member %s", member["id"])
    return {"transcript": text, "reply": reply, "audio": audio_b64}


# --- shopping list ---------------------------------------------------------------

class ShoppingIn(BaseModel):
    item: str = Field(min_length=1, max_length=100)
    qty: str | None = Field(default=None, max_length=50)


class ItemRef(BaseModel):
    item: str = Field(min_length=1, max_length=100)


@router.get("/shopping")
async def shopping(member=Depends(current_member)):
    rows = await db.get_shopping_list()
    return {"items": [{"id": r["id"], "item": r["item"], "qty": r["qty"]} for r in rows]}


@router.post("/shopping", dependencies=[Depends(same_origin)])
async def shopping_add(body: ShoppingIn, member=Depends(current_member)):
    if not await db.add_shopping_item(body.item.strip(), (body.qty or "").strip() or None, member["id"]):
        raise HTTPException(409, f"'{body.item}' is already on the list")
    return {"ok": True}


@router.post("/shopping/bought", dependencies=[Depends(same_origin)])
async def shopping_bought(body: ItemRef, member=Depends(current_member)):
    if await db.mark_item_bought(body.item, member["id"]) is None:
        raise HTTPException(404, f"'{body.item}' isn't on the list")
    return {"ok": True}


class GoingIn(BaseModel):
    store: str | None = Field(default=None, max_length=60)


@router.post("/shopping/going", dependencies=[Depends(same_origin)])
async def shopping_going(body: GoingIn, member=Depends(current_member)):
    try:
        return {"notified": await pantry.going_shopping(member, body.store)}
    except pantry.TooSoon:
        raise HTTPException(429, "you already told everyone in the last half hour")


# --- fridge ----------------------------------------------------------------------

class FridgeIn(BaseModel):
    item: str = Field(min_length=1, max_length=80)
    expires_on: str | None = None               # YYYY-MM-DD from a date input
    mine: bool = False


class FridgeDone(BaseModel):
    rebuy: bool = False


@router.get("/fridge")
async def fridge(member=Depends(current_member)):
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    return {"items": [{"id": r["id"], "item": r["item"], "owner": r["owner_name"],
                       "mine": r["owner_id"] == member["id"],
                       "expires_on": r["expires_on"].isoformat() if r["expires_on"] else None,
                       **pantry.expiry(r["expires_on"], today)} for r in await db.open_pantry()]}


@router.post("/fridge", dependencies=[Depends(same_origin)])
async def fridge_add(body: FridgeIn, member=Depends(current_member)):
    expires = None
    if body.expires_on:
        try:
            expires = date.fromisoformat(body.expires_on)
        except ValueError:
            raise HTTPException(400, "expiry must be YYYY-MM-DD")
    iid = await db.add_pantry_item(body.item.strip(), member["id"] if body.mine else None, expires, member["id"])
    return {"id": iid}


@router.post("/fridge/{item_id}/done", dependencies=[Depends(same_origin)])
async def fridge_done(item_id: int, body: FridgeDone, member=Depends(current_member)):
    item = await db.use_pantry_item(item_id)
    if item is None:
        raise HTTPException(404, f"no fridge item #{item_id}")
    rebought = await db.add_shopping_item(item, None, member["id"]) if body.rebuy else False
    return {"ok": True, "added_to_list": rebought}


# --- polls -----------------------------------------------------------------------

class PollIn(BaseModel):
    question: str = Field(min_length=1, max_length=140)
    options: list[str] = Field(min_length=2, max_length=6)


class VoteIn(BaseModel):
    option_index: int = Field(ge=0, le=5)


@router.get("/polls")
async def poll_list(member=Depends(current_member)):
    return {"polls": [{**{k: t[k] for k in ("id", "question", "closed", "options", "waiting_on", "created_by_name")},
                       "mine": t["created_by"] == member["id"], "my_vote": t["voted"].get(member["id"])}
                      for t in await polls.overview()]}


@router.post("/polls", dependencies=[Depends(same_origin)])
async def poll_create(body: PollIn, member=Depends(current_member)):
    try:
        return {"id": await polls.create(body.question, body.options, member)}
    except polls.PollError as e:
        raise HTTPException(400, str(e))


@router.post("/polls/{poll_id}/vote", dependencies=[Depends(same_origin)])
async def poll_vote(poll_id: int, body: VoteIn, member=Depends(current_member)):
    poll = await db.get_poll(poll_id)
    if poll is None or poll["closed"]:
        raise HTTPException(404, "no open poll with that id")
    if body.option_index >= len(polls._options(poll)):
        raise HTTPException(400, "no such option")
    await db.cast_vote(poll_id, member["id"], body.option_index)
    return {"ok": True}


@router.delete("/polls/{poll_id}/vote", dependencies=[Depends(same_origin)])
async def poll_unvote(poll_id: int, member=Depends(current_member)):
    if not await db.remove_vote(poll_id, member["id"]):
        raise HTTPException(404, "no vote of yours in an open poll with that id")
    return {"ok": True}


@router.delete("/polls/{poll_id}", dependencies=[Depends(same_origin)])
async def poll_delete(poll_id: int, member=Depends(current_member)):
    if not await db.delete_poll(poll_id, member["id"]):
        raise HTTPException(404, "no poll of yours with that id")
    return {"ok": True}


@router.post("/polls/{poll_id}/close", dependencies=[Depends(same_origin)])
async def poll_close(poll_id: int, member=Depends(current_member)):
    if not await db.close_poll(poll_id, member["id"]):
        raise HTTPException(404, "no open poll of yours with that id")
    return {"ok": True}


# --- packages --------------------------------------------------------------------

class PackageIn(BaseModel):
    description: str = Field(min_length=1, max_length=120)
    owner: str | None = Field(default=None, max_length=60)
    carrier: str | None = Field(default=None, max_length=60)
    day: str | None = Field(default=None, max_length=40)


class PackageStatus(BaseModel):
    status: str


def _package_out(r) -> dict:
    return {"id": r["id"], "description": r["description"], "carrier": r["carrier"],
            "owner": r["owner_name"], "status": r["status"],
            "expected_on": r["expected_on"].isoformat() if r["expected_on"] else None}


@router.get("/packages")
async def packages(member=Depends(current_member)):
    return {"packages": [_package_out(r) for r in await db.get_open_packages()]}


@router.post("/packages", dependencies=[Depends(same_origin)])
async def package_add(body: PackageIn, member=Depends(current_member)):
    # Same rules as Abdo's add_package tool: the day is resolved in code, and an
    # unknown owner is an error, never a guess.
    expected_on: date | None = None
    if body.day and body.day.strip():
        try:
            expected_on = calendar_svc.resolve_date(body.day)
        except ValueError:
            raise HTTPException(400, f"couldn't understand the day '{body.day}'")
    owner_id = member["id"]
    if body.owner and body.owner.strip():
        owner_id = await db.get_member_id_by_name(body.owner)
        if owner_id is None:
            raise HTTPException(400, f"nobody in the suite is called '{body.owner}'")
    pid = await db.add_package(body.description.strip(), (body.carrier or "").strip() or None,
                               owner_id, expected_on)
    return {"id": pid}


@router.patch("/packages/{package_id}", dependencies=[Depends(same_origin)])
async def package_status(package_id: int, body: PackageStatus, member=Depends(current_member)):
    if body.status not in ("arrived", "picked_up", "cancelled"):
        raise HTTPException(400, "status must be arrived, picked_up, or cancelled")
    if await db.update_package_status(package_id, body.status) is None:
        raise HTTPException(404, f"no open package #{package_id}")
    return {"ok": True}


# --- calendar (read-only; changes go through Abdo, who confirms) -------------------

@router.get("/calendar")
async def calendar(days: int = Query(default=7, ge=1, le=31), member=Depends(current_member)):
    if not calendar_svc.is_configured():
        return {"configured": False, "events": []}
    try:
        events = await read_calendar(days)
    except calendar_svc.CalendarReadError:
        # A degraded read must never look like an empty calendar.
        raise HTTPException(503, "couldn't reach the calendar, try again in a moment")
    return {"configured": True,
            "events": [{"id": e["id"], "summary": e["summary"], **event_when(e)} for e in events]}


# --- today: statuses, prayer times, my reminders -----------------------------------

class StatusIn(BaseModel):
    text: str = Field(min_length=1, max_length=80)
    until_day: str | None = Field(default=None, max_length=40)
    until_time: str | None = Field(default=None, max_length=10)
    place: str | None = Field(default=None, max_length=80)   # travel mode


class PrefsIn(BaseModel):
    briefing: bool | None = None
    briefing_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    prayers: bool | None = None


@router.get("/today")
async def today(member=Depends(current_member)):
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    statuses = [{"member_id": s["member_id"], "name": s["name"], "text": s["text"],
                 "until": s["until_at"].isoformat() if s["until_at"] else None,
                 "place": s.get("place")}
                for s in await db.current_statuses()]
    prayers = None
    loc = await location.location_for(member["id"])
    if prayer.is_configured() and loc is not None:
        local = datetime.now(ZoneInfo(loc.tz))      # travel mode: their place, their clock
        times = prayer.times_at(local.date(), loc)
        upcoming = [n for n in prayer.PRAYERS if times[n] > local]
        prayers = {"next": upcoming[0] if upcoming else None,
                   "place": None if loc.is_home else loc.label,
                   "times": [{"name": n, "ar": prayer.NAMES[n][0], "en": prayer.NAMES[n][1],
                              "at": times[n].isoformat(), "label": prayer.clock12(times[n])}
                             for n in prayer.ORDER]}
    free = await timetable.who_is_free(datetime.now(ZoneInfo(settings.timezone)).date())
    reminders = [{"id": r["id"], "text": r["text"], "due_at": r["due_at"].isoformat(),
                  "set_by": r["created_by_name"] if r["created_by"] not in (None, member["id"]) else None}
                 for r in await db.upcoming_reminders(member["id"])]
    return {"statuses": statuses, "prayers": prayers, "reminders": reminders,
            "free": {"from": free["from"], "to": free["to"], "together": free["together"],
                     "away": [p["name"] for p in free["people"] if p["away"]],
                     "no_timetable": [p["name"] for p in free["people"] if not p["has_timetable"]]}}


@router.get("/briefing")
async def get_briefing(member=Depends(current_member)):
    """The member's briefing, live (the same data the 8:00 notification is rendered from)."""
    return await briefing.gather(member)


@router.put("/status", dependencies=[Depends(same_origin)])
async def set_status(body: StatusIn, member=Depends(current_member)):
    try:
        until = resolve_until(body.until_day, body.until_time)
    except TimeError_ as e:
        raise HTTPException(400, str(e))
    place = None
    if body.place and body.place.strip():
        try:
            place = await location.geocode(body.place)
        except location.PlaceNotFound as e:
            raise HTTPException(400, str(e))
        except Exception:
            log.exception("geocoding failed")
            raise HTTPException(502, "couldn't look up that place right now; try again")
    await db.set_status(member["id"], body.text.strip(), until, place)
    return {"ok": True, "place": place["label"] if place else None}


@router.delete("/status", dependencies=[Depends(same_origin)])
async def clear_status(member=Depends(current_member)):
    await db.clear_status(member["id"])
    return {"ok": True}


@router.delete("/reminders/{reminder_id}", dependencies=[Depends(same_origin)])
async def cancel_reminder(reminder_id: int, member=Depends(current_member)):
    # Only a reminder that's for you, or one you set, can be cancelled.
    if await db.cancel_reminder(reminder_id, member["id"]) is None:
        raise HTTPException(404, f"no pending reminder #{reminder_id} of yours")
    return {"ok": True}


@router.get("/me/prefs")
async def get_prefs(member=Depends(current_member)):
    return {"prefs": notify.prefs_of(member)}


@router.patch("/me/prefs", dependencies=[Depends(same_origin)])
async def update_prefs(body: PrefsIn, member=Depends(current_member)):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "nothing to change")
    saved = await db.set_member_prefs(member["id"], updates)
    return {"prefs": {**notify.PREF_DEFAULTS, **saved}}


# --- schedule + exams ----------------------------------------------------------------

class ExtractIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class ClassIn(BaseModel):
    course: str = Field(max_length=40)
    section: str = Field(default="", max_length=20)
    kind: str = Field(default="", max_length=10)
    days: list[str]
    start: str = Field(max_length=5)
    end: str = Field(max_length=5)
    room: str = Field(default="", max_length=40)
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)


class ProposedExamIn(BaseModel):
    course: str = Field(min_length=1, max_length=40)
    kind: str
    date: str
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    room: str | None = Field(default=None, max_length=40)


class ScheduleIn(BaseModel):
    classes: list[ClassIn] = Field(max_length=60)
    term_start: str | None = None
    term_end: str | None = None
    exams: list[ProposedExamIn] = Field(default_factory=list, max_length=20)   # test slots to add


class ExamIn(BaseModel):
    course: str = Field(min_length=1, max_length=40)
    kind: str
    date: str                                       # YYYY-MM-DD (picked in a date input)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    room: str | None = Field(default=None, max_length=40)


def _class_out(c) -> dict:
    return {"course": c["course"], "section": c["section"] or "", "kind": c["kind"] or "",
            "day": timetable.DAYS[c["weekday"]], "start": c["starts"].strftime("%H:%M"),
            "end": c["ends"].strftime("%H:%M"), "room": c["room"] or "",
            "term_start": c["term_start"].isoformat(), "term_end": c["term_end"].isoformat()}


@router.get("/schedule")
async def schedule(member=Depends(current_member)):
    return {"classes": [_class_out(c) for c in await db.classes_for(member["id"])]}


@router.post("/schedule/parse", dependencies=[Depends(same_origin)])
async def schedule_parse(body: ExtractIn, member=Depends(current_member)):
    """Reads pasted Quest list-view text (deterministic, no model). Saves nothing: the member
    reviews the proposed classes and test slots, then PUTs."""
    try:
        return quest.parse(body.text)
    except quest.QuestFormatError as e:
        raise HTTPException(400, str(e))


@router.put("/schedule", dependencies=[Depends(same_origin)])
async def schedule_save(body: ScheduleIn, member=Depends(current_member)):
    try:
        ts = date.fromisoformat(body.term_start) if body.term_start else None
        te = date.fromisoformat(body.term_end) if body.term_end else None
    except ValueError:
        raise HTTPException(400, "term dates must be YYYY-MM-DD")
    rows, problems = timetable.validate([c.model_dump() for c in body.classes], ts, te)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    tz = ZoneInfo(settings.timezone)
    new_exams = []
    for x in body.exams:
        if x.kind not in timetable.KINDS:
            raise HTTPException(400, "an exam is a quiz, midterm, or final")
        try:
            d = date.fromisoformat(x.date)
        except ValueError:
            raise HTTPException(400, "exam dates must be YYYY-MM-DD")
        starts = datetime.combine(d, datetime.strptime(x.start, "%H:%M").time(), tz)
        ends = datetime.combine(d, datetime.strptime(x.end, "%H:%M").time(), tz) if x.end else None
        new_exams.append((x, starts, ends))
    await db.replace_classes(member["id"], rows)
    added = 0
    for x, starts, ends in new_exams:
        # Re-importing must not duplicate an exam already on the list.
        same = await db.exams_between(starts, starts + timedelta(minutes=1), member["id"])
        if any(e["course"] == x.course.upper() for e in same):
            continue
        await db.add_exam(member["id"], x.course.strip().upper(), x.kind, starts, ends,
                          (x.room or "").strip() or None, member["id"])
        added += 1
    return {"saved": len(rows), "exams_added": added}


@router.get("/exams")
async def exams(member=Depends(current_member)):
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    rows = await db.exams_between(now - timedelta(hours=3), now + timedelta(days=60))
    return {"exams": [{"id": e["id"], "member_id": e["member_id"], "name": e["name"], "course": e["course"],
                       "kind": e["kind"], "starts_at": e["starts_at"].isoformat(),
                       "ends_at": e["ends_at"].isoformat() if e["ends_at"] else None, "room": e["room"],
                       "can_delete": member["id"] in (e["member_id"], e["created_by"])} for e in rows]}


@router.post("/exams", dependencies=[Depends(same_origin)])
async def exam_add(body: ExamIn, member=Depends(current_member)):
    if body.kind not in timetable.KINDS:
        raise HTTPException(400, "an exam is a quiz, midterm, or final")
    tz = ZoneInfo(settings.timezone)
    try:
        d = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    h, m = (int(x) for x in body.time.split(":"))
    starts = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
    ends = None
    if body.end_time:
        eh, em = (int(x) for x in body.end_time.split(":"))
        ends = datetime(d.year, d.month, d.day, eh, em, tzinfo=tz)
        if ends <= starts:
            raise HTTPException(400, "the exam ends before it starts")
    eid = await db.add_exam(member["id"], body.course.strip().upper(), body.kind, starts, ends,
                            (body.room or "").strip() or None, member["id"])
    return {"id": eid}


@router.delete("/exams/{exam_id}", dependencies=[Depends(same_origin)])
async def exam_delete(exam_id: int, member=Depends(current_member)):
    if await db.delete_exam(exam_id, member["id"]) is None:
        raise HTTPException(404, "no exam of yours with that id")
    return {"ok": True}


# --- money ---------------------------------------------------------------------------

class ExpenseIn(BaseModel):
    description: str = Field(min_length=1, max_length=120)
    amount: str = Field(min_length=1, max_length=20)
    paid_by: int | None = None                       # member id; default: me
    split_with: list[int] | None = None              # member ids; default: everyone


class SettlementIn(BaseModel):
    to_member: int
    amount: str = Field(min_length=1, max_length=20)
    from_member: int | None = None                   # default: me


@router.get("/money")
async def money_overview(member=Depends(current_member)):
    members = await db.all_members()
    names = {m["id"]: m["name"] for m in members}
    expenses, shares, settlements = await db.money_ledger()
    net = money.balances(list(names), expenses, shares, settlements)
    return {
        "me": member["id"],
        "members": [{"id": m["id"], "name": m["name"]} for m in members],
        "balances": [{"member_id": m, "name": names[m], "net_cents": v} for m, v in net.items()],
        "transfers": [{"from_member": f, "from_name": names[f], "to_member": t, "to_name": names[t],
                       "cents": c} for f, t, c in money.settle_up(net)],
        "expenses": [{**{k: e[k] for k in ("id", "description", "amount_cents", "paid_by", "paid_by_name",
                                           "created_by")},
                      "spent_on": e["spent_on"].isoformat(), "shares": e["shares"],
                      "can_delete": member["id"] in (e["paid_by"], e["created_by"])}
                     for e in await db.recent_expenses(30)],
        "payments": [{"id": t["id"], "from_name": t["from_name"], "to_name": t["to_name"],
                      "cents": t["amount_cents"], "settled_on": t["settled_on"].isoformat(),
                      "can_delete": member["id"] in (t["from_member"], t["to_member"], t["created_by"])}
                     for t in await db.recent_settlements(10)],
    }


@router.post("/expenses", dependencies=[Depends(same_origin)])
async def expense_add(body: ExpenseIn, member=Depends(current_member)):
    ids = {m["id"] for m in await db.all_members()}
    payer = body.paid_by or member["id"]
    among = body.split_with if body.split_with else sorted(ids)
    if payer not in ids or any(m not in ids for m in among):
        raise HTTPException(400, "unknown roommate")
    try:
        amount = money.parse_amount(body.amount)
        shares = money.split(amount, among)
    except money.MoneyError as e:
        raise HTTPException(400, str(e))
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    eid = await db.add_expense(body.description.strip(), amount, payer, member["id"], today, shares)
    return {"id": eid, "shares": shares}


@router.delete("/expenses/{expense_id}", dependencies=[Depends(same_origin)])
async def expense_delete(expense_id: int, member=Depends(current_member)):
    if await db.delete_expense(expense_id, member["id"]) is None:
        raise HTTPException(404, "no expense you paid or recorded with that id")
    return {"ok": True}


@router.post("/settlements", dependencies=[Depends(same_origin)])
async def settlement_add(body: SettlementIn, member=Depends(current_member)):
    ids = {m["id"] for m in await db.all_members()}
    frm = body.from_member or member["id"]
    if frm not in ids or body.to_member not in ids:
        raise HTTPException(400, "unknown roommate")
    if frm == body.to_member:
        raise HTTPException(400, "a payment needs two different people")
    if member["id"] not in (frm, body.to_member):
        raise HTTPException(403, "you can only record payments you sent or received")
    try:
        amount = money.parse_amount(body.amount)
    except money.MoneyError as e:
        raise HTTPException(400, str(e))
    sid = await db.add_settlement(frm, body.to_member, amount, member["id"],
                                  datetime.now(ZoneInfo(settings.timezone)).date())
    return {"id": sid}


@router.delete("/settlements/{settlement_id}", dependencies=[Depends(same_origin)])
async def settlement_delete(settlement_id: int, member=Depends(current_member)):
    if not await db.delete_settlement(settlement_id, member["id"]):
        raise HTTPException(404, "no payment of yours with that id")
    return {"ok": True}


# --- push ------------------------------------------------------------------------

class PushKeys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class PushSubscription(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: PushKeys


class PushEndpoint(BaseModel):
    endpoint: str = Field(max_length=1000)


@router.get("/push/public-key")
async def push_public_key(member=Depends(current_member)):
    return {"key": settings.vapid_public_key if push.enabled() else None}


@router.post("/push/subscribe", dependencies=[Depends(same_origin)])
async def push_subscribe(body: PushSubscription, member=Depends(current_member)):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(400, "invalid push endpoint")
    await db.save_push_subscription(member["id"], body.endpoint, body.keys.p256dh, body.keys.auth)
    return {"ok": True}


@router.post("/push/unsubscribe", dependencies=[Depends(same_origin)])
async def push_unsubscribe(body: PushEndpoint, member=Depends(current_member)):
    await db.delete_push_subscription(body.endpoint)
    return {"ok": True}


@router.post("/push/test", dependencies=[Depends(same_origin)])
async def push_test(member=Depends(current_member)):
    if not push.enabled():
        raise HTTPException(503, "push isn't configured on the server")
    sent = await push.send_to_member(member["id"], "Abdo",
                                     f"أهلاً يا {member['name']} — notifications work 🎉", "/settings")
    return {"sent": sent}
