from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class RerankModelConfigSchema(BaseModel):
    rerank_model_name: str = Field(..., description="rerank 模型 ID")
    rerank_model_url: str = Field(..., description="rerank 模型名称")
    rerank_auth_token: str = Field(..., description="rerank 模型服务authtoken")


def _env_value(name: str) -> str:
    return (os.getenv(name) or "").strip()


def create_default_rerank_model_conf() -> RerankModelConfigSchema:
    """Build the fallback rerank config exclusively from environment.

    P0-SEC-01: no hardcoded credentials or internal endpoints. When
    ``MAP_RERANK_AUTH_TOKEN`` / ``MAP_RERANK_BASE_URL`` are unset the config
    resolves to empty values and downstream consumers fail closed (they
    reject incomplete rerank configs before any network call).
    """
    return RerankModelConfigSchema(
        rerank_model_url=_env_value("MAP_RERANK_BASE_URL"),
        rerank_model_name=(
            _env_value("MAP_RERANK_MODEL_NAME")
            or "jina-reranker-v2-base-multilingual"
        ),
        rerank_auth_token=_env_value("MAP_RERANK_AUTH_TOKEN"),
    )
