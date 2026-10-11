# Abdo — Phase 2 Build Spec: Household Knowledge Base (RAG)

Turns Abdo from a friendly chat into something that actually knows the house. Anyone tells Abdo a fact — the wifi password, the AC technician's number, where the spare key lives, when the syndicate fees are due — and Abdo stores it; anyone asks about the house later and Abdo retrieves it. This is the stickiest feature in the whole project, and it's your Meeting Copilot pipeline reused at a smaller, simpler scale (one fact = one row, no heavy chunking).

Prerequisite: **Phase 1 deployed and lived-with.** Don't build this until the bot is running and the family (or at least you) actually talk to it.

## 1. Definition of done

- Abdo decides, on its own, when to **store** a household fact vs **retrieve** one (Claude tool use — same pattern as the dog tools).
- Facts persist in `household_facts` (already in your schema) with a 1024-dim embedding.
- Retrieval is semantic and **cross-lingual**: a fact stored in Arabic is findable by an English question and vice-versa.
- "Where's the spare key?" / "what's the wifi password?" / "el-fatoura due imta?" return the right stored fact, or an honest "I don't have that" when nothing matches.

## 2. Embedding model — DECIDED: Cohere Embed v4

Use **Cohere Embed v4** (`embed-v4.0`, `output_dimension=1024`). Strong, consistent multilingual quality including Arabic; input-type routing for documents vs queries; trivial cost at household volume; **no model loaded in the container**, so the Railway deploy stays light.

The hard constraints that drove this: the schema pins `household_facts.embedding` to `vector(1024)`, so the embedder must output exactly 1024 dimensions, and it must handle Arabic well — English-first models (OpenAI 3-large, Voyage) lose 10–20 points on Arabic and would quietly wreck recall. (A local BGE-M3 path was considered and set aside: free per call, but ~2 GB of container RAM would push off Railway Hobby.)

## 3. Schema

Already created in Phase 1's `schema.sql` — no migration:

```sql
CREATE TABLE household_facts (
    id          BIGSERIAL PRIMARY KEY,
    category    TEXT,
    content     TEXT NOT NULL,
    embedding   vector(1024),
    created_by  INTEGER REFERENCES family_members(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

At household scale (tens to low hundreds of facts) a sequential scan over `<=>` is instant, so **no index is required**. If the table ever grows large, add one:

```sql
CREATE INDEX ON household_facts USING hnsw (embedding vector_cosine_ops);
```

## 4. Config additions (`app/config.py`)

```python
    cohere_api_key: str | None = None     # required if using the hosted embedder
```

## 5. Dependencies

Add to `requirements.txt`:
```
pgvector
cohere
```

## 6. Wire pgvector into the existing pool (modify `app/db.py`)

`asyncpg` doesn't know the `vector` type by default. Register it once per connection via the pool's `init` hook so you can pass Python lists straight through. **Modify the existing `init_pool`:**

```python
from pgvector.asyncpg import register_vector

async def _init_conn(con):
    await register_vector(con)

async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=5, init=_init_conn
    )
```

Then add the two queries:

```python
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
```

`<=>` is pgvector's cosine-distance operator (smaller = closer). Cohere v4 embeddings are normalized, so cosine is correct.

## 7. Embeddings module (`app/embeddings.py`, new)

```python
import cohere
from app.config import settings

# Cohere Embed v4 at 1024 dims to match the schema's vector(1024).
_client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
EMBED_MODEL = "embed-v4.0"
EMBED_DIM = 1024


async def embed(text: str, *, input_type: str) -> list[float]:
    """input_type = 'search_document' when storing, 'search_query' when retrieving."""
    resp = await _client.embed(
        model=EMBED_MODEL,
        texts=[text],
        input_type=input_type,
        output_dimension=EMBED_DIM,
        embedding_types=["float"],
    )
    return resp.embeddings.float[0]   # verify attribute name (.float / .float_) against your SDK version
