"""Web-app login: a 6-digit code delivered by Telegram DM, then a long-lived session.

Why a Telegram-delivered code: it works inside an installed iPhone web app, where
OAuth and magic-link redirects land in Safari instead of the app, and it needs no
passwords or new provider. Telegram is only the code's delivery channel, like SMS
for a bank. Only hashes of codes and session tokens are stored.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app import db, telegram
from app.config import settings

log = logging.getLogger("abdo")

COOKIE = "abdo_session"
CODE_TTL = timedelta(minutes=10)
CODE_MAX_ATTEMPTS = 5
CODES_PER_WINDOW = 3
CODE_WINDOW = timedelta(minutes=15)
SESSION_TTL = timedelta(days=90)
SESSION_TOUCH_EVERY = timedelta(days=1)


class AuthDisabled(Exception):
    """SESSION_PEPPER isn't configured, so web login is off."""


def enabled() -> bool:
    return bool(settings.session_pepper)


def _hash(value: str) -> str:
    if not settings.session_pepper:
        raise AuthDisabled()
    return hmac.new(settings.session_pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def code_message(code: str) -> str:
    return (f"كود الدخول بتاعك على تطبيق عبده: {code}\n"
            f"Your Abdo login code: {code} (valid 10 minutes).\n"
            f"If this wasn't you, just ignore it.")


async def request_code(name: str) -> None:
    """DM a fresh login code to the member called `name`, if there is one with a
    Telegram id. Silent either way: the caller always answers "ok", so the endpoint
    can't be used to discover who lives in the suite."""
    member_id = await db.get_member_id_by_name(name)
    if member_id is None:
        return
    member = await db.get_member(member_id)
    if not member or not member["telegram_id"]:
        return
    if await db.count_recent_login_codes(member_id, _now() - CODE_WINDOW) >= CODES_PER_WINDOW:
        log.warning("login code rate limit hit for member %s", member_id)
        return
    code = f"{secrets.randbelow(10**6):06d}"
    await db.create_login_code(member_id, _hash(code), _now() + CODE_TTL)
    # A private chat's id is the user's Telegram id.
    await telegram.send_message(member["telegram_id"], code_message(code))


async def verify_code(name: str, code: str):
    """Check a code. Returns the member id on success, else None. Five wrong tries
    kill the code; a used or expired code never works."""
    member_id = await db.get_member_id_by_name(name)
    if member_id is None:
        return None
    row = await db.latest_live_login_code(member_id)
    if row is None or row["attempts"] >= CODE_MAX_ATTEMPTS:
        return None
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()) or \
            not hmac.compare_digest(_hash(code), row["code_hash"]):
        await db.bump_login_code_attempts(row["id"])
        return None
    if not await db.use_login_code(row["id"]):
        return None
    return member_id


async def create_session(member_id: int, user_agent: str | None) -> str:
    """Start a session and return the raw token for the cookie (never stored)."""
    token = secrets.token_urlsafe(32)
    await db.create_session(_hash(token), member_id, (user_agent or "")[:300], _now() + SESSION_TTL)
    return token


async def member_for_token(token: str | None):
    """The member behind a session cookie, or None. Slides the expiry forward at
    most once a day, so an app used regularly never logs you out."""
    if not token or not enabled():
        return None
    token_hash = _hash(token)
    member = await db.session_member(token_hash)
    if member is None:
        return None
    last = member["session_last_seen"]
    if last is None or _now() - last > SESSION_TOUCH_EVERY:
        await db.touch_session(token_hash, _now() + SESSION_TTL)
    return member


async def end_session(token: str | None) -> None:
    if token and enabled():
        await db.delete_session(_hash(token))
