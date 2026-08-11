
import json

from httpx import HTTPError, request


def main():
    data = {
        "query": "坐班车多少钱",
        "staff_code": "ley001",
        "agent_code": "Company_News",
        "backend_env_base_url": "https://ubddev.supcon.com:8080",
        "rerank_model_config": {
            "rerank_model_name": "jina-reranker-v2-base-multilingual",
            "rerank_model_url": "http://10.50.56.243/v1/rerank",
            "rerank_auth_token": "gpustack_de6adf356d53ae9f_c803a9395068e4879708f267852629cb"
        },
        "scene_agent_config": {
            "prompt": "你是公司信息助手，请为用户提供公司新闻与内部动态信息。你必须调用工具来检索信息，回答用户的提问。你可以同时调用多次工具。你不能拒接回答问题，应该在调用工具后尽最大努力给出回答。",
            "description": "公司动态新闻场景：公司新闻与内部动态和资料。包括但不限于公司发文，公司政策制度、规章流程文件，公司知识库，产品说明等等",
            "tool_names": [
                "zhiwen_agent"
            ],
            "max_steps": 2,
            "force_tool_call": True,
            "stop_on_no_tool_call": True,
            "scene_post_summary": {
                "enabled": True,
                "system_prompt": "总结提示词",
                "llm_config": {
                    "model": "deepseek-v4-flash",
                    "temperature": "0.7",
                    "base_url": "http://10.50.56.243/v1",
                    "api_key": "gpustack_de6adf356d53ae9f_c803a9395068e4879708f267852629cb"
                }
            },
            "llm_config": {
                "model": "deepseek-v4-flash",
                "temperature": "0.7",
                "base_url": "http://10.50.56.243/v1",
                "api_key": "gpustack_de6adf356d53ae9f_c803a9395068e4879708f267852629cb"
            }
        },
        "tool_context": {
            "Company_News": {
                "zhiwen_agent": {
                    "tenant_id": 'dt',
                    "disassembly_system_prompt": r"你是信息收集专家，你需要根据用户的原始问题，识别出**回答该问题所需的所有关键信息点**，并将每个信息点转化为一个**原子查询任务**。\n\n你可能需要的一些背景信息：\n- 公司是MAP（Multi Agent Path），是国产工业自动化领域的龙头，尤其在流程工业（如石化、化工、电力等）占有较高市场份额。\n- 用户是MAP（Multi Agent Path）的员工，需要站在用户的角度理解用户问题的意图。\n- 当前日期是{current_time}，本年度参考MAP（Multi Agent Path）2026年最新的文件，默认在MAP（Multi Agent Path）公司背景下回答\n\n核心原则：\n- **只拆解出真正需要的信息点**，不要为了凑数而生成冗余查询\n- 每个查询任务**必须是原子问题，不能再拆分**，非原子查询问题可能因为需要组合信息导致没有交集而找不到答案\n- **子问题应该简洁多样，从不同角度、不同粒度、不同表述方式提问**，避免所有问题都使用相同的句式结构或前缀词\n- **优先使用核心关键词组合**，而非冗长的完整句子，例如\"请假审批流程\"优于\"员工请假的审批流程是怎样的\"\n- 关键词组合要具体精准，保留核心实体、时间范围、指标名称等关键限定词，避免使用抽象笼统的表述\n- **每个子问题应聚焦不同的信息维度**，减少词汇重复，以获得更全面且不重复的检索结果\n- 如果用户问题本身就是原子问题，则直接返回用户问题作为唯一的查询任务\n- 子问题总数不超过6个（这是限制，不是目标数量）",
                    "ubd_auth_str": "Bearer uhE-6Ge3bUrvpMr7gJKfA",
                    "user_id": 2293382801220192,
                    "user_name": "lienyu",
                    "staff_code": "ley001",
                    "disassembly_user_prompt": r"用户当前问题: {query}\n\n请思考：要完整回答这个问题，需要获取哪些**不同维度**的信息？\n\n拆解步骤：\n1. 识别问题中的核心信息需求（可能是1个，也可能是多个）\n2. 为每个信息需求设计一个独立的、可直接检索的查询\n3. 确保每个查询聚焦不同的信息维度，避免重复\n\n拆解要求：\n- 每个子查询应该是独立的、可直接用于检索的关键词或短语\n- 避免使用相同的句式结构，保持表述多样性\n- 优先使用简洁的关键词组合，而非完整的问句\n- **只生成必要的查询，不要为了数量而重复或冗余**\n",
                    "sup_token": "Bearer uhE-6Ge3bUrvpMr7gJKfA",
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
                        }
                    }
            }
        }
    }

    headers = {'accept': 'application/json, application/*+json',
               'content-type': 'application/json', ''
               'x-request-id': '0bb8ed2ca2414bbebb09100281c1e6fc',
               'x-request-token': 'Bearer uhE-6Ge3bUrvpMr7gJKfA',
               'x-username': 'lienyu',
            #    'content-length': '2661',
               'host': '10.48.2.201:10000',
               'connection': 'Keep-Alive',
               'user-agent': 'Apache-HttpClient/4.5.14 (Java/1.8.0_212)',
               'accept-encoding': 'gzip,deflate'}

    url = 'http://localhost:10000/global_domain/debug/scene_agent/run'
    # url = 'http://localhost:10000/global_domain/chat/stream/v3'


    try:
        response = request(
            method='POST',
            url=url,
            headers=headers,
            json=data,
            timeout=1200.0
        )
        response.raise_for_status()
        result = response.json()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except HTTPError as e:
        print(f"请求失败: {e}")
        raise

if __name__ == "__main__":
    main()

