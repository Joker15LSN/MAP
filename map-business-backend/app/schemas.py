from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)
    history: list[dict[str, Any]] | None = None


class ModelRecord(BaseModel):
    model_name: str
    model_type: str = "本地"
    model_url: str
    is_default: bool = False
    api_type: str = "http"


class ModelCenterConfig(BaseModel):
    large_models: list[ModelRecord] = Field(default_factory=list)
    asr_models: list[ModelRecord] = Field(default_factory=list)
    tts_models: list[ModelRecord] = Field(default_factory=list)
    embedding_models: list[ModelRecord] = Field(default_factory=list)
    rerank_models: list[ModelRecord] = Field(default_factory=list)


class BasicSettingItem(BaseModel):
    setting_code: str
    setting_name: str
    setting_value: str
    category: str
    description: str = ""
    editable: bool = True


class AddressConfigItem(BaseModel):
    address_code: str
    address_name: str
    base_url: str
    timeout_s: int = 30
    enabled: bool = True
    remarks: str = ""


class DataAccessItem(BaseModel):
    source_name: str
    source_type: str
    auth_mode: str
    endpoint: str
    database_name: str
    enabled: bool = True
    owner: str
    last_sync: str | None = None


class DataAssetItem(BaseModel):
    asset_code: str
    asset_name: str
    asset_type: str
    source_name: str
    row_count: int = 0
    refresh_cycle: str = "daily"
    enabled: bool = True
    last_updated: str | None = None


class MasterAgentConfig(BaseModel):
    agent_code: str = "Master"
    display_name: str = "Master 智能体"
    model: str = "qwen3-next-80b"
    temperature: float = 0.2
    max_tokens: int = 4096
    summarize_style: str = "结构化总结 + 关键结论优先"
    enabled: bool = True
    scene_selector_model: str = "qwen3-next-80b"
    route_strategy: str = "scene_first"
    stream_version: str = "v2"
    timeout_s: int = 180
    fallback_enabled: bool = True
    query_rewrite_enabled: bool = False
    content_review_enabled: bool = False
    policies: list[str] = Field(
        default_factory=lambda: [
            "先进行场景识别，再触发业务智能体并行调用",
            "优先使用结构化数据源返回业务结论",
            "置信度不足时给出补充提问建议",
        ]
    )


class AgentMountedResource(BaseModel):
    resource_name: str
    resource_type: str
    source_name: str = ""
    permission_scope: str = "跟随智能体"
    dimension_status: str = "同步成功"
    created_at: str | None = None
    enabled: bool = True


class AgentToolPrompt(BaseModel):
    tool_name: str
    system_prompt: str = ""
    user_prompt: str = ""


class AgentPromptVersion(BaseModel):
    version: str
    updated_at: str
    operator: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 4096
    version_note: str = ""


class AgentPromptConfig(BaseModel):
    base_model: str = "qwen3-next-80b"
    system_prompt: str = ""
    user_prompt: str = ""
    summary_prompt: str = ""
    tool_prompts: list[AgentToolPrompt] = Field(default_factory=list)
    temperature: float = 0.1
    max_tokens: int = 4096
    current_version: str = "v1"
    version_note: str = ""
    history_versions: list[AgentPromptVersion] = Field(default_factory=list)


class AgentTestConfig(BaseModel):
    publish_status: str = "已发布"
    last_saved_at: str | None = None
    draft_messages: list[dict[str, str]] = Field(default_factory=list)


class BusinessAgentConfig(BaseModel):
    agent_code: str
    display_name: str
    scene_name: str
    owner_team: str
    agent_type: str = "business"
    model: str = "qwen3-next-80b"
    enabled: bool = True
    weight: int = 100
    timeout_s: int = 120
    retry_limit: int = 1
    parallel_limit: int = 3
    data_scope: str = "team"
    prompt_template: str = ""
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=lambda: ["all"])
    mounted_resources: list[AgentMountedResource] = Field(default_factory=list)
    glossary_terms: list[str] = Field(default_factory=list)
    prompt_config: AgentPromptConfig = Field(default_factory=AgentPromptConfig)
    test_config: AgentTestConfig = Field(default_factory=AgentTestConfig)
    last_updated: str | None = None


