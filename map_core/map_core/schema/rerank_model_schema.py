from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RerankModelConfigSchema(BaseModel):
    rerank_model_name: str = Field(..., description="rerank 模型 ID")
    rerank_model_url: str = Field(..., description="rerank 模型名称")
    rerank_auth_token: str = Field(..., description="rerank 模型服务authtoken")

def create_default_rerank_model_conf() -> RerankModelConfigSchema:
    return RerankModelConfigSchema(
        rerank_model_url = 'http://10.50.56.243/v1/rerank',
        rerank_model_name = 'jina-reranker-v2-base-multilingual',
        rerank_auth_token = 'gpustack_c60ea7b6efa4784c_22039bb6f38836e6a955588a5df04306'
    )