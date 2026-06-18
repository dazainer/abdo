import math

import cohere
from app.config import settings

# Cohere Embed v4 at 1024 dims to match the schema's vector(1024).
_client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
EMBED_MODEL = "embed-v4.0"
EMBED_DIM = 1024


def is_valid(vec) -> bool:
    """A storable/usable embedding is exactly EMBED_DIM finite, non-all-zero floats.

    Must accept the value however it arrives. Cohere returns a Python list, but
    pgvector (with register_vector) decodes a vector read back from Postgres into a
    numpy ndarray of float32. The old isinstance(list/tuple) + isinstance(int/float)
    checks false-REJECTED every valid row read from the DB — a numpy ndarray isn't a
    list and numpy float32 isn't a Python float — which silently flagged good rows as
    invalid (backfill) and could drop them from recall. So: never test truthiness on an
    array; check by length and coerce each element to float, accepting list/tuple/ndarray
    /any sequence alike.
    """
    if vec is None:
        return False
    try:
        if len(vec) != EMBED_DIM:
            return False
        vals = [float(x) for x in vec]
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(x) for x in vals) and any(x != 0.0 for x in vals)


async def embed(text: str, *, input_type: str) -> list[float]:
    """input_type = 'search_document' when storing, 'search_query' when retrieving.

    Documents and queries are embedded asymmetrically; mismatching them measurably
    hurts retrieval, so callers must pass the right one.
    """
    resp = await _client.embed(
        model=EMBED_MODEL,
        texts=[text],
        input_type=input_type,
        output_dimension=EMBED_DIM,
        embedding_types=["float"],
    )
    return resp.embeddings.float_[0]
