import asyncpg
from pgvector.asyncpg import register_vector
from app.config import settings

_pool: asyncpg.Pool | None = None


async def _init_conn(con):
    # Teach asyncpg the pgvector `vector` type so we can pass Python lists straight through.
    await register_vector(con)


async def _init_conn_json(con):
    await _init_conn(con)
    # jsonb <-> dict (members.prefs) instead of raw JSON strings.
    import json
    await con.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=5, init=_init_conn_json
    )


async def close_pool() -> None:
    if _pool:
        await _pool.close()


# --- members -------------------------------------------------------------------

async def get_member_by_telegram_id(tg_id: int):
    async with _pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM members WHERE telegram_id = $1", tg_id)


async def get_member(member_id: int):
    async with _pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM members WHERE id = $1", member_id)


async def roster_string() -> str:
    """Everyone in the suite, for the prompt: 'Sam (Sam Example, سام), ...'."""
    async with _pool.acquire() as con:
        rows = await con.fetch("SELECT name, full_name, arabic_name FROM members ORDER BY id")
    parts = []
    for r in rows:
        extra = ", ".join(x for x in (r["full_name"], r["arabic_name"]) if x)
        parts.append(f"{r['name']} ({extra})" if extra else r["name"])
    return ", ".join(parts) or "the suite"


async def get_member_id_by_name(name: str):
    """Resolve a spoken name to a member id, or None. Matches the everyday name, the
    Arabic name, the full name, or just a first/last name from the full name
    ("Samuel" or "Example" -> the member everyone calls Sam). Case-insensitive."""
    name = (name or "").strip()
    if not name:
        return None
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id FROM members "
            "WHERE name ILIKE $1 OR arabic_name = $1 OR full_name ILIKE $1 "
            "   OR full_name ILIKE ($1 || ' %') OR full_name ILIKE ('% ' || $1) "
            "ORDER BY (name ILIKE $1) DESC, id LIMIT 1",
            name,
        )
    return row["id"] if row else None


# --- conversation log ----------------------------------------------------------

async def log_message(*, thread: str, channel: str, role: str, member_id, content: str,
                      modality: str = "text", speaker_confidence: float | None = None) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO messages (thread, channel, role, member_id, content, modality, "
            "speaker_confidence) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            thread, channel, role, member_id, content, modality, speaker_confidence,
        )


async def recent_messages(thread: str, limit: int = 10):
    """The last `limit` turns of a thread, oldest first, with the speaker's name."""
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "SELECT m.role, m.content, mem.name AS speaker "
            "FROM messages m LEFT JOIN members mem ON mem.id = m.member_id "
            "WHERE m.thread = $1 ORDER BY m.created_at DESC, m.id DESC LIMIT $2",
            thread, limit,
        )
    return list(reversed(rows))  # back to chronological order


async def thread_page(thread: str, before_id: int | None = None, limit: int = 30):
    """One page of a thread for the web app, newest first (the caller reverses).
    `before_id` pages backwards through older messages."""
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT m.id, m.role, m.content, m.channel, m.modality, m.created_at, "
            "       mem.name AS speaker "
            "FROM messages m LEFT JOIN members mem ON mem.id = m.member_id "
            "WHERE m.thread = $1 AND ($2::bigint IS NULL OR m.id < $2) "
            "ORDER BY m.id DESC LIMIT $3",
            thread, before_id, limit,
        )


# --- web login + sessions ------------------------------------------------------

async def create_login_code(member_id: int, code_hash: str, expires_at) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO login_codes (member_id, code_hash, expires_at) VALUES ($1, $2, $3)",
            member_id, code_hash, expires_at,
        )


async def count_recent_login_codes(member_id: int, since) -> int:
    async with _pool.acquire() as con:
        return await con.fetchval(
            "SELECT count(*) FROM login_codes WHERE member_id = $1 AND created_at >= $2",
            member_id, since,
        )


async def latest_live_login_code(member_id: int):
    """The newest unused, unexpired code for a member (older ones are superseded)."""
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT id, code_hash, attempts FROM login_codes "
            "WHERE member_id = $1 AND NOT used AND expires_at > now() "
            "ORDER BY created_at DESC LIMIT 1",
            member_id,
        )


async def bump_login_code_attempts(code_id: int) -> None:
    async with _pool.acquire() as con:
        await con.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE id = $1", code_id)


