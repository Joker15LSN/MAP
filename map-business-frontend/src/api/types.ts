import type { RequestDetail } from 'map-tree-core';

export type ViewMode = 'chat' | 'backend';
export type ChatRole = 'user' | 'assistant';
export type ChatMode = 'global' | 'flow';

export type ModelTabKey =
  | 'large_models'
  | 'asr_models'
  | 'tts_models'
  | 'embedding_models'
  | 'rerank_models';

export type AdminPageKey =
  | 'model-center'
  | 'basic-settings'
  | 'address-config'
  | 'data-access'
  | 'data-assets'
  | 'mcp-server'
  | 'skills'
  | 'master-agent'
  | 'business-agent'
  | 'flow-policy'
  | 'scenario-hub'
  | 'skill-hub'
  | 'session-management'
  | 'dashboard'
  | 'security'
  | 'glossary'
  | 'home-recommendation'
  | 'permission'
  | 'user-role';

export type TracePanelMode = 'trace' | 'source' | 'flow';
export type AgentConfigTabKey = 'basic' | 'resource' | 'glossary' | 'prompt' | 'test';

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  agentNames?: string[];
}

export interface SourceReferenceItem {
  id: string;
  source: string;
  title: string;
  summary: string;
  date: string;
}

export interface ChatHistoryItem {
  id: string;
  question: string;
  answer: string;
  created_at: string;
  detail?: RequestDetail;
  sources: SourceReferenceItem[];
}

export interface ChatModeState {
  chatHistory: ChatHistoryItem[];
  activeHistoryId: string | null;
  messages: ChatMessage[];
  inputValue: string;
  detail?: RequestDetail;
}

export interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface AdminSummary {
  updated_at: string;
  master_version?: string;
  business_agent_count: number;
  business_agent_enabled_count: number;
  permission_rule_count: number;
  knowledge_binding_count: number;
  skill_enabled_count: number;
  release_count: number;
  model_count: number;
  user_count: number;
  user_enabled_count: number;
  mcp_server_count?: number;
  skill_count?: number;
}

export interface ModelRecord {
  model_name: string;
  model_type: string;
  model_url: string;
  is_default: boolean;
  api_type: string;
}

export interface ModelCenterConfig {
  large_models: ModelRecord[];
  asr_models: ModelRecord[];
  tts_models: ModelRecord[];
  embedding_models: ModelRecord[];
  rerank_models: ModelRecord[];
}

export interface BasicSettingItem {
  setting_code: string;
  setting_name: string;
  setting_value: string;
  category: string;
  description: string;
  editable: boolean;
}

export interface AddressConfigItem {
  address_code: string;
  address_name: string;
  base_url: string;
  timeout_s: number;
  enabled: boolean;
  remarks: string;
}

export interface DataAccessItem {
  source_name: string;
  source_type: string;
  auth_mode: string;
  endpoint: string;
  database_name: string;
  enabled: boolean;
  owner: string;
  last_sync?: string | null;
}

export interface DataAssetItem {
  asset_code: string;
  asset_name: string;
  asset_type: string;
  source_name: string;
  row_count: number;
  refresh_cycle: string;
  enabled: boolean;
  last_updated?: string | null;
}

export interface MasterAgentConfig {
  agent_code: string;
  display_name: string;
  model: string;
  temperature: number;
  max_tokens: number;
  summarize_style: string;
  scene_selector_model: string;
  route_model: string;
  summary_model: string;
  route_strategy: string;
  stream_version: string;
  timeout_s: number;
  route_prompt: string;
  summary_prompt: string;
  current_version: string;
  draft_version: string;
  prompt_versions: MasterPromptVersionItem[];
  policies: string[];
}

export interface MasterPromptVersionItem {
  version: string;
  created_at: string;
  operator: string;
  note: string;
  route_prompt: string;
  summary_prompt: string;
  route_model: string;
  summary_model: string;
  model: string;
  temperature: number;
  max_tokens: number;
}

export interface AgentMountedResourceItem {
  resource_name: string;
  resource_type: string;
  source_name: string;
  permission_scope: string;
  dimension_status: string;
  created_at?: string | null;
  enabled: boolean;
}