class SessionPolicyItem(BaseModel):
    policy_code: str
    policy_name: str
    status: str = "enabled"
    retention_days: int = 90
    rate_limit_qpm: int = 120
    updated_by: str = "admin"
    updated_at: str | None = None


class DashboardCardConfig(BaseModel):
    card_code: str
    card_name: str
    metric_expr: str
    refresh_interval_s: int = 30
    enabled: bool = True


class SecurityPolicyItem(BaseModel):
    rule_code: str
    rule_name: str
    severity: str = "medium"
    strategy: str = "alert"
    enabled: bool = True
    last_updated: str | None = None


class GlossaryTermItem(BaseModel):
    term: str
    category: str
    definition: str
    synonyms: list[str] = Field(default_factory=list)
    status: str = "enabled"
    updated_at: str | None = None


class HomeRecommendationItem(BaseModel):
    recommendation_id: str
    title: str
    target_scene: str
    priority: int = 100
    enabled: bool = True
    operator: str = "admin"
    updated_at: str | None = None


class PermissionRule(BaseModel):
    role: str
    allowed_agents: list[str]
    allowed_operations: list[str]
    staff_codes: list[str] = Field(default_factory=list)
    department_codes: list[str] = Field(default_factory=list)
    active: bool = True


class RolePolicy(BaseModel):
    role_code: str
    role_name: str
    permissions: list[str]
    data_scope: str
    enabled: bool = True


class UserAccount(BaseModel):
    staff_code: str
    user_name: str
    department: str
    roles: list[str]
    status: str = "enabled"
    last_login: str | None = None


class KnowledgeBinding(BaseModel):
    team: str
    kb_code: str
    kb_name: str
    kb_type: str = "team"
    embedding_model: str = "bge-m3"
    update_mode: str = "daily_sync"
    enabled: bool = True
    readable_roles: list[str]


class SkillPolicy(BaseModel):
    skill_code: str
    skill_name: str
    skill_type: str = "analysis"
    source: str = "builtin"
    max_calls: int = 5
    timeout_s: int = 90
    enabled: bool = True
    visible_roles: list[str] = Field(default_factory=lambda: ["all"])


class ReleaseRecord(BaseModel):
    id: str
    version: str = "v1"
    operator: str
    note: str
    affected_agents: list[str] = Field(default_factory=list)
    risk_level: str = "low"
    created_at: str