async def use_login_code(code_id: int) -> bool:
    """Mark a code used. False if it was already used (a double submit)."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE login_codes SET used = true WHERE id = $1 AND NOT used RETURNING id", code_id)
    return row is not None


async def create_session(token_hash: str, member_id: int, user_agent: str | None, expires_at) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO web_sessions (token_hash, member_id, user_agent, expires_at) "
            "VALUES ($1, $2, $3, $4)",
            token_hash, member_id, user_agent, expires_at,
        )


async def session_member(token_hash: str):
    """The member behind a live session (+ session timestamps), or None."""
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT m.*, s.last_seen_at AS session_last_seen "
            "FROM web_sessions s JOIN members m ON m.id = s.member_id "
            "WHERE s.token_hash = $1 AND s.expires_at > now()",
            token_hash,
        )


async def touch_session(token_hash: str, expires_at) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "UPDATE web_sessions SET last_seen_at = now(), expires_at = $2 WHERE token_hash = $1",
            token_hash, expires_at,
        )


async def delete_session(token_hash: str) -> None:
    async with _pool.acquire() as con:
        await con.execute("DELETE FROM web_sessions WHERE token_hash = $1", token_hash)


# --- members (all) + preferences ------------------------------------------------

async def all_members():
    async with _pool.acquire() as con:
        return await con.fetch("SELECT * FROM members ORDER BY id")


async def set_member_prefs(member_id: int, updates: dict) -> dict:
    """Merge `updates` into members.prefs and return the result. (The jsonb codec set in
    _init_conn_json encodes/decodes dicts, so no manual json.dumps here.)"""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE members SET prefs = prefs || $2::jsonb WHERE id = $1 RETURNING prefs",
            member_id, updates,
        )
    return dict(row["prefs"])


# --- reminders -------------------------------------------------------------------

async def add_reminder(member_id: int, text: str, due_at, created_by) -> int:
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "INSERT INTO reminders (member_id, text, due_at, created_by) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            member_id, text, due_at, created_by,
        )
    return row["id"]


async def upcoming_reminders(member_id: int | None = None):
    """Pending reminders (optionally for one member), soonest first."""
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT r.id, r.text, r.due_at, r.member_id, r.created_by, "
            "       m.name AS member_name, c.name AS created_by_name "
            "FROM reminders r JOIN members m ON m.id = r.member_id "
            "LEFT JOIN members c ON c.id = r.created_by "
            "WHERE NOT r.done AND ($1::int IS NULL OR r.member_id = $1) "
            "ORDER BY r.due_at",
            member_id,
        )


async def cancel_reminder(reminder_id: int, member_id: int | None = None):
    """Cancel a pending reminder. With member_id, only one that is for, or was set by,
    that member. Returns its text, or None if there's no such pending reminder."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE reminders SET done = true WHERE id = $1 AND NOT done "
            "AND ($2::int IS NULL OR member_id = $2 OR created_by = $2) RETURNING text",
            reminder_id, member_id,
        )
    return row["text"] if row else None


async def due_reminder_ids(now) -> list[int]:
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "SELECT id FROM reminders WHERE NOT done AND due_at <= $1 ORDER BY due_at LIMIT 50", now)
    return [r["id"] for r in rows]


async def claim_reminder(reminder_id: int):
    """Atomically take a due reminder for delivery. Only one caller ever gets the row,
    so two scheduler instances can't both send it."""
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "UPDATE reminders r SET done = true, sent_at = now() "
            "FROM members m WHERE r.id = $1 AND NOT r.done AND m.id = r.member_id "
            "RETURNING r.id, r.text, r.due_at, r.member_id, r.created_by, "
            "          (SELECT name FROM members WHERE id = r.created_by) AS created_by_name",
            reminder_id,
        )


