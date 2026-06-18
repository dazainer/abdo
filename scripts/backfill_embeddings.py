"""Backfill / repair household_facts embeddings.

The original wifi-recall miss was a fact stored with a NULL/zero/wrong-dimension
embedding — invisible to vector search (which is why re-adding it "fixed" recall).
This scans household_facts, re-embeds any row whose embedding is missing or not a
valid 1024-dim vector, and writes it back with input_type="search_document".

Reads DATABASE_URL + COHERE_API_KEY from the environment (.env). Makes Cohere
calls (small cost) and writes to the DB — run it deliberately, not in CI.

    python scripts/backfill_embeddings.py            # repair bad rows
    python scripts/backfill_embeddings.py --all      # re-embed every row
    python scripts/backfill_embeddings.py --dry-run  # report only, no writes
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app import embeddings
from app.config import settings


async def main(do_all: bool, dry_run: bool) -> None:
    con = await asyncpg.connect(settings.database_url)
    try:
        rows = await con.fetch("SELECT id, content, embedding FROM household_facts ORDER BY id")
        bad, fixed = 0, 0
        for r in rows:
            vec = r["embedding"]
            # asyncpg returns a vector as a list/np array when valid, None when NULL.
            ok = vec is not None and embeddings.is_valid(list(vec))
            if ok and not do_all:
                continue
            if not ok:
                bad += 1
            print(f"{'(dry-run) ' if dry_run else ''}re-embedding #{r['id']}: {r['content'][:60]!r}")
            if dry_run:
                continue
            new_vec = await embeddings.embed(r["content"], input_type="search_document")
            if not embeddings.is_valid(new_vec):
                print(f"  !! still invalid after embed — skipping #{r['id']}")
                continue
            await con.execute(
                "UPDATE household_facts SET embedding = $2 WHERE id = $1", r["id"], new_vec
            )
            fixed += 1
        print(f"\n{len(rows)} rows scanned, {bad} invalid, {fixed} re-embedded"
              f"{' (dry-run, nothing written)' if dry_run else ''}.")
    finally:
        await con.close()


if __name__ == "__main__":
    asyncio.run(main("--all" in sys.argv, "--dry-run" in sys.argv))
