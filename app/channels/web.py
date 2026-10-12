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
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app import auth, briefing, calendar_svc, db, notify, prayer, push, stt, tts, turn
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


class PrefsIn(BaseModel):
    briefing: bool | None = None
    briefing_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    prayers: bool | None = None


@router.get("/today")
async def today(member=Depends(current_member)):
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    statuses = [{"member_id": s["member_id"], "name": s["name"], "text": s["text"],
                 "until": s["until_at"].isoformat() if s["until_at"] else None}
                for s in await db.current_statuses()]
    prayers = None
    if prayer.is_configured():
        times = prayer.times_for(now.date())
        upcoming = [n for n in prayer.PRAYERS if times[n] > now]
        prayers = {"next": upcoming[0] if upcoming else None,
                   "times": [{"name": n, "ar": prayer.NAMES[n][0], "en": prayer.NAMES[n][1],
                              "at": times[n].isoformat(), "label": prayer.clock12(times[n])}
                             for n in prayer.ORDER]}
    reminders = [{"id": r["id"], "text": r["text"], "due_at": r["due_at"].isoformat(),
                  "set_by": r["created_by_name"] if r["created_by"] not in (None, member["id"]) else None}
                 for r in await db.upcoming_reminders(member["id"])]
    return {"statuses": statuses, "prayers": prayers, "reminders": reminders}


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
    await db.set_status(member["id"], body.text.strip(), until)
    return {"ok": True}


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
