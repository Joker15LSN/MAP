
import json

from httpx import HTTPError, request


def main():
    data = {
        "query": "基本每股收益",
        "additional_user_prompt": "",
        "staff_code": "0120250028",
        "agent_code": "General_Assistant",
        "scene_agent_config": {
            "prompt": "你是一个有用的助手。拆解用户问题，然后调用工具来完成任务。你可以同时调用多次工具",
            "tool_names": [
                # "read_uploaded_file_chunk",
                "search_mount_kbs"
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
                "search_mount_kbs": {
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
                    ]
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
    
