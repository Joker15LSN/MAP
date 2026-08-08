from typing import Optional

from pydantic import BaseModel, Field

from .rerank_model_schema import RerankModelConfigSchema


class KnowledgeBaseSchema(BaseModel):
    embed_name: str = Field(..., description="embedding 模型名称")
    embed_url: str = Field(..., description="embedding 模型服务链接")
    embed_auth_token: str = Field(..., description="embedding 模型服务authtoken")
    
    kb_code: str = Field(..., description="知识库编号")
    kb_name: Optional[str] = Field(default=None, description='知识库名称')
    kb_description: Optional[str] = Field(default=None, description='知识库描述')

class MountedKBsSchema(BaseModel):
    rerank_model_config: RerankModelConfigSchema
    kb_configs: list[KnowledgeBaseSchema]

class MountedKBsAgentSchema(MountedKBsSchema):
    disassembly_system_prompt :str =  "你是问题拆解助手。请把用户问题拆成可独立查询的子问题。你应该把对于多个主体的复杂查询分解为对于单个主体的查询。对于某个主体多维度的查询拆解为有限个数个单维度的查询。拆解后的问题请保留明确的主语。\n作为参考，下游支持的查询范围为人力资源有关数据，主要包括但不限于：\n- 人员日程\n- 人员日报/周报\n- 人员详情\n- 会议\n- 公司组织架构\n- 人员考勤出勤\n- 等\n\n---\n### 补充信息\n如无额外信息，则问题所指的公司是MAP（Multi Agent Path）有限公司\n现在的日期是 {current_time}"
    disassembly_user_prompt :str =  "请拆解这个问题：{query}"
    summarize_prompt :str = "你是效率数据分析助手。请基于问题查询结果进行总结。在优先保留所有关键信息的前提下，保持叙述高度简洁，精炼。"
