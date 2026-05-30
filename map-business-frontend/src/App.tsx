import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  ConfigProvider,
  Drawer,
  Input,
  Modal,
  Select,
  Switch,
  Table,
  Tabs,
  Tag,
  carbonDarkTheme,
  carbonTheme,
} from '@agentscope-ai/design';
import type { ColumnsType } from 'antd/es/table';

import { RequestCallTree, type RequestDetail } from 'map-tree-core';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

type ViewMode = 'chat' | 'backend';
type ChatRole = 'user' | 'assistant';
type ChatMode = 'global' | 'flow';

type ModelTabKey = 'large_models' | 'asr_models' | 'tts_models' | 'embedding_models' | 'rerank_models';

type AdminPageKey =
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

type TracePanelMode = 'trace' | 'source' | 'flow';
type AgentConfigTabKey = 'basic' | 'resource' | 'glossary' | 'prompt' | 'test';

interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  agentNames?: string[];
}

interface SourceReferenceItem {
  id: string;
  source: string;
  title: string;
  summary: string;
  date: string;
}

interface ChatHistoryItem {
  id: string;
  question: string;
  answer: string;
  created_at: string;
  detail?: RequestDetail;
  sources: SourceReferenceItem[];
}

interface ChatModeState {
  chatHistory: ChatHistoryItem[];
  activeHistoryId: string | null;
  messages: ChatMessage[];
  inputValue: string;
  detail?: RequestDetail;
}

interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

interface AdminSummary {
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

interface ModelRecord {
  model_name: string;
  model_type: string;
  model_url: string;
  is_default: boolean;
  api_type: string;
}

interface ModelCenterConfig {
  large_models: ModelRecord[];
  asr_models: ModelRecord[];
  tts_models: ModelRecord[];
  embedding_models: ModelRecord[];
  rerank_models: ModelRecord[];
}

interface BasicSettingItem {
  setting_code: string;
  setting_name: string;
  setting_value: string;
  category: string;
  description: string;
  editable: boolean;
}

interface AddressConfigItem {
  address_code: string;
  address_name: string;
  base_url: string;
  timeout_s: number;
  enabled: boolean;
  remarks: string;
}

interface DataAccessItem {
  source_name: string;
  source_type: string;
  auth_mode: string;
  endpoint: string;
  database_name: string;
  enabled: boolean;
  owner: string;
  last_sync?: string | null;
}

interface DataAssetItem {
  asset_code: string;
  asset_name: string;
  asset_type: string;
  source_name: string;
  row_count: number;
  refresh_cycle: string;
  enabled: boolean;
  last_updated?: string | null;
}

interface MasterAgentConfig {
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

interface MasterPromptVersionItem {
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

interface AgentMountedResourceItem {
  resource_name: string;
  resource_type: string;
  source_name: string;
  permission_scope: string;
  dimension_status: string;
  created_at?: string | null;
  enabled: boolean;
}

interface AgentToolPromptItem {
  tool_name: string;
  system_prompt: string;
  user_prompt: string;
}

interface AgentPromptVersionItem {
  version: string;
  updated_at: string;
  operator: string;
  model: string;
  temperature: number;
  max_tokens: number;
  version_note: string;
}

interface AgentPromptConfig {
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

interface AgentToolInternalPromptItem {
  tool_name: string;
  prompt: string;
  enabled: boolean;
}

interface AgentResourceMountItem {
  mount_id: string;
  resource_type: 'mcp_server' | 'mcp_tool' | 'skill' | 'knowledge_base' | 'data_model' | 'builtin_tool';
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

interface AgentTestConfig {
  publish_status: string;
  last_saved_at?: string | null;
  draft_messages: Array<{ role: string; content: string }>;
}

interface BusinessAgentConfig {
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

interface SessionPolicyItem {
  policy_code: string;
  policy_name: string;
  status: string;
  retention_days: number;
  rate_limit_qpm: number;
  updated_by: string;
  updated_at?: string | null;
}

interface DashboardCardConfig {
  card_code: string;
  card_name: string;
  metric_expr: string;
  refresh_interval_s: number;
  enabled: boolean;
}

interface SecurityPolicyItem {
  rule_code: string;
  rule_name: string;
  severity: string;
  strategy: string;
  enabled: boolean;
  last_updated?: string | null;
}

interface GlossaryTermItem {
  term: string;
  category: string;
  definition: string;
  synonyms: string[];
  status: string;
  updated_at?: string | null;
}

interface HomeRecommendationItem {
  recommendation_id: string;
  title: string;
  target_scene: string;
  priority: number;
  enabled: boolean;
  operator: string;
  updated_at?: string | null;
}

interface PermissionRule {
  role: string;
  allowed_agents: string[];
  allowed_operations: string[];
  staff_codes: string[];
  department_codes: string[];
  active: boolean;
}

interface RolePolicy {
  role_code: string;
  role_name: string;
  permissions: string[];
  data_scope: string;
  enabled: boolean;
}

interface UserAccount {
  staff_code: string;
  user_name: string;
  department: string;
  roles: string[];
  status: string;
  last_login?: string | null;
}

interface KnowledgeBinding {
  team: string;
  kb_code: string;
  kb_name: string;
  kb_type: string;
  embedding_model: string;
  update_mode: string;
  enabled: boolean;
  readable_roles: string[];
}

interface SkillPolicy {
  skill_code: string;
  skill_name: string;
  skill_type: string;
  source: string;
  max_calls: number;
  timeout_s: number;
  enabled: boolean;
  visible_roles: string[];
}

interface FlowScenarioPolicy {
  enabled: boolean;
  mode: string;
  allowed_scenarios: string[];
  allow_graph_repair: boolean;
  max_graph_cycles: number;
}

interface FlowSkillPolicy {
  enabled: boolean;
  mount_mode: string;
  runtime_auth_check: boolean;
}

interface FlowPolicyConfig {
  scenario_policy: FlowScenarioPolicy;
  skill_policy: FlowSkillPolicy;
  max_node_budget: number;
  fallback_to_global: boolean;
  notes: string;
}

interface ScenarioPackConfig {
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

interface FlowSkillDescriptor {
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

interface McpToolConfig {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  enabled: boolean;
  last_seen_at?: string | null;
}

interface McpServerConfig {
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

interface UploadedSkill {
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

interface FlowRuntimeSnapshot {
  updated_at?: string;
  flow_policy: FlowPolicyConfig;
  scenario_packs: ScenarioPackConfig[];
  flow_skill_descriptors: FlowSkillDescriptor[];
  mcp_servers?: McpServerConfig[];
  skills?: UploadedSkill[];
}

interface FlowRequestConfig {
  scenario_policy: FlowScenarioPolicy;
  skill_policy: FlowSkillPolicy;
  max_node_budget: number;
  fallback_to_global: boolean;
}

interface ReleaseRecord {
  id: string;
  version: string;
  operator: string;
  note: string;
  affected_agents: string[];
  risk_level: string;
  created_at: string;
}

interface AdminFullConfig {
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

const QUICK_QUESTIONS = [
  '介绍一下中国杭州',
  '杭州有哪些代表性产业？',
  '杭州有哪些值得去的景点？',
  '杭州的历史文化特点是什么？',
  '杭州适合几月份旅游？',
  '请用 5 点总结杭州这座城市。',
];

const MODEL_OPTIONS = [
  { label: 'deepseek-v4-flash', value: 'deepseek-v4-flash' },
  { label: 'deepseek-v4-flash', value: 'deepseek-v4-flash' },
  { label: 'deepseek-chat', value: 'deepseek-chat' },
];

const ROUTE_STRATEGY_OPTIONS = [
  { label: 'scene_first', value: 'scene_first' },
  { label: 'master_only', value: 'master_only' },
  { label: 'hybrid', value: 'hybrid' },
];

const STREAM_VERSION_OPTIONS = [
  { label: 'v2', value: 'v2' },
  { label: 'v3', value: 'v3' },
];

const MODEL_TAB_MAP: Record<ModelTabKey, string> = {
  large_models: '大模型',
  asr_models: 'ASR',
  tts_models: 'TTS',
  embedding_models: 'Embedding',
  rerank_models: 'Rerank',
};

const ADMIN_PAGE_LABEL: Record<AdminPageKey, string> = {
  'model-center': '模型管理',
  'basic-settings': '基础设置',
  'address-config': '地址配置',
  'data-access': '数据接入',
  'data-assets': '数据管理',
  'mcp-server': 'MCP Server',
  skills: 'Skills',
  'master-agent': 'Master智能体',
  'business-agent': '业务智能体',
  'flow-policy': '心流策略',
  'scenario-hub': 'ScenarioHub',
  'skill-hub': 'SkillHub',
  'session-management': '会话管理',
  dashboard: '数据看板',
  security: '安全管理',
  glossary: '词库管理',
  'home-recommendation': '首页推荐',
  permission: '权限策略',
  'user-role': '角色与用户',
};

const CHAT_MODE_LABEL: Record<ChatMode, string> = {
  global: '全域模式',
  flow: '心流模式',
};

const FLOW_MODE_DEFAULT_CONFIG = {
  scenario_policy: {
    enabled: true,
    mode: 'auto',
    allowed_scenarios: [],
    allow_graph_repair: true,
    max_graph_cycles: 2,
  },
  skill_policy: {
    enabled: true,
    mount_mode: 'agent_scoped',
    runtime_auth_check: true,
  },
  max_node_budget: 12,
  fallback_to_global: true,
};

const toHistoryPayload = (messages: ChatMessage[]) =>
  messages.map((item) => ({
    role: item.role,
    content: item.content,
  }));

const nowIso = () => new Date().toISOString();
const THEME_STORAGE_KEY = 'map_theme_mode';

const sanitizeText = (value: unknown): string => {
  if (typeof value === 'string') {
    return value;
  }
  if (value === undefined || value === null) {
    return '';
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const dedupeStrings = (items: string[]): string[] => Array.from(new Set(items.filter(Boolean)));

const extractAgentNamesFromRows = (rows: Record<string, unknown>[]): string[] =>
  dedupeStrings(rows.map((row) => sanitizeText(row.agent_name) || sanitizeText(row.agent_code)).filter(Boolean));

const extractAgentNamesFromDetail = (requestDetail: RequestDetail | undefined): string[] => {
  if (!requestDetail?.agent_timeline?.length) {
    return [];
  }
  return dedupeStrings(
    requestDetail.agent_timeline
      .map((row) => sanitizeText(row?.agent_name) || sanitizeText(row?.agent_code))
      .filter(Boolean),
  );
};

const toPrettyJson = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '-';
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const parseListInput = (value: string): string[] =>
  value
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean);

const stringifyListInput = (items: string[]): string => items.join(', ');

const toFlowRequestConfig = (flowPolicy: FlowPolicyConfig | null | undefined): FlowRequestConfig => {
  if (!flowPolicy) {
    return {
      scenario_policy: {
        enabled: FLOW_MODE_DEFAULT_CONFIG.scenario_policy.enabled,
        mode: FLOW_MODE_DEFAULT_CONFIG.scenario_policy.mode,
        allowed_scenarios: [...FLOW_MODE_DEFAULT_CONFIG.scenario_policy.allowed_scenarios],
        allow_graph_repair: FLOW_MODE_DEFAULT_CONFIG.scenario_policy.allow_graph_repair,
        max_graph_cycles: FLOW_MODE_DEFAULT_CONFIG.scenario_policy.max_graph_cycles,
      },
      skill_policy: {
        enabled: FLOW_MODE_DEFAULT_CONFIG.skill_policy.enabled,
        mount_mode: FLOW_MODE_DEFAULT_CONFIG.skill_policy.mount_mode,
        runtime_auth_check: FLOW_MODE_DEFAULT_CONFIG.skill_policy.runtime_auth_check,
      },
      max_node_budget: FLOW_MODE_DEFAULT_CONFIG.max_node_budget,
      fallback_to_global: FLOW_MODE_DEFAULT_CONFIG.fallback_to_global,
    };
  }
  return {
    scenario_policy: {
      enabled: flowPolicy.scenario_policy.enabled,
      mode: flowPolicy.scenario_policy.mode,
      allowed_scenarios: flowPolicy.scenario_policy.allowed_scenarios || [],
      allow_graph_repair: flowPolicy.scenario_policy.allow_graph_repair,
      max_graph_cycles: flowPolicy.scenario_policy.max_graph_cycles,
    },
    skill_policy: {
      enabled: flowPolicy.skill_policy.enabled,
      mount_mode: flowPolicy.skill_policy.mount_mode,
      runtime_auth_check: flowPolicy.skill_policy.runtime_auth_check,
    },
    max_node_budget: flowPolicy.max_node_budget,
    fallback_to_global: flowPolicy.fallback_to_global,
  };
};

const parseSseFrames = (buffer: string): { events: SseEvent[]; remaining: string } => {
  const frames = buffer.split('\n\n');
  const remaining = frames.pop() ?? '';
  const events: SseEvent[] = [];

  for (const frame of frames) {
    const lines = frame.split('\n');
    const eventLine = lines.find((line) => line.startsWith('event:'));
    const dataLine = lines.find((line) => line.startsWith('data:'));
    if (!eventLine || !dataLine) {
      continue;
    }
    const eventName = eventLine.slice('event:'.length).trim();
    const payloadText = dataLine.slice('data:'.length).trim();
    try {
      const data = JSON.parse(payloadText) as Record<string, unknown>;
      events.push({ event: eventName, data });
    } catch {
      // ignore invalid frames
    }
  }

  return { events, remaining };
};

const createBaseDetail = (query: string): RequestDetail => ({
  request: {
    request_id: `pending-${Date.now()}`,
    query,
    status: 'running',
    duration_s: 0,
    token_total: 0,
  },
  agent_timeline: [],
  agent_events: [],
  tool_calls: [],
  summary: {
    agent_event_count: 0,
    tool_call_count: 0,
  },
});

const createEmptyChatModeState = (): ChatModeState => ({
  chatHistory: [],
  activeHistoryId: null,
  messages: [],
  inputValue: '',
  detail: undefined,
});

const createEmptyModelRecord = (tab: ModelTabKey): ModelRecord => {
  const suffix = Date.now();
  const defaultNameByTab: Record<ModelTabKey, string> = {
    large_models: `new-llm-${suffix}`,
    asr_models: `new-asr-${suffix}`,
    tts_models: `new-tts-${suffix}`,
    embedding_models: `new-embedding-${suffix}`,
    rerank_models: `new-rerank-${suffix}`,
  };
  return {
    model_name: defaultNameByTab[tab],
    model_type: '远程',
    model_url: 'https://api.deepseek.com',
    is_default: false,
    api_type: 'openai_compatible',
  };
};

const deriveSourcesFromDetail = (requestDetail: RequestDetail | undefined, answer: string): SourceReferenceItem[] => {
  if (!requestDetail) {
    return [];
  }

  const calls = (requestDetail.tool_calls || []) as Array<Record<string, unknown>>;
  const items = calls
    .filter((row) => sanitizeText(row.output))
    .slice(0, 6)
    .map((row, index) => {
      const source = sanitizeText(row.tool) || '通用助手';
      const summary = sanitizeText(row.output).slice(0, 160);
      return {
        id: `src-${index}-${sanitizeText(row.tool_id) || Date.now()}`,
        source,
        title: `${source}返回结果`,
        summary: summary || answer.slice(0, 120),
        date: new Date().toISOString().slice(0, 10),
      };
    });

  if (items.length > 0) {
    return items;
  }

  return [
    {
      id: `src-fallback-${Date.now()}`,
      source: '通用助手',
      title: '回答来源摘要',
      summary: answer.slice(0, 160) || '当前回答未产出可追踪来源。',
      date: new Date().toISOString().slice(0, 10),
    },
  ];
};

const createEmptyBusinessAgent = (): BusinessAgentConfig => ({
  agent_code: `Agent_${Date.now()}`,
  display_name: '新业务智能体',
  scene_name: '未命名场景',
  owner_team: '业务团队',
  agent_type: 'business',
  model: MODEL_OPTIONS[0].value,
  enabled: true,
  weight: 100,
  timeout_s: 120,
  retry_limit: 1,
  parallel_limit: 3,
  data_scope: 'team',
  prompt_template: '你是业务智能体，请基于配置的工具与数据资源回答问题。',
  description: '',
  tools: ['general_qa_agent'],
  allowed_roles: ['all'],
  mounted_resources: [],
  resource_mounts: [],
  glossary_terms: [],
  prompt_config: {
    base_model: MODEL_OPTIONS[0].value,
    system_prompt: '你是业务智能体，请先给结论，再给证据。',
    user_prompt: '{query}',
    tool_call_prompt: '请根据用户问题选择合适的 tool 或 skill，并说明必要的数据依据。',
    tool_internal_prompts: [{ tool_name: 'general_qa_agent', prompt: '直接回答问题。', enabled: true }],
    summary_prompt: '请输出 TL;DR 与关键指标。',
    tool_prompts: [{ tool_name: 'general_qa_agent', system_prompt: '直接回答问题。', user_prompt: '{query}' }],
    temperature: 0.1,
    max_tokens: 4096,
    current_version: 'v1',
    version_note: '初始化版本',
    history_versions: [],
  },
  test_config: {
    publish_status: '未发布',
    last_saved_at: null,
    draft_messages: [],
  },
  last_updated: null,
});

const normalizeBusinessAgent = (agent: BusinessAgentConfig): BusinessAgentConfig => {
  const toolPromptFallback = (agent.tools || []).map((tool) => ({
    tool_name: tool,
    system_prompt: '',
    user_prompt: '{query}',
  }));
  return {
    ...agent,
    tools: agent.tools || [],
    allowed_roles: agent.allowed_roles || ['all'],
    mounted_resources: agent.mounted_resources || [],
    resource_mounts: agent.resource_mounts || [],
    glossary_terms: agent.glossary_terms || [],
    prompt_config: {
      base_model: agent.prompt_config?.base_model || agent.model || MODEL_OPTIONS[0].value,
      system_prompt: agent.prompt_config?.system_prompt || '',
      user_prompt: agent.prompt_config?.user_prompt || '{query}',
      tool_call_prompt: agent.prompt_config?.tool_call_prompt || agent.prompt_config?.system_prompt || '',
      tool_internal_prompts: agent.prompt_config?.tool_internal_prompts || [],
      summary_prompt: agent.prompt_config?.summary_prompt || '',
      tool_prompts: agent.prompt_config?.tool_prompts?.length ? agent.prompt_config.tool_prompts : toolPromptFallback,
      temperature: agent.prompt_config?.temperature ?? 0.1,
      max_tokens: agent.prompt_config?.max_tokens ?? 4096,
      current_version: agent.prompt_config?.current_version || 'v1',
      version_note: agent.prompt_config?.version_note || '',
      history_versions: agent.prompt_config?.history_versions || [],
    },
    test_config: {
      publish_status: agent.test_config?.publish_status || '未发布',
      last_saved_at: agent.test_config?.last_saved_at || null,
      draft_messages: agent.test_config?.draft_messages || [],
    },
  };
};

const normalizeFlowSkillDescriptor = (item: FlowSkillDescriptor): FlowSkillDescriptor => ({
  ...item,
  mount_agents: item.mount_agents || [],
  required_scopes: item.required_scopes || [],
  allowed_users: item.allowed_users || ['*'],
  allowed_tenants: item.allowed_tenants || ['*'],
  allowed_scenarios: item.allowed_scenarios || [],
  allowed_actions: item.allowed_actions || ['execute'],
  audit_tags: item.audit_tags || [],
});

const cloneFlowRequestConfig = (config: FlowRequestConfig): FlowRequestConfig => ({
  scenario_policy: {
    enabled: config.scenario_policy.enabled,
    mode: config.scenario_policy.mode,
    allowed_scenarios: [...(config.scenario_policy.allowed_scenarios || [])],
    allow_graph_repair: config.scenario_policy.allow_graph_repair,
    max_graph_cycles: config.scenario_policy.max_graph_cycles,
  },
  skill_policy: {
    enabled: config.skill_policy.enabled,
    mount_mode: config.skill_policy.mount_mode,
    runtime_auth_check: config.skill_policy.runtime_auth_check,
  },
  max_node_budget: config.max_node_budget,
  fallback_to_global: config.fallback_to_global,
});

const App = () => {
  const [viewMode, setViewMode] = useState<ViewMode>('chat');
  const [adminPage, setAdminPage] = useState<AdminPageKey>('model-center');
  const [modelTab, setModelTab] = useState<ModelTabKey>('large_models');
  const [modelSearch, setModelSearch] = useState('');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [isDark, setIsDark] = useState<boolean>(() => {
    if (typeof window === 'undefined') {
      return false;
    }
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === 'dark') {
      return true;
    }
    if (stored === 'light') {
      return false;
    }
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  });
  const [tracePanelOpen, setTracePanelOpen] = useState(false);
  const [tracePanelMode, setTracePanelMode] = useState<TracePanelMode>('trace');
  const [chatMode, setChatMode] = useState<ChatMode>('global');
  const [chatModeState, setChatModeState] = useState<Record<ChatMode, ChatModeState>>({
    global: createEmptyChatModeState(),
    flow: createEmptyChatModeState(),
  });
  const [isStreaming, setIsStreaming] = useState(false);
  const detail = chatModeState[chatMode].detail;
  const messages = chatModeState[chatMode].messages;
  const inputValue = chatModeState[chatMode].inputValue;
  const chatHistory = chatModeState[chatMode].chatHistory;
  const activeHistoryId = chatModeState[chatMode].activeHistoryId;

  const setChatHistory = (updater: ChatHistoryItem[] | ((current: ChatHistoryItem[]) => ChatHistoryItem[])) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextChatHistory = typeof updater === 'function' ? updater(modeState.chatHistory) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          chatHistory: nextChatHistory,
        },
      };
    });
  };

