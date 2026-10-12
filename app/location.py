"""Where a member is, for prayer times, the briefing's weather, and the Today screen.

Travel mode: a status can carry a place ("away in Montreal till Sunday"). While that status
lasts, that member's location is the place; otherwise it's the suite's home. No GPS: the
person says where they are, and it ends with the status.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app import db
from app.config import settings

log = logging.getLogger("abdo")


@dataclass(frozen=True)
class Location:
    lat: float
    lng: float
    tz: str
    label: str | None = None        # None = home

    @property
    def is_home(self) -> bool:
        return self.label is None


def home() -> Location | None:
    if settings.home_lat is None or settings.home_lng is None:
        return None
    return Location(settings.home_lat, settings.home_lng, settings.timezone)


class PlaceNotFound(ValueError):
    pass


async def _search(name: str) -> dict:
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": name, "count": 1, "language": "en", "format": "json"})
        r.raise_for_status()
        return r.json()


async def geocode(name: str) -> dict:
    """'Montreal' -> {"label": "Montreal, Canada", "lat", "lng", "tz"}. Raises PlaceNotFound."""
    name = (name or "").strip()
    if not name:
        raise PlaceNotFound("no place given")
    results = (await _search(name)).get("results") or []
    if not results:
        raise PlaceNotFound(f"couldn't find a place called '{name}'")
    r = results[0]
    label = ", ".join(x for x in (r.get("name"), r.get("country")) if x)
    return {"label": label, "lat": r["latitude"], "lng": r["longitude"],
            "tz": r.get("timezone") or settings.timezone}


def from_status(status) -> Location | None:
    """The travel location in an unexpired status row, if it has one."""
    if not status or status.get("place_lat") is None or status.get("place_lng") is None:
        return None
    if status["until_at"] is not None and status["until_at"] <= datetime.now(timezone.utc):
        return None
    return Location(status["place_lat"], status["place_lng"], status.get("place_tz") or settings.timezone,
                    status.get("place"))


async def all_locations(members=None) -> dict[int, Location | None]:
    """member_id -> where they are now (their travel place, else home)."""
    by_member = {s["member_id"]: s for s in await db.current_statuses()}
    members = members if members is not None else await db.all_members()
    return {m["id"]: from_status(by_member.get(m["id"])) or home() for m in members}


async def location_for(member_id: int) -> Location | None:
    for s in await db.current_statuses():
        if s["member_id"] == member_id:
            return from_status(s) or home()
    return home()
