from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class FridayChatHistoryItem(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: str = Field(default="", max_length=8000)


class FridayChatContextOverrides(BaseModel):
    container: Optional[str] = None
    request_id: Optional[str] = None
    rid: Optional[str] = None


class FridayChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: Optional[str] = None
    history: List[FridayChatHistoryItem] = Field(default_factory=list)
    context_overrides: Optional[FridayChatContextOverrides] = None


class FridayConfigRequest(BaseModel):
    base_url: str = Field(..., min_length=1, max_length=512)
    model: str = Field(..., min_length=1, max_length=256)
