"""Web push (VAPID). Phase 6 only wires it up and sends a test; Phase 7's briefing,
prayer times, and timers call send_to_member."""
import asyncio
import json
import logging

from pywebpush import WebPushException, webpush

from app import db
from app.config import settings

log = logging.getLogger("abdo")


def enabled() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def _send(sub, payload: str) -> None:
    webpush(
        subscription_info={"endpoint": sub["endpoint"],
                           "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
        data=payload,
        vapid_private_key=settings.vapid_private_key,
        vapid_claims={"sub": settings.vapid_subject},
        ttl=60 * 60,
    )


async def send_to_member(member_id: int, title: str, body: str, url: str = "/") -> int:
    """Push to every device the member subscribed. Returns how many deliveries the
    push service accepted. Expired subscriptions (404/410) are deleted."""
    if not enabled():
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for sub in await db.push_subscriptions_for(member_id):
        try:
            # pywebpush is blocking (requests + crypto): keep it off the event loop.
            await asyncio.to_thread(_send, sub, payload)
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                await db.delete_push_subscription(sub["endpoint"])
            else:
                log.warning("push to member %s failed (%s): %s", member_id, status, e)
    return sent
