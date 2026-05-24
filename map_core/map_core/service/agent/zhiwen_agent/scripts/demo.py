
import json
from ...base import AgentRequest
from ..agent import ZhiwenAgent
import asyncio
import os

from .....config.common import QWEN3_NEXT_80B_CONFIG
from .....utils.llm_engine import LLMEngine
from ..prompts import build_disassembly_prompts

async def _demo() -> None:
    tenant_id = os.getenv("ZHIWEN_TENANT_ID", "dt")
    user_id = os.getenv("ZHIWEN_USER_ID", "your_user_id")
    user_name = os.getenv("ZHIWEN_USER_NAME", "your_name")
    auth_token = os.getenv("ZHIWEN_AUTH", "your_token")
    staff_code = os.getenv("ZHIWEN_STAFF_CODE", "0120250028")
    query = os.getenv("ZHIWEN_QUERY", "坐班车多少钱")
    max_items = 6
    disiassemble_sys_prompt, disiassemble_user_prompt = build_disassembly_prompts(max_items=max_items)

    print(f'disiassemble_sys_prompt: \n{disiassemble_sys_prompt}')
    print(f'disiassemble_user_prompt: \n{disiassemble_user_prompt}')


    print(f'disiassemble_sys_prompt as json: \n{json.dumps(disiassemble_sys_prompt, ensure_ascii=False)}')
    print(f'disiassemble_user_prompt as json: \n{json.dumps(disiassemble_user_prompt,  ensure_ascii=False)}')

    if not tenant_id or not user_id or not user_name or not auth_token:
        print(
            "请先设置环境变量：ZHIWEN_TENANT_ID / ZHIWEN_USER_ID / ZHIWEN_USER_NAME / ZHIWEN_AUTH"
        )
        return

    agent = ZhiwenAgent(llm=LLMEngine(QWEN3_NEXT_80B_CONFIG))
    request = AgentRequest(
        query=query,
        staff_code=staff_code,
        summarize=True,
        extra={
            "request_id": "sample_req_id",
            "caller_agent_name": "org_agent",
            "rerank_model_config": {
                    "rerank_model_name": "qwen3z6b",
                    "rerank_model_url":"http://10.50.56.243/v1/rerank",
                    "rerank_auth_token": "gpustack_de6adf356d53ae9f_c803a9395068e4879708f267852629cb",
                    "rerank_score_threshold": 0.1
                },
            "tool_context": {

                "org_agent": {
                    "zhiwen_agent": {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "user_name": user_name,
                        "sources": ["REPORT_MARKET", "KMS", "OA", "SEP"],
                        "source_config": {
                            "kms_config": {
                                "cbb_kb_algo_api": "http://10.54.56.39:1103/multiKnowledgeSearch",
                                "kms_file_code_getter_api": "",
                                "kms_permission_check_api": "https://ubd.supcon.com/msService/public/kms/catalog/kmscatalog/getPermission",
                                "kb_configs": [
                                    {
                                        "kb_code": "knowledgeBase1773766800619",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1770089017363",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1770089018724",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1775840400487",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1768323600406",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933554583",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933556087",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933557487",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933558758",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933560043",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933562631",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933563936",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933565239",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933566554",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933569184",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933570546",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933571836",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933573116",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    },
                                    {
                                        "kb_code": "knowledgeBase1755933574435",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    }
                                ],
                                "emb_configs": [
                                    {
                                        "model_name": "bge",
                                        "model_url":"http://10.50.56.243/v1/embeddings",
                                        "auth_token": "gpustack_c60ea7b6efa4784c_22039bb6f38836e6a955588a5df04306"
                                    }
                                ]
                            },

                            "report_market_config": {
                                "cbb_kb_algo_api": "http://10.48.1.46:1103/multiKnowledgeSearch",
                                "kb_configs": [
                                    {
                                        "kb_code": "knowledgeBase1775183932434",
                                        "emb_model_name": "bge",
                                        "filter_by_file_ids": None
                                    }
                                ],
                                "emb_configs": [
                                    {
                                        "model_name": "bge",
                                        "model_url": "http://10.50.56.243/v1/embeddings",
                                        "auth_token": "gpustack_de6adf356d53ae9f_c803a9395068e4879708f267852629cb"
                                    }
                                ],
                                "rpt_mkt_file_code_getter_api": "",
                                "rpt_mkt_permission_check_api": "",
                                "rpt_mkt_oauth_url": ""
                            },
                            "oa_config": {
                                "oa_search_api": "https://oa.supcon.com/api/RESTAdapter/other/OA/search",
                                "oa_es_url": "http://10.30.3.120:8090",
                                "oa_url_prefix": "https://oa.supcon.com"
                            },
                            "sep_config": {
                                "sep_search_api": "https://sep.supcon.com:8080/api/search/list"
                            }
                        },
                        "disassembly_system_prompt": (
                            # "你是问题拆解助手。请把用户问题拆成可独立检索的子问题。"
                            # "现在日期是{current_time}。"
                            disiassemble_sys_prompt
                        ),
                        "disassembly_user_prompt": \
                            #   "请拆解问题：{query}",
                            disiassemble_user_prompt,
                        "summarize_prompt": (
                            "你是企业知识检索助手。请基于检索结果给出准确、简洁总结。"
                        ),
                    }
                }
            },
            "request_token": auth_token,
        },
    )

    result = await agent.run(request)
    print("AgentResult:")
    print(f"  success: {result.success}")
    print(f"  error: {result.error}")
    print(f"  content:\n{result.content}")
    print(f"  content len:\n{len(result.content)}")
    print(f"  meta_data: {result.meta_data}")
    data_items = result.data_source.get("data", []) if result.data_source else []
    print(f"  data items: {len(data_items)}")

if __name__ == "__main__":
    asyncio.run(_demo())
