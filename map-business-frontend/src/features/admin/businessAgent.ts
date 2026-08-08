import type { BusinessAgentConfig, FlowSkillDescriptor, ModelRecord, ModelTabKey } from '../../api/types';
import { MODEL_OPTIONS } from './constants';

export const createEmptyModelRecord = (tab: ModelTabKey): ModelRecord => {
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

export const createEmptyBusinessAgent = (): BusinessAgentConfig => ({
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

export const normalizeBusinessAgent = (agent: BusinessAgentConfig): BusinessAgentConfig => {
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

export const normalizeFlowSkillDescriptor = (item: FlowSkillDescriptor): FlowSkillDescriptor => ({
  ...item,
  mount_agents: item.mount_agents || [],
  required_scopes: item.required_scopes || [],
  allowed_users: item.allowed_users || ['*'],
  allowed_tenants: item.allowed_tenants || ['*'],
  allowed_scenarios: item.allowed_scenarios || [],
  allowed_actions: item.allowed_actions || ['execute'],
  audit_tags: item.audit_tags || [],
});
