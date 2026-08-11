import type { AdminPageKey, ModelTabKey } from '../../api/types';

export const MODEL_OPTIONS = [
  { label: 'deepseek-v4-flash', value: 'deepseek-v4-flash' },
  { label: 'deepseek-v4-flash', value: 'deepseek-v4-flash' },
  { label: 'deepseek-chat', value: 'deepseek-chat' },
];

export const ROUTE_STRATEGY_OPTIONS = [
  { label: 'scene_first', value: 'scene_first' },
  { label: 'master_only', value: 'master_only' },
  { label: 'hybrid', value: 'hybrid' },
];

export const STREAM_VERSION_OPTIONS = [
  { label: 'v2', value: 'v2' },
  { label: 'v3', value: 'v3' },
];

export const MODEL_TAB_MAP: Record<ModelTabKey, string> = {
  large_models: '大模型',
  asr_models: 'ASR',
  tts_models: 'TTS',
  embedding_models: 'Embedding',
  rerank_models: 'Rerank',
};

export const ADMIN_PAGE_LABEL: Record<AdminPageKey, string> = {
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