async def claim_daily(member_id: int, kind: str, local_date) -> bool:
    """Claim a once-per-day send (briefing, a prayer). True only for the first caller."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "INSERT INTO sent_log (member_id, kind, local_date) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING RETURNING member_id",
            member_id, kind, local_date,
        )
    return row is not None


# --- money -----------------------------------------------------------------------

async def add_expense(description: str, amount_cents: int, paid_by: int, created_by,
                      spent_on, shares: dict[int, int]) -> int:
    """Insert an expense and its shares atomically. Returns the expense id."""
    async with _pool.acquire() as con:
        async with con.transaction():
            eid = await con.fetchval(
                "INSERT INTO expenses (description, amount_cents, paid_by, created_by, spent_on) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                description, amount_cents, paid_by, created_by, spent_on,
            )
            await con.executemany(
                "INSERT INTO expense_shares (expense_id, member_id, share_cents) VALUES ($1, $2, $3)",
                [(eid, m, c) for m, c in shares.items()],
            )
    return eid


async def add_settlement(from_member: int, to_member: int, amount_cents: int, created_by,
                         settled_on) -> int:
    async with _pool.acquire() as con:
        return await con.fetchval(
            "INSERT INTO settlements (from_member, to_member, amount_cents, created_by, settled_on) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            from_member, to_member, amount_cents, created_by, settled_on,
        )


async def money_ledger():
    """Everything balances need (non-deleted): (expenses, shares, settlements)."""
    async with _pool.acquire() as con:
        expenses = await con.fetch("SELECT id, paid_by, amount_cents FROM expenses WHERE NOT deleted")
        shares = await con.fetch(
            "SELECT s.member_id, s.share_cents FROM expense_shares s "
            "JOIN expenses e ON e.id = s.expense_id WHERE NOT e.deleted")
        settlements = await con.fetch(
            "SELECT from_member, to_member, amount_cents FROM settlements WHERE NOT deleted")
    return expenses, shares, settlements


async def recent_expenses(limit: int = 20):
    """Newest first, with payer name and the split as [{member_id, name, share_cents}]."""
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "SELECT e.id, e.description, e.amount_cents, e.paid_by, e.created_by, e.spent_on, "
            "       m.name AS paid_by_name "
            "FROM expenses e JOIN members m ON m.id = e.paid_by "
            "WHERE NOT e.deleted ORDER BY e.spent_on DESC, e.id DESC LIMIT $1",
            limit,
        )
        ids = [r["id"] for r in rows]
        shares = await con.fetch(
            "SELECT s.expense_id, s.member_id, s.share_cents, m.name FROM expense_shares s "
            "JOIN members m ON m.id = s.member_id WHERE s.expense_id = ANY($1::bigint[]) "
            "ORDER BY m.id", ids,
        ) if ids else []
    by_expense: dict[int, list] = {}
    for s in shares:
        by_expense.setdefault(s["expense_id"], []).append(
            {"member_id": s["member_id"], "name": s["name"], "share_cents": s["share_cents"]})
    return [{**dict(r), "shares": by_expense.get(r["id"], [])} for r in rows]


async def recent_settlements(limit: int = 20):
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT t.id, t.from_member, t.to_member, t.amount_cents, t.created_by, t.settled_on, "
            "       f.name AS from_name, o.name AS to_name "
            "FROM settlements t JOIN members f ON f.id = t.from_member "
            "JOIN members o ON o.id = t.to_member "
            "WHERE NOT t.deleted ORDER BY t.settled_on DESC, t.id DESC LIMIT $1",
            limit,
        )


async def delete_expense(expense_id: int, member_id: int):
    """Soft-delete, only by whoever paid or recorded it. Returns the description, or None."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE expenses SET deleted = true WHERE id = $1 AND NOT deleted "
            "AND (paid_by = $2 OR created_by = $2) RETURNING description",
            expense_id, member_id,
        )
    return row["description"] if row else None


