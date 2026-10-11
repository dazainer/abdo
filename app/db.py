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


# --- members -------------------------------------------------------------------

async def get_member_by_telegram_id(tg_id: int):
    async with _pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM members WHERE telegram_id = $1", tg_id)


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
