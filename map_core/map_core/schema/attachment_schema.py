from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AttachmentSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    file_type: str = Field(min_length=1)
    file_url: str = Field(min_length=1)


class UploadedKBFileSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    embed_id: str = Field(..., description="embedding 模型 ID")
    embed_name: str = Field(..., description="embedding 模型名称")
    embed_url: str = Field(..., description="embedding 模型服务链接")
    embed_auth_token: str = Field(..., description="embedding 模型服务authtoken")

    file_id: str = Field(..., description="文件ID")
    file_name: str = Field(..., description="文件名称")
    kb_code: str = Field(..., description="知识库编号")