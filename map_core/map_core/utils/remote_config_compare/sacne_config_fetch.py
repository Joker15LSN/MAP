from __future__ import annotations

import asyncio
import json
from typing import Optional

from pydantic import BaseModel
from pymilvus import AsyncMilvusClient

from ...service.agent.base import AgentRequest
from ...service.scene_agent_config_provider import (
    SceneAgentConfigFetchResult,
    SceneAgentConfigProvider,
)

PROCUREMENT: str = "Procurement"
COMPANY_NEWS: str = "Company_News"
MASTER: str = "MASTER"
PARK_SERVICE: str = "Park_Service"
GENERAL_ASSISTANT: str = "General_Assistant"
HR: str = "HR"
IPD_RD: str = "IPD_RD"
SUPPLY_CHAIN: str = "Supply_Chain"
CUSTOMER_ASSISTANT: str = "Customer_Assistant"
ENGINEERING: str = "Engineering"
MARKET_ASSISTANT: str = "Market_Assistant"
QUALITY: str = "Quality"
OPERATIONS: str = "Operations"
ECOSYSTEM_PARTNER: str = "Ecosystem_Partner"
INDUSTRIAL_ASSISTANT: str = "Industrial_Assistant"
FINANCIAL_ASSISTANT: str = "Financial_Assistant"


DEFAULT_SCENES = [
    PROCUREMENT,
    COMPANY_NEWS,
    MASTER,
    PARK_SERVICE,
    GENERAL_ASSISTANT,
    HR,
    IPD_RD,
    SUPPLY_CHAIN,
    CUSTOMER_ASSISTANT,
    ENGINEERING,
    MARKET_ASSISTANT,
    QUALITY,
    OPERATIONS,
    ECOSYSTEM_PARTNER,
    INDUSTRIAL_ASSISTANT,
    FINANCIAL_ASSISTANT,
]

ENV_NAME_UBD_DEV = 'ubddev'
ENV_NAME_UBD_PROD = 'ubdprod'


ENV_2_BACKEND_URL = {
    ENV_NAME_UBD_DEV: 'https://ubddev.supcon.com:8080',
    ENV_NAME_UBD_PROD: 'https://ubd.supcon.com',
}

DEFAULT_ENVS = list(ENV_2_BACKEND_URL.keys())


class MetricMeta(BaseModel):
    metric_code: str
    metric_name: str
    metric_meaning: str

class DataModelMeta(BaseModel):
    '''

                "data_origin_id": item.get("data_origin_id"),
            "data_model_name": item.get("data_model_name"),
            "data_model_description": item.get("data_model_description"),
    '''
    data_origin_id: str
    data_model_name: str
    data_model_description: str




async def afetch_agent_configs_by_refs(scene_codes: list[str], env='ubddev') -> SceneAgentConfigFetchResult:
    '''
    return example (P0-SEC-01: credentials/internal endpoints redacted):
    {"scene_agent_configs":{"Procurement":{"prompt":"你是采购管理专家，请协助用户进行采购流程与供应商管理。","tool_names":["search_mounted_kb_agent","ask_database_agent"],"max_steps":1,"description":"采购场景","force_tool_call":true,"stop_on_no_tool_call":true,"llm_config":{"base_url":"https://<model-endpoint>/v1","api_key":"<redacted>","model":"Qwen3-Next"}},"tool_context":{}}}

    '''
    """通过 refs 获取 agent 实际配置，参数全部写死。"""
    backend_env_base_url = ENV_2_BACKEND_URL.get(env, None)
    if not backend_env_base_url:
        raise RuntimeError(f'env: {env} is not supported')
    request = AgentRequest(
        query="",
        staff_code="anonymous",
        extra={
            "backend_env_base_url": backend_env_base_url,
            "backend_env": "RELEASE_STATE",
            "request_id": "",
            "session_id": "-",
            "request_token": "",
            "x_userid": "missing",
            "x_username": "missing",
        },
    )
    provider = SceneAgentConfigProvider(endpoint="", timeout_s=5.0)
    return await provider.fetch_by_refs(scene_codes, request)


# def fetch_agent_configs_by_refs(scene_codes: list[str]) -> SceneAgentConfigFetchResult:
#     """同步版本：通过 refs 获取 agent 实际配置，参数全部写死。"""
#     return asyncio.run(afetch_agent_configs_by_refs(scene_codes))