class AdminState(BaseModel):
    updated_at: str
    model_center: ModelCenterConfig
    basic_settings: list[BasicSettingItem]
    address_configs: list[AddressConfigItem]
    data_access_items: list[DataAccessItem]
    data_assets: list[DataAssetItem]
    master_agent: MasterAgentConfig
    business_agents: list[BusinessAgentConfig]
    session_policies: list[SessionPolicyItem]
    dashboard_cards: list[DashboardCardConfig]
    security_policies: list[SecurityPolicyItem]
    glossary_terms: list[GlossaryTermItem]
    homepage_recommendations: list[HomeRecommendationItem]
    permission_rules: list[PermissionRule]
    role_policies: list[RolePolicy]
    user_accounts: list[UserAccount]
    knowledge_bindings: list[KnowledgeBinding]
    skill_policies: list[SkillPolicy]
    release_history: list[ReleaseRecord]

    @staticmethod
    def default() -> "AdminState":
        now = datetime.now().isoformat()
        return AdminState(
            updated_at=now,
            model_center=ModelCenterConfig(
                large_models=[
                    ModelRecord(model_name="qwen3-next", model_url="http://10.50.56.243/v1", is_default=True),
                    ModelRecord(model_name="deepseek-v4-flash", model_url="http://10.50.56.243/v1"),
                    ModelRecord(model_name="deepseek-v4-flash-nvidia", model_url="http://10.50.56.243/v1"),
                ],
                asr_models=[
                    ModelRecord(model_name="paraformer-zh", model_type="远程", model_url="https://asr.map.local/v1"),
                ],
                tts_models=[
                    ModelRecord(model_name="cosyvoice-v2", model_type="远程", model_url="https://tts.map.local/v1"),
                ],
                embedding_models=[
                    ModelRecord(model_name="bge-m3", model_type="远程", model_url="https://embedding.map.local/v1", is_default=True),
                ],
                rerank_models=[
                    ModelRecord(model_name="bge-reranker-v2", model_type="远程", model_url="https://rerank.map.local/v1", is_default=True),
                ],
            ),
            basic_settings=[
                BasicSettingItem(
                    setting_code="max_concurrency",
                    setting_name="系统并发上限",
                    setting_value="200",
                    category="运行时",
                    description="控制全局请求并发上限",
                ),
                BasicSettingItem(
                    setting_code="default_stream_version",
                    setting_name="默认流式版本",
                    setting_value="v2",
                    category="流式协议",
                    description="前后台默认使用的 SSE 协议版本",
                ),
                BasicSettingItem(
                    setting_code="session_retention_days",
                    setting_name="会话保留天数",
                    setting_value="90",
                    category="存储",
                    description="会话与运行日志保留时长",
                ),
            ],
            address_configs=[
                AddressConfigItem(
                    address_code="map_core",
                    address_name="MAP Core API",
                    base_url="http://map_core:10000",
                    timeout_s=60,
                    enabled=True,
                    remarks="算法服务入口",
                ),
                AddressConfigItem(
                    address_code="knowledge",
                    address_name="知识库服务",
                    base_url="http://knowledge:8080",
                    timeout_s=30,
                    enabled=True,
                    remarks="知识检索入口",
                ),
            ],
            data_access_items=[
                DataAccessItem(
                    source_name="map_db_dev",
                    source_type="PostgreSQL",
                    auth_mode="密码",
                    endpoint="10.50.56.243:5432",
                    database_name="map_db_dev",
                    owner="数据平台组",
                    last_sync=now,
                ),
                DataAccessItem(
                    source_name="map_logs",
                    source_type="MongoDB",
                    auth_mode="用户名密码",
                    endpoint="10.50.56.243:27017",
                    database_name="map_logs",
                    owner="可观测性组",
                    last_sync=now,
                ),
            ],
            data_assets=[
                DataAssetItem(
                    asset_code="asset_contract",
                    asset_name="合同台账",
                    asset_type="table",
                    source_name="map_db_dev",
                    row_count=580000,
                    refresh_cycle="daily",
                    last_updated=now,
                ),
                DataAssetItem(
                    asset_code="asset_inventory",
                    asset_name="库存成本",
                    asset_type="table",
                    source_name="map_db_dev",
                    row_count=920000,
                    refresh_cycle="hourly",
                    last_updated=now,
                ),
            ],
            master_agent=MasterAgentConfig(),
            business_agents=[
                BusinessAgentConfig(
                    agent_code="Operations",
                    display_name="经营分析助手",
                    scene_name="经营分析",
                    owner_team="数字经营部",
                    data_scope="enterprise",
                    description="负责经营分析、KPI 拆解与月度总结。",
                    tools=["ask_database_agent", "wenshu_agent"],
                    allowed_roles=["admin", "ops", "finance"],
                    prompt_template="你是经营分析助手，优先返回关键指标与同比环比。",
                    glossary_terms=[],
                    mounted_resources=[
                        AgentMountedResource(
                            resource_name="商机数量",
                            resource_type="指标数据模型",
                            source_name="ESSENDATA",
                            created_at=now,
                        ),
                        AgentMountedResource(
                            resource_name="商机金额_定存",
                            resource_type="指标数据模型",
                            source_name="ESSENDATA",
                            created_at=now,
                        ),
                        AgentMountedResource(
                            resource_name="商机实际中标金额",
                            resource_type="指标数据模型",
                            source_name="ESSENDATA",
                            created_at=now,
                        ),
                    ],
                    prompt_config=AgentPromptConfig(
                        base_model="deepseek-v4-flash-ep",
                        system_prompt="你是市场分析助手，聚焦高置信度数据结论。",
                        user_prompt="{query}",
                        summary_prompt="请输出 TL;DR 与关键数据点。",
                        tool_prompts=[
                            AgentToolPrompt(tool_name="团队知识库", system_prompt="优先引用团队知识条目", user_prompt="{query}"),
                            AgentToolPrompt(tool_name="指标数据模型", system_prompt="优先使用指标口径返回", user_prompt="{query}"),
                            AgentToolPrompt(tool_name="数据库数据模型", system_prompt="SQL 结果需附口径", user_prompt="{query}"),
                            AgentToolPrompt(tool_name="效率派", system_prompt="仅用于动作建议", user_prompt="{query}"),
                            AgentToolPrompt(tool_name="互联网搜索", system_prompt="必须标注来源", user_prompt="{query}"),
                        ],
                        temperature=0.1,
                        max_tokens=4096,
                        current_version="v3",
                        version_note="删除冗余 prompt，强化来源标注。",
                        history_versions=[
                            AgentPromptVersion(
                                version="v3",
                                updated_at=now,
                                operator="admin",
                                model="deepseek-v4-flash-ep",
                                temperature=0.1,
                                max_tokens=4096,
                                version_note="删除冗余 prompt，强化来源标注。",
                            ),
                            AgentPromptVersion(
                                version="v2",
                                updated_at=now,
                                operator="admin",
                                model="deepseek-v4-flash-ep",
                                temperature=0.1,
                                max_tokens=4096,
                                version_note="统一工具提示词模板。",
                            ),
                        ],
                    ),
                    test_config=AgentTestConfig(publish_status="已发布", last_saved_at=now),
                    last_updated=now,
                ),
                BusinessAgentConfig(
                    agent_code="Marketing",
                    display_name="市场洞察助手",
                    scene_name="市场洞察",
                    owner_team="市场部",
                    data_scope="market",
                    description="负责竞品与市场趋势信息采集分析。",
                    tools=["web_search_agent", "search_mounted_kb_agent"],
                    allowed_roles=["admin", "marketing"],
                    prompt_template="你是市场洞察助手，需标注信息来源可信度。",
                    prompt_config=AgentPromptConfig(
                        base_model="qwen3-next-80b",
                        system_prompt="你是市场洞察助手，优先输出可信来源。",
                        user_prompt="{query}",
                        summary_prompt="输出 3 条关键洞察。",
                        tool_prompts=[
                            AgentToolPrompt(tool_name="互联网搜索", system_prompt="保留来源与时间", user_prompt="{query}"),
                            AgentToolPrompt(tool_name="团队知识库", system_prompt="优先内部资料", user_prompt="{query}"),
                        ],
                    ),
                    test_config=AgentTestConfig(publish_status="已发布", last_saved_at=now),
                    last_updated=now,
                ),
                BusinessAgentConfig(
                    agent_code="CustomerSuccess",
                    display_name="客户成功助手",
                    scene_name="客户经营",
                    owner_team="客户成功部",
                    data_scope="customer",
                    description="负责客户健康度与续约风险分析。",
                    tools=["ask_database_agent", "industry_chat_agent"],
                    allowed_roles=["admin", "cs", "sales"],
                    prompt_template="你是客户成功助手，优先输出客户风险分级与建议动作。",
                    prompt_config=AgentPromptConfig(
                        base_model="qwen3-next-80b",
                        system_prompt="你是客户成功助手，输出风险等级与动作建议。",
                        user_prompt="{query}",
                        summary_prompt="先给风险等级，再给证据。",
                        tool_prompts=[
                            AgentToolPrompt(tool_name="数据库数据模型", system_prompt="输出结构化表格摘要", user_prompt="{query}"),
                        ],
                    ),
                    test_config=AgentTestConfig(publish_status="已发布", last_saved_at=now),
                    last_updated=now,
                ),
            ],
            session_policies=[
                SessionPolicyItem(
                    policy_code="session_retention",
                    policy_name="会话留存策略",
                    retention_days=90,
                    rate_limit_qpm=120,
                    updated_at=now,
                ),
                SessionPolicyItem(
                    policy_code="sse_stream_limit",
                    policy_name="流式连接策略",
                    retention_days=30,
                    rate_limit_qpm=300,
                    updated_at=now,
                ),
            ],
            dashboard_cards=[
                DashboardCardConfig(
                    card_code="request_qps",
                    card_name="请求 QPS",
                    metric_expr="sum(rate(request_total[1m]))",
                    refresh_interval_s=30,
                ),
                DashboardCardConfig(
                    card_code="latency_p95",
                    card_name="P95 耗时",
                    metric_expr="histogram_quantile(0.95, request_duration_bucket)",
                    refresh_interval_s=30,
                ),
            ],
            security_policies=[
                SecurityPolicyItem(
                    rule_code="content_review",
                    rule_name="内容审查",
                    severity="high",
                    strategy="block",
                    enabled=True,
                    last_updated=now,
                ),
                SecurityPolicyItem(
                    rule_code="prompt_injection",
                    rule_name="Prompt 注入检测",
                    severity="medium",
                    strategy="alert",
                    enabled=True,
                    last_updated=now,
                ),
            ],
            glossary_terms=[
                GlossaryTermItem(
                    term="订单毛利率",
                    category="经营",
                    definition="订单收入减去订单成本后除以订单收入",
                    synonyms=["毛利率", "订单利润率"],
                    updated_at=now,
                ),
                GlossaryTermItem(
                    term="续约风险",
                    category="客户",
                    definition="客户在未来一个续约周期内流失的概率评估",
                    synonyms=["流失风险"],
                    updated_at=now,
                ),
            ],
            homepage_recommendations=[
                HomeRecommendationItem(
                    recommendation_id="rec_001",
                    title="近三月出库量最高的 5 个物料",
                    target_scene="经营分析",
                    priority=100,
                    updated_at=now,
                ),
                HomeRecommendationItem(
                    recommendation_id="rec_002",
                    title="本月新增合同额与回款率概览",
                    target_scene="销售运营",
                    priority=90,
                    updated_at=now,
                ),
            ],
            permission_rules=[
                PermissionRule(
                    role="admin",
                    allowed_agents=["Master", "Operations", "Marketing", "CustomerSuccess"],
                    allowed_operations=["chat", "config.read", "config.write", "release"],
                    department_codes=["ALL"],
                ),
                PermissionRule(
                    role="ops",
                    allowed_agents=["Master", "Operations"],
                    allowed_operations=["chat", "config.read"],
                    department_codes=["OPS"],
                ),
                PermissionRule(
                    role="marketing",
                    allowed_agents=["Master", "Marketing"],
                    allowed_operations=["chat", "config.read"],
                    department_codes=["MKT"],
                ),
            ],
            role_policies=[
                RolePolicy(
                    role_code="admin",
                    role_name="管理员",
                    permissions=["chat", "config.read", "config.write", "release"],
                    data_scope="all",
                    enabled=True,
                ),
                RolePolicy(
                    role_code="ops",
                    role_name="运营分析",
                    permissions=["chat", "config.read"],
                    data_scope="ops_domain",
                    enabled=True,
                ),
                RolePolicy(
                    role_code="marketing",
                    role_name="市场分析",
                    permissions=["chat", "config.read"],
                    data_scope="marketing_domain",
                    enabled=True,
                ),
            ],
            user_accounts=[
                UserAccount(
                    staff_code="A0001",
                    user_name="张涛",
                    department="数字经营部",
                    roles=["admin"],
                    status="enabled",
                    last_login=now,
                ),
                UserAccount(
                    staff_code="A0321",
                    user_name="李楠",
                    department="市场部",
                    roles=["marketing"],
                    status="enabled",
                    last_login=now,
                ),
                UserAccount(
                    staff_code="A0412",
                    user_name="王琳",
                    department="客户成功部",
                    roles=["ops"],
                    status="disabled",
                    last_login=now,
                ),
            ],
            knowledge_bindings=[
                KnowledgeBinding(
                    team="数字经营部",
                    kb_code="kb_ops_001",
                    kb_name="经营分析知识库",
                    kb_type="team",
                    embedding_model="bge-m3",
                    update_mode="hourly_sync",
                    readable_roles=["admin", "ops", "finance"],
                ),
                KnowledgeBinding(
                    team="市场部",
                    kb_code="kb_mkt_001",
                    kb_name="市场情报知识库",
                    kb_type="team",
                    embedding_model="bge-m3",
                    update_mode="daily_sync",
                    readable_roles=["admin", "marketing"],
                ),
            ],
            skill_policies=[
                SkillPolicy(skill_code="sql_explain", skill_name="SQL 解释与生成", skill_type="data", source="builtin"),
                SkillPolicy(skill_code="trend_detect", skill_name="趋势检测", skill_type="analysis", source="builtin"),
                SkillPolicy(skill_code="risk_alert", skill_name="风险告警", skill_type="governance", source="builtin"),
            ],
            release_history=[
                ReleaseRecord(
                    id="rel-20260524-001",
                    version="v1",
                    operator="system",
                    note="初始化 MAP 管理配置",
                    affected_agents=["Master", "Operations", "Marketing", "CustomerSuccess"],
                    risk_level="low",
                    created_at=now,
                )
            ],
        )
