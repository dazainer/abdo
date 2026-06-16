import asyncpg
from pgvector.asyncpg import register_vector
from app.config import settings

_pool: asyncpg.Pool | None = None


async def _init_conn(con):
    # Teach asyncpg the pgvector `vector` type so we can pass Python lists straight through.
    await register_vector(con)


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=5, init=_init_conn
    )


async def close_pool() -> None:
    if _pool:
        await _pool.close()


async def get_member_by_telegram_id(tg_id: int):
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT * FROM family_members WHERE telegram_id = $1", tg_id
        )


async def roster_string() -> str:
    async with _pool.acquire() as con:
        rows = await con.fetch("SELECT name, role FROM family_members ORDER BY id")
    return ", ".join(f"{r['name']} ({r['role']})" for r in rows) or "the family"


async def log_message(member_id, chat_id, role, content) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO messages (member_id, chat_id, role, content) "
            "VALUES ($1, $2, $3, $4)",
            member_id, chat_id, role, content,
        )


async def recent_messages(chat_id, limit: int = 10):
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "SELECT role, content FROM messages WHERE chat_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            chat_id, limit,
        )
    return list(reversed(rows))  # back to chronological order


# --- dog tool backing queries ---

async def dogs_fed_today():
    async with _pool.acquire() as con:
        return await con.fetchrow(
            "SELECT f.fed_on, f.created_at, m.name AS fed_by_name "
            "FROM dog_feedings f "
            "LEFT JOIN family_members m ON m.id = f.fed_by "
            "WHERE f.fed_on = (now() AT TIME ZONE 'Africa/Cairo')::date"
        )


async def mark_dogs_fed(member_id: int) -> bool:
    """Idempotent for the day. Returns True if this was a fresh feeding."""
    async with _pool.acquire() as con:
        result = await con.execute(
            "INSERT INTO dog_feedings (fed_by) VALUES ($1) "
            "ON CONFLICT (fed_on) DO NOTHING",
            member_id,
        )
    return result.endswith("1")   # "INSERT 0 1" => newly inserted


# --- household knowledge base (Phase 2) ---

async def add_fact(category, content, embedding, created_by) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "INSERT INTO household_facts (category, content, embedding, created_by) "
            "VALUES ($1, $2, $3, $4)",
            category, content, embedding, created_by,   # embedding is a plain list
        )


async def search_facts(embedding, k: int = 4):
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT category, content, embedding <=> $1 AS distance "
            "FROM household_facts ORDER BY embedding <=> $1 LIMIT $2",
            embedding, k,
        )


# --- live location (Phase 3) ---

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
            "FROM member_locations l JOIN family_members m ON m.id = l.member_id "
            "WHERE m.name ILIKE $1", name,
        )


async def get_all_locations():
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT m.name, l.lat, l.lng, l.updated_at "
            "FROM member_locations l JOIN family_members m ON m.id = l.member_id"
        )


# --- shared shopping list ---

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
            "SELECT item, qty FROM shopping_items WHERE NOT bought ORDER BY created_at"
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