```

The `input_type` distinction matters: documents and queries are embedded asymmetrically, and mismatching them measurably hurts retrieval.

## 8. Tools (add to `app/tools.py`)

```python
{
    "name": "remember_fact",
    "description": (
        "Store a piece of household knowledge for later recall — a contact number, "
        "the wifi password, where something is kept, a recurring bill, an appliance or "
        "service detail. Use whenever someone tells you a fact about the house to keep."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact as one clear, self-contained sentence."},
            "category": {"type": "string",
                         "enum": ["contact", "wifi", "appliance", "location_of_things",
                                  "schedule", "bill", "misc"]},
        },
        "required": ["content", "category"],
    },
},
{
    "name": "recall_facts",
    "description": (
        "Search stored household knowledge to answer a question about the house "
        "(where something is, a number, a password, a bill). Use this before saying you don't know."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look up, in natural language."}},
        "required": ["query"],
    },
},
```

Dispatch (in `run_tool`, which is already `async` and receives `member_id`):

```python
from app import embeddings

if name == "remember_fact":
    vec = await embeddings.embed(tool_input["content"], input_type="search_document")
    await db.add_fact(tool_input["category"], tool_input["content"], vec, member_id)
    return "Stored."

if name == "recall_facts":
    vec = await embeddings.embed(tool_input["query"], input_type="search_query")
    rows = await db.search_facts(vec, k=4)
    if not rows:
        return "No matching household facts found."
    return "\n".join(f"- [{r['category']}] {r['content']}" for r in rows)
```

The brain's tool loop already handles arbitrary tools, so `brain.py` needs **no changes** — adding these to `TOOLS` + `run_tool` is the whole integration. That's the payoff of the architecture principle.

## 9. Persona update (`app/prompts.py`)

Add under "What you can do" so Abdo knows the capability exists:

```
- You can remember household facts people tell you (numbers, passwords, where things are kept, bills, appliances) and recall them later. When someone shares a fact worth keeping, store it. When someone asks something about the house, search your memory first — only say you don't know after searching.
- Treat stored facts as family-internal. Don't volunteer sensitive ones (like passwords) unless the person is clearly asking for them.
```

## 10. Gotchas

- **1024 is an invariant.** Switch models only to another 1024-dim output. A dimension mismatch throws on insert; silently truncating a non-Matryoshka model corrupts retrieval.
- **Document vs query embedding.** Use `search_document` on store and `search_query` on recall. Getting these backwards is a silent quality killer.
- **Cross-lingual is the point.** This is why we don't use an English-only embedder — someone stores "مفتاح احتياطي في الدرج" and later asks "where's the spare key" and it still matches.
- **Duplicates.** Storing the same fact twice makes two rows. Fine for v1; if it gets noisy, search for a near-identical fact before inserting and update instead.
- **Don't log fact contents** at debug level — passwords may pass through.

## 11. Build checklist

- [ ] Choose embedder (Cohere hosted vs BGE-M3 local) and set deps accordingly
- [ ] Add `register_vector` to `init_pool`
- [ ] `add_fact` / `search_facts` in `db.py`
- [ ] `embeddings.py` with the `embed()` you chose
- [ ] `remember_fact` / `recall_facts` tools + dispatch
- [ ] Persona update in `prompts.py`
- [ ] Env: `COHERE_API_KEY` (hosted path)
- [ ] Test: tell Abdo a fact → restart the service → ask for it → confirm recall
- [ ] Test cross-lingual: store in Arabic, ask in English

## 12. CLAUDE.md update

- Flip **current phase** to Phase 2.
- Under Stack, add: "Embeddings: Cohere `embed-v4.0` at **1024 dims** (must match `household_facts.embedding vector(1024)`)."
- Add `COHERE_API_KEY` to the env list.
- Add a gotcha: "Embedding dimension is fixed at 1024; document vs query input types must not be swapped."
