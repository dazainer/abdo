# Abdo — Reliability Fixes, Voice Polish & Orders Feature

Five things, in priority order. The first two share a root cause (the model not grounding in its tools) and must NOT be "fixed" by escalating to Sonnet — the fixes are prompt discipline + deterministic data + guards, all on Haiku 4.5. First, confirm the date-handling fix from the previous session actually deployed (check the `GET /` version marker); some calendar symptoms below imply it didn't land or only covered writes.

---

## 1. Tool-grounding reliability (fixes both the wifi-recall miss AND the calendar confabulation)

**Symptoms:** (a) a fact that exists in `household_facts` ("wifi password") was answered with "I don't have it" repeatedly; (b) on a calendar move, Abdo claimed an unrelated event ("cairo", actually Saturday) was on Friday and tried to reconcile it with the event being moved.

**Root cause:** the model answered from its own head instead of calling/trusting the tool result.

### 1a. Diagnose first
- Check the logs: was `recall_facts` actually invoked on the failed turns, or did the model reply without a tool call?
- Run a direct retrieval test: does `search_facts(embed("wifi password", input_type="search_query"))` return the wifi row? This separates a **tool-calling** problem from a **retrieval** problem. Fix both defensively below.

### 1b. Prompt grounding rules (add to the system prompt)
```
# Grounding (important)
- For ANY question about household info — passwords, Wi-Fi, numbers, where things are kept, bills, schedules — you MUST call recall_facts before answering. Never say you don't have something without searching first.
- When using the calendar, operate ONLY on the specific event the user named. Never infer a relationship between unrelated events. State each event's date and weekday exactly as get_calendar returns it — never guess or compute a weekday yourself.
- Trust tool results over your own assumptions. If a tool returns nothing, say so plainly; don't invent an answer.
```

### 1c. Retrieval integrity
- **Verify input types:** store with `input_type="search_document"`, recall with `input_type="search_query"`. A mismatch silently tanks matching.
- **Backfill embeddings:** the original wifi fact may have been stored with a NULL/zero/wrong-dim embedding (which is why re-adding it fixed recall). Scan `household_facts` for rows whose `embedding` is NULL or not 1024-dim and re-embed them. Add a guard so a fact is never stored without a valid embedding.
- **No over-aggressive cutoff:** return top-k (k=4) and let the model judge; if there's a hard distance threshold filtering everything out, loosen/remove it.

### 1d. Store dedup (fixes the double-save)
Re-stating the wifi password created two rows (the real fact + a garbled "the wifi password" fragment). Before inserting in `remember_fact`, search for a near-duplicate (high cosine similarity, e.g. distance < 0.15, or normalized-text match); if found, **update** that row instead of inserting a second. Also instruct the model to store a fact once, as one self-contained sentence.

---

## 2. Calendar: single confirmation + correct weekday reads

- **Confirm once.** The write-confirm is good (especially for voice), but it's currently re-asking. Confirm exactly once, stating the fully-resolved details — title + resolved date + weekday + time — then act on "yes." Don't ask again.
- **Read weekdays from the dated reference**, not by computing. Reuse the deterministic date resolver + dated weekday table from the previous session's fix on the *reading* side too, so reporting an event's weekday can't drift (the "cairo on Friday" bug). If that resolver/table isn't in yet, it's the prerequisite — build it first.

---

## 3. Voice output quality

### 3a. Speed + stability (in `tts.py`)
Add `voice_settings` to the TTS call:
```python
"voice_settings": {"stability": 0.55, "similarity_boost": 0.75, "speed": 0.9}
```
`speed` ranges 0.7–1.2 (default 1.0); 0.9 slows Abdo to a clearer pace. Don't go below ~0.85 (quality degrades). Slightly higher stability also steadies the pace.

### 3b. Numbers & credentials (Flash v2.5 can't normalize numbers)
Flash v2.5 deliberately skips number normalization, so digit strings ("2013", phone numbers, amounts) come out wrong, and forcing normalization on is Enterprise-only. Three-part fix:

1. **Prompt:** in voice mode, write ordinary numbers as words ("سنة ألفين وتلتاشر", "اتنين وعشرين جنيه"), not digits.
2. **Credentials as text, never spoken.** Add to the voice-mode prompt:
   ```
   When you must say a password, Wi-Fi key, PIN, or any code out loud, spell it
out as individual words in its corresponding language — each letter named and each digit
as its own word (/zero, one, two, three/صفر، واحد، اتنين، تلاتة…), slowly. NEVER put the raw form
(e.g. "2013" or "K-O-K-I-2-0-1-3") in the spoken text — only the worded-out
version. Note capitals where they matter (e.g. "Capital K" for a capital K).
   ```
   (`reply_text_alongside_voice` already sends the text, so they get the exact string.)
3. **Backstop model routing in `tts.py`:** if the to-be-spoken text still contains any digit, synthesize that one reply with `eleven_multilingual_v2` (which pronounces numbers correctly) instead of Flash; otherwise stay on Flash. Trades a little latency only on number-bearing replies.
   ```python
   model = "eleven_multilingual_v2" if any(c.isdigit() for c in text) else settings.tts_model
   ```

