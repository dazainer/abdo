"""Proactive delivery: everything Abdo says first (reminders, briefing, prayers) goes
through deliver(). Push if the member has the app's notifications on, otherwise a
Telegram message. Never both, so nothing arrives twice."""
import json
import logging

from app import push, telegram

log = logging.getLogger("abdo")

PREF_DEFAULTS = {"briefing": True, "briefing_time": "08:00", "prayers": True}


def prefs_of(member) -> dict:
    """The member's preferences with defaults filled in."""
    raw = member["prefs"] if member and "prefs" in member.keys() else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return {**PREF_DEFAULTS, **(raw or {})}


async def deliver(member, title: str, body: str, url: str = "/", silent: bool = False) -> str:
    """Returns the channel used: "push", "telegram", or "none"."""
    try:
        if await push.send_to_member(member["id"], title, body, url, silent=silent) > 0:
            return "push"
    except Exception:
        log.exception("push delivery failed for member %s; falling back to Telegram", member["id"])
    if member["telegram_id"]:
        text = f"{title}\n{body}" if body else title
        await telegram.send_message(member["telegram_id"], text, disable_notification=silent)
        return "telegram"
    log.warning("no channel to reach member %s (%r)", member["id"], title)
    return "none"