async def delete_settlement(settlement_id: int, member_id: int) -> bool:
    """Soft-delete a payment, only by someone involved in it or who recorded it."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE settlements SET deleted = true WHERE id = $1 AND NOT deleted "
            "AND (from_member = $2 OR to_member = $2 OR created_by = $2) RETURNING id",
            settlement_id, member_id,
        )
    return row is not None


# --- status ----------------------------------------------------------------------

async def set_status(member_id: int, text: str, until_at, place: dict | None = None) -> None:
    """place: {"label", "lat", "lng", "tz"} from the geocoder (travel mode), or None."""
    p = place or {}
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO member_status (member_id, text, until_at, updated_at, place, place_lat, "
            "                           place_lng, place_tz) "
            "VALUES ($1, $2, $3, now(), $4, $5, $6, $7) "
            "ON CONFLICT (member_id) DO UPDATE SET text = $2, until_at = $3, updated_at = now(), "
            "    place = $4, place_lat = $5, place_lng = $6, place_tz = $7",
            member_id, text, until_at, p.get("label"), p.get("lat"), p.get("lng"), p.get("tz"),
        )


async def clear_status(member_id: int) -> bool:
    async with _pool.acquire() as con:
        result = await con.execute("DELETE FROM member_status WHERE member_id = $1", member_id)
    return not result.endswith(" 0")


async def current_statuses():
    """Unexpired statuses, with names."""
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT s.member_id, m.name, s.text, s.until_at, s.updated_at, "
            "       s.place, s.place_lat, s.place_lng, s.place_tz "
            "FROM member_status s JOIN members m ON m.id = s.member_id "
            "WHERE s.until_at IS NULL OR s.until_at > now() ORDER BY m.id"
        )


# --- web push subscriptions ----------------------------------------------------

async def save_push_subscription(member_id: int, endpoint: str, p256dh: str, auth: str) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO push_subscriptions (member_id, endpoint, p256dh, auth) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (endpoint) DO UPDATE SET member_id = $1, p256dh = $3, auth = $4",
            member_id, endpoint, p256dh, auth,
        )


async def delete_push_subscription(endpoint: str) -> None:
    async with _pool.acquire() as con:
        await con.execute("DELETE FROM push_subscriptions WHERE endpoint = $1", endpoint)


async def push_subscriptions_for(member_id: int):
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE member_id = $1",
            member_id,
        )


# --- household knowledge base --------------------------------------------------

async def add_fact(category, content, embedding, created_by) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO household_facts (category, content, embedding, created_by) "
            "VALUES ($1, $2, $3, $4)",
            category, content, embedding, created_by,   # embedding is a plain list
        )


async def nearest_fact(embedding):
    """The single closest stored fact to this embedding, with its cosine distance,
    for the dedup check before inserting. Returns a row (id, content, distance) or None."""
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT id, content, embedding <=> $1 AS distance "
            "FROM household_facts WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> $1 LIMIT 1",
            embedding,
        )


async def update_fact(fact_id, category, content, embedding) -> None:
    """Overwrite an existing fact in place (used when a re-stated fact is a near
    duplicate of one we already have — update, never insert a second row)."""
    async with _pool.acquire() as con:
        await con.execute(
            "UPDATE household_facts SET category = $2, content = $3, embedding = $4 "
            "WHERE id = $1",
            fact_id, category, content, embedding,
        )


async def search_facts(embedding, k: int = 4):
    # Suite-wide: NO member_id/category scope — anyone can recall any household
    # fact. Only skip rows with no embedding so a legacy NULL row can't crowd out a
    # real match (those get backfilled via scripts/backfill_embeddings.py). No distance
    # cutoff — return top-k and let the model judge relevance. id is selected so the
    # recall path can log which fact matched.
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT id, category, content, embedding <=> $1 AS distance "
            "FROM household_facts WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> $1 LIMIT $2",
            embedding, k,
        )


# --- live location -------------------------------------------------------------

async def upsert_location(member_id, lat, lng) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO member_locations (member_id, lat, lng, updated_at) "
            "VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (member_id) DO UPDATE SET lat = $2, lng = $3, updated_at = now()",
            member_id, lat, lng,
        )


async def get_location(name):
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT m.name, l.lat, l.lng, l.updated_at "
            "FROM member_locations l JOIN members m ON m.id = l.member_id "
            "WHERE m.name ILIKE $1", name,
        )


async def get_all_locations():
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT m.name, l.lat, l.lng, l.updated_at "
            "FROM member_locations l JOIN members m ON m.id = l.member_id"
        )


# --- shared shopping list ------------------------------------------------------

async def add_shopping_item(item, qty, member_id) -> bool:
    """Add to the shared list. Returns False if an open entry already exists
    (guarded by the partial unique index on lower(item) WHERE NOT bought)."""
    async with _pool.acquire() as con:
        try:
            await con.execute(
                "INSERT INTO shopping_items (item, qty, added_by) VALUES ($1, $2, $3)",
                item, qty, member_id,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False


async def get_shopping_list():
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT id, item, qty FROM shopping_items WHERE NOT bought ORDER BY created_at"
        )


async def mark_item_bought(item, member_id):
    """Mark the matching open item bought. Returns the stored item name, or None."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE shopping_items SET bought = true, bought_by = $2, bought_at = now() "
            "WHERE lower(item) = lower($1) AND NOT bought RETURNING item",
            item, member_id,
        )
    return row["item"] if row else None


async def clear_shopping_list(member_id) -> int:
    """Mark every open item bought. Returns how many were cleared."""
    async with _pool.acquire() as con:
        result = await con.execute(
            "UPDATE shopping_items SET bought = true, bought_by = $1, bought_at = now() "
            "WHERE NOT bought",
            member_id,
        )
    return int(result.split()[-1])   # "UPDATE 3" -> 3


# --- packages / deliveries -----------------------------------------------------

async def add_package(description, carrier, owner_id, expected_on) -> int:
    """Record an incoming package. `expected_on` is a resolved date or None.
    Returns the new package id."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "INSERT INTO packages (description, carrier, owner_id, expected_on) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            description, carrier, owner_id, expected_on,
        )
    return row["id"]


async def get_open_packages(owner_id=None):
    """Packages not yet picked up (expected or arrived), optionally for one member."""
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT p.id, p.description, p.carrier, p.expected_on, p.status, "
            "       m.name AS owner_name "
            "FROM packages p LEFT JOIN members m ON m.id = p.owner_id "
            "WHERE p.status IN ('expected', 'arrived') "
            "  AND ($1::int IS NULL OR p.owner_id = $1) "
            "ORDER BY p.expected_on NULLS LAST, p.created_at",
            owner_id,
        )


async def update_package_status(package_id, status):
    """Move an open package to a new status. Returns its description, or None if
    there's no open package with that id (never pretend a change happened)."""
    async with _pool.acquire() as con:
        row = await con.fetchrow(
            "UPDATE packages SET status = $2, updated_at = now() "
            "WHERE id = $1 AND status IN ('expected', 'arrived') RETURNING description",
            package_id, status,
        )
    return row["description"] if row else None
