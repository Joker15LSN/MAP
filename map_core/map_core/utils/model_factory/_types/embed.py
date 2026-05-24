from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubscriptableBaseModel(BaseModel):
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        if key in self.__pydantic_fields_set__:
            return True
        if key in self.__class__.model_fields:
            field = self.__class__.model_fields[key]
            return field.default is not None or field.default_factory is not None
        return False


class SupconEmbedResult(SubscriptableBaseModel):

    embedding: list[float]

    index: int

    object: str = "embedding"


class SupconEmbedResponse(SubscriptableBaseModel):
    """本地Embedding接口返回."""

    data: list[SupconEmbedResult]

    model: str

    object: str

    usage: dict


class EmbedClientConfig(BaseModel):
    """Embedding client config."""

    url: str

    model: str

    normalized: bool = Field(alias="isNorm")

    timeout: float

    model_config = ConfigDict(
        validate_by_name=True,
        validate_by_alias=True,
        extra="allow",
    )
