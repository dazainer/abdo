import asyncpg
from app.config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)


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
