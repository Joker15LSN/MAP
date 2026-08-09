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
    return example:
    {"scene_agent_configs":{"Procurement":{"prompt":"你是采购管理专家，请协助用户进行采购流程与供应商管理。","additional_user_prompt":"用户提示词","tool_names":["search_mounted_kb_agent","ask_database_agent"],"max_steps":1,"description":"采购场景：采购寻源、比价招投标、合同与交付验收、供应商绩效。该场景可以查询 财务付款发票明细报表、库存成本报表、合同台账按月表、财务付款明细报表等数据表","force_tool_call":true,"stop_on_no_tool_call":true,"llm_config":{"base_url":"http://10.50.56.243/v1","api_key":"gpustack_de6adf356d53ae9f_c803a9395068e4879708f267852629cb","model":"Qwen3-Next","temperature":0.7,"logprobs":null,"top_logprobs":null,"max_tokens":4096,"timeout":120.0,"stream_timeout":300.0,"chunk_timeout":30.0,"max_retries":2,"top_p":1.0,"top_k":20,"frequency_penalty":0.0,"presence_penalty":0.0,"extra_headers":{},"chat_template_kwargs":{}},"scene_post_summary":{"enabled":true,"system_prompt":null,"user_prompt_template":null,"llm_config":null}}},"tool_context":{"Procurement":{"ask_database_agent":{"disassembly_system_prompt":"你是数据库数据模型专家，请协助用户进行数据查询和分析。","selected_data_model_ids":[6439589137508752,6433466611329472,6433465874147776,6433465187789248,6433464515848640],"agent_id":8,"user_id":null,"business_domain":8,"description":{"6450487336397200":"按月+行项目维度展开，含物料编码/名称/型号、品牌、询价单号、项目、供应商、含税单价/数量/金额、已付款/未付款/到货金额等明细字段","6450487336561040":"含付款金额、付款日期、供应商、付款状态等","6450487336724880":"含供应商、发票金额、税额、直供标识等","6450487336692112":"含物料编码、仓库、库存数量/金额等","6450487336659344":"含合同金额、订单金额、核算月份等"},"disassembly_user_prompt":"查询数据库中的数据","userName":"missing"},"search_mounted_kb_agent":{"disassembly_system_prompt":"你是一个知识库助手，请根据用户的问题提供准确的知识库信息。","description":{},"disassembly_user_prompt":"请帮我解答这个问题","kb_configs":[]}}}}

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


