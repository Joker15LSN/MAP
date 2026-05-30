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


class FridayReportRunRequest(BaseModel):
    report_type: Literal["weekly", "monthly"] = "weekly"
    lookback_days: int = Field(default=7, ge=1, le=62)
    force: bool = True


class FridayReportConfigRequest(BaseModel):
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    weekly_day: int = Field(default=0, ge=0, le=6)
    weekly_hour: int = Field(default=9, ge=0, le=23)
    monthly_day: int = Field(default=1, ge=1, le=28)
    monthly_hour: int = Field(default=9, ge=0, le=23)
    monthly_minute: int = Field(default=15, ge=0, le=59)
    slow_threshold_s: float = Field(default=5.0, ge=0.1, le=3600)
