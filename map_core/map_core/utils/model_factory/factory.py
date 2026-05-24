"""模型工厂，包括：
- 获取 embedding
- ...
"""

import asyncio

from ._config import DEFAULT_EMBEDDING_CHOICE, EMBEDDING_REGISTRY
from ._types.embed import SupconEmbedResponse
from .http_client import ChatClient, EmbedClient


async def aembed_text(text: str):
    embed_client = EmbedClient(response_cls=SupconEmbedResponse)
    embed_client_config = EMBEDDING_REGISTRY[DEFAULT_EMBEDDING_CHOICE]
    try:
        embedding_response: SupconEmbedResponse = await asyncio.wait_for(
                embed_client.aembed(
                    data={
                        "input": text, 
                        "model": embed_client_config.model, 
                    "isNorm": embed_client_config.normalized,
                },
                url=embed_client_config.url,
                stream=False,
            ),
            timeout=embed_client_config.timeout,
        )
        if not embedding_response.data:
            raise ValueError("Failed to get question embedding")
        text_embedding = embedding_response.data[0].embedding
        if not text_embedding:
            raise ValueError("Failed to get question embedding")
    except asyncio.TimeoutError:
        print("Embedding request timed out")
    except Exception as e:
        print(f"Embedding request failed: {e}")

    return text_embedding

async def aembed_documents(documents: list[str]):
    embed_client = EmbedClient(response_cls=SupconEmbedResponse)
    embed_client_config = EMBEDDING_REGISTRY[DEFAULT_EMBEDDING_CHOICE]

    try:
        embedding_response: SupconEmbedResponse = await asyncio.wait_for(
                embed_client.aembed(
                    data={
                        "input": documents, 
                        "model": embed_client_config.model, 
                    "isNorm": embed_client_config.normalized,
                },
                url=embed_client_config.url,
                stream=False,
            ),
            timeout=embed_client_config.timeout,
        )
        if not embedding_response.data:
            raise ValueError("Failed to get question embedding")
        embedding_list = [_["embedding"] for _ in embedding_response.data]
        if not embedding_list:
            raise ValueError("Failed to get question embedding")
    except asyncio.TimeoutError:
        print("Embedding request timed out")
    except Exception as e:
        print(f"Embedding request failed: {e}")

    return embedding_list