export interface AgentToolPromptItem {
  tool_name: string;
  system_prompt: string;
  user_prompt: string;
}

export interface AgentPromptVersionItem {
  version: string;
  updated_at: string;
  operator: string;
  model: string;
  temperature: number;
  max_tokens: number;
  version_note: string;
}

export interface AgentPromptConfig {
  base_model: string;
  system_prompt: string;
  user_prompt: string;
  tool_call_prompt: string;
  tool_internal_prompts: AgentToolInternalPromptItem[];
  summary_prompt: string;
  tool_prompts: AgentToolPromptItem[];
  temperature: number;
  max_tokens: number;
  current_version: string;
  version_note: string;
  history_versions: AgentPromptVersionItem[];
}

export interface AgentToolInternalPromptItem {
  tool_name: string;
  prompt: string;
  enabled: boolean;
}

export interface AgentResourceMountItem {
  mount_id: string;
  resource_type:
    | 'mcp_server'
    | 'mcp_tool'
    | 'skill'
    | 'knowledge_base'
    | 'data_model'
    | 'builtin_tool';
  resource_id: string;
  resource_name: string;
  source_name: string;
  enabled: boolean;
  include_all_tools: boolean;
  mcp_server_id?: string | null;
  mcp_tool_names: string[];
  skill_id?: string | null;
  kb_code?: string | null;
  data_model_code?: string | null;
  builtin_tool_name?: string | null;
  created_at?: string | null;
  config: Record<string, unknown>;
}

export interface AgentTestConfig {
  publish_status: string;
  last_saved_at?: string | null;
  draft_messages: Array<{ role: string; content: string }>;
}

export interface BusinessAgentConfig {
  agent_code: string;
  display_name: string;
  scene_name: string;
  owner_team: string;
  agent_type: string;
  model: string;
  enabled: boolean;
  weight: number;
  timeout_s: number;
  retry_limit: number;
  parallel_limit: number;
  data_scope: string;
  prompt_template: string;
  description: string;
  tools: string[];
  allowed_roles: string[];
  mounted_resources: AgentMountedResourceItem[];
  resource_mounts: AgentResourceMountItem[];
  glossary_terms: string[];
  prompt_config: AgentPromptConfig;
  test_config: AgentTestConfig;
  last_updated?: string | null;
}

export interface SessionPolicyItem {
  policy_code: string;
  policy_name: string;
  status: string;
  retention_days: number;
  rate_limit_qpm: number;
  updated_by: string;
  updated_at?: string | null;
}

export interface DashboardCardConfig {
  card_code: string;
  card_name: string;
  metric_expr: string;
  refresh_interval_s: number;
  enabled: boolean;
}

export interface SecurityPolicyItem {
  rule_code: string;
  rule_name: string;
  severity: string;
  strategy: string;
  enabled: boolean;
  last_updated?: string | null;
}

export interface GlossaryTermItem {
  term: string;
  category: string;
  definition: string;
  synonyms: string[];
  status: string;
  updated_at?: string | null;
}

export interface HomeRecommendationItem {
  recommendation_id: string;
  title: string;
  target_scene: string;
  priority: number;
  enabled: boolean;
  operator: string;
  updated_at?: string | null;
}

export interface PermissionRule {
  role: string;
  allowed_agents: string[];
  allowed_operations: string[];
  staff_codes: string[];
  department_codes: string[];
  active: boolean;
}

export interface RolePolicy {
  role_code: string;
  role_name: string;
  permissions: string[];
  data_scope: string;
  enabled: boolean;
}

export interface UserAccount {
  staff_code: string;
  user_name: string;
  department: string;
  roles: string[];
  status: string;
  last_login?: string | null;
}

export interface KnowledgeBinding {
  team: string;
  kb_code: string;
  kb_name: string;
  kb_type: string;
  embedding_model: string;
  update_mode: string;
  enabled: boolean;
  readable_roles: string[];
}

