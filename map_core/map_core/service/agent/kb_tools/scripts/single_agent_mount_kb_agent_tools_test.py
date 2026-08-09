
import json

from httpx import HTTPError, request


def main():
    data = {
        "query": "查询在2024.1.1到2024.6.30文档中描述的基本每股收益",
        "additional_user_prompt": "",
        "staff_code": "0120250028",
        "agent_code": "General_Assistant",
        "scene_agent_config": {
            "prompt": "你是一个有用的助手。拆解用户问题，然后调用工具来完成任务。你可以同时调用多次工具",
            "tool_names": [
                # "read_uploaded_file_chunk",
                "search_mounted_kb_agent"
            ],
            "max_steps": 5,
            "description": "啥都能干一点的有用的agent助手",
            "force_tool_call": True,
            "scene_post_summary": {
                "enabled": True,
                "system_prompt": "你是一个总结智能体。你需要总结回答，突出重点，保留所有关键信息。",
            },
        },
        "tool_context": {
            "General_Assistant": {
                "search_mounted_kb_agent": {
                    # "rerank_model_config": {
                    #     "rerank_model_url": "http://10.50.56.243/v1/rerank",
                    #     "rerank_model_name": "jina-reranker-v2-base-multilingual",
                    #     "rerank_auth_token": "gpustack_c60ea7b6efa4784c_22039bb6f38836e6a955588a5df04306",
                    # },
                    "kb_configs": [
                        {
                            "embed_name": "bge",
                            "embed_url": "http://10.50.56.243/v1/embeddings",
                            "embed_auth_token": "gpustack_67740332be54f86f_6711f81dbbcecdf9f85be842418e44d9",
                            "kb_name": "MAP（Multi Agent Path）2025年第三季度报告_知识库",
                            "kb_code": "knowledgeBase1773298195139",
                        }
                    ],
                    "disassembly_system_prompt" : "你是问题拆解助手。请把用户问题拆成可独立查询的子问题。你应该把对于多个主体的复杂查询分解为对于单个主体的查询。对于某个主体多维度的查询拆解为有限个数个单维度的查询。拆解后的问题请保留明确的主语。\n作为参考，下游支持的查询范围为人力资源有关数据，主要包括但不限于：\n- 人员日程\n- 人员日报/周报\n- 人员详情\n- 会议\n- 公司组织架构\n- 人员考勤出勤\n- 等\n\n---\n### 补充信息\n如无额外信息，则问题所指的公司是MAP（Multi Agent Path）有限公司\n现在的日期是 {current_time}",
                    "disassembly_user_prompt":"请拆解这个问题：{query}",
                    "summarize_prompt" : "你是效率数据分析助手。请基于问题查询结果进行总结。在优先保留所有关键信息的前提下，保持叙述高度简洁，精炼。"

                }
            }
        },
        # "uploaded_kb_files": [
        #     {
        #         "embed_id": "bge",
        #         "embed_name": "bge",
        #         "embed_url": "http://10.50.56.243/v1/embeddings",
        #         "embed_auth_token": "gpustack_67740332be54f86f_6711f81dbbcecdf9f85be842418e44d9",
        #         "file_id": "6343111009762944",
        #         "file_name": "MAP（Multi Agent Path）2025年第三季度报告_表格_",
        #         "kb_code": "knowledgeBase1773298195139",
        #     }
        # ],
        # "rerank_model_config": {
        #     "rerank_model_url": "http://10.50.56.243/v1/rerank",
        #     "rerank_model_name": "jina-reranker-v2-base-multilingual",
        #     "rerank_auth_token": "gpustack_c60ea7b6efa4784c_22039bb6f38836e6a955588a5df04306",
        # },
    }

    headers = {
        'Content-Type': 'application/json'
    }

    url = 'http://localhost:10000/global_domain/debug/scene_agent/run'

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

