from enum import Enum

from ._types.embed import EmbedClientConfig


class EmbeddingChoice(Enum):

    SUPCON = "supcon"

    # Add your custom embedding choices here
    CUSTOM = "custom"


# Embedding configuration
#! 这一部分要加到项目 root config 中
EMBEDDING_REGISTRY: dict[EmbeddingChoice, EmbedClientConfig] = {
    EmbeddingChoice.SUPCON: EmbedClientConfig(
        url="http://10.16.11.41:1114/v1/embeddings",
        model="m3e",  # m3e / bge
        normalized=True,  # type: ignore
        timeout=30.0,
    ),  # type: ignore
}

DEFAULT_EMBEDDING_CHOICE = EmbeddingChoice.SUPCON
