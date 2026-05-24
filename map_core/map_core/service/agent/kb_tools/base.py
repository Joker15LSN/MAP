import json
import os
from typing import Any, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field

from map_core.service.agent.base import AgentRequest, ToolResult

from ....config import MAP_KB_API_BASE_URL
from .remote_api import ResultItem, SearchResultItem

KB_API_BASE_URL = MAP_KB_API_BASE_URL


def build_item_as_dict(
    item: ResultItem,
    #    is_search = False
) -> dict[str, Any]:
    sub_title_parts = [item.header1, item.header2, item.header3]
    sub_title_parts = [sub_header for sub_header in sub_title_parts if sub_header]
    res = {
        "file_name": item.title,
        "chunk_content": item.chunk_content,
        "chunk_index": item.chunk_index,
    }
    if sub_title_parts:
        res["sub_headers"] = "->".join(sub_title_parts)
    # if is_search:
    #     res['file_id'] = item.kb_file_id
    return res

class SearchKbChunkOutput(BaseModel):
    """语义搜索知识库 chunk 输出结果"""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="搜索是否成功")
    results: list[SearchResultItem] = Field(
        default_factory=list, description="搜索到的 chunk 结果列表"
    )
    total_count: int = Field(default=0, description="结果总数")
    error: str | None = Field(default=None, description="错误信息")


class RerankModelConfig(BaseModel):
    rerank_model_name: str = Field(..., description="rerank 模型 ID")
    rerank_model_url: str = Field(..., description="rerank 模型名称")
    rerank_auth_token: str = Field(..., description="rerank 模型服务authtoken")


def fetch_tool_self_dict(agent_request: AgentRequest, self_tool_name: str) -> dict:
    if not agent_request.extra:
        raise ValueError("cannot fetch extra dict for tool.")
    caller_agent_name: str = str(agent_request.extra.get("caller_agent_name"))
    self_extra_dict = (
        agent_request.extra.get("tool_context", {})
        .get(caller_agent_name, {})
        .get(self_tool_name)
    )
    assert self_extra_dict
    return self_extra_dict


def temp_to_tool_result(search_chunk_output: SearchKbChunkOutput) -> dict[str, Any]:
    '''
    临时方案，将search_chunk_output dump为字典
    '''

    if search_chunk_output.success:
        item_dicts = []
        for item in search_chunk_output.results:
            _dict = build_item_as_dict(item=item)
            # _dict['backend_id'] = item.backend_id
            _dict['ba_id'] = item.ba_id
            item_dicts.append(_dict)
        return {
            'success': search_chunk_output.success,
            'results': item_dicts,
            'total_count': search_chunk_output.total_count,
        }
    else:
        return {
            'success': search_chunk_output.success,
            'error': search_chunk_output.error
        }


def  _build_contents(search_chunk_output: SearchKbChunkOutput) -> list[str]:
    res = []
    for result in search_chunk_output.results:
        content_obj = build_item_as_dict(item=result)
        res.append(json.dumps(content_obj, ensure_ascii=False))
    return res


def to_tool_result(tool_name:str, search_chunk_output: SearchKbChunkOutput) -> ToolResult:
    '''
    search_chunk_output 转化为ToolResult
    '''
    tool_result = ToolResult(
        name = tool_name,
        success=search_chunk_output.success,

    )
    if search_chunk_output.success:
        tool_result.content = '\n'.join(_build_contents(search_chunk_output))
        tool_result.data_source = {
            "source": f"{tool_name}_source",
            "data": [item.model_dump() for item in search_chunk_output.results],
        }
                                
    else:
        tool_result.error = search_chunk_output.error
        tool_result.success = search_chunk_output.success
    return tool_result
