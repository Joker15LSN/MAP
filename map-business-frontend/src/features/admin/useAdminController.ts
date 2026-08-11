import { useMemo, useState } from 'react';
import { apiRequest, fetchJson } from '../../api/client';
import type {
  AddressConfigItem,
  AdminFullConfig,
  AdminPageKey,
  AdminSummary,
  AgentConfigTabKey,
  BasicSettingItem,
  BusinessAgentConfig,
  ChatRole,
  DashboardCardConfig,
  DataAccessItem,
  DataAssetItem,
  FlowPolicyConfig,
  FlowSkillDescriptor,
  GlossaryTermItem,
  HomeRecommendationItem,
  KnowledgeBinding,
  MasterAgentConfig,
  MasterPromptVersionItem,
  ModelCenterConfig,
  ModelRecord,
  ModelTabKey,
  PermissionRule,
  ReleaseRecord,
  RolePolicy,
  SecurityPolicyItem,
  SessionPolicyItem,
  SkillPolicy,
  UserAccount,
} from '../../api/types';
import { cloneFlowRequestConfig, toFlowRequestConfig } from '../chat/flowConfig';
import { normalizeBusinessAgent, normalizeFlowSkillDescriptor, createEmptyModelRecord } from './businessAgent';
import type { AdminApi } from './AdminApi';
import type { FlowStrategyController } from './useFlowStrategyController';

const DEFAULT_FLOW_POLICY: FlowPolicyConfig = {
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
};

/**
 * 管理端控制器:持有管理端全部数据状态与保存/发布操作,
 * 向 App shell 暴露 AdminApi,供 AdminView 及其子页面消费。
 * 与聊天端共享的 flow 策略状态通过 FlowStrategyController 协作。
 */