  const setActiveHistoryId = (updater: string | null | ((current: string | null) => string | null)) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextActiveHistoryId = typeof updater === 'function' ? updater(modeState.activeHistoryId) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          activeHistoryId: nextActiveHistoryId,
        },
      };
    });
  };

  const setMessages = (updater: ChatMessage[] | ((current: ChatMessage[]) => ChatMessage[])) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextMessages = typeof updater === 'function' ? updater(modeState.messages) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          messages: nextMessages,
        },
      };
    });
  };

  const setInputValue = (updater: string | ((current: string) => string)) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextInputValue = typeof updater === 'function' ? updater(modeState.inputValue) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          inputValue: nextInputValue,
        },
      };
    });
  };

  const setDetail = (
    updater: RequestDetail | undefined | ((current: RequestDetail | undefined) => RequestDetail | undefined),
  ) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextDetail = typeof updater === 'function' ? updater(modeState.detail) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          detail: nextDetail,
        },
      };
    });
  };

  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState('');
  const [adminSummary, setAdminSummary] = useState<AdminSummary | null>(null);
  const [masterConfig, setMasterConfig] = useState<MasterAgentConfig | null>(null);
  const [businessAgents, setBusinessAgents] = useState<BusinessAgentConfig[]>([]);
  const [modelCenter, setModelCenter] = useState<ModelCenterConfig | null>(null);
  const [basicSettings, setBasicSettings] = useState<BasicSettingItem[]>([]);
  const [addressConfigs, setAddressConfigs] = useState<AddressConfigItem[]>([]);
  const [dataAccessItems, setDataAccessItems] = useState<DataAccessItem[]>([]);
  const [dataAssets, setDataAssets] = useState<DataAssetItem[]>([]);
  const [sessionPolicies, setSessionPolicies] = useState<SessionPolicyItem[]>([]);
  const [dashboardCards, setDashboardCards] = useState<DashboardCardConfig[]>([]);
  const [securityPolicies, setSecurityPolicies] = useState<SecurityPolicyItem[]>([]);
  const [glossaryTerms, setGlossaryTerms] = useState<GlossaryTermItem[]>([]);
  const [homepageRecommendations, setHomepageRecommendations] = useState<HomeRecommendationItem[]>([]);
  const [permissionRules, setPermissionRules] = useState<PermissionRule[]>([]);
  const [rolePolicies, setRolePolicies] = useState<RolePolicy[]>([]);
  const [userAccounts, setUserAccounts] = useState<UserAccount[]>([]);
  const [knowledgeBindings, setKnowledgeBindings] = useState<KnowledgeBinding[]>([]);
  const [skillPolicies, setSkillPolicies] = useState<SkillPolicy[]>([]);
  const [flowPolicy, setFlowPolicy] = useState<FlowPolicyConfig | null>(null);
  const [flowRuntimeSnapshot, setFlowRuntimeSnapshot] = useState<FlowRuntimeSnapshot | null>(null);
  const [flowUseRuntimePolicy, setFlowUseRuntimePolicy] = useState(true);
  const [flowOverrideOpen, setFlowOverrideOpen] = useState(false);
  const [flowSessionOverride, setFlowSessionOverride] = useState<FlowRequestConfig>(FLOW_MODE_DEFAULT_CONFIG);
  const [scenarioPacks, setScenarioPacks] = useState<ScenarioPackConfig[]>([]);
  const [flowSkillDescriptors, setFlowSkillDescriptors] = useState<FlowSkillDescriptor[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerConfig[]>([]);
  const [uploadedSkills, setUploadedSkills] = useState<UploadedSkill[]>([]);
  const [masterVersions, setMasterVersions] = useState<MasterPromptVersionItem[]>([]);
  const [masterDiff, setMasterDiff] = useState('');
  const [releaseHistory, setReleaseHistory] = useState<ReleaseRecord[]>([]);
  const [expandedInputOpen, setExpandedInputOpen] = useState(false);
  const [expandedInputValue, setExpandedInputValue] = useState('');
  const [agentTestInput, setAgentTestInput] = useState('');
  const [agentTestMessages, setAgentTestMessages] = useState<Array<{ role: ChatRole; content: string }>>([]);
  const [agentTestLoading, setAgentTestLoading] = useState(false);

  const [releaseNote, setReleaseNote] = useState('');
  const [releaseVersion, setReleaseVersion] = useState('v1');
  const [releaseRiskLevel, setReleaseRiskLevel] = useState('low');
  const [saveStatus, setSaveStatus] = useState('');

  const [editingAgent, setEditingAgent] = useState<BusinessAgentConfig | null>(null);
  const [editingAgentOpen, setEditingAgentOpen] = useState(false);
  const [agentConfigTab, setAgentConfigTab] = useState<AgentConfigTabKey>('basic');
  const streamAbortRef = useRef<AbortController | null>(null);
  const chatMessageListRef = useRef<HTMLDivElement | null>(null);

  const latestAssistantContent = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'assistant') {
        return messages[i].content;
      }
    }
    return '';
  }, [messages]);

  const activeHistoryItem = useMemo(
    () => chatHistory.find((item) => item.id === activeHistoryId) || null,
    [chatHistory, activeHistoryId],
  );

  const traceSourceItems = useMemo(() => {
    if (activeHistoryItem?.sources?.length) {
      return activeHistoryItem.sources;
    }
    return deriveSourcesFromDetail(detail, latestAssistantContent);
  }, [activeHistoryItem, detail, latestAssistantContent]);

  const flowHitData = useMemo(() => {
    const requestRecord = (detail?.request || {}) as unknown as Record<string, unknown>;
    const flowConfig = requestRecord.flow_config;
    const flowSnapshot = requestRecord.flow_snapshot;
    const flowPolicyHit = requestRecord.flow_policy_hit;
    const matchedScenarios = Array.isArray(requestRecord.matched_scenarios)
      ? (requestRecord.matched_scenarios as Record<string, unknown>[])
      : [];
    const fallbackReason = sanitizeText(requestRecord.fallback_reason);
    const flowGraph = requestRecord.flow_graph;
    const skillAuthorization = Array.isArray(requestRecord.flow_skill_authorization)
      ? (requestRecord.flow_skill_authorization as Record<string, unknown>[])
      : [];
    const nodeResults = Array.isArray(requestRecord.flow_node_results)
      ? (requestRecord.flow_node_results as Record<string, unknown>[])
      : [];
    const stepVerdicts = Array.isArray(requestRecord.flow_step_verdicts)
      ? (requestRecord.flow_step_verdicts as Record<string, unknown>[])
      : [];
    const repairEvents = Array.isArray(requestRecord.flow_repair_events)
      ? (requestRecord.flow_repair_events as Record<string, unknown>[])
      : [];
    const flowDoneMeta = requestRecord.flow_done_meta;

    const hasData =
      Boolean(flowConfig) ||
      Boolean(flowSnapshot) ||
      Boolean(flowPolicyHit) ||
      Boolean(flowGraph) ||
      Boolean(flowDoneMeta) ||
      matchedScenarios.length > 0 ||
      Boolean(fallbackReason) ||
      skillAuthorization.length > 0 ||
      nodeResults.length > 0 ||
      stepVerdicts.length > 0 ||
      repairEvents.length > 0;

    if (!hasData) {
      return null;
    }

    return {
      flowConfig,
      flowSnapshot,
      flowPolicyHit,
      matchedScenarios,
      fallbackReason,
      flowGraph,
      skillAuthorization,
      nodeResults,
      stepVerdicts,
      repairEvents,
      flowDoneMeta,
    };
  }, [detail]);

  const filteredModels = useMemo(() => {
    if (!modelCenter) {
      return [];
    }
    const rows = modelCenter[modelTab] || [];
    const keyword = modelSearch.trim().toLowerCase();
    if (!keyword) {
      return rows;
    }
    return rows.filter((row) =>
      [row.model_name, row.model_type, row.model_url, row.api_type].some((item) => item.toLowerCase().includes(keyword)),
    );
  }, [modelCenter, modelTab, modelSearch]);

  const runtimeFlowRequestConfig = useMemo(
    () => toFlowRequestConfig(flowRuntimeSnapshot?.flow_policy || flowPolicy),
    [flowRuntimeSnapshot, flowPolicy],
  );

  const effectiveFlowRequestConfig = useMemo(
    () => (flowUseRuntimePolicy ? runtimeFlowRequestConfig : flowSessionOverride),
    [flowUseRuntimePolicy, runtimeFlowRequestConfig, flowSessionOverride],
  );

  useEffect(() => {
    if (!flowUseRuntimePolicy) {
      return;
    }
    setFlowSessionOverride(cloneFlowRequestConfig(runtimeFlowRequestConfig));
  }, [flowUseRuntimePolicy, runtimeFlowRequestConfig]);

  useEffect(() => {
    const holder = chatMessageListRef.current;
    if (!holder) {
      return;
    }
    const behavior: ScrollBehavior = isStreaming ? 'auto' : 'smooth';
    const raf = window.requestAnimationFrame(() => {
      holder.scrollTo({
        top: holder.scrollHeight,
        behavior,
      });
    });
    return () => window.cancelAnimationFrame(raf);
  }, [messages, isStreaming, chatMode, activeHistoryId]);

  const applyActionToDetail = (current: RequestDetail, actionRow: Record<string, unknown>): RequestDetail => {
    const next: RequestDetail = {
      ...current,
      agent_events: [...current.agent_events, actionRow],
    };

    const action = sanitizeText(actionRow.action);
    const payload = (actionRow.payload as Record<string, unknown>) || {};

    if (action === 'tool_call') {
      next.tool_calls = [
        ...next.tool_calls,
        {
          agent_code: actionRow.agent_code,
          agent_name: actionRow.agent_name,
          tool: payload.tool_name,
          tool_id: payload.tool_call_id,
          step: actionRow.step,
          args: payload.args,
          status: 'running',
          ts: nowIso(),
        },
      ];
    }

    if (action === 'tool_result') {
      const toolCallId = sanitizeText(payload.tool_call_id);
      const toolName = sanitizeText(payload.tool_name);
      const resultSummary = sanitizeText(actionRow.result_summary);
      next.tool_calls = next.tool_calls.map((row) => {
        const rowRecord = row as Record<string, unknown>;
        const sameId = sanitizeText(rowRecord.tool_id) === toolCallId;
        const sameTool = sanitizeText(rowRecord.tool) === toolName;
        if (!sameId || !sameTool) {
          return row;
        }
        return {
          ...rowRecord,
          output: resultSummary,
          status: sanitizeText(actionRow.status) || 'success',
          end_ts: nowIso(),
        };
      });
    }

    next.summary = {
      ...next.summary,
      agent_event_count: next.agent_events.length,
      tool_call_count: next.tool_calls.length,
    };

    return next;
  };

  const applyMetaEvent = (meta: Record<string, unknown>) => {
    const phase = sanitizeText(meta.phase);

    if (phase === 'agent_result') {
      const rows = Array.isArray(meta.agents) ? (meta.agents as Record<string, unknown>[]) : [];
      const agentNames = extractAgentNamesFromRows(rows);
      if (agentNames.length > 0) {
        setMessages((current) => {
          const cloned = [...current];
          const last = cloned[cloned.length - 1];
          if (!last || last.role !== 'assistant') {
            return current;
          }
          cloned[cloned.length - 1] = {
            ...last,
            agentNames: dedupeStrings([...(last.agentNames || []), ...agentNames]),
          };
          return cloned;
        });
      }
    }

    setDetail((current) => {
      if (!current) {
        return current;
      }

      const next: RequestDetail = {
        ...current,
      };

      if (phase === 'scene_selected' && meta.scene_result && typeof meta.scene_result === 'object') {
        next.request = {
          ...next.request,
          scene_result: meta.scene_result as Record<string, unknown>,
        };
      }

      if (phase === 'flow_mode_initialized') {
        next.request = {
          ...next.request,
          flow_config: meta.flow_config,
          flow_snapshot: meta.config_snapshot,
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'flow_policy_hit') {
        next.request = {
          ...next.request,
          flow_policy_hit: meta,
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'scenario_resolved') {
        next.request = {
          ...next.request,
          matched_scenarios: meta.matched_scenarios,
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'flow_graph_built') {
        next.request = {
          ...next.request,
          flow_graph: meta.graph,
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'flow_fallback') {
        next.request = {
          ...next.request,
          fallback_reason: meta.reason,
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'skill_authorization') {
        const existed = ((next.request as unknown as Record<string, unknown>).flow_skill_authorization as unknown[]) || [];
        next.request = {
          ...next.request,
          flow_skill_authorization: [
            ...existed,
            {
              node_id: meta.node_id,
              agent_code: meta.agent_code,
              authorized_skills: meta.authorized_skills,
              denied_skills: meta.denied_skills,
            },
          ],
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'flow_node_result') {
        const existed = ((next.request as unknown as Record<string, unknown>).flow_node_results as unknown[]) || [];
        next.request = {
          ...next.request,
          flow_node_results: [...existed, meta.node_result],
          flow_step_verdicts: [
            ...((((next.request as unknown as Record<string, unknown>).flow_step_verdicts as unknown[]) || [])),
            meta.step_verdict,
          ],
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'flow_repair_applied') {
        const existed = ((next.request as unknown as Record<string, unknown>).flow_repair_events as unknown[]) || [];
        next.request = {
          ...next.request,
          flow_repair_events: [
            ...existed,
            {
              candidate: meta.candidate,
              repair_node: meta.repair_node,
            },
          ],
        } as unknown as RequestDetail['request'];
      }

      if (phase === 'agent_action') {
        const rows = Array.isArray(meta.agents) ? (meta.agents as Record<string, unknown>[]) : [];
        let rolling = next;
        for (const row of rows) {
          rolling = applyActionToDetail(rolling, row);
        }
        return rolling;
      }

      if (phase === 'agent_result') {
        const rows = Array.isArray(meta.agents) ? (meta.agents as Record<string, unknown>[]) : [];
        next.agent_timeline = [
          ...next.agent_timeline,
          ...rows.map((row) => ({
            agent_code: row.agent_code,
            agent_name: row.agent_name,
            status: row.success ? 'success' : 'failed',
            duration_s: row.duration_s,
            ts: nowIso(),
            payload: row,
          })),
        ];
      }

      return next;
    });
  };

  const loadAdminData = async () => {
    setAdminLoading(true);
    setAdminError('');
    try {
      const [summaryResp, fullResp] = await Promise.all([
        fetch('/api/admin/summary'),
        fetch('/api/admin/full-config'),
      ]);

      if (!summaryResp.ok || !fullResp.ok) {
        throw new Error('管理端数据加载失败');
      }

      const summary = (await summaryResp.json()) as AdminSummary;
      const full = (await fullResp.json()) as AdminFullConfig;
      setAdminSummary(summary);
      setModelCenter(full.model_center);
      setBasicSettings(full.basic_settings || []);
      setAddressConfigs(full.address_configs || []);
      setDataAccessItems(full.data_access_items || []);
      setDataAssets(full.data_assets || []);
      setMasterConfig(full.master_agent || null);
      setBusinessAgents((full.business_agents || []).map((item) => normalizeBusinessAgent(item)));
      setSessionPolicies(full.session_policies || []);
      setDashboardCards(full.dashboard_cards || []);
      setSecurityPolicies(full.security_policies || []);
      setGlossaryTerms(full.glossary_terms || []);
      setHomepageRecommendations(full.homepage_recommendations || []);
      setPermissionRules(full.permission_rules || []);
      setRolePolicies(full.role_policies || []);
      setUserAccounts(full.user_accounts || []);
      setKnowledgeBindings(full.knowledge_bindings || []);
      setSkillPolicies(full.skill_policies || []);
      setFlowPolicy(
        full.flow_policy || {
          scenario_policy: {
            enabled: true,
            mode: 'auto',
            allowed_scenarios: [],
            allow_graph_repair: true,
            max_graph_cycles: 2,
          },
          skill_policy: {
            enabled: true,
            mount_mode: 'agent_scoped',
            runtime_auth_check: true,
          },
          max_node_budget: 12,
          fallback_to_global: true,
          notes: '',
        },
      );
      setFlowRuntimeSnapshot({
        updated_at: full.updated_at,
        flow_policy: full.flow_policy || {
          scenario_policy: {
            enabled: true,
            mode: 'auto',
            allowed_scenarios: [],
            allow_graph_repair: true,
            max_graph_cycles: 2,
          },
          skill_policy: {
            enabled: true,
            mount_mode: 'agent_scoped',
            runtime_auth_check: true,
          },
          max_node_budget: 12,
          fallback_to_global: true,
          notes: '',
        },
        scenario_packs: full.scenario_packs || [],
        flow_skill_descriptors: full.flow_skill_descriptors || [],
        mcp_servers: full.mcp_servers || [],
        skills: full.skills || [],
      });
      setFlowSessionOverride(
        cloneFlowRequestConfig(
          toFlowRequestConfig(
            full.flow_policy || {
              scenario_policy: {
                enabled: true,
                mode: 'auto',
                allowed_scenarios: [],
                allow_graph_repair: true,
                max_graph_cycles: 2,
              },
              skill_policy: {
                enabled: true,
                mount_mode: 'agent_scoped',
                runtime_auth_check: true,
              },
              max_node_budget: 12,
              fallback_to_global: true,
              notes: '',
            },
          ),
        ),
      );
      setScenarioPacks(full.scenario_packs || []);
      setFlowSkillDescriptors((full.flow_skill_descriptors || []).map((item) => normalizeFlowSkillDescriptor(item)));
      setMcpServers(full.mcp_servers || []);
      setUploadedSkills(full.skills || []);
      setMasterVersions(full.master_agent?.prompt_versions || []);
      setReleaseHistory(full.release_history || []);
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : '管理端数据加载失败');
    } finally {
      setAdminLoading(false);
    }
  };

  const loadFlowRuntimeSnapshot = async () => {
    try {
      const response = await fetch('/api/admin/flow-runtime-snapshot');
      if (!response.ok) {
        return;
      }
      const snapshot = (await response.json()) as FlowRuntimeSnapshot;
      setFlowRuntimeSnapshot(snapshot);
      if (!flowPolicy) {
        setFlowPolicy(snapshot.flow_policy || null);
      }
      if (!scenarioPacks.length) {
        setScenarioPacks(snapshot.scenario_packs || []);
      }
      if (!flowSkillDescriptors.length) {
        setFlowSkillDescriptors((snapshot.flow_skill_descriptors || []).map((item) => normalizeFlowSkillDescriptor(item)));
      }
      if (snapshot.mcp_servers) {
        setMcpServers(snapshot.mcp_servers);
      }
      if (snapshot.skills) {
        setUploadedSkills(snapshot.skills);
      }
    } catch {
      // keep local default config when snapshot is unavailable
    }
  };

  useEffect(() => {
    if (viewMode === 'backend') {
      void loadAdminData();
    }
  }, [viewMode]);

  useEffect(() => {
    void loadFlowRuntimeSnapshot();
  }, []);

  useEffect(() => {
    document.body.classList.toggle('theme-dark', isDark);
    window.localStorage.setItem(THEME_STORAGE_KEY, isDark ? 'dark' : 'light');
  }, [isDark]);

  const stopStreaming = () => {
    if (!streamAbortRef.current) {
      return;
    }
    streamAbortRef.current.abort();
    streamAbortRef.current = null;
    setIsStreaming(false);
    setDetail((current) => {
      if (!current) {
        return current;
      }
      return {
        ...current,
        request: {
          ...current.request,
          status: 'stopped',
        },
      };
    });
  };

  const handleSend = async (query: string) => {
    const trimmed = query.trim();
    if (!trimmed || isStreaming) {
      return;
    }
    const requestMode = chatMode;
    const streamEndpoint = requestMode === 'flow' ? '/api/chat/stream/flow/v1' : '/api/chat/stream/v2';
    const syncEndpoint = requestMode === 'flow' ? '/api/chat/flow/v1' : '/api/chat';
    const flowConfigForRequest =
      requestMode === 'flow'
        ? cloneFlowRequestConfig(flowUseRuntimePolicy ? runtimeFlowRequestConfig : effectiveFlowRequestConfig)
        : undefined;
    const requestPayload = {
      query: trimmed,
      history: toHistoryPayload(messages),
      ...(requestMode === 'flow' ? { flow_config: flowConfigForRequest } : {}),
    };

    const historyId = `h-${Date.now()}`;

    const userMessage: ChatMessage = {
      id: `u-${Date.now()}`,
      role: 'user',
      content: trimmed,
    };
    const assistantMessage: ChatMessage = {
      id: `a-${Date.now()}`,
      role: 'assistant',
      content: '',
      agentNames: [],
    };

    setChatHistory((prev) => [
      {
        id: historyId,
        question: trimmed,
        answer: '',
        created_at: new Date().toISOString(),
        sources: [],
      },
      ...prev,
    ]);
    setActiveHistoryId(historyId);
    setTracePanelOpen(false);
    setTracePanelMode('trace');
    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setInputValue('');
    setIsStreaming(true);
    setDetail(createBaseDetail(trimmed));
    const controller = new AbortController();
    streamAbortRef.current = controller;

    try {
      const response = await fetch(streamEndpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        signal: controller.signal,
        body: JSON.stringify(requestPayload),
      });

      if (!response.ok || !response.body) {
        throw new Error(`stream request failed: ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        sseBuffer += decoder.decode(value, { stream: true });
        const parsed = parseSseFrames(sseBuffer);
        sseBuffer = parsed.remaining;

        for (const frame of parsed.events) {
          if (frame.event === 'start') {
            setDetail((current) => {
              if (!current) {
                return current;
              }
              return {
                ...current,
                request: {
                  ...current.request,
                  request_id: sanitizeText(frame.data.request_id) || current.request.request_id,
                  state_id: sanitizeText(frame.data.state_id),
                },
              };
            });
          }

          if (frame.event === 'meta') {
            applyMetaEvent(frame.data);
          }

          if (frame.event === 'content_delta') {
            const delta = sanitizeText(frame.data.content);
            if (delta) {
              setMessages((current) => {
                const cloned = [...current];
                const last = cloned[cloned.length - 1];
                if (!last || last.role !== 'assistant') {
                  return current;
                }
                cloned[cloned.length - 1] = {
                  ...last,
                  content: `${last.content}${delta}`,
                };
                return cloned;
              });
              setChatHistory((current) =>
                current.map((item) =>
                  item.id === historyId
                    ? {
                        ...item,
                        answer: `${item.answer}${delta}`,
                      }
                    : item,
                ),
              );
            }
          }

          if (frame.event === 'done') {
            const finalContent = sanitizeText(frame.data.content);
            if (finalContent) {
              setMessages((current) => {
                const cloned = [...current];
                const last = cloned[cloned.length - 1];
                if (!last || last.role !== 'assistant') {
                  return current;
                }
                cloned[cloned.length - 1] = {
                  ...last,
                  content: finalContent,
                };
                return cloned;
              });
            }
            setDetail((current) => {
              if (!current) {
                return current;
              }
              let next: RequestDetail = {
                ...current,
                request: {
                  ...current.request,
                  status: 'success',
                },
              };
              if (requestMode === 'flow' && frame.data.meta && typeof frame.data.meta === 'object') {
                const doneMeta = frame.data.meta as Record<string, unknown>;
                next = {
                  ...next,
                  request: {
                    ...next.request,
                    flow_done_meta: doneMeta.flow,
                  } as unknown as RequestDetail['request'],
                };
              }
              setChatHistory((historyRows) =>
                historyRows.map((item) =>
                  item.id === historyId
                    ? {
                        ...item,
                        answer: finalContent || item.answer,
                        detail: next,
                        sources: deriveSourcesFromDetail(next, finalContent || item.answer),
                      }
                    : item,
                ),
              );
              return {
                ...next,
              };
            });
          }

          if (frame.event === 'error') {
            throw new Error(sanitizeText(frame.data.error) || 'stream error');
          }
        }
      }
    } catch (error) {
      const aborted = error instanceof Error && error.name === 'AbortError';
      if (aborted) {
        setChatHistory((historyRows) =>
          historyRows.map((item) =>
            item.id === historyId
              ? {
                  ...item,
                  detail: detail
                    ? {
                        ...detail,
                        request: {
                          ...detail.request,
                          status: 'stopped',
                        },
                      }
                    : item.detail,
                }
              : item,
          ),
        );
        return;
      }
      const syncResponse = await fetch(syncEndpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestPayload),
      });
      const syncPayload = (await syncResponse.json()) as { content?: string };
      const content = sanitizeText(syncPayload.content);
      setMessages((current) => {
        const cloned = [...current];
        const last = cloned[cloned.length - 1];
        if (!last || last.role !== 'assistant') {
          return current;
        }
        cloned[cloned.length - 1] = {
          ...last,
          content,
        };
        return cloned;
      });
      setDetail((current) => {
        if (!current) {
          return current;
        }
        const next = {
          ...current,
          request: {
            ...current.request,
            status: 'success',
          },
        };
        setChatHistory((historyRows) =>
          historyRows.map((item) =>
            item.id === historyId
              ? {
                  ...item,
                  answer: content,
                  detail: next,
                  sources: deriveSourcesFromDetail(next, content),
                }
              : item,
          ),
        );
        return {
          ...next,
        };
      });
    } finally {
      streamAbortRef.current = null;
      setIsStreaming(false);
    }
  };

  const updateModelRecord = (target: ModelRecord, patch: Partial<ModelRecord>) => {
    if (!modelCenter) {
      return;
    }
    setModelCenter((current) => {
      if (!current) {
        return current;
      }
      const rows = current[modelTab] || [];
      const index = rows.findIndex(
        (row) =>
          row.model_name === target.model_name &&
          row.model_url === target.model_url &&
          row.api_type === target.api_type,
      );
      if (index < 0) {
        return current;
      }
      const nextRows = [...rows];
      nextRows[index] = {
        ...nextRows[index],
        ...patch,
      };
      return {
        ...current,
        [modelTab]: nextRows,
      };
    });
  };

  const addModelRecord = () => {
    setModelCenter((current) => {
      if (!current) {
        return current;
      }
      return {
        ...current,
        [modelTab]: [...(current[modelTab] || []), createEmptyModelRecord(modelTab)],
      };
    });
  };

  const removeModelRecord = (target: ModelRecord) => {
    setModelCenter((current) => {
      if (!current) {
        return current;
      }
      const rows = current[modelTab] || [];
      const index = rows.findIndex(
        (row) =>
          row.model_name === target.model_name &&
          row.model_url === target.model_url &&
          row.api_type === target.api_type,
      );
      if (index < 0) {
        return current;
      }
      const nextRows = rows.filter((_, rowIndex) => rowIndex !== index);
      return {
        ...current,
        [modelTab]: nextRows,
      };
    });
  };

  const setDefaultModel = (target: ModelRecord, checked: boolean) => {
    setModelCenter((current) => {
      if (!current) {
        return current;
      }
      const nextRows = (current[modelTab] || []).map((row) => {
        const isCurrent =
          row.model_name === target.model_name &&
          row.model_url === target.model_url &&
          row.api_type === target.api_type;
        if (checked) {
          return { ...row, is_default: isCurrent };
        }
        if (!isCurrent) {
          return row;
        }
        return { ...row, is_default: false };
      });
      return {
        ...current,
        [modelTab]: nextRows,
      };
    });
  };

  const saveModelCenter = async () => {
    if (!modelCenter) {
      return;
    }
    await saveSection('/api/admin/model-center', modelCenter, '模型配置已保存', '模型配置保存失败');
  };

  const saveSection = async (url: string, body: unknown, successText: string, failText: string) => {
    setSaveStatus('保存中...');
    const response = await fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (response.ok) {
      setSaveStatus(successText);
      await loadAdminData();
    } else {
      setSaveStatus(failText);
    }
  };

  const saveMasterConfig = async () => {
    if (!masterConfig) {
      return;
    }
    await saveSection('/api/admin/master-agent', masterConfig, 'Master 配置已保存', 'Master 配置保存失败');
  };

  const saveEditingBusinessAgent = async () => {
    if (!editingAgent) {
      return;
    }
    const isNewAgent = !businessAgents.some((item) => item.agent_code === editingAgent.agent_code);
    const url = isNewAgent ? '/api/admin/business-agents' : `/api/admin/business-agents/${editingAgent.agent_code}`;
    const method = isNewAgent ? 'POST' : 'PUT';
    const response = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(editingAgent),
    });
    if (response.ok) {
      setEditingAgentOpen(false);
      setEditingAgent(null);
      setSaveStatus(isNewAgent ? '业务智能体已新增' : '业务智能体配置已保存');
      await loadAdminData();
    } else {
      setSaveStatus(isNewAgent ? '业务智能体新增失败' : '业务智能体配置保存失败');
    }
  };

  const savePermissionRules = async () => {
    await saveSection('/api/admin/permission-rules', permissionRules, '权限策略已保存', '权限策略保存失败');
  };

  const saveRolePolicies = async () => {
    await saveSection('/api/admin/role-policies', rolePolicies, '角色策略已保存', '角色策略保存失败');
  };

  const saveUserAccounts = async () => {
    await saveSection('/api/admin/user-accounts', userAccounts, '用户配置已保存', '用户配置保存失败');
  };

  const saveSkillPolicies = async () => {
    await saveSection('/api/admin/skill-policies', skillPolicies, 'Skill 策略已保存', 'Skill 策略保存失败');
  };

  const saveFlowPolicy = async () => {
    if (!flowPolicy) {
      return;
    }
    await saveSection('/api/admin/flow-policy', flowPolicy, '心流策略已保存', '心流策略保存失败');
  };

  const saveScenarioPacks = async () => {
    await saveSection('/api/admin/scenario-packs', scenarioPacks, 'ScenarioHub 配置已保存', 'ScenarioHub 配置保存失败');
  };

  const saveFlowSkillDescriptors = async () => {
    await saveSection(
      '/api/admin/flow-skill-descriptors',
      flowSkillDescriptors,
      'SkillHub 配置已保存',
      'SkillHub 配置保存失败',
    );
  };

  const saveMcpServers = async () => {
    await saveSection('/api/admin/mcp-servers', mcpServers, 'MCP Server 配置已保存', 'MCP Server 配置保存失败');
  };

  const saveUploadedSkills = async () => {
    await saveSection('/api/admin/skills', uploadedSkills, 'Skill 配置已保存', 'Skill 配置保存失败');
  };

  const publishMasterPrompt = async () => {
    const response = await fetch('/api/admin/master-agent/publish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operator: 'admin', note: releaseNote || 'Master 提示词发布', version: releaseVersion || undefined }),
    });
    if (response.ok) {
      const payload = await response.json();
      setMasterDiff(payload.diff || '');
      setReleaseNote('');
      setSaveStatus('Master 提示词已发布');
      await loadAdminData();
    } else {
      setSaveStatus('Master 提示词发布失败');
    }
  };

  const diffMasterPrompt = async (version?: string) => {
    const target = version || masterConfig?.current_version || 'current';
    const response = await fetch(`/api/admin/master-agent/diff?from=${encodeURIComponent(target)}&to=current`);
    if (response.ok) {
      const payload = await response.json();
      setMasterDiff(payload.diff || '');
    }
  };

  const rollbackMasterPrompt = async (version: string) => {
    const response = await fetch('/api/admin/master-agent/rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version, operator: 'admin', note: `切换到 ${version}` }),
    });
    if (response.ok) {
      setSaveStatus(`已切换到 Master ${version}`);
      await loadAdminData();
    } else {
      setSaveStatus(`切换 Master ${version} 失败`);
    }
  };

  const runAgentTest = async () => {
    if (!editingAgent || !agentTestInput.trim()) {
      return;
    }
    const question = agentTestInput.trim();
    setAgentTestInput('');
    setAgentTestMessages((prev) => [...prev, { role: 'user', content: question }]);
    setAgentTestLoading(true);
    try {
      const response = await fetch(`/api/admin/business-agents/${editingAgent.agent_code}/test-chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: question,
          history: agentTestMessages.map((item) => ({ role: item.role, content: item.content })),
          agent: editingAgent,
        }),
      });
      const payload = await response.json();
      const content = payload?.result?.content || payload?.result?.error || '测试无返回内容';
      setAgentTestMessages((prev) => [...prev, { role: 'assistant', content }]);
    } catch (error) {
      setAgentTestMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `测试失败：${error instanceof Error ? error.message : String(error)}` },
      ]);
    } finally {
      setAgentTestLoading(false);
    }
  };

  const publishConfigSnapshot = async () => {
    if (!releaseNote.trim()) {
      setSaveStatus('请输入发布说明');
      return;
    }
    const query = new URLSearchParams({
      note: releaseNote.trim(),
      operator: 'admin',
      version: releaseVersion,
      risk_level: releaseRiskLevel,
    }).toString();
    const response = await fetch(`/api/admin/release-history?${query}`, { method: 'POST' });
    if (response.ok) {
      setReleaseNote('');
      setSaveStatus('配置发布记录已新增');
      await loadAdminData();
    } else {
      setSaveStatus('发布记录新增失败');
    }
  };

  const businessColumns: ColumnsType<BusinessAgentConfig> = [
    {
      title: '智能体名称',
      dataIndex: 'display_name',
      key: 'display_name',
      width: 160,
    },
    { title: '编码', dataIndex: 'agent_code', key: 'agent_code', width: 140 },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      render: (value) => <span className="text-ellipsis-cell">{value || '-'}</span>,
    },
    { title: '模型', dataIndex: 'model', key: 'model', width: 160 },
    {
      title: '状态',
      key: 'enabled',
      width: 90,
      render: (_, row) => <Tag color={row.enabled ? 'green' : 'red'}>{row.enabled ? '启用' : '停用'}</Tag>,
    },
    {
      title: '最后发布时间',
      key: 'last_updated',
      width: 170,
      render: (_, row) => row.last_updated || '-',
    },
    {
      title: '操作',
      key: 'action',
      width: 88,
      render: (_, row) => (
        <Button
          type="link"
          onClick={() => {
            setEditingAgent(normalizeBusinessAgent({ ...row }));
            setEditingAgentOpen(true);
            setAgentConfigTab('basic');
          }}
        >
          配置
        </Button>
      ),
    },
  ];

  const renderChatView = () => {
    const hasTraceData = Boolean(
      detail && (detail.agent_events.length > 0 || detail.agent_timeline.length > 0 || detail.tool_calls.length > 0),
    );
    const hasFlowHitData = Boolean(flowHitData);
    const tracePanelTitle =
      tracePanelMode === 'trace' ? '问答溯源' : tracePanelMode === 'source' ? '回答来源' : 'Flow 策略命中';

    return (
      <div className={`map-chat-layout ${tracePanelOpen ? 'trace-open' : ''}`}>
        <Card className="map-chat-main" title={chatMode === 'flow' ? '心流智能协作' : '全域智能协作'}>
          <div className="chat-main-header">
            <div className="mode-switch-group">
              <Button
                size="small"
                type={chatMode === 'global' ? 'primary' : 'default'}
                disabled={isStreaming}
                onClick={() => setChatMode('global')}
              >
                全域模式
              </Button>
              <Button
                size="small"
                type={chatMode === 'flow' ? 'primary' : 'default'}
                disabled={isStreaming}
                onClick={() => setChatMode('flow')}
              >
                心流模式
              </Button>
            </div>
            <Tag className="mode-tag">{CHAT_MODE_LABEL[chatMode]}</Tag>
            <Tag className={`stream-state-tag ${isStreaming ? 'running' : 'ready'}`}>{isStreaming ? '思考中...' : '就绪'}</Tag>
            {chatMode === 'flow' ? (
              <div className="flow-mode-runtime-bar">
                <Tag>{flowUseRuntimePolicy ? '策略来源：管理端默认' : '策略来源：会话覆写'}</Tag>
                <Tag>
                  策略快照：
                  {flowRuntimeSnapshot?.updated_at ? flowRuntimeSnapshot.updated_at : '本地默认'}
                </Tag>
                <span className="flow-mode-runtime-switch-label">使用管理端默认</span>
                <Switch
                  size="small"
                  checked={flowUseRuntimePolicy}
                  disabled={isStreaming}
                  onChange={(checked) => {
                    setFlowUseRuntimePolicy(checked);
                    if (checked) {
                      setFlowSessionOverride(cloneFlowRequestConfig(runtimeFlowRequestConfig));
                    }
                  }}
                />
                <Button
                  size="small"
                  disabled={isStreaming}
                  onClick={() => setFlowOverrideOpen((value) => !value)}
                >
                  {flowOverrideOpen ? '收起覆写' : '会话覆写'}
                </Button>
              </div>
            ) : null}
          </div>
          {chatMode === 'flow' && flowOverrideOpen ? (
            <div className="flow-override-panel">
              <div className="flow-override-grid">
                <label className="switch-row">
                  <span>Scenario Policy 启用</span>
                  <Switch
                    checked={flowSessionOverride.scenario_policy.enabled}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(checked) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        scenario_policy: { ...current.scenario_policy, enabled: checked },
                      }))
                    }
                  />
                </label>
                <label>
                  <span>Scenario 模式</span>
                  <Select
                    value={flowSessionOverride.scenario_policy.mode}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    options={[
                      { label: 'auto', value: 'auto' },
                      { label: 'manual', value: 'manual' },
                    ]}
                    onChange={(value) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        scenario_policy: {
                          ...current.scenario_policy,
                          mode: value,
                        },
                      }))
                    }
                  />
                </label>
                <label className="switch-row">
                  <span>允许图修复</span>
                  <Switch
                    checked={flowSessionOverride.scenario_policy.allow_graph_repair}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(checked) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        scenario_policy: {
                          ...current.scenario_policy,
                          allow_graph_repair: checked,
                        },
                      }))
                    }
                  />
                </label>
                <label>
                  <span>最大修复轮次</span>
                  <Input
                    value={String(flowSessionOverride.scenario_policy.max_graph_cycles)}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(event) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        scenario_policy: {
                          ...current.scenario_policy,
                          max_graph_cycles: Number(event.target.value || 0),
                        },
                      }))
                    }
                  />
                </label>
                <label className="full-span">
                  <span>允许场景（逗号或换行分隔，留空表示自动匹配）</span>
                  <Input.TextArea
                    autoSize={{ minRows: 2, maxRows: 4 }}
                    value={stringifyListInput(flowSessionOverride.scenario_policy.allowed_scenarios || [])}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(event) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        scenario_policy: {
                          ...current.scenario_policy,
                          allowed_scenarios: parseListInput(event.target.value),
                        },
                      }))
                    }
                  />
                </label>
                <label className="switch-row">
                  <span>Skill Policy 启用</span>
                  <Switch
                    checked={flowSessionOverride.skill_policy.enabled}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(checked) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        skill_policy: { ...current.skill_policy, enabled: checked },
                      }))
                    }
                  />
                </label>
                <label>
                  <span>挂载模式</span>
                  <Select
                    value={flowSessionOverride.skill_policy.mount_mode}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    options={[{ label: 'agent_scoped', value: 'agent_scoped' }]}
                    onChange={(value) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        skill_policy: { ...current.skill_policy, mount_mode: value },
                      }))
                    }
                  />
                </label>
                <label className="switch-row">
                  <span>执行时二次鉴权</span>
                  <Switch
                    checked={flowSessionOverride.skill_policy.runtime_auth_check}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(checked) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        skill_policy: { ...current.skill_policy, runtime_auth_check: checked },
                      }))
                    }
                  />
                </label>
                <label>
                  <span>最大节点预算</span>
                  <Input
                    value={String(flowSessionOverride.max_node_budget)}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(event) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        max_node_budget: Number(event.target.value || 0),
                      }))
                    }
                  />
                </label>
                <label className="switch-row">
                  <span>编排失败回退全域链路</span>
                  <Switch
                    checked={flowSessionOverride.fallback_to_global}
                    disabled={flowUseRuntimePolicy || isStreaming}
                    onChange={(checked) =>
                      setFlowSessionOverride((current) => ({
                        ...current,
                        fallback_to_global: checked,
                      }))
                    }
                  />
                </label>
              </div>
              <div className="flow-override-actions">
                <Button
                  size="small"
                  disabled={isStreaming}
                  onClick={() => setFlowSessionOverride(cloneFlowRequestConfig(runtimeFlowRequestConfig))}
                >
                  重置为管理端默认
                </Button>
                <Button size="small" type="primary" disabled={isStreaming} onClick={() => setFlowUseRuntimePolicy(false)}>
                  使用会话覆写
                </Button>
              </div>
            </div>
          ) : null}
          <div className="chat-message-list" ref={chatMessageListRef}>
            {messages.length === 0 ? (
              <div className="empty-hint">
                <div>输入问题开始问答。</div>
                <div className="quick-list">
                  {QUICK_QUESTIONS.map((item) => (
                    <button key={item} className="quick-item" onClick={() => void handleSend(item)}>
                      {item}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
            {messages.map((item, index) => (
              <div key={item.id} className={`message-row ${item.role}`}>
                <div className="message-role">{item.role === 'user' ? '你' : 'MAP'}</div>
                <div className="message-content">
                  {item.role === 'assistant' ? (
                    <div className="message-markdown">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          a: ({ ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
                        }}
                      >
                        {item.content || (isStreaming ? '思考中...' : '')}
                      </ReactMarkdown>
                    </div>
                  ) : (
                    item.content
                  )}
                </div>
                {item.role === 'assistant' && (item.content || isStreaming) ? (
                  <div className="assistant-actions-row">
                    <Button
                      size="small"
                      onClick={() => {
                        setTracePanelMode('trace');
                        setTracePanelOpen(true);
                      }}
                      disabled={!hasTraceData}
                    >
                      思考过程
                    </Button>
                    <Button
                      size="small"
                      onClick={() => {
                        setTracePanelMode('source');
                        setTracePanelOpen(true);
                      }}
                      disabled={!hasTraceData && !traceSourceItems.length}
                    >
                      查看来源
                    </Button>
                    {chatMode === 'flow' ? (
                      <Button
                        size="small"
                        onClick={() => {
                          setTracePanelMode('flow');
                          setTracePanelOpen(true);
                        }}
                        disabled={!hasFlowHitData}
                      >
                        策略命中
                      </Button>
                    ) : null}
                    <Tag>
                      {(() => {
                        const agentNames =
                          item.agentNames && item.agentNames.length > 0
                            ? item.agentNames
                            : index === messages.length - 1
                              ? extractAgentNamesFromDetail(detail)
                              : [];
                        return `子智能体：${agentNames.length ? agentNames.join(' / ') : 'MasterAgent 任务调度'}`;
                      })()}
                    </Tag>
                    {index === messages.length - 1 ? <Tag>{detail?.request.status || (isStreaming ? 'running' : 'success')}</Tag> : null}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
          <div className="chat-input-row">
            <Input.TextArea
              value={inputValue}
              placeholder="输入业务问题，Enter 发送，Shift+Enter 换行"
              autoSize={{ minRows: 2, maxRows: 6 }}
              onChange={(event) => setInputValue(event.target.value)}
              onPressEnter={(event) => {
                if (!event.shiftKey) {
                  event.preventDefault();
                  void handleSend(inputValue);
                }
              }}
            />
            <div className="chat-input-actions">
              <Button
                onClick={() => {
                  setExpandedInputValue(inputValue);
                  setExpandedInputOpen(true);
                }}
              >
                扩展输入
              </Button>
              {isStreaming ? (
                <Button danger onClick={() => stopStreaming()}>
                  停止
                </Button>
              ) : null}
              <Button type="primary" loading={isStreaming} onClick={() => void handleSend(inputValue)}>
                发送
              </Button>
            </div>
          </div>
        </Card>

        <Modal
          title="扩展输入"
          open={expandedInputOpen}
          width={820}
          onCancel={() => setExpandedInputOpen(false)}
          onOk={() => {
            setInputValue(expandedInputValue);
            setExpandedInputOpen(false);
            void handleSend(expandedInputValue);
          }}
          okText="发送"
          cancelText="取消"
        >
          <Input.TextArea
            className="expanded-chat-input"
            value={expandedInputValue}
            autoSize={{ minRows: 12, maxRows: 18 }}
            placeholder="输入长文本。弹窗内 Enter 换行，Ctrl/Cmd+Enter 发送。"
            onChange={(event) => setExpandedInputValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                setInputValue(expandedInputValue);
                setExpandedInputOpen(false);
                void handleSend(expandedInputValue);
              }
            }}
          />
        </Modal>

        {tracePanelOpen ? (
          <aside className="map-trace-sidebar">
            <Card
              className="map-chat-tree"
              title={tracePanelTitle}
              extra={<Button type="text" onClick={() => setTracePanelOpen(false)}>收起</Button>}
            >
              {tracePanelMode === 'trace' ? (
                <>
                  <div className="tree-holder">
                    <RequestCallTree detail={detail} />
                  </div>
                  <div className="tree-footer">输出摘要：{latestAssistantContent ? latestAssistantContent.slice(0, 120) : '-'}</div>
                </>
              ) : tracePanelMode === 'source' ? (
                <div className="source-list">
                  {traceSourceItems.length === 0 ? <div className="empty-hint">暂无可展示来源。</div> : null}
                  {traceSourceItems.map((item) => (
                    <Card key={item.id} size="small" className="source-card">
                      <div className="source-card-title">{item.title}</div>
                      <div className="source-card-meta">
                        <span>{item.source}</span>
                        <span>{item.date}</span>
                      </div>
                      <div className="source-card-summary">{item.summary}</div>
                    </Card>
                  ))}
                </div>
              ) : (
                <div className="flow-hit-panel">
                  {!flowHitData ? <div className="empty-hint">暂无策略命中信息。</div> : null}
                  {flowHitData ? (
                    <>
                      <div className="flow-hit-kpis">
                        <Tag>命中场景: {flowHitData.matchedScenarios.length}</Tag>
                        <Tag>节点结果: {flowHitData.nodeResults.length}</Tag>
                        <Tag>鉴权记录: {flowHitData.skillAuthorization.length}</Tag>
                        <Tag>修复事件: {flowHitData.repairEvents.length}</Tag>
                      </div>
                      {flowHitData.fallbackReason ? (
                        <Alert
                          type="warning"
                          message={`回退原因：${flowHitData.fallbackReason}`}
                          className="flow-hit-alert"
                        />
                      ) : null}
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">运行时配置快照</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowSnapshot)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">本次生效策略</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowConfig)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">策略命中信息</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowPolicyHit)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">命中场景</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.matchedScenarios)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">Skill 鉴权结果</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.skillAuthorization)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">执行节点结果</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.nodeResults)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">步骤判定</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.stepVerdicts)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">修复轨迹</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.repairEvents)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">构建执行图</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowGraph)}</pre>
                      </div>
                      <div className="flow-hit-block">
                        <div className="flow-hit-title">Done 流程元数据</div>
                        <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowDoneMeta)}</pre>
                      </div>
                    </>
                  ) : null}
                </div>
              )}
            </Card>
          </aside>
        ) : null}
      </div>
    );
  };

  const renderModelCenterPage = () => (
    <Card
      loading={adminLoading}
      title="模型管理"
      extra={
        <div className="backend-toolbar">
          <Button onClick={addModelRecord}>添加模型</Button>
          <Button type="primary" onClick={() => void saveModelCenter()}>
            保存模型配置
          </Button>
          <Input
            value={modelSearch}
            placeholder="搜索模型名称..."
            onChange={(event) => setModelSearch(event.target.value)}
            style={{ width: 220 }}
          />
        </div>
      }
    >
      <Tabs
        activeKey={modelTab}
        onChange={(key) => setModelTab(key as ModelTabKey)}
        items={(Object.keys(MODEL_TAB_MAP) as ModelTabKey[]).map((key) => ({
          key,
          label: MODEL_TAB_MAP[key],
          children: (
            <Table
              pagination={false}
              rowKey={(row) => `${key}_${row.model_name}`}
              dataSource={filteredModels}
              columns={[
                {
                  title: '模型名称',
                  dataIndex: 'model_name',
                  key: 'model_name',
                  render: (_, row) => (
                    <Input
                      value={row.model_name}
                      onChange={(event) => updateModelRecord(row, { model_name: event.target.value })}
                    />
                  ),
                },
                {
                  title: '模型类型',
                  dataIndex: 'model_type',
                  key: 'model_type',
                  render: (_, row) => (
                    <Select
                      value={row.model_type}
                      options={[
                        { label: '远程', value: '远程' },
                        { label: '本地', value: '本地' },
                      ]}
                      onChange={(value) => updateModelRecord(row, { model_type: value })}
                    />
                  ),
                },
                {
                  title: '模型地址',
                  dataIndex: 'model_url',
                  key: 'model_url',
                  render: (_, row) => (
                    <Input
                      value={row.model_url}
                      onChange={(event) => updateModelRecord(row, { model_url: event.target.value })}
                    />
                  ),
                },
                {
                  title: '默认模型',
                  key: 'is_default',
                  render: (_, row) => <Switch checked={row.is_default} onChange={(checked) => setDefaultModel(row, checked)} />,
                },
                {
                  title: '接口类型',
                  dataIndex: 'api_type',
                  key: 'api_type',
                  render: (_, row) => (
                    <Input
                      value={row.api_type}
                      onChange={(event) => updateModelRecord(row, { api_type: event.target.value })}
                    />
                  ),
                },
                {
                  title: '操作',
                  key: 'action',
                  render: (_, row) => (
                    <div className="table-actions-inline">
                      <Button type="link" danger onClick={() => removeModelRecord(row)}>
                        删除
                      </Button>
                    </div>
                  ),
                },
              ]}
            />
          ),
        }))}
      />
    </Card>
  );

  const renderBasicSettingsPage = () => (
    <Card
      loading={adminLoading}
      title="基础设置"
      extra={
        <Button
          type="primary"
          onClick={() => void saveSection('/api/admin/basic-settings', basicSettings, '基础设置已保存', '基础设置保存失败')}
        >
          保存配置
        </Button>
      }
    >
      <Table
        rowKey="setting_code"
        pagination={false}
        dataSource={basicSettings}
        columns={[
          { title: '配置项', dataIndex: 'setting_name', key: 'setting_name' },
          { title: '分类', dataIndex: 'category', key: 'category' },
          {
            title: '配置值',
            key: 'setting_value',
            render: (_, row, index) => (
              <Input
                disabled={!row.editable}
                value={row.setting_value}
                onChange={(event) => {
                  const next = [...basicSettings];
                  next[index] = { ...next[index], setting_value: event.target.value };
                  setBasicSettings(next);
                }}
              />
            ),
          },
          { title: '说明', dataIndex: 'description', key: 'description' },
        ]}
      />
    </Card>
  );

  const renderAddressConfigPage = () => (
    <Card
      loading={adminLoading}
      title="地址配置"
      extra={
        <Button
          type="primary"
          onClick={() =>
            void saveSection('/api/admin/address-configs', addressConfigs, '地址配置已保存', '地址配置保存失败')
          }
        >
          保存地址
        </Button>
      }
    >
      <Table
        rowKey="address_code"
        pagination={false}
        dataSource={addressConfigs}
        columns={[
          { title: '地址编码', dataIndex: 'address_code', key: 'address_code' },
          { title: '地址名称', dataIndex: 'address_name', key: 'address_name' },
          { title: 'Base URL', dataIndex: 'base_url', key: 'base_url' },
          { title: '超时(s)', dataIndex: 'timeout_s', key: 'timeout_s' },
          {
            title: '状态',
            key: 'enabled',
            render: (_, row, index) => (
              <Switch
                checked={row.enabled}
                onChange={(checked) => {
                  const next = [...addressConfigs];
                  next[index] = { ...next[index], enabled: checked };
                  setAddressConfigs(next);
                }}
              />
            ),
          },
          { title: '备注', dataIndex: 'remarks', key: 'remarks' },
        ]}
      />
    </Card>
  );

  const renderDataAccessPage = () => (
    <Card
      loading={adminLoading}
      title="数据接入"
      extra={
        <Button
          type="primary"
          onClick={() =>
            void saveSection('/api/admin/data-connectors', dataAccessItems, '数据接入配置已保存', '数据接入配置保存失败')
          }
        >
          保存接入
        </Button>
      }
    >
      <Table
        rowKey="source_name"
        pagination={false}
        dataSource={dataAccessItems}
        columns={[
          { title: '数据源', dataIndex: 'source_name', key: 'source_name' },
          { title: '类型', dataIndex: 'source_type', key: 'source_type' },
          { title: '鉴权', dataIndex: 'auth_mode', key: 'auth_mode' },
          { title: 'Endpoint', dataIndex: 'endpoint', key: 'endpoint' },
          { title: '库名', dataIndex: 'database_name', key: 'database_name' },
          { title: '负责人', dataIndex: 'owner', key: 'owner' },
          { title: '最近同步', dataIndex: 'last_sync', key: 'last_sync' },
          {
            title: '启用',
            key: 'enabled',
            render: (_, row, index) => (
              <Switch
                checked={row.enabled}
                onChange={(checked) => {
                  const next = [...dataAccessItems];
                  next[index] = { ...next[index], enabled: checked };
                  setDataAccessItems(next);
                }}
              />
            ),
          },
        ]}
      />
    </Card>
  );

  const renderDataAssetsPage = () => (
    <Card
      loading={adminLoading}
      title="数据管理"
      extra={
        <Button
          type="primary"
          onClick={() => void saveSection('/api/admin/data-assets', dataAssets, '数据资产配置已保存', '数据资产配置保存失败')}
        >
          保存资产
        </Button>
      }
    >
      <Table
        rowKey="asset_code"
        pagination={false}
        dataSource={dataAssets}
        columns={[
          { title: '资产编码', dataIndex: 'asset_code', key: 'asset_code' },
          { title: '资产名称', dataIndex: 'asset_name', key: 'asset_name' },
          { title: '类型', dataIndex: 'asset_type', key: 'asset_type' },
          { title: '来源', dataIndex: 'source_name', key: 'source_name' },
          { title: '行数', dataIndex: 'row_count', key: 'row_count' },
          { title: '刷新周期', dataIndex: 'refresh_cycle', key: 'refresh_cycle' },
          {
            title: '启用',
            key: 'enabled',
            render: (_, row, index) => (
              <Switch
                checked={row.enabled}
                onChange={(checked) => {
                  const next = [...dataAssets];
                  next[index] = { ...next[index], enabled: checked };
                  setDataAssets(next);
                }}
              />
            ),
          },
          { title: '更新时间', dataIndex: 'last_updated', key: 'last_updated' },
        ]}
      />
    </Card>
  );

  const renderMcpServerPage = () => (
    <Card
      loading={adminLoading}
      title="MCP Server"
      extra={
        <div className="backend-toolbar">
          <Button
            onClick={() =>
              setMcpServers([
                {
                  server_id: `mcp-${Date.now()}`,
                  display_name: '新 MCP Server',
                  transport: 'stdio',
                  enabled: true,
                  command: '',
                  args: [],
                  url: '',
                  headers: {},
                  env_refs: {},
                  timeout_s: 30,
                  tools: [],
                  status: 'unknown',
                  remarks: '',
                },
                ...mcpServers,
              ])
            }
          >
            新增
          </Button>
          <Button type="primary" onClick={() => void saveMcpServers()}>
            保存
          </Button>
        </div>
      }
    >
      <Table
        className="wide-config-table"
        rowKey="server_id"
        pagination={false}
        scroll={{ x: 1280 }}
        dataSource={mcpServers}
        columns={[
          { title: 'Server ID', dataIndex: 'server_id', key: 'server_id', width: 180 },
          {
            title: '名称',
            key: 'display_name',
            width: 180,
            render: (_, row, index) => (
              <Input
                value={row.display_name}
                onChange={(event) => {
                  const next = [...mcpServers];
                  next[index] = { ...row, display_name: event.target.value };
                  setMcpServers(next);
                }}
              />
            ),
          },
          {
            title: 'Transport',
            key: 'transport',
            width: 150,
            render: (_, row, index) => (
              <Select
                value={row.transport}
                options={[
                  { label: 'stdio', value: 'stdio' },
                  { label: 'sse', value: 'sse' },
                  { label: 'streamable_http', value: 'streamable_http' },
                ]}
                onChange={(value) => {
                  const next = [...mcpServers];
                  next[index] = { ...row, transport: value };
                  setMcpServers(next);
                }}
              />
            ),
          },
          {
            title: '命令 / URL',
            key: 'endpoint',
            width: 260,
            render: (_, row, index) => (
              <Input
                value={row.transport === 'stdio' ? row.command : row.url}
                onChange={(event) => {
                  const next = [...mcpServers];
                  next[index] =
                    row.transport === 'stdio'
                      ? { ...row, command: event.target.value }
                      : { ...row, url: event.target.value };
                  setMcpServers(next);
                }}
              />
            ),
          },
          {
            title: 'Args',
            key: 'args',
            width: 220,
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.args)}
                onChange={(event) => {
                  const next = [...mcpServers];
                  next[index] = { ...row, args: parseListInput(event.target.value) };
                  setMcpServers(next);
                }}
              />
            ),
          },
          {
            title: 'Tools',
            key: 'tools',
            width: 260,
            render: (_, row) => (
              <div className="chip-wrap">
                {row.tools.map((tool) => (
                  <Tag key={tool.name}>{tool.name}</Tag>
                ))}
              </div>
            ),
          },
          { title: '状态', dataIndex: 'status', key: 'status', width: 160 },
          {
            title: '启用',
            key: 'enabled',
            width: 80,
            render: (_, row, index) => (
              <Switch
                checked={row.enabled}
                onChange={(checked) => {
                  const next = [...mcpServers];
                  next[index] = { ...row, enabled: checked };
                  setMcpServers(next);
                }}
              />
            ),
          },
        ]}
      />
    </Card>
  );

  const renderSkillsPage = () => (
    <Card
      loading={adminLoading}
      title="Skills"
      extra={
        <div className="backend-toolbar">
          <Button
            onClick={() =>
              setUploadedSkills([
                {
                  skill_id: `skill-${Date.now()}`,
                  name: 'new_skill',
                  display_name: '新 Skill',
                  version: '1.0.0',
                  description: '',
                  content: '# 新 Skill\n\n请在这里填写 skill 指令。',
                  metadata: {},
                  mount_agents: [],
                  status: 'active',
                  source: 'manual_upload',
                  uploaded_at: new Date().toISOString(),
                  updated_at: new Date().toISOString(),
                },
                ...uploadedSkills,
              ])
            }
          >
            新增
          </Button>
          <Button type="primary" onClick={() => void saveUploadedSkills()}>
            保存
          </Button>
        </div>
      }
    >
      <Table
        className="wide-config-table"
        rowKey="skill_id"
        pagination={false}
        scroll={{ x: 1280 }}
        dataSource={uploadedSkills}
        columns={[
          { title: 'Skill ID', dataIndex: 'skill_id', key: 'skill_id', width: 180 },
          {
            title: '展示名称',
            key: 'display_name',
            width: 180,
            render: (_, row, index) => (
              <Input
                value={row.display_name}
                onChange={(event) => {
                  const next = [...uploadedSkills];
                  next[index] = { ...row, display_name: event.target.value };
                  setUploadedSkills(next);
                }}
              />
            ),
          },
          {
            title: '挂载 Agent',
            key: 'mount_agents',
            width: 220,
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.mount_agents)}
                onChange={(event) => {
                  const next = [...uploadedSkills];
                  next[index] = { ...row, mount_agents: parseListInput(event.target.value) };
                  setUploadedSkills(next);
                }}
              />
            ),
          },
          {
            title: 'Skill 指令',
            key: 'content',
            render: (_, row, index) => (
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 6 }}
                value={row.content}
                onChange={(event) => {
                  const next = [...uploadedSkills];
                  next[index] = { ...row, content: event.target.value, updated_at: new Date().toISOString() };
                  setUploadedSkills(next);
                }}
              />
            ),
          },
          {
            title: '状态',
            key: 'status',
            width: 100,
            render: (_, row, index) => (
              <Switch
                checked={row.status === 'active'}
                onChange={(checked) => {
                  const next = [...uploadedSkills];
                  next[index] = { ...row, status: checked ? 'active' : 'inactive' };
                  setUploadedSkills(next);
                }}
              />
            ),
          },
        ]}
      />
    </Card>
  );

  const renderMasterAgentPage = () => (
    <Card
      loading={adminLoading}
      title="Master 智能体"
      extra={
        <Button type="primary" onClick={() => void saveMasterConfig()}>
          保存配置
        </Button>
      }
    >
      {masterConfig ? (
        <div className="form-grid">
          <label>
            <span>显示名称</span>
            <Input
              value={masterConfig.display_name}
              onChange={(event) => setMasterConfig({ ...masterConfig, display_name: event.target.value })}
            />
          </label>
          <label>
            <span>模型</span>
            <Select
              value={masterConfig.model}
              options={MODEL_OPTIONS}
              onChange={(value) => setMasterConfig({ ...masterConfig, model: value })}
            />
          </label>
          <label>
            <span>路由模型</span>
            <Select
              value={masterConfig.route_model || masterConfig.scene_selector_model}
              options={MODEL_OPTIONS}
              onChange={(value) => setMasterConfig({ ...masterConfig, route_model: value, scene_selector_model: value })}
            />
          </label>
          <label>
            <span>总结模型</span>
            <Select
              value={masterConfig.summary_model || masterConfig.model}
              options={MODEL_OPTIONS}
              onChange={(value) => setMasterConfig({ ...masterConfig, summary_model: value })}
            />
          </label>
          <label>
            <span>路由策略</span>
            <Select
              value={masterConfig.route_strategy}
              options={ROUTE_STRATEGY_OPTIONS}
              onChange={(value) => setMasterConfig({ ...masterConfig, route_strategy: value })}
            />
          </label>
          <label>
            <span>Temperature</span>
            <Input
              value={String(masterConfig.temperature)}
              onChange={(event) => setMasterConfig({ ...masterConfig, temperature: Number(event.target.value || 0) })}
            />
          </label>
          <label>
            <span>Max Tokens</span>
            <Input
              value={String(masterConfig.max_tokens)}
              onChange={(event) => setMasterConfig({ ...masterConfig, max_tokens: Number(event.target.value || 0) })}
            />
          </label>
          <label>
            <span>Stream 版本</span>
            <Select
              value={masterConfig.stream_version}
              options={STREAM_VERSION_OPTIONS}
              onChange={(value) => setMasterConfig({ ...masterConfig, stream_version: value })}
            />
          </label>
          <label>
            <span>超时(s)</span>
            <Input
              value={String(masterConfig.timeout_s)}
              onChange={(event) => setMasterConfig({ ...masterConfig, timeout_s: Number(event.target.value || 0) })}
            />
          </label>
          <label className="full-span">
            <span>总结策略</span>
            <Input
              value={masterConfig.summarize_style}
              onChange={(event) => setMasterConfig({ ...masterConfig, summarize_style: event.target.value })}
            />
          </label>
          <label className="full-span">
            <span>场景路由提示词</span>
            <Input.TextArea
              autoSize={{ minRows: 4, maxRows: 10 }}
              value={masterConfig.route_prompt}
              onChange={(event) => setMasterConfig({ ...masterConfig, route_prompt: event.target.value })}
            />
          </label>
          <label className="full-span">
            <span>总结提示词</span>
            <Input.TextArea
              autoSize={{ minRows: 4, maxRows: 10 }}
              value={masterConfig.summary_prompt}
              onChange={(event) => setMasterConfig({ ...masterConfig, summary_prompt: event.target.value })}
            />
          </label>
          <label className="full-span">
            <span>执行策略（每行一条）</span>
            <Input.TextArea
              autoSize={{ minRows: 4, maxRows: 8 }}
              value={masterConfig.policies.join('\n')}
              onChange={(event) =>
                setMasterConfig({
                  ...masterConfig,
                  policies: event.target.value
                    .split('\n')
                    .map((item) => item.trim())
                    .filter(Boolean),
                })
              }
            />
          </label>
          <div className="full-span detail-layout">
            <Card
              size="small"
              title={`提示词版本：${masterConfig.current_version || 'draft'}`}
              extra={
                <div className="backend-toolbar">
                  <Button onClick={() => void diffMasterPrompt(masterConfig.current_version)}>查看 Diff</Button>
                  <Button type="primary" onClick={() => void publishMasterPrompt()}>
                    发布提示词
                  </Button>
                </div>
              }
            >
              {masterDiff ? <pre className="raw-json master-diff-view">{masterDiff}</pre> : <div className="empty-hint">发布前可查看当前草稿与历史版本差异。</div>}
            </Card>
            <Table
              size="small"
              rowKey="version"
              pagination={false}
              dataSource={masterVersions}
              columns={[
                { title: '版本', dataIndex: 'version', key: 'version', width: 100 },
                { title: '操作人', dataIndex: 'operator', key: 'operator', width: 100 },
                { title: '说明', dataIndex: 'note', key: 'note' },
                { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 180 },
                {
                  title: '操作',
                  key: 'actions',
                  width: 220,
                  render: (_, row) => (
                    <div className="backend-toolbar">
                      <Button size="small" onClick={() => void diffMasterPrompt(row.version)}>
                        Diff 当前
                      </Button>
                      <Button size="small" onClick={() => void rollbackMasterPrompt(row.version)}>
                        切换
                      </Button>
                    </div>
                  ),
                },
              ]}
            />
          </div>
        </div>
      ) : null}
    </Card>
  );

  const renderBusinessAgentPage = () => (
    <Card
      loading={adminLoading}
      title="业务智能体"
      extra={
        <div className="backend-toolbar">
          <Button
            type="primary"
            onClick={() => {
              setEditingAgent(createEmptyBusinessAgent());
              setEditingAgentOpen(true);
              setAgentConfigTab('basic');
            }}
          >
            新增业务智能体
          </Button>
          <Button onClick={() => void loadAdminData()}>刷新</Button>
        </div>
      }
    >
      <Table rowKey="agent_code" dataSource={businessAgents} columns={businessColumns} pagination={false} />
    </Card>
  );

  const renderFlowPolicyPage = () => (
    <Card
      loading={adminLoading}
      title="心流策略"
      extra={
        <Button type="primary" onClick={() => void saveFlowPolicy()}>
          保存策略
        </Button>
      }
    >
      {flowPolicy ? (
        <div className="form-grid">
          <label className="switch-row">
            <span>Scenario Policy 启用</span>
            <Switch
              checked={flowPolicy.scenario_policy.enabled}
              onChange={(checked) =>
                setFlowPolicy({
                  ...flowPolicy,
                  scenario_policy: { ...flowPolicy.scenario_policy, enabled: checked },
                })
              }
            />
          </label>
          <label>
            <span>Scenario 模式</span>
            <Select
              value={flowPolicy.scenario_policy.mode}
              options={[
                { label: 'auto', value: 'auto' },
                { label: 'manual', value: 'manual' },
              ]}
              onChange={(value) =>
                setFlowPolicy({
                  ...flowPolicy,
                  scenario_policy: { ...flowPolicy.scenario_policy, mode: value },
                })
              }
            />
          </label>
          <label className="switch-row">
            <span>允许图修复</span>
            <Switch
              checked={flowPolicy.scenario_policy.allow_graph_repair}
              onChange={(checked) =>
                setFlowPolicy({
                  ...flowPolicy,
                  scenario_policy: { ...flowPolicy.scenario_policy, allow_graph_repair: checked },
                })
              }
            />
          </label>
          <label>
            <span>最大修复轮次</span>
            <Input
              value={String(flowPolicy.scenario_policy.max_graph_cycles)}
              onChange={(event) =>
                setFlowPolicy({
                  ...flowPolicy,
                  scenario_policy: {
                    ...flowPolicy.scenario_policy,
                    max_graph_cycles: Number(event.target.value || 0),
                  },
                })
              }
            />
          </label>
          <label className="full-span">
            <span>允许场景（逗号或换行分隔，留空表示自动匹配）</span>
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 6 }}
              value={stringifyListInput(flowPolicy.scenario_policy.allowed_scenarios || [])}
              onChange={(event) =>
                setFlowPolicy({
                  ...flowPolicy,
                  scenario_policy: {
                    ...flowPolicy.scenario_policy,
                    allowed_scenarios: parseListInput(event.target.value),
                  },
                })
              }
            />
          </label>
          <label className="switch-row">
            <span>Skill Policy 启用</span>
            <Switch
              checked={flowPolicy.skill_policy.enabled}
              onChange={(checked) =>
                setFlowPolicy({
                  ...flowPolicy,
                  skill_policy: { ...flowPolicy.skill_policy, enabled: checked },
                })
              }
            />
          </label>
          <label>
            <span>挂载模式</span>
            <Select
              value={flowPolicy.skill_policy.mount_mode}
              options={[{ label: 'agent_scoped', value: 'agent_scoped' }]}
              onChange={(value) =>
                setFlowPolicy({
                  ...flowPolicy,
                  skill_policy: { ...flowPolicy.skill_policy, mount_mode: value },
                })
              }
            />
          </label>
          <label className="switch-row">
            <span>执行时二次鉴权</span>
            <Switch
              checked={flowPolicy.skill_policy.runtime_auth_check}
              onChange={(checked) =>
                setFlowPolicy({
                  ...flowPolicy,
                  skill_policy: { ...flowPolicy.skill_policy, runtime_auth_check: checked },
                })
              }
            />
          </label>
          <label>
            <span>最大节点预算</span>
            <Input
              value={String(flowPolicy.max_node_budget)}
              onChange={(event) =>
                setFlowPolicy({
                  ...flowPolicy,
                  max_node_budget: Number(event.target.value || 0),
                })
              }
            />
          </label>
          <label className="switch-row">
            <span>编排失败回退全域链路</span>
            <Switch
              checked={flowPolicy.fallback_to_global}
              onChange={(checked) =>
                setFlowPolicy({
                  ...flowPolicy,
                  fallback_to_global: checked,
                })
              }
            />
          </label>
          <label className="full-span">
            <span>备注</span>
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 6 }}
              value={flowPolicy.notes}
              onChange={(event) =>
                setFlowPolicy({
                  ...flowPolicy,
                  notes: event.target.value,
                })
              }
            />
          </label>
        </div>
      ) : null}
    </Card>
  );

  const renderScenarioHubPage = () => (
    <Card
      loading={adminLoading}
      title="ScenarioHub 场景包"
      extra={
        <Button type="primary" onClick={() => void saveScenarioPacks()}>
          保存场景包
        </Button>
      }
    >
      <Table
        rowKey="scenario_id"
        pagination={false}
        dataSource={scenarioPacks}
        columns={[
          { title: 'Scenario ID', dataIndex: 'scenario_id', key: 'scenario_id', width: 220 },
          {
            title: '场景名称',
            key: 'display_name',
            width: 150,
            render: (_, row, index) => (
              <Input
                value={row.display_name}
                onChange={(event) => {
                  const next = [...scenarioPacks];
                  next[index] = { ...next[index], display_name: event.target.value };
                  setScenarioPacks(next);
                }}
              />
            ),
          },
          {
            title: '业务域',
            key: 'domain',
            width: 180,
            render: (_, row, index) => (
              <Input
                value={row.domain}
                onChange={(event) => {
                  const next = [...scenarioPacks];
                  next[index] = { ...next[index], domain: event.target.value };
                  setScenarioPacks(next);
                }}
              />
            ),
          },
          {
            title: '触发意图',
            key: 'trigger_intents',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.trigger_intents)}
                onChange={(event) => {
                  const next = [...scenarioPacks];
                  next[index] = { ...next[index], trigger_intents: parseListInput(event.target.value) };
                  setScenarioPacks(next);
                }}
              />
            ),
          },
          {
            title: 'Required Agents',
            key: 'required_agents',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.required_agents)}
                onChange={(event) => {
                  const next = [...scenarioPacks];
                  next[index] = { ...next[index], required_agents: parseListInput(event.target.value) };
                  setScenarioPacks(next);
                }}
              />
            ),
          },
          {
            title: 'Auth Scopes',
            key: 'auth_scopes',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.auth_scopes)}
                onChange={(event) => {
                  const next = [...scenarioPacks];
                  next[index] = { ...next[index], auth_scopes: parseListInput(event.target.value) };
                  setScenarioPacks(next);
                }}
              />
            ),
          },
          {
            title: '状态',
            key: 'status',
            width: 80,
            render: (_, row, index) => (
              <Switch
                checked={row.status === 'active'}
                onChange={(checked) => {
                  const next = [...scenarioPacks];
                  next[index] = { ...next[index], status: checked ? 'active' : 'inactive' };
                  setScenarioPacks(next);
                }}
              />
            ),
          },
        ]}
      />
    </Card>
  );

  const renderSkillHubPage = () => (
    <Card
      loading={adminLoading}
      title="SkillHub 能力挂载"
      extra={
        <Button type="primary" onClick={() => void saveFlowSkillDescriptors()}>
          保存能力
        </Button>
      }
    >
      <Table
        className="wide-config-table"
        rowKey="skill_id"
        pagination={false}
        scroll={{ x: 1280 }}
        dataSource={flowSkillDescriptors}
        columns={[
          { title: 'Skill ID', dataIndex: 'skill_id', key: 'skill_id', width: 190 },
          {
            title: '展示名称',
            key: 'display_name',
            width: 150,
            render: (_, row, index) => (
              <Input
                value={row.display_name}
                onChange={(event) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], display_name: event.target.value };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
          {
            title: 'Tool Name',
            key: 'tool_name',
            width: 180,
            render: (_, row, index) => (
              <Input
                value={row.tool_name}
                onChange={(event) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], tool_name: event.target.value };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
          {
            title: '挂载 Agent',
            key: 'mount_agents',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.mount_agents)}
                onChange={(event) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], mount_agents: parseListInput(event.target.value) };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
          {
            title: '所需 Scope',
            key: 'required_scopes',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.required_scopes)}
                onChange={(event) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], required_scopes: parseListInput(event.target.value) };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
          {
            title: '允许用户',
            key: 'allowed_users',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.allowed_users || ['*'])}
                onChange={(event) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], allowed_users: parseListInput(event.target.value) };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
          {
            title: '允许租户',
            key: 'allowed_tenants',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.allowed_tenants || ['*'])}
                onChange={(event) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], allowed_tenants: parseListInput(event.target.value) };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
          {
            title: '允许场景',
            key: 'allowed_scenarios',
            render: (_, row, index) => (
              <Input
                value={stringifyListInput(row.allowed_scenarios || [])}
                onChange={(event) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], allowed_scenarios: parseListInput(event.target.value) };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
          {
            title: '状态',
            key: 'status',
            width: 80,
            render: (_, row, index) => (
              <Switch
                checked={row.status === 'active'}
                onChange={(checked) => {
                  const next = [...flowSkillDescriptors];
                  next[index] = { ...next[index], status: checked ? 'active' : 'inactive' };
                  setFlowSkillDescriptors(next);
                }}
              />
            ),
          },
        ]}
      />
    </Card>
  );

  const renderSessionPage = () => (
    <Card
      loading={adminLoading}
      title="会话管理"
      extra={
        <Button
          type="primary"
          onClick={() =>
            void saveSection('/api/admin/session-policies', sessionPolicies, '会话策略已保存', '会话策略保存失败')
          }
        >
          保存会话策略
        </Button>
      }
    >
      <Table
        rowKey="policy_code"
        pagination={false}
        dataSource={sessionPolicies}
        columns={[
          { title: '策略编码', dataIndex: 'policy_code', key: 'policy_code' },
          { title: '策略名称', dataIndex: 'policy_name', key: 'policy_name' },
          { title: '保留天数', dataIndex: 'retention_days', key: 'retention_days' },
          { title: '限流(QPM)', dataIndex: 'rate_limit_qpm', key: 'rate_limit_qpm' },
          { title: '状态', dataIndex: 'status', key: 'status' },
          { title: '更新人', dataIndex: 'updated_by', key: 'updated_by' },
          { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
        ]}
      />
    </Card>
  );

  const renderDashboardPage = () => (
    <Card
      loading={adminLoading}
      title="数据看板"
      extra={
        <Button
          type="primary"
          onClick={() =>
            void saveSection('/api/admin/dashboard-cards', dashboardCards, '看板配置已保存', '看板配置保存失败')
          }
        >
          保存看板
        </Button>
      }
    >
      <Table
        rowKey="card_code"
        pagination={false}
        dataSource={dashboardCards}
        columns={[
          { title: '卡片编码', dataIndex: 'card_code', key: 'card_code' },
          { title: '卡片名称', dataIndex: 'card_name', key: 'card_name' },
          { title: '指标表达式', dataIndex: 'metric_expr', key: 'metric_expr' },
          { title: '刷新间隔(s)', dataIndex: 'refresh_interval_s', key: 'refresh_interval_s' },
          {
            title: '启用',
            key: 'enabled',
            render: (_, row, index) => (
              <Switch
                checked={row.enabled}
                onChange={(checked) => {
                  const next = [...dashboardCards];
                  next[index] = { ...next[index], enabled: checked };
                  setDashboardCards(next);
                }}
              />
            ),
          },
        ]}
      />
    </Card>
  );

  const renderSecurityPage = () => (
    <Card
      loading={adminLoading}
      title="安全管理"
      extra={
        <Button
          type="primary"
          onClick={() =>
            void saveSection('/api/admin/security-policies', securityPolicies, '安全策略已保存', '安全策略保存失败')
          }
        >
          保存安全策略
        </Button>
      }
    >
      <Table
        rowKey="rule_code"
        pagination={false}
        dataSource={securityPolicies}
        columns={[
          { title: '规则编码', dataIndex: 'rule_code', key: 'rule_code' },
          { title: '规则名称', dataIndex: 'rule_name', key: 'rule_name' },
          { title: '级别', dataIndex: 'severity', key: 'severity' },
          { title: '策略', dataIndex: 'strategy', key: 'strategy' },
          {
            title: '启用',
            key: 'enabled',
            render: (_, row, index) => (
              <Switch
                checked={row.enabled}
                onChange={(checked) => {
                  const next = [...securityPolicies];
                  next[index] = { ...next[index], enabled: checked };
                  setSecurityPolicies(next);
                }}
              />
            ),
          },
          { title: '更新时间', dataIndex: 'last_updated', key: 'last_updated' },
        ]}
      />
    </Card>
  );

  const renderGlossaryPage = () => (
    <Card
      loading={adminLoading}
      title="词库管理"
      extra={
        <Button
          type="primary"
          onClick={() => void saveSection('/api/admin/glossary-terms', glossaryTerms, '词库已保存', '词库保存失败')}
        >
          保存词库
        </Button>
      }
    >
      <Table
        rowKey="term"
        pagination={false}
        dataSource={glossaryTerms}
        columns={[
          { title: '词条', dataIndex: 'term', key: 'term' },
          { title: '分类', dataIndex: 'category', key: 'category' },
          { title: '定义', dataIndex: 'definition', key: 'definition' },
          { title: '同义词', key: 'synonyms', render: (_, row) => row.synonyms.join(' / ') },
          { title: '状态', dataIndex: 'status', key: 'status' },
          { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
        ]}
      />
    </Card>
  );

  const renderHomeRecommendationPage = () => (
    <Card
      loading={adminLoading}
      title="首页推荐"
      extra={
        <Button
          type="primary"
          onClick={() =>
            void saveSection(
              '/api/admin/homepage-recommendations',
              homepageRecommendations,
              '首页推荐已保存',
              '首页推荐保存失败',
            )
          }
        >
          保存推荐
        </Button>
      }
    >
      <Table
        rowKey="recommendation_id"
        pagination={false}
        dataSource={homepageRecommendations}
        columns={[
          { title: '推荐ID', dataIndex: 'recommendation_id', key: 'recommendation_id' },
          { title: '标题', dataIndex: 'title', key: 'title' },
          { title: '目标场景', dataIndex: 'target_scene', key: 'target_scene' },
          { title: '优先级', dataIndex: 'priority', key: 'priority' },
          {
            title: '启用',
            key: 'enabled',
            render: (_, row, index) => (
              <Switch
                checked={row.enabled}
                onChange={(checked) => {
                  const next = [...homepageRecommendations];
                  next[index] = { ...next[index], enabled: checked };
                  setHomepageRecommendations(next);
                }}
              />
            ),
          },
          { title: '操作人', dataIndex: 'operator', key: 'operator' },
          { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
        ]}
      />
    </Card>
  );

  const renderPermissionPage = () => (
    <div className="backend-grid-2">
      <Card title="权限策略" loading={adminLoading} extra={<Button type="primary" onClick={() => void savePermissionRules()}>保存权限</Button>}>
        <Table
          rowKey="role"
          pagination={false}
          dataSource={permissionRules}
          columns={[
            { title: '角色', dataIndex: 'role', key: 'role' },
            { title: '可用智能体', key: 'allowed_agents', render: (_, row) => row.allowed_agents.join(' / ') },
            { title: '可用操作', key: 'allowed_operations', render: (_, row) => row.allowed_operations.join(' / ') },
            { title: '部门范围', key: 'department_codes', render: (_, row) => row.department_codes.join(' / ') || '-' },
            { title: '指定人员', key: 'staff_codes', render: (_, row) => row.staff_codes.join(' / ') || '-' },
            {
              title: '启用',
              key: 'active',
              render: (_, row, index) => (
                <Switch
                  checked={row.active}
                  onChange={(checked) => {
                    const next = [...permissionRules];
                    next[index] = { ...next[index], active: checked };
                    setPermissionRules(next);
                  }}
                />
              ),
            },
          ]}
        />
      </Card>

      <Card title="知识库与 Skill" loading={adminLoading} extra={<Button type="primary" onClick={() => void saveSkillPolicies()}>保存 Skill</Button>}>
        <Tabs
          items={[
            {
              key: 'kb',
              label: '知识库',
              children: (
                <Table
                  rowKey="kb_code"
                  pagination={false}
                  dataSource={knowledgeBindings}
                  columns={[
                    { title: '团队', dataIndex: 'team', key: 'team' },
                    { title: '知识库', dataIndex: 'kb_name', key: 'kb_name' },
                    { title: '编码', dataIndex: 'kb_code', key: 'kb_code' },
                    { title: '类型', dataIndex: 'kb_type', key: 'kb_type' },
                    { title: 'Embedding', dataIndex: 'embedding_model', key: 'embedding_model' },
                    { title: '更新策略', dataIndex: 'update_mode', key: 'update_mode' },
                    { title: '可访问角色', key: 'readable_roles', render: (_, row) => row.readable_roles.join(' / ') },
                  ]}
                />
              ),
            },
            {
              key: 'skill',
              label: 'Skill',
              children: (
                <Table
                  rowKey="skill_code"
                  pagination={false}
                  dataSource={skillPolicies}
                  columns={[
                    { title: 'Skill', dataIndex: 'skill_name', key: 'skill_name' },
                    { title: '编码', dataIndex: 'skill_code', key: 'skill_code' },
                    { title: '类型', dataIndex: 'skill_type', key: 'skill_type' },
                    { title: '来源', dataIndex: 'source', key: 'source' },
                    { title: '最大调用', dataIndex: 'max_calls', key: 'max_calls' },
                    { title: '超时(s)', dataIndex: 'timeout_s', key: 'timeout_s' },
                    { title: '可见角色', key: 'visible_roles', render: (_, row) => row.visible_roles.join(' / ') },
                    {
                      title: '状态',
                      key: 'enabled',
                      render: (_, row, index) => (
                        <Switch
                          checked={row.enabled}
                          onChange={(checked) => {
                            const next = [...skillPolicies];
                            next[index] = { ...next[index], enabled: checked };
                            setSkillPolicies(next);
                          }}
                        />
                      ),
                    },
                  ]}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );

  const renderUserRolePage = () => (
    <Card title="角色与用户" loading={adminLoading}>
      <Tabs
        items={[
          {
            key: 'role',
            label: '角色策略',
            children: (
              <>
                <div className="inline-right-btn">
                  <Button type="primary" onClick={() => void saveRolePolicies()}>
                    保存角色策略
                  </Button>
                </div>
                <Table
                  rowKey="role_code"
                  pagination={false}
                  dataSource={rolePolicies}
                  columns={[
                    { title: '角色编码', dataIndex: 'role_code', key: 'role_code' },
                    { title: '角色名称', dataIndex: 'role_name', key: 'role_name' },
                    { title: '权限', key: 'permissions', render: (_, row) => row.permissions.join(' / ') },
                    { title: '数据范围', dataIndex: 'data_scope', key: 'data_scope' },
                    {
                      title: '启用',
                      key: 'enabled',
                      render: (_, row, index) => (
                        <Switch
                          checked={row.enabled}
                          onChange={(checked) => {
                            const next = [...rolePolicies];
                            next[index] = { ...next[index], enabled: checked };
                            setRolePolicies(next);
                          }}
                        />
                      ),
                    },
                  ]}
                />
              </>
            ),
          },
          {
            key: 'user',
            label: '用户管理',
            children: (
              <>
                <div className="inline-right-btn">
                  <Button type="primary" onClick={() => void saveUserAccounts()}>
                    保存用户配置
                  </Button>
                </div>
                <Table
                  rowKey="staff_code"
                  pagination={false}
                  dataSource={userAccounts}
                  columns={[
                    { title: '工号', dataIndex: 'staff_code', key: 'staff_code' },
                    { title: '姓名', dataIndex: 'user_name', key: 'user_name' },
                    { title: '部门', dataIndex: 'department', key: 'department' },
                    { title: '角色', key: 'roles', render: (_, row) => row.roles.join(' / ') },
                    { title: '状态', dataIndex: 'status', key: 'status' },
                    { title: '最近登录', dataIndex: 'last_login', key: 'last_login' },
                  ]}
                />
              </>
            ),
          },
        ]}
      />
    </Card>
  );

  const renderReleasePanel = () => (
    <Card title="发布记录" loading={adminLoading}>
      <div className="release-row">
        <Input
          value={releaseNote}
          placeholder="输入发布说明，例如：更新经营分析智能体工具权限"
          onChange={(event) => setReleaseNote(event.target.value)}
        />
        <Input value={releaseVersion} placeholder="版本号，如 v1.3.0" onChange={(event) => setReleaseVersion(event.target.value)} />
        <Select
          value={releaseRiskLevel}
          options={[
            { label: 'low', value: 'low' },
            { label: 'medium', value: 'medium' },
            { label: 'high', value: 'high' },
          ]}
          onChange={(value) => setReleaseRiskLevel(value)}
        />
        <Button type="primary" onClick={() => void publishConfigSnapshot()}>
          新增发布记录
        </Button>
      </div>
      <Table
        rowKey="id"
        dataSource={releaseHistory}
        pagination={false}
        columns={[
          { title: 'ID', dataIndex: 'id', key: 'id' },
          { title: '版本', dataIndex: 'version', key: 'version' },
          { title: '操作人', dataIndex: 'operator', key: 'operator' },
          { title: '说明', dataIndex: 'note', key: 'note' },
          { title: '影响智能体', key: 'affected_agents', render: (_, row) => row.affected_agents.join(' / ') },
          { title: '风险等级', dataIndex: 'risk_level', key: 'risk_level' },
          { title: '时间', dataIndex: 'created_at', key: 'created_at' },
        ]}
      />
    </Card>
  );

  const renderBackendContent = () => {
    switch (adminPage) {
      case 'model-center':
        return renderModelCenterPage();
      case 'basic-settings':
        return renderBasicSettingsPage();
      case 'address-config':
        return renderAddressConfigPage();
      case 'data-access':
        return renderDataAccessPage();
      case 'data-assets':
        return renderDataAssetsPage();
      case 'mcp-server':
        return renderMcpServerPage();
      case 'skills':
        return renderSkillsPage();
      case 'master-agent':
        return renderMasterAgentPage();
      case 'business-agent':
        return renderBusinessAgentPage();
      case 'flow-policy':
        return renderFlowPolicyPage();
      case 'scenario-hub':
        return renderScenarioHubPage();
      case 'skill-hub':
        return renderSkillHubPage();
      case 'session-management':
        return renderSessionPage();
      case 'dashboard':
        return renderDashboardPage();
      case 'security':
        return renderSecurityPage();
      case 'glossary':
        return renderGlossaryPage();
      case 'home-recommendation':
        return renderHomeRecommendationPage();
      case 'permission':
        return renderPermissionPage();
      case 'user-role':
        return renderUserRolePage();
      default:
        return renderModelCenterPage();
    }
  };

  const renderBackendView = () => {
    const summaryCards = [
      { key: 'model', title: '模型总数', value: adminSummary?.model_count ?? '-' },
      { key: 'agent', title: '业务智能体', value: adminSummary?.business_agent_count ?? '-' },
      { key: 'perm', title: '权限策略', value: adminSummary?.permission_rule_count ?? '-' },
      { key: 'user', title: '启用用户', value: adminSummary?.user_enabled_count ?? '-' },
    ];

    const navGroups: Array<{ title: string; items: Array<{ key: AdminPageKey; label: string }> }> = [
      {
        title: '模型管理',
        items: [
          { key: 'model-center', label: '模型管理' },
          { key: 'basic-settings', label: '基础设置' },
          { key: 'address-config', label: '地址配置' },
        ],
      },
      {
        title: '数据连接器',
        items: [
          { key: 'data-access', label: '数据接入' },
          { key: 'data-assets', label: '数据管理' },
        ],
      },
      {
        title: '智能体配置',
        items: [
          { key: 'master-agent', label: 'Master智能体' },
          { key: 'business-agent', label: '业务智能体' },
          { key: 'mcp-server', label: 'MCP Server' },
          { key: 'skills', label: 'Skills' },
          { key: 'flow-policy', label: '心流策略' },
          { key: 'scenario-hub', label: 'ScenarioHub' },
          { key: 'skill-hub', label: 'SkillHub' },
        ],
      },
      {
        title: '运营管理中心',
        items: [
          { key: 'session-management', label: '会话管理' },
          { key: 'dashboard', label: '数据看板' },
          { key: 'security', label: '安全管理' },
          { key: 'glossary', label: '词库管理' },
          { key: 'home-recommendation', label: '首页推荐' },
        ],
      },
      {
        title: '用户与权限',
        items: [
          { key: 'permission', label: '权限策略' },
          { key: 'user-role', label: '角色与用户' },
        ],
      },
    ];

    return (
      <div className="backend-wrapper">
        {adminError ? <Alert type="error" message={adminError} showIcon style={{ marginBottom: 12 }} /> : null}
        {saveStatus ? <Alert type="info" message={saveStatus} showIcon style={{ marginBottom: 12 }} /> : null}

        <div className="backend-overview-grid">
          {summaryCards.map((item) => (
            <Card key={item.key} className="metric-card" loading={adminLoading}>
              <div className="metric-title">{item.title}</div>
              <div className="metric-value">{item.value}</div>
            </Card>
          ))}
        </div>

        <div className="backend-layout">
          <aside className="backend-menu">
            {navGroups.map((group) => (
              <div key={group.title} className="backend-nav-group">
                <div className="backend-nav-title">{group.title}</div>
                <div className="backend-nav-list">
                  {group.items.map((item) => (
                    <button
                      key={item.key}
                      className={`backend-nav-item ${adminPage === item.key ? 'active' : ''}`}
                      onClick={() => setAdminPage(item.key)}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </aside>

          <section className="backend-content">
            <div className="backend-content-header">
              <div>
                <h2>{ADMIN_PAGE_LABEL[adminPage]}</h2>
                <p>对齐线上后台管理结构，支持模型、智能体、权限和运营配置。</p>
              </div>
              <Button onClick={() => void loadAdminData()} loading={adminLoading}>
                刷新当前数据
              </Button>
            </div>
            {renderBackendContent()}
            {renderReleasePanel()}
          </section>
        </div>
      </div>
    );
  };

  return (
    <ConfigProvider {...(isDark ? carbonDarkTheme : carbonTheme)}>
      <div className={`map-console-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
        <aside className={`map-sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}>
          <div className={`map-brand ${sidebarCollapsed ? 'collapsed' : ''}`}>
            <div className="brand-mark">MAP</div>
            <div className={`brand-meta ${sidebarCollapsed ? 'collapsed' : ''}`}>
              <div className="brand-title">MAP Console</div>
              <div className="brand-subtitle">Multi Agent Path</div>
            </div>
          </div>

          <Button
            className={`map-sidebar-rail-toggle ${sidebarCollapsed ? 'collapsed' : ''}`}
            type="text"
            onClick={() => setSidebarCollapsed((prev) => !prev)}
          >
            {sidebarCollapsed ? '›' : '‹'}
          </Button>

          {viewMode === 'chat' && sidebarCollapsed ? (
            <Button
              className="collapsed-new-chat"
              type="primary"
              onClick={() => {
                setMessages([]);
                setDetail(undefined);
                setInputValue('');
                setActiveHistoryId(null);
                setTracePanelOpen(false);
              }}
            >
              +
            </Button>
          ) : null}

          {viewMode === 'chat' ? (
            <div className="chat-history-panel">
              <div className="history-head">
                <span className={`history-title ${sidebarCollapsed ? 'collapsed' : ''}`}>历史记录</span>
                <Button
                  type="text"
                  size="small"
                  className={`new-chat-btn ${sidebarCollapsed ? 'collapsed' : ''}`}
                  onClick={() => {
                    setMessages([]);
                    setDetail(undefined);
                    setInputValue('');
                    setActiveHistoryId(null);
                    setTracePanelOpen(false);
                  }}
                >
                  + 新对话
                </Button>
              </div>
              <div className="history-list">
                {chatHistory.map((item) => (
                  <button
                    key={item.id}
                    className={`history-item ${activeHistoryId === item.id ? 'active' : ''}`}
                    onClick={() => {
                      setActiveHistoryId(item.id);
                      setMessages([
                        { id: `${item.id}-q`, role: 'user', content: item.question },
                        {
                          id: `${item.id}-a`,
                          role: 'assistant',
                          content: item.answer,
                          agentNames: extractAgentNamesFromDetail(item.detail),
                        },
                      ]);
                      setDetail(item.detail);
                      setTracePanelOpen(false);
                      setTracePanelMode('trace');
                    }}
                    title={item.question}
                  >
                    <span>{item.question}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className={`sidebar-placeholder ${sidebarCollapsed ? 'collapsed' : ''}`}>后台配置导航</div>
          )}

          <div className="sidebar-switcher">
            <Button type={viewMode === 'chat' ? 'primary' : 'default'} block onClick={() => setViewMode('chat')}>
              {sidebarCollapsed ? '前台' : '前台问答'}
            </Button>
            <Button type={viewMode === 'backend' ? 'primary' : 'default'} block onClick={() => setViewMode('backend')}>
              {sidebarCollapsed ? '后台' : '后台管理'}
            </Button>
          </div>
        </aside>

        <main className="map-main">
          <header className="main-header">
            <div>
              <h1>{viewMode === 'chat' ? '前台问答' : '后台管理'}</h1>
              <p>
                {viewMode === 'chat'
                  ? '提问后可在右侧查看思考过程与回答来源。'
                  : '后台功能与算法服务解耦，由业务后端独立承载管理流程。'}
              </p>
            </div>
            <div className="main-header-actions">
              <Tag>{isDark ? '深色' : '浅色'}</Tag>
              <Switch checked={isDark} onChange={setIsDark} />
              {viewMode === 'backend' ? (
                <Button onClick={() => void loadAdminData()} loading={adminLoading}>
                  刷新管理数据
                </Button>
              ) : null}
            </div>
          </header>

          {viewMode === 'chat' ? renderChatView() : renderBackendView()}
        </main>

        <Drawer
          title={editingAgent ? `编辑业务智能体：${editingAgent.display_name}` : '编辑业务智能体'}
          open={editingAgentOpen}
          width={860}
          onClose={() => {
            setEditingAgentOpen(false);
            setEditingAgent(null);
            setAgentConfigTab('basic');
          }}
          extra={
            <Button type="primary" onClick={() => void saveEditingBusinessAgent()}>
              保存
            </Button>
          }
        >
          {editingAgent ? (
            <Tabs
              activeKey={agentConfigTab}
              onChange={(key) => setAgentConfigTab(key as AgentConfigTabKey)}
              items={[
                {
                  key: 'basic',
                  label: '基本配置',
                  children: (
                    <div className="form-grid">
                      <label>
                        <span>智能体名称</span>
                        <Input
                          value={editingAgent.display_name}
                          onChange={(event) => setEditingAgent({ ...editingAgent, display_name: event.target.value })}
                          placeholder="请输入智能体名称"
                        />
                      </label>
                      <label>
                        <span>智能体编号</span>
                        <Input
                          value={editingAgent.agent_code}
                          onChange={(event) => setEditingAgent({ ...editingAgent, agent_code: event.target.value })}
                        />
                      </label>
                      <label className="full-span">
                        <span>描述</span>
                        <Input.TextArea
                          autoSize={{ minRows: 3, maxRows: 6 }}
                          value={editingAgent.description}
                          onChange={(event) => setEditingAgent({ ...editingAgent, description: event.target.value })}
                          placeholder="请输入描述"
                        />
                      </label>
                      <label>
                        <span>场景</span>
                        <Input
                          value={editingAgent.scene_name}
                          onChange={(event) => setEditingAgent({ ...editingAgent, scene_name: event.target.value })}
                        />
                      </label>
                      <label>
                        <span>归属团队</span>
                        <Input
                          value={editingAgent.owner_team}
                          onChange={(event) => setEditingAgent({ ...editingAgent, owner_team: event.target.value })}
                        />
                      </label>
                      <label>
                        <span>模型</span>
                        <Select
                          value={editingAgent.model}
                          options={MODEL_OPTIONS}
                          onChange={(value) => setEditingAgent({ ...editingAgent, model: value })}
                        />
                      </label>
                      <label>
                        <span>数据范围</span>
                        <Input
                          value={editingAgent.data_scope}
                          onChange={(event) => setEditingAgent({ ...editingAgent, data_scope: event.target.value })}
                        />
                      </label>
                      <label>
                        <span>调度权重</span>
                        <Input
                          value={String(editingAgent.weight)}
                          onChange={(event) => setEditingAgent({ ...editingAgent, weight: Number(event.target.value || 0) })}
                        />
                      </label>
                      <label>
                        <span>超时(s)</span>
                        <Input
                          value={String(editingAgent.timeout_s)}
                          onChange={(event) => setEditingAgent({ ...editingAgent, timeout_s: Number(event.target.value || 0) })}
                        />
                      </label>
                      <label>
                        <span>重试次数</span>
                        <Input
                          value={String(editingAgent.retry_limit)}
                          onChange={(event) => setEditingAgent({ ...editingAgent, retry_limit: Number(event.target.value || 0) })}
                        />
                      </label>
                      <label>
                        <span>并发上限</span>
                        <Input
                          value={String(editingAgent.parallel_limit)}
                          onChange={(event) => setEditingAgent({ ...editingAgent, parallel_limit: Number(event.target.value || 0) })}
                        />
                      </label>
                      <label className="switch-row">
                        <span>启用状态</span>
                        <Switch checked={editingAgent.enabled} onChange={(checked) => setEditingAgent({ ...editingAgent, enabled: checked })} />
                      </label>
                    </div>
                  ),
                },
                {
                  key: 'resource',
                  label: '资源挂载',
                  children: (
                    <div className="detail-layout">
                      <Card
                        size="small"
                        title="typed 资源挂载"
                        extra={
                          <Button
                            size="small"
                            onClick={() =>
                              setEditingAgent({
                                ...editingAgent,
                                resource_mounts: [
                                  ...(editingAgent.resource_mounts || []),
                                  {
                                    mount_id: `mount-${Date.now()}`,
                                    resource_type: 'builtin_tool',
                                    resource_id: 'general_qa_agent',
                                    resource_name: '通用问答',
                                    source_name: '',
                                    enabled: true,
                                    include_all_tools: false,
                                    mcp_tool_names: [],
                                    builtin_tool_name: 'general_qa_agent',
                                    created_at: new Date().toISOString(),
                                    config: {},
                                  },
                                ],
                              })
                            }
                          >
                            添加资源
                          </Button>
                        }
                      >
                        <Table
                          size="small"
                          pagination={false}
                          rowKey="mount_id"
                          dataSource={editingAgent.resource_mounts || []}
                          columns={[
                            {
                              title: '类型',
                              key: 'resource_type',
                              width: 150,
                              render: (_, row, index) => (
                                <Select
                                  value={row.resource_type}
                                  options={[
                                    { label: 'MCP Server', value: 'mcp_server' },
                                    { label: 'MCP Tool', value: 'mcp_tool' },
                                    { label: 'Skill', value: 'skill' },
                                    { label: '知识库', value: 'knowledge_base' },
                                    { label: '数据模型', value: 'data_model' },
                                    { label: '内置工具', value: 'builtin_tool' },
                                  ]}
                                  onChange={(value) => {
                                    const next = [...(editingAgent.resource_mounts || [])];
                                    next[index] = { ...row, resource_type: value };
                                    setEditingAgent({ ...editingAgent, resource_mounts: next });
                                  }}
                                />
                              ),
                            },
                            {
                              title: '资源',
                              key: 'resource',
                              render: (_, row, index) => {
                                const options =
                                  row.resource_type === 'mcp_server' || row.resource_type === 'mcp_tool'
                                    ? mcpServers.map((server) => ({ label: server.display_name, value: server.server_id }))
                                    : row.resource_type === 'skill'
                                      ? uploadedSkills.map((skill) => ({ label: skill.display_name, value: skill.skill_id }))
                                      : [
                                          { label: 'general_qa_agent', value: 'general_qa_agent' },
                                          { label: 'search_mounted_kb_agent', value: 'search_mounted_kb_agent' },
                                          { label: 'ask_database_agent', value: 'ask_database_agent' },
                                        ];
                                return (
                                  <Select
                                    value={row.mcp_server_id || row.skill_id || row.builtin_tool_name || row.resource_id}
                                    options={options}
                                    onChange={(value) => {
                                      const next = [...(editingAgent.resource_mounts || [])];
                                      next[index] = {
                                        ...row,
                                        resource_id: value,
                                        resource_name: value,
                                        mcp_server_id: row.resource_type.startsWith('mcp') ? value : row.mcp_server_id,
                                        skill_id: row.resource_type === 'skill' ? value : row.skill_id,
                                        builtin_tool_name: row.resource_type === 'builtin_tool' ? value : row.builtin_tool_name,
                                      };
                                      setEditingAgent({ ...editingAgent, resource_mounts: next });
                                    }}
                                  />
                                );
                              },
                            },
                            {
                              title: 'MCP Tools',
                              key: 'mcp_tools',
                              render: (_, row, index) => {
                                const server = mcpServers.find((item) => item.server_id === row.mcp_server_id);
                                return (
                                  <Select
                                    mode="multiple"
                                    disabled={!server || row.resource_type !== 'mcp_tool'}
                                    value={row.mcp_tool_names || []}
                                    options={(server?.tools || []).map((tool) => ({ label: tool.name, value: tool.name }))}
                                    onChange={(value) => {
                                      const next = [...(editingAgent.resource_mounts || [])];
                                      next[index] = { ...row, mcp_tool_names: value, include_all_tools: false };
                                      setEditingAgent({ ...editingAgent, resource_mounts: next });
                                    }}
                                  />
                                );
                              },
                            },
                            {
                              title: '全部工具',
                              key: 'include_all_tools',
                              width: 100,
                              render: (_, row, index) => (
                                <Switch
                                  checked={row.include_all_tools}
                                  disabled={!row.resource_type.startsWith('mcp')}
                                  onChange={(checked) => {
                                    const next = [...(editingAgent.resource_mounts || [])];
                                    next[index] = { ...row, include_all_tools: checked };
                                    setEditingAgent({ ...editingAgent, resource_mounts: next });
                                  }}
                                />
                              ),
                            },
                            {
                              title: '启用',
                              key: 'enabled',
                              width: 80,
                              render: (_, row, index) => (
                                <Switch
                                  checked={row.enabled}
                                  onChange={(checked) => {
                                    const next = [...(editingAgent.resource_mounts || [])];
                                    next[index] = { ...row, enabled: checked };
                                    setEditingAgent({ ...editingAgent, resource_mounts: next });
                                  }}
                                />
                              ),
                            },
                          ]}
                        />
                      </Card>
                      <Card
                        size="small"
                        title="挂载工具"
                        extra={
                          <Button
                            size="small"
                            onClick={() =>
                              setEditingAgent({
                                ...editingAgent,
                                tools: [...editingAgent.tools, `新工具${editingAgent.tools.length + 1}`],
                              })
                            }
                          >
                            添加工具
                          </Button>
                        }
                      >
                        <div className="chip-wrap">
                          {editingAgent.tools.map((tool) => (
                            <Tag key={tool}>{tool}</Tag>
                          ))}
                        </div>
                      </Card>
                      <Card
                        size="small"
                        title="资源列表"
                        extra={
                          <Button
                            size="small"
                            onClick={() =>
                              setEditingAgent({
                                ...editingAgent,
                                mounted_resources: [
                                  ...editingAgent.mounted_resources,
                                  {
                                    resource_name: `新资源${editingAgent.mounted_resources.length + 1}`,
                                    resource_type: '指标数据模型',
                                    source_name: 'ESSENDATA',
                                    permission_scope: '跟随智能体',
                                    dimension_status: '同步成功',
                                    created_at: new Date().toISOString(),
                                    enabled: true,
                                  },
                                ],
                              })
                            }
                          >
                            添加资源
                          </Button>
                        }
                      >
                        <Table
                          size="small"
                          pagination={false}
                          rowKey={(row) => `${row.resource_name}-${row.created_at || ''}`}
                          dataSource={editingAgent.mounted_resources}
                          columns={[
                            { title: '资源', dataIndex: 'resource_name', key: 'resource_name' },
                            { title: '类型', dataIndex: 'resource_type', key: 'resource_type' },
                            { title: '来源', dataIndex: 'source_name', key: 'source_name' },
                            { title: '权限', dataIndex: 'permission_scope', key: 'permission_scope' },
                            { title: '维度标注', dataIndex: 'dimension_status', key: 'dimension_status' },
                            {
                              title: '启用',
                              key: 'enabled',
                              render: (_, row, index) => (
                                <Switch
                                  checked={row.enabled}
                                  onChange={(checked) => {
                                    const next = [...editingAgent.mounted_resources];
                                    next[index] = { ...next[index], enabled: checked };
                                    setEditingAgent({ ...editingAgent, mounted_resources: next });
                                  }}
                                />
                              ),
                            },
                          ]}
                        />
                      </Card>
                    </div>
                  ),
                },
                {
                  key: 'glossary',
                  label: '关联词库',
                  children: (
                    <div className="detail-layout">
                      <Card
                        size="small"
                        title="已关联术语库"
                        extra={
                          <Button
                            size="small"
                            onClick={() =>
                              setEditingAgent({
                                ...editingAgent,
                                glossary_terms: [...editingAgent.glossary_terms, `术语库${editingAgent.glossary_terms.length + 1}`],
                              })
                            }
                          >
                            + 关联术语库
                          </Button>
                        }
                      >
                        {editingAgent.glossary_terms.length === 0 ? <div className="empty-hint">暂无关联的术语库</div> : null}
                        <div className="chip-wrap">
                          {editingAgent.glossary_terms.map((term, index) => (
                            <Tag key={`${term}-${index}`}>{term}</Tag>
                          ))}
                        </div>
                      </Card>
                    </div>
                  ),
                },
                {
                  key: 'prompt',
                  label: '提示词管理',
                  children: (
                    <div className="detail-layout">
                      <Card size="small" title="提示词版本配置">
                        <div className="form-grid">
                          <label>
                            <span>基座模型配置</span>
                            <Select
                              value={editingAgent.prompt_config.base_model}
                              options={MODEL_OPTIONS}
                              onChange={(value) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, base_model: value },
                                })
                              }
                            />
                          </label>
                          <label>
                            <span>Temperature（0=严谨 ←→ 1=创意）</span>
                            <Input
                              value={String(editingAgent.prompt_config.temperature)}
                              onChange={(event) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: {
                                    ...editingAgent.prompt_config,
                                    temperature: Number(event.target.value || 0),
                                  },
                                })
                              }
                            />
                          </label>
                          <label>
                            <span>Max Tokens（范围: 1024-8196）</span>
                            <Input
                              value={String(editingAgent.prompt_config.max_tokens)}
                              onChange={(event) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: {
                                    ...editingAgent.prompt_config,
                                    max_tokens: Number(event.target.value || 0),
                                  },
                                })
                              }
                            />
                          </label>
                          <label className="full-span">
                            <span>工具调用提示词</span>
                            <Input.TextArea
                              autoSize={{ minRows: 3, maxRows: 8 }}
                              value={editingAgent.prompt_config.tool_call_prompt}
                              onChange={(event) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, tool_call_prompt: event.target.value },
                                })
                              }
                            />
                          </label>
                          <label className="full-span">
                            <span>系统提示词（兼容字段）</span>
                            <Input.TextArea
                              autoSize={{ minRows: 3, maxRows: 8 }}
                              value={editingAgent.prompt_config.system_prompt}
                              onChange={(event) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, system_prompt: event.target.value },
                                })
                              }
                            />
                          </label>
                          <label className="full-span">
                            <span>User Prompt</span>
                            <Input.TextArea
                              autoSize={{ minRows: 3, maxRows: 8 }}
                              value={editingAgent.prompt_config.user_prompt}
                              onChange={(event) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, user_prompt: event.target.value },
                                })
                              }
                            />
                          </label>
                          <label className="full-span">
                            <span>总结提示词</span>
                            <Input.TextArea
                              autoSize={{ minRows: 3, maxRows: 8 }}
                              value={editingAgent.prompt_config.summary_prompt}
                              onChange={(event) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, summary_prompt: event.target.value },
                                })
                              }
                            />
                          </label>
                          <label className="full-span">
                            <span>版本说明</span>
                            <Input
                              value={editingAgent.prompt_config.version_note}
                              onChange={(event) =>
                                setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, version_note: event.target.value },
                                })
                              }
                            />
                          </label>
                        </div>
                      </Card>
                      <Card
                        size="small"
                        title="工具内部提示词"
                        extra={
                          <Button
                            size="small"
                            onClick={() =>
                              setEditingAgent({
                                ...editingAgent,
                                prompt_config: {
                                  ...editingAgent.prompt_config,
                                  tool_internal_prompts: [
                                    ...editingAgent.prompt_config.tool_internal_prompts,
                                    { tool_name: 'general_qa_agent', prompt: '', enabled: true },
                                  ],
                                },
                              })
                            }
                          >
                            新增
                          </Button>
                        }
                      >
                        <Table
                          size="small"
                          pagination={false}
                          rowKey={(row, index) => `${row.tool_name}-${index}`}
                          dataSource={editingAgent.prompt_config.tool_internal_prompts}
                          columns={[
                            {
                              title: '工具',
                              key: 'tool_name',
                              width: 160,
                              render: (_, row, index) => (
                                <Input
                                  value={row.tool_name}
                                  onChange={(event) => {
                                    const next = [...editingAgent.prompt_config.tool_internal_prompts];
                                    next[index] = { ...row, tool_name: event.target.value };
                                    setEditingAgent({
                                      ...editingAgent,
                                      prompt_config: { ...editingAgent.prompt_config, tool_internal_prompts: next },
                                    });
                                  }}
                                />
                              ),
                            },
                            {
                              title: '拆分/并发执行提示词',
                              key: 'prompt',
                              render: (_, row, index) => (
                                <Input.TextArea
                                  autoSize={{ minRows: 2, maxRows: 5 }}
                                  value={row.prompt}
                                  onChange={(event) => {
                                    const next = [...editingAgent.prompt_config.tool_internal_prompts];
                                    next[index] = { ...row, prompt: event.target.value };
                                    setEditingAgent({
                                      ...editingAgent,
                                      prompt_config: { ...editingAgent.prompt_config, tool_internal_prompts: next },
                                    });
                                  }}
                                />
                              ),
                            },
                            {
                              title: '启用',
                              key: 'enabled',
                              width: 80,
                              render: (_, row, index) => (
                                <Switch
                                  checked={row.enabled}
                                  onChange={(checked) => {
                                    const next = [...editingAgent.prompt_config.tool_internal_prompts];
                                    next[index] = { ...row, enabled: checked };
                                    setEditingAgent({
                                      ...editingAgent,
                                      prompt_config: { ...editingAgent.prompt_config, tool_internal_prompts: next },
                                    });
                                  }}
                                />
                              ),
                            },
                          ]}
                        />
                      </Card>
                      <Card size="small" title="工具提示词">
                        <Table
                          size="small"
                          pagination={false}
                          rowKey="tool_name"
                          dataSource={editingAgent.prompt_config.tool_prompts}
                          columns={[
                            { title: '工具', dataIndex: 'tool_name', key: 'tool_name', width: 120 },
                            {
                              title: 'System Prompt',
                              key: 'system_prompt',
                              render: (_, row, index) => (
                                <Input.TextArea
                                  autoSize={{ minRows: 2, maxRows: 4 }}
                                  value={row.system_prompt}
                                  onChange={(event) => {
                                    const next = [...editingAgent.prompt_config.tool_prompts];
                                    next[index] = { ...next[index], system_prompt: event.target.value };
                                    setEditingAgent({
                                      ...editingAgent,
                                      prompt_config: { ...editingAgent.prompt_config, tool_prompts: next },
                                    });
                                  }}
                                />
                              ),
                            },
                            {
                              title: 'User Prompt',
                              key: 'user_prompt',
                              render: (_, row, index) => (
                                <Input.TextArea
                                  autoSize={{ minRows: 2, maxRows: 4 }}
                                  value={row.user_prompt}
                                  onChange={(event) => {
                                    const next = [...editingAgent.prompt_config.tool_prompts];
                                    next[index] = { ...next[index], user_prompt: event.target.value };
                                    setEditingAgent({
                                      ...editingAgent,
                                      prompt_config: { ...editingAgent.prompt_config, tool_prompts: next },
                                    });
                                  }}
                                />
                              ),
                            },
                          ]}
                        />
                      </Card>
                      <Card size="small" title="历史版本">
                        <Table
                          size="small"
                          pagination={false}
                          rowKey="version"
                          dataSource={editingAgent.prompt_config.history_versions}
                          columns={[
                            { title: '版本号', dataIndex: 'version', key: 'version' },
                            { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
                            { title: '操作人', dataIndex: 'operator', key: 'operator' },
                            { title: '模型', dataIndex: 'model', key: 'model' },
                            { title: 'Temperature', dataIndex: 'temperature', key: 'temperature' },
                            { title: 'Max Tokens', dataIndex: 'max_tokens', key: 'max_tokens' },
                            { title: '版本说明', dataIndex: 'version_note', key: 'version_note' },
                          ]}
                        />
                      </Card>
                    </div>
                  ),
                },
                {
                  key: 'test',
                  label: '测试',
                  children: (
                    <div className="detail-layout">
                      <Card size="small" title="测试">
                        <div className="summary-row">
                          <Tag>当前状态：{editingAgent.test_config.publish_status || '未发布'}</Tag>
                          <Tag>最后保存：{editingAgent.test_config.last_saved_at || '-'}</Tag>
                        </div>
                        <div className="agent-test-chat">
                          <div className="agent-test-messages">
                            {agentTestMessages.length === 0 ? <div className="empty-hint">只测试当前业务智能体，不影响已发布配置。</div> : null}
                            {agentTestMessages.map((item, index) => (
                              <div key={`${item.role}-${index}`} className={`agent-test-message ${item.role}`}>
                                <Tag>{item.role === 'user' ? '用户' : '智能体'}</Tag>
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.content}</ReactMarkdown>
                              </div>
                            ))}
                          </div>
                          <Input.TextArea
                            value={agentTestInput}
                            autoSize={{ minRows: 3, maxRows: 6 }}
                            placeholder="向当前业务智能体提问"
                            onChange={(event) => setAgentTestInput(event.target.value)}
                            onPressEnter={(event) => {
                              if (!event.shiftKey) {
                                event.preventDefault();
                                void runAgentTest();
                              }
                            }}
                          />
                          <div className="chat-input-actions">
                            <Button onClick={() => setAgentTestMessages([])}>清空</Button>
                            <Button type="primary" loading={agentTestLoading} onClick={() => void runAgentTest()}>
                              发送测试
                            </Button>
                          </div>
                        </div>
                      </Card>
                    </div>
                  ),
                },
              ]}
            />
          ) : null}
        </Drawer>
      </div>
    </ConfigProvider>
  );
};

export default App;