export interface SkillPolicy {
  skill_code: string;
  skill_name: string;
  skill_type: string;
  source: string;
  max_calls: number;
  timeout_s: number;
  enabled: boolean;
  visible_roles: string[];
}

export interface FlowScenarioPolicy {
  enabled: boolean;
  mode: string;
  allowed_scenarios: string[];
  allow_graph_repair: boolean;
  max_graph_cycles: number;
}

export interface FlowSkillPolicy {
  enabled: boolean;
  mount_mode: string;
  runtime_auth_check: boolean;
}

export interface FlowPolicyConfig {
  scenario_policy: FlowScenarioPolicy;
  skill_policy: FlowSkillPolicy;
  max_node_budget: number;
  fallback_to_global: boolean;
  notes: string;
}

export interface ScenarioPackConfig {
  scenario_id: string;
  display_name: string;
  version: string;
  domain: string;
  description: string;
  trigger_intents: string[];
  required_agents: string[];
  optional_agents: string[];
  auth_scopes: string[];
  status: string;
}

export interface FlowSkillDescriptor {
  skill_id: string;
  name: string;
  display_name: string;
  version: string;
  description: string;
  tool_name: string;
  executor_type?: string;
  content?: string;
  metadata?: Record<string, unknown>;
  mount_agents: string[];
  required_scopes: string[];
  allowed_users: string[];
  allowed_tenants: string[];
  allowed_scenarios: string[];
  allowed_actions: string[];
  audit_tags: string[];
  status: string;
}

export interface McpToolConfig {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  enabled: boolean;
  last_seen_at?: string | null;
}

export interface McpServerConfig {
  server_id: string;
  display_name: string;
  transport: 'stdio' | 'sse' | 'streamable_http';
  enabled: boolean;
  command: string;
  args: string[];
  url: string;
  headers: Record<string, string>;
  env_refs: Record<string, string>;
  timeout_s: number;
  tools: McpToolConfig[];
  status: string;
  last_refreshed_at?: string | null;
  remarks: string;
}

export interface UploadedSkill {
  skill_id: string;
  name: string;
  display_name: string;
  version: string;
  description: string;
  content: string;
  metadata: Record<string, unknown>;
  mount_agents: string[];
  status: string;
  source: string;
  uploaded_at: string;
  updated_at?: string | null;
}

export interface FlowRuntimeSnapshot {
  updated_at?: string;
  flow_policy: FlowPolicyConfig;
  scenario_packs: ScenarioPackConfig[];
  flow_skill_descriptors: FlowSkillDescriptor[];
  mcp_servers?: McpServerConfig[];
  skills?: UploadedSkill[];
}

export interface FlowRequestConfig {
  scenario_policy: FlowScenarioPolicy;
  skill_policy: FlowSkillPolicy;
  max_node_budget: number;
  fallback_to_global: boolean;
}

export interface ReleaseRecord {
  id: string;
  version: string;
  operator: string;
  note: string;
  affected_agents: string[];
  risk_level: string;
  created_at: string;
}

export interface AdminFullConfig {
  updated_at: string;
  model_center: ModelCenterConfig;
  basic_settings: BasicSettingItem[];
  address_configs: AddressConfigItem[];
  data_access_items: DataAccessItem[];
  data_assets: DataAssetItem[];
  master_agent: MasterAgentConfig;
  business_agents: BusinessAgentConfig[];
  session_policies: SessionPolicyItem[];
  dashboard_cards: DashboardCardConfig[];
  security_policies: SecurityPolicyItem[];
  glossary_terms: GlossaryTermItem[];
  homepage_recommendations: HomeRecommendationItem[];
  permission_rules: PermissionRule[];
  role_policies: RolePolicy[];
  user_accounts: UserAccount[];
  knowledge_bindings: KnowledgeBinding[];
  skill_policies: SkillPolicy[];
  flow_policy: FlowPolicyConfig;
  scenario_packs: ScenarioPackConfig[];
  flow_skill_descriptors: FlowSkillDescriptor[];
  mcp_servers: McpServerConfig[];
  skills: UploadedSkill[];
  release_history: ReleaseRecord[];
}
