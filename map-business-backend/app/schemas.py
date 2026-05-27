from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)
    history: list[dict[str, Any]] | None = None
    tool_context: dict[str, Any] | None = None
    flow_config: dict[str, Any] | None = None
    scene_selection: dict[str, Any] | None = None
    dispatch_config: dict[str, Any] | None = None


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
    model: str = "deepseek-v4-flash"
    temperature: float = 0.2
    max_tokens: int = 4096
    summarize_style: str = "结构化总结 + 关键结论优先"
    enabled: bool = True
    scene_selector_model: str = "deepseek-v4-flash"
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
    base_model: str = "deepseek-v4-flash"
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
    model: str = "deepseek-v4-flash"
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


class FlowScenarioPolicy(BaseModel):
    enabled: bool = True
    mode: str = "auto"
    allowed_scenarios: list[str] = Field(default_factory=list)
    allow_graph_repair: bool = True
    max_graph_cycles: int = 2


class FlowSkillPolicy(BaseModel):
    enabled: bool = True
    mount_mode: str = "agent_scoped"
    runtime_auth_check: bool = True


class FlowPolicyConfig(BaseModel):
    scenario_policy: FlowScenarioPolicy = Field(default_factory=FlowScenarioPolicy)
    skill_policy: FlowSkillPolicy = Field(default_factory=FlowSkillPolicy)
    max_node_budget: int = 12
    fallback_to_global: bool = True
    notes: str = ""


class ScenarioPackConfig(BaseModel):
    scenario_id: str
    display_name: str
    version: str = "1.0.0"
    domain: str
    description: str = ""
    trigger_intents: list[str] = Field(default_factory=list)
    required_agents: list[str] = Field(default_factory=list)
    optional_agents: list[str] = Field(default_factory=list)
    auth_scopes: list[str] = Field(default_factory=list)
    status: str = "active"


class FlowSkillDescriptor(BaseModel):
    skill_id: str
    name: str
    display_name: str
    version: str = "1.0.0"
    description: str = ""
    tool_name: str
    mount_agents: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=lambda: ["*"])
    allowed_tenants: list[str] = Field(default_factory=lambda: ["*"])
    allowed_scenarios: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: ["execute"])
    audit_tags: list[str] = Field(default_factory=list)
    status: str = "active"


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
    flow_policy: FlowPolicyConfig = Field(default_factory=FlowPolicyConfig)
    scenario_packs: list[ScenarioPackConfig] = Field(default_factory=list)
    flow_skill_descriptors: list[FlowSkillDescriptor] = Field(default_factory=list)
    release_history: list[ReleaseRecord]

    @staticmethod
    def default() -> "AdminState":
        now = datetime.now().isoformat()
        return AdminState(
            updated_at=now,
            model_center=ModelCenterConfig(
                large_models=[
                    ModelRecord(
                        model_name="deepseek-v4-flash",
                        model_type="远程",
                        model_url="https://api.deepseek.com",
                        is_default=True,
                        api_type="openai_compatible",
                    ),
                ],
                asr_models=[],
                tts_models=[],
                embedding_models=[],
                rerank_models=[],
            ),
            basic_settings=[
                BasicSettingItem(
                    setting_code="max_concurrency",
                    setting_name="系统并发上限",
                    setting_value="100",
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
            ],
            data_access_items=[],
            data_assets=[],
            master_agent=MasterAgentConfig(
                model="deepseek-v4-flash",
                scene_selector_model="deepseek-v4-flash",
                policies=[
                    "优先根据场景路由分发到业务智能体",
                    "无法命中业务智能体时由通用问答兜底",
                    "置信度不足时提示补充信息",
                ],
            ),
            business_agents=[
                BusinessAgentConfig(
                    agent_code="General_Assistant",
                    display_name="通用问答智能体",
                    scene_name="通用问答",
                    owner_team="平台团队",
                    data_scope="global",
                    description="用于基础问答、知识介绍与一般咨询。",
                    tools=["general_qa_agent"],
                    allowed_roles=["all"],
                    prompt_template=(
                        "你是通用问答助手，回答要准确、简洁、可读。"
                        "当问题涉及地理/历史知识时，优先给结构化要点。"
                    ),
                    prompt_config=AgentPromptConfig(
                        base_model="deepseek-v4-flash",
                        system_prompt=(
                            "你是企业通用问答助手，请直接回答用户问题。"
                            "如果信息不确定请明确说明不确定性。"
                        ),
                        user_prompt="{query}",
                        summary_prompt="请给出简洁总结与关键信息点。",
                        tool_prompts=[
                            AgentToolPrompt(
                                tool_name="general_qa_agent",
                                system_prompt="直接给出中文结论，必要时分点回答。",
                                user_prompt="{query}",
                            ),
                        ],
                        temperature=0.1,
                        max_tokens=4096,
                        current_version="v1",
                        version_note="初始化配置（真实环境）",
                        history_versions=[],
                    ),
                    test_config=AgentTestConfig(publish_status="已发布", last_saved_at=now),
                    last_updated=now,
                ),
            ],
            session_policies=[],
            dashboard_cards=[],
            security_policies=[],
            glossary_terms=[],
            homepage_recommendations=[],
            permission_rules=[],
            role_policies=[],
            user_accounts=[],
            knowledge_bindings=[],
            skill_policies=[],
            flow_policy=FlowPolicyConfig(
                scenario_policy=FlowScenarioPolicy(
                    enabled=True,
                    mode="auto",
                    allowed_scenarios=[],
                    allow_graph_repair=True,
                    max_graph_cycles=2,
                ),
                skill_policy=FlowSkillPolicy(
                    enabled=True,
                    mount_mode="agent_scoped",
                    runtime_auth_check=True,
                ),
                max_node_budget=12,
                fallback_to_global=True,
                notes="默认自动策略：场景自动匹配，失败可回退全域模式。",
            ),
            scenario_packs=[],
            flow_skill_descriptors=[],
            release_history=[],
        )
