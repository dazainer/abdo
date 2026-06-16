import cohere
from app.config import settings

# Cohere Embed v4 at 1024 dims to match the schema's vector(1024).
_client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
EMBED_MODEL = "embed-v4.0"
EMBED_DIM = 1024


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