### 3c. Egyptian dialect (add this glossary to the persona in `prompts.py`)
```
# Dialect — speak Egyptian (مصري) only
Use Egyptian colloquial forms. NEVER use Levantine (بدك، شو، هلأ، منيح), Gulf, or formal MSA.
- "I don't have" → معنديش (NOT "ما عندي" / "لا أملك")
- "I want / if you want" → عايز / عايزة / لو عايز (NOT "بدي/بدك" — that's Levantine — and NOT "أريد")
- "now" → دلوقتي (NOT "الآن")
- "how" → إزاي (NOT "كيف")
- "what" → إيه (NOT "ماذا")
- "why" → ليه (NOT "لماذا")
- "this/that" → ده / دي (NOT "هذا/هذه")
- "like this" → كده (NOT "هكذا")
- "but" → بس (NOT "لكن")
- "also" → كمان (NOT "أيضاً")
- "there is" → في (NOT "يوجد")
- "a lot / very" → كتير / أوي
- future tense → هـ (هيجي، هعمل) (NOT "سوف")
- "good / okay" → تمام / كويس

# Object pronouns — fuse them, Egyptian-style
Attach object + indirect-object pronouns as fused suffixes. NEVER use the
standalone إياه / إياها / إياهم (that's formal/MSA).
- "tell it to me" → يقولهولي / قوله لي   (NOT "يقول لي إياه")
- "save it for you" → احفظهولك            (NOT "احفظه ليك" / "احفظه لك")
- "give it to me" → اديهولي               (NOT "اعطني إياه")
- "send it to me" → ابعتهولي
Keep it warm and natural, the way a Cairene actually talks.
```

### 3d. "c" → ق
A TTS rendering quirk for Latin letters. Once credentials go out as text (3b), it stops mattering in practice — don't chase it. (If a specific word ever needs forcing, ElevenLabs pronunciation dictionaries are the tool, but it's not worth it here.)

---

## 4. Migrate STT off the deprecated model (TIME-SENSITIVE)

`scribe_v1` is **deprecated and removed July 9, 2026.** Change the `STT_MODEL` default to **`scribe_v2`** now (env-swappable, but make v2 the default) so voice-in doesn't break. Smoke-test a voice note after switching.

---

## 5. New feature: order / delivery tracking

Solves the "someone's home, doorbell rings, no idea if it's a prepaid order or COD, no tip ready" problem. Same tool pattern as everything else; **reuse the deterministic day-reference resolver from the calendar fix** — do not duplicate date logic.

### 5a. Schema (`schema.sql` — new migration)
```sql
CREATE TABLE orders (
    id           BIGSERIAL PRIMARY KEY,
    description  TEXT NOT NULL,                    -- "Amazon package", "Breadfast groceries"
    expected_on  DATE NOT NULL,
    ordered_by   INTEGER REFERENCES family_members(id),
    paid         BOOLEAN NOT NULL DEFAULT false,   -- true = prepaid, false = cash on delivery
    tip_note     TEXT,                             -- "50 EGP set aside", or NULL
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | arrived | cancelled
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_orders_expected ON orders (expected_on);
```

### 5b. Tools (`tools.py`)
```python
{
    "name": "add_order",
    "description": "Record an incoming online order/delivery so anyone home knows what's coming. Use when someone says a package or order is arriving.",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What the order is."},
            "day": {"type": "string", "description": "When it's expected: a weekday, 'today'/'tomorrow', or an explicit date. Resolved server-side."},
            "ordered_by": {"type": "string", "description": "Name of who placed it, if known."},
            "paid": {"type": "boolean", "description": "true if prepaid, false if cash on delivery."},
            "tip_note": {"type": "string", "description": "Tip set aside, e.g. '50 EGP', if mentioned."},
        },
        "required": ["description", "day", "paid"],
    },
},
{
    "name": "get_orders",
    "description": "List pending orders/deliveries expected on a day (default today). Use when someone asks if any orders are coming — e.g. the doorbell rang.",
    "input_schema": {
        "type": "object",
        "properties": {"day": {"type": "string", "description": "Which day; defaults to today."}},
        "required": [],
    },
},
{
    "name": "mark_order_arrived",
    "description": "Mark an order as arrived once it's been received.",
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "integer"}},
        "required": ["order_id"],
    },
},
```

Dispatch resolves `day` via the shared date resolver, maps `ordered_by` name → member id, and `get_orders` returns pending rows for the date with: description, who ordered it, paid vs COD, tip note. (`mark_order_arrived` flips status.)

### 5c. Persona (`prompts.py`)
```
- You can track incoming online orders/deliveries. When someone says an order is coming, store it with: what it is, when, who ordered it, whether it's prepaid or cash-on-delivery, and any tip set aside. When someone asks if there are orders (e.g. the doorbell rang), list today's pending ones and for each say: what it is, who ordered it, paid or cash-on-delivery (with amount if known), and whether a tip is ready.
```

---

## 6. Keep it on Haiku, test, ship

- All of the above stays on **Haiku 4.5**. If the Sonnet escalation / keyword heuristic is still present, this is the moment to finish removing it.
- Extend `tests/smoke.py`: recall-grounding (model must call recall_facts before "don't have it"), store dedup (re-stated fact updates, not duplicates), calendar single-confirmation + correct weekday read, orders add/get/resolve-date, and a digit-bearing reply routing to multilingual_v2.
- Bump the `GET /` version marker (e.g. `2026-06-17-grounding+voice+orders`) so deploys are confirmable.
