"""Buses and the ION: next departures for the suite's saved trips.

Source: the GraphQL API behind GRT's own departures site (stoptimes.grt.ca). It serves the
current schedule (holiday service included) and switches a departure to live tracking on its
own when GRT has it, so there's no GTFS to download, store, or keep fresh. It isn't a
documented open-data API, so: one small query per trip, a 60-second cache, and any failure is
reported as "couldn't reach GRT", never filled in with guessed times.

A trip is "board at any of from_stops, get off at any of to_stops" (optionally only on some
routes). A departure counts only if its trip actually reaches a to_stop after the boarding
stop, which also settles the direction. A trip without to_stops is just "what's leaving
this stop".
"""
import asyncio
import logging
import re
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

log = logging.getLogger("abdo.transit")

CACHE_SECONDS = 60
LOOKAHEAD = 40          # departures fetched per trip (a couple of hours at a busy stop)
_cache: dict[tuple, tuple[float, list[dict]]] = {}

ARRIVALS_QUERY = """
query($stops: [ID]!, $limit: Int) {
  arrivals(filter: { stops: $stops }, limit: $limit) {
    stop { id name }
    route { shortName }
    trip { headsign stops { stop { id name } departure } }
    type
    departure
    departureDeltaFromScheduledSeconds
  }
}"""


def now() -> datetime:
    """The local time (a function so tests can pin it)."""
    return datetime.now(ZoneInfo(settings.timezone))


class TransitError(Exception):
    """GRT's departures couldn't be fetched. Safe to say out loud."""


def _parse(arrivals: list[dict]) -> list[dict]:
    out = []
    for a in arrivals or []:
        kind = (a.get("type") or "").upper()
        if "CANCEL" in kind or "SKIP" in kind:
            continue
        try:
            at = datetime.fromisoformat(a["departure"])
            stops = [(s["stop"]["id"], s["stop"]["name"], datetime.fromisoformat(s["departure"]))
                     for s in (a["trip"] or {}).get("stops") or []]
        except (KeyError, TypeError, ValueError):
            continue
        route = a["route"]["shortName"]
        headsign = (a["trip"] or {}).get("headsign") or ""
        out.append({
            "route": route,
            "headsign": headsign.removeprefix(f"{route} "),        # ION's are "301 Fairway"
            "stop_id": a["stop"]["id"],
            "stop_name": a["stop"].get("name") or "",
            "at": at,
            "live": kind not in ("", "SCHEDULED"),
            "delay_min": round((a.get("departureDeltaFromScheduledSeconds") or 0) / 60),
            "trip_stops": stops,
        })
    return sorted(out, key=lambda d: d["at"])


async def _post(stops: list[str]) -> list[dict]:
    async with httpx.AsyncClient(timeout=8, headers={"origin": "https://stoptimes.grt.ca"}) as client:
        r = await client.post(settings.grt_api_url, json={
            "query": ARRIVALS_QUERY, "variables": {"stops": stops, "limit": LOOKAHEAD}})
        r.raise_for_status()
        body = r.json()
    if body.get("errors") or "data" not in body:
        raise TransitError(f"GRT answered with an error: {str(body.get('errors'))[:200]}")
    return body["data"]["arrivals"] or []


async def departures(stops: list[str]) -> list[dict]:
    """Upcoming departures from any of these stops, soonest first (cached for a minute)."""
    key = tuple(sorted(stops))
    hit = _cache.get(key)
    if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        deps = _parse(await _post(list(key)))
    except TransitError:
        raise
    except Exception as e:                       # network, HTTP status, bad JSON
        log.warning("GRT departures failed for %s: %s", key, e)
        raise TransitError("couldn't reach GRT's departures") from e
    _cache[key] = (_time.monotonic(), deps)
    return deps


def match(trip, deps: list[dict], now: datetime, n: int = 3) -> list[dict]:
    """The next n departures that actually make this trip, with when they get there."""
    to_stops, routes = set(trip["to_stops"] or []), set(trip["routes"] or [])
    out = []
    for d in deps:
        if d["at"] < now or (routes and d["route"] not in routes):
            continue
        arrive = arrive_name = None
        if to_stops:
            ids = [s[0] for s in d["trip_stops"]]
            if d["stop_id"] not in ids:
                continue
            after = d["trip_stops"][ids.index(d["stop_id"]) + 1:]
            stop = next((s for s in after if s[0] in to_stops), None)
            if stop is None:
                continue                         # wrong direction, or doesn't go there
            arrive_name, arrive = stop[1], stop[2]
        out.append({**{k: v for k, v in d.items() if k != "trip_stops"},
                    "arrive": arrive, "arrive_name": arrive_name,
                    "in_min": max(0, int((d["at"] - now).total_seconds() // 60))})
        if len(out) == n:
            break
    return out


async def next_for(trip, now: datetime, n: int = 3) -> list[dict]:
    return match(trip, await departures(list(trip["from_stops"])), now, n)


async def overview(trips, now: datetime) -> list[dict]:
    """Every saved trip with its next departures (or its own error), fetched together."""
    async def one(t):
        try:
            return {"name": t["name"], "ok": True, "departures": await next_for(t, now)}
        except TransitError:
            return {"name": t["name"], "ok": False, "departures": []}
    return list(await asyncio.gather(*(one(t) for t in trips)))


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - {"to", "the", "from", "trip", "bus"}


def find_trip(trips, said: str | None):
    """A saved trip by name as said: exact, then the one sharing the most words. None if unclear."""
    if not said:
        return None
    by_name = {t["name"].lower(): t for t in trips}
    if said.strip().lower() in by_name:
        return by_name[said.strip().lower()]
    want = _words(said)
    scored = sorted(((len(want & _words(t["name"])) - 0.01 * len(_words(t["name"]) - want), t)
                     for t in trips), key=lambda x: -x[0])
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def describe(trip, deps: list[dict]) -> str:
    """One trip's departures as text for the model."""
    if not deps:
        return f"{trip['name']}: nothing in the next couple of hours."
    parts = []
    several = len({d["stop_name"] for d in deps}) > 1
    for d in deps:
        s = f"{d['route']} to {d['headsign']}" + (f" from {d['stop_name']}" if several else "")
        s += f" at {clock(d['at'])} (in {d['in_min']} min"
        s += f", live, {d['delay_min']} min late)" if d["live"] and d["delay_min"] > 0 else (", live)" if d["live"] else ")")
        if d["arrive"]:
            s += f", gets to {d['arrive_name']} {clock(d['arrive'])}"
        parts.append(s)
    where = "" if several else f" (from {deps[0]['stop_name']})"
    return f"{trip['name']}{where}: " + "; ".join(parts) + "."
