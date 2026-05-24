from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, PositiveInt, field_validator


class LLMConfig(BaseModel):
    base_url: str
    api_key: str = Field(default="")
    model: str = Field(default="local")
    temperature: float = Field(default=0.7, ge=0, le=2)
    logprobs: bool | None = Field(default=None)
    top_logprobs: int | None = Field(default=None, ge=0)
    max_tokens: Optional[PositiveInt] = Field(default=4096)
    timeout: float = Field(default=120.0, gt=0)
    thinking: dict[str, Any] = Field(default_factory=lambda: {"type": "disabled"})
    stream_timeout: float = Field(default=300.0, gt=0)  # 流式响应总超时时间
    chunk_timeout: float = Field(default=30.0, gt=0)  # 单个chunk超时时间
    max_retries: int = Field(default=2, ge=0)
    top_p: float = Field(default=1.0, ge=0, le=1)
    top_k: int = Field(default=20, ge=0)
    frequency_penalty: float = Field(default=0.0, ge=-2, le=2)
    presence_penalty: float = Field(default=0.0, ge=-2, le=2)
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    chat_template_kwargs: Dict[str, Any] = Field(
        default_factory=lambda: {"enable_thinking": False}
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str:
        return "" if value is None else value