export function useAdminController(flow: FlowStrategyController): AdminApi {
  const [adminPage, setAdminPage] = useState<AdminPageKey>('model-center');
  const [modelTab, setModelTab] = useState<ModelTabKey>('large_models');
  const [modelSearch, setModelSearch] = useState('');

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

  const [masterVersions, setMasterVersions] = useState<MasterPromptVersionItem[]>([]);
  const [masterDiff, setMasterDiff] = useState('');
  const [releaseHistory, setReleaseHistory] = useState<ReleaseRecord[]>([]);

  const [releaseNote, setReleaseNote] = useState('');
  const [releaseVersion, setReleaseVersion] = useState('v1');
  const [releaseRiskLevel, setReleaseRiskLevel] = useState('low');
  const [saveStatus, setSaveStatus] = useState('');

  const [editingAgent, setEditingAgent] = useState<BusinessAgentConfig | null>(null);
  const [editingAgentOpen, setEditingAgentOpen] = useState(false);
  const [agentConfigTab, setAgentConfigTab] = useState<AgentConfigTabKey>('basic');
  const [agentTestInput, setAgentTestInput] = useState('');
  const [agentTestMessages, setAgentTestMessages] = useState<Array<{ role: ChatRole; content: string }>>([]);
  const [agentTestLoading, setAgentTestLoading] = useState(false);

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

  const loadAdminData = async () => {
    setAdminLoading(true);
    setAdminError('');
    try {
      const [summary, full] = await Promise.all([
        fetchJson<AdminSummary>('/api/admin/summary'),
        fetchJson<AdminFullConfig>('/api/admin/full-config'),
      ]);
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
      flow.setFlowPolicy(full.flow_policy || DEFAULT_FLOW_POLICY);
      flow.setFlowRuntimeSnapshot({
        updated_at: full.updated_at,
        flow_policy: full.flow_policy || DEFAULT_FLOW_POLICY,
        scenario_packs: full.scenario_packs || [],
        flow_skill_descriptors: full.flow_skill_descriptors || [],
        mcp_servers: full.mcp_servers || [],
        skills: full.skills || [],
      });
      flow.setFlowSessionOverride(
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
      flow.setScenarioPacks(full.scenario_packs || []);
      flow.setFlowSkillDescriptors((full.flow_skill_descriptors || []).map((item) => normalizeFlowSkillDescriptor(item)));
      flow.setMcpServers(full.mcp_servers || []);
      flow.setUploadedSkills(full.skills || []);
      setMasterVersions(full.master_agent?.prompt_versions || []);
      setReleaseHistory(full.release_history || []);
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : '管理端数据加载失败');
    } finally {
      setAdminLoading(false);
    }
  };

  const saveSection = async (url: string, body: unknown, successText: string, failText: string) => {
    setSaveStatus('保存中...');
    const response = await apiRequest(url, {
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
    const response = await apiRequest(url, {
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
    if (!flow.flowPolicy) {
      return;
    }
    await saveSection('/api/admin/flow-policy', flow.flowPolicy, '心流策略已保存', '心流策略保存失败');
  };

  const saveScenarioPacks = async () => {
    await saveSection('/api/admin/scenario-packs', flow.scenarioPacks, 'ScenarioHub 配置已保存', 'ScenarioHub 配置保存失败');
  };

  const saveFlowSkillDescriptors = async () => {
    await saveSection(
      '/api/admin/flow-skill-descriptors',
      flow.flowSkillDescriptors,
      'SkillHub 配置已保存',
      'SkillHub 配置保存失败',
    );
  };

  const saveMcpServers = async () => {
    await saveSection('/api/admin/mcp-servers', flow.mcpServers, 'MCP Server 配置已保存', 'MCP Server 配置保存失败');
  };

  const saveUploadedSkills = async () => {
    await saveSection('/api/admin/skills', flow.uploadedSkills, 'Skill 配置已保存', 'Skill 配置保存失败');
  };

  const publishMasterPrompt = async () => {
    const response = await apiRequest('/api/admin/master-agent/publish', {
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
    const response = await apiRequest(`/api/admin/master-agent/diff?from=${encodeURIComponent(target)}&to=current`);
    if (response.ok) {
      const payload = await response.json();
      setMasterDiff(payload.diff || '');
    }
  };

  const rollbackMasterPrompt = async (version: string) => {
    const response = await apiRequest('/api/admin/master-agent/rollback', {
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
      const response = await apiRequest(`/api/admin/business-agents/${editingAgent.agent_code}/test-chat`, {
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
    const response = await apiRequest(`/api/admin/release-history?${query}`, { method: 'POST' });
    if (response.ok) {
      setReleaseNote('');
      setSaveStatus('配置发布记录已新增');
      await loadAdminData();
    } else {
      setSaveStatus('发布记录新增失败');
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

  const adminApi: AdminApi = {
    adminPage,
    setAdminPage,
    modelTab,
    setModelTab,
    modelSearch,
    setModelSearch,
    filteredModels,
    adminLoading,
    setAdminLoading,
    adminError,
    setAdminError,
    adminSummary,
    setAdminSummary,
    masterConfig,
    setMasterConfig,
    businessAgents,
    setBusinessAgents,
    modelCenter,
    setModelCenter,
    basicSettings,
    setBasicSettings,
    addressConfigs,
    setAddressConfigs,
    dataAccessItems,
    setDataAccessItems,
    dataAssets,
    setDataAssets,
    sessionPolicies,
    setSessionPolicies,
    dashboardCards,
    setDashboardCards,
    securityPolicies,
    setSecurityPolicies,
    glossaryTerms,
    setGlossaryTerms,
    homepageRecommendations,
    setHomepageRecommendations,
    permissionRules,
    setPermissionRules,
    rolePolicies,
    setRolePolicies,
    userAccounts,
    setUserAccounts,
    knowledgeBindings,
    setKnowledgeBindings,
    skillPolicies,
    setSkillPolicies,
    flowPolicy: flow.flowPolicy,
    setFlowPolicy: flow.setFlowPolicy,
    flowRuntimeSnapshot: flow.flowRuntimeSnapshot,
    setFlowRuntimeSnapshot: flow.setFlowRuntimeSnapshot,
    scenarioPacks: flow.scenarioPacks,
    setScenarioPacks: flow.setScenarioPacks,
    flowSkillDescriptors: flow.flowSkillDescriptors,
    setFlowSkillDescriptors: flow.setFlowSkillDescriptors,
    mcpServers: flow.mcpServers,
    setMcpServers: flow.setMcpServers,
    uploadedSkills: flow.uploadedSkills,
    setUploadedSkills: flow.setUploadedSkills,
    masterVersions,
    setMasterVersions,
    masterDiff,
    setMasterDiff,
    releaseHistory,
    setReleaseHistory,
    releaseNote,
    setReleaseNote,
    releaseVersion,
    setReleaseVersion,
    releaseRiskLevel,
    setReleaseRiskLevel,
    saveStatus,
    setSaveStatus,
    editingAgent,
    setEditingAgent,
    editingAgentOpen,
    setEditingAgentOpen,
    agentConfigTab,
    setAgentConfigTab,
    agentTestInput,
    setAgentTestInput,
    agentTestMessages,
    setAgentTestMessages,
    agentTestLoading,
    setAgentTestLoading,
    loadAdminData,
    saveSection,
    updateModelRecord,
    addModelRecord,
    removeModelRecord,
    setDefaultModel,
    saveModelCenter,
    saveMasterConfig,
    saveEditingBusinessAgent,
    savePermissionRules,
    saveRolePolicies,
    saveUserAccounts,
    saveSkillPolicies,
    saveFlowPolicy,
    saveScenarioPacks,
    saveFlowSkillDescriptors,
    saveMcpServers,
    saveUploadedSkills,
    publishMasterPrompt,
    diffMasterPrompt,
    rollbackMasterPrompt,
    runAgentTest,
    publishConfigSnapshot,
  };

  return adminApi;
}
