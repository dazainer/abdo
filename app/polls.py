"""Suite polls ("where are we eating?"): single choice, one vote per person (changeable),
closed by whoever asked. Tallies are computed here for Abdo, the app, and the briefing."""
import json

from app import db, notify

MIN_OPTIONS, MAX_OPTIONS = 2, 6


class PollError(ValueError):
    """The message is safe to show."""


def _options(poll) -> list[str]:
    raw = poll["options"]
    return json.loads(raw) if isinstance(raw, str) else list(raw)


def clean_options(options: list[str]) -> list[str]:
    seen, out = set(), []
    for o in options or []:
        o = (o or "").strip()
        if o and o.lower() not in seen:
            seen.add(o.lower())
            out.append(o[:60])
    if not MIN_OPTIONS <= len(out) <= MAX_OPTIONS:
        raise PollError(f"a poll needs {MIN_OPTIONS} to {MAX_OPTIONS} different options")
    return out


def tally(poll, votes, members) -> dict:
    """One poll with counts, who voted for what, and who hasn't voted yet."""
    opts = _options(poll)
    mine = [v for v in votes if v["poll_id"] == poll["id"]]
    voted = {v["member_id"]: v["option_index"] for v in mine}
    return {
        "id": poll["id"], "question": poll["question"], "closed": poll["closed"],
        "created_by": poll["created_by"],
        "options": [{"text": o, "count": sum(1 for v in mine if v["option_index"] == i),
                     "voters": [v["name"] for v in mine if v["option_index"] == i]} for i, o in enumerate(opts)],
        "voted": voted,
        "waiting_on": [m["name"] for m in members if m["id"] not in voted],
    }


async def overview() -> list[dict]:
    """Open polls and ones closed in the last few days, tallied."""
    polls = await db.polls_recent()
    if not polls:
        return []
    votes = await db.poll_votes([p["id"] for p in polls])
    members = await db.all_members()
    return [{**tally(p, votes, members), "created_by_name": p["created_by_name"]} for p in polls]


def match_option(t: dict, raw) -> int | None:
    """A vote as said -> option index: '2' (1-based), an exact name, or a unique partial match."""
    text = str(raw or "").strip().lower()
    opts = [o["text"].lower() for o in t["options"]]
    if text.isdigit() and 1 <= int(text) <= len(opts):
        return int(text) - 1
    if text in opts:
        return opts.index(text)
    partial = [i for i, o in enumerate(opts) if text and (text in o or o in text)]
    return partial[0] if len(partial) == 1 else None


def describe(t: dict) -> str:
    counts = ", ".join(f"{o['text']} {o['count']}" + (f" ({', '.join(o['voters'])})" if o["voters"] else "")
                       for o in t["options"])
    state = "closed" if t["closed"] else ("waiting on " + ", ".join(t["waiting_on"]) if t["waiting_on"] else "everyone voted")
    return f"Poll #{t['id']} '{t['question']}': {counts}; {state}."


def leaders(t: dict) -> list[str]:
    top = max((o["count"] for o in t["options"]), default=0)
    return [o["text"] for o in t["options"] if top and o["count"] == top]


async def create(question: str, options: list[str], creator) -> int:
    question = (question or "").strip()
    if not question:
        raise PollError("what's the question?")
    opts = clean_options(options)
    pid = await db.create_poll(question[:140], opts, creator["id"])
    for m in await db.all_members():
        if m["id"] != creator["id"]:
            await notify.deliver(m, f"📊 {creator['name']} asks: {question}",
                                 " / ".join(opts) + ". Vote in the app, or tell Abdo.", url="/lists")
    return pid
