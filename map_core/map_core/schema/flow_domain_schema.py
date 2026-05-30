from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .attachment_schema import AttachmentSchema
from .global_domain_schema import GlobalDomainChatSchema
from .tool_extra_result_schema import ToolExtraResultSchema


class ScenarioPolicyConfigSchema(BaseModel):
    enabled: bool = True
    mode: Literal["auto", "manual"] = "auto"
    allowed_scenarios: list[str] | None = None
    allow_graph_repair: bool = True
    max_graph_cycles: int = Field(default=2, ge=0, le=10)


class SkillPolicyConfigSchema(BaseModel):
    enabled: bool = True
    mount_mode: Literal["agent_scoped"] = "agent_scoped"
    runtime_auth_check: bool = True


class FlowConfigSchema(BaseModel):
    scenario_policy: ScenarioPolicyConfigSchema = Field(
        default_factory=ScenarioPolicyConfigSchema
    )
    skill_policy: SkillPolicyConfigSchema = Field(
        default_factory=SkillPolicyConfigSchema
    )
    max_node_budget: int = Field(default=12, ge=1, le=64)
    fallback_to_global: bool = True


class FlowChatRequest(GlobalDomainChatSchema):
    flow_config: FlowConfigSchema = Field(default_factory=FlowConfigSchema)


class ScenarioPackSchema(BaseModel):
    scenario_id: str
    display_name: str
    version: str = "1.0.0"
    domain: str
    description: str = ""
    trigger_intents: list[str] = Field(default_factory=list)
    required_agents: list[str] = Field(default_factory=list)
    optional_agents: list[str] = Field(default_factory=list)
    auth_scopes: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive"] = "active"


class SkillDescriptorSchema(BaseModel):
    skill_id: str
    name: str
    display_name: str
    version: str = "1.0.0"
    description: str = ""
    tool_name: str
    executor_type: str = "tool"
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    mount_agents: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=lambda: ["*"])
    allowed_tenants: list[str] = Field(default_factory=lambda: ["*"])
    allowed_scenarios: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: ["execute"])
    audit_tags: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive"] = "active"


class RepairCandidateSchema(BaseModel):
    action: str
    target_agent: str
    reason: str


class GraphNodeSchema(BaseModel):
    node_id: str
    scenario_id: str | None = None
    agent_code: str
    goal: str
    evidence_contract: list[str] = Field(default_factory=list)
    allowed_capabilities: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "passed", "failed", "uncertain", "skipped"] = (
        "pending"
    )
    depends_on: list[str] = Field(default_factory=list)


class GraphEdgeSchema(BaseModel):
    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")
    condition: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class BusinessExecutionGraphSchema(BaseModel):
    graph_id: str
    scenario_ids: list[str] = Field(default_factory=list)
    activated_hyperedges: list[str] = Field(default_factory=list)
    nodes: list[GraphNodeSchema] = Field(default_factory=list)
    edges: list[GraphEdgeSchema] = Field(default_factory=list)
    repair_candidates: list[RepairCandidateSchema] = Field(default_factory=list)


class NodeExecutionResultSchema(BaseModel):
    node_id: str
    agent_code: str
    executor_type: Literal["skill", "tool", "mixed"] = "tool"
    executor_names: list[str] = Field(default_factory=list)
    status: Literal["success", "failed", "uncertain", "denied"]
    content: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)


class StepVerdictSchema(BaseModel):
    node_id: str
    verdict: Literal["pass", "fail", "uncertain"]
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    matched_evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    repair_candidates: list[RepairCandidateSchema] = Field(default_factory=list)


class FlowDomainChatResponse(BaseModel):
    content: str
    attachment_results: list[AttachmentSchema] | None = None
    tool_extra_results: list[ToolExtraResultSchema] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class FlowDomainStreamEvent(BaseModel):
    event: Literal["start", "content_delta", "meta", "done", "error"]
    data: dict[str, Any]
