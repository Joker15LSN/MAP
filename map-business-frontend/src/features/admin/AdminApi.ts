import type { Dispatch, SetStateAction } from 'react';
import type {
  AddressConfigItem,
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
  FlowRuntimeSnapshot,
  GlossaryTermItem,
  HomeRecommendationItem,
  KnowledgeBinding,
  MasterAgentConfig,
  MasterPromptVersionItem,
  McpServerConfig,
  ModelCenterConfig,
  ModelRecord,
  ModelTabKey,
  PermissionRule,
  ReleaseRecord,
  RolePolicy,
  ScenarioPackConfig,
  SecurityPolicyItem,
  SessionPolicyItem,
  SkillPolicy,
  UploadedSkill,
  UserAccount,
} from '../../api/types';

/**
 * 管理端全部状态与操作回调的集合。
 *
 * 状态仍由 App shell 持有(与聊天端共享 flow 策略等),此处通过 props
 * 注入到 AdminView,避免在管理端组件内复制状态导致行为漂移。
 */
export interface AdminApi {
  // 页面导航
  adminPage: AdminPageKey;
  setAdminPage: Dispatch<SetStateAction<AdminPageKey>>;
  modelTab: ModelTabKey;
  setModelTab: Dispatch<SetStateAction<ModelTabKey>>;
  modelSearch: string;
  setModelSearch: Dispatch<SetStateAction<string>>;
  filteredModels: ModelRecord[];

  // 加载状态
  adminLoading: boolean;
  setAdminLoading: Dispatch<SetStateAction<boolean>>;
  adminError: string;
  setAdminError: Dispatch<SetStateAction<string>>;
  adminSummary: AdminSummary | null;
  setAdminSummary: Dispatch<SetStateAction<AdminSummary | null>>;

  // 各配置区块
  masterConfig: MasterAgentConfig | null;
  setMasterConfig: Dispatch<SetStateAction<MasterAgentConfig | null>>;
  businessAgents: BusinessAgentConfig[];
  setBusinessAgents: Dispatch<SetStateAction<BusinessAgentConfig[]>>;
  modelCenter: ModelCenterConfig | null;
  setModelCenter: Dispatch<SetStateAction<ModelCenterConfig | null>>;
  basicSettings: BasicSettingItem[];
  setBasicSettings: Dispatch<SetStateAction<BasicSettingItem[]>>;
  addressConfigs: AddressConfigItem[];
  setAddressConfigs: Dispatch<SetStateAction<AddressConfigItem[]>>;
  dataAccessItems: DataAccessItem[];
  setDataAccessItems: Dispatch<SetStateAction<DataAccessItem[]>>;
  dataAssets: DataAssetItem[];
  setDataAssets: Dispatch<SetStateAction<DataAssetItem[]>>;
  sessionPolicies: SessionPolicyItem[];
  setSessionPolicies: Dispatch<SetStateAction<SessionPolicyItem[]>>;
  dashboardCards: DashboardCardConfig[];
  setDashboardCards: Dispatch<SetStateAction<DashboardCardConfig[]>>;
  securityPolicies: SecurityPolicyItem[];
  setSecurityPolicies: Dispatch<SetStateAction<SecurityPolicyItem[]>>;
  glossaryTerms: GlossaryTermItem[];
  setGlossaryTerms: Dispatch<SetStateAction<GlossaryTermItem[]>>;
  homepageRecommendations: HomeRecommendationItem[];
  setHomepageRecommendations: Dispatch<SetStateAction<HomeRecommendationItem[]>>;
  permissionRules: PermissionRule[];
  setPermissionRules: Dispatch<SetStateAction<PermissionRule[]>>;
  rolePolicies: RolePolicy[];
  setRolePolicies: Dispatch<SetStateAction<RolePolicy[]>>;
  userAccounts: UserAccount[];
  setUserAccounts: Dispatch<SetStateAction<UserAccount[]>>;
  knowledgeBindings: KnowledgeBinding[];
  setKnowledgeBindings: Dispatch<SetStateAction<KnowledgeBinding[]>>;
  skillPolicies: SkillPolicy[];
  setSkillPolicies: Dispatch<SetStateAction<SkillPolicy[]>>;
  flowPolicy: FlowPolicyConfig | null;
  setFlowPolicy: Dispatch<SetStateAction<FlowPolicyConfig | null>>;
  flowRuntimeSnapshot: FlowRuntimeSnapshot | null;
  setFlowRuntimeSnapshot: Dispatch<SetStateAction<FlowRuntimeSnapshot | null>>;
  scenarioPacks: ScenarioPackConfig[];
  setScenarioPacks: Dispatch<SetStateAction<ScenarioPackConfig[]>>;
  flowSkillDescriptors: FlowSkillDescriptor[];
  setFlowSkillDescriptors: Dispatch<SetStateAction<FlowSkillDescriptor[]>>;
  mcpServers: McpServerConfig[];
  setMcpServers: Dispatch<SetStateAction<McpServerConfig[]>>;
  uploadedSkills: UploadedSkill[];
  setUploadedSkills: Dispatch<SetStateAction<UploadedSkill[]>>;
  masterVersions: MasterPromptVersionItem[];
  setMasterVersions: Dispatch<SetStateAction<MasterPromptVersionItem[]>>;
  masterDiff: string;
  setMasterDiff: Dispatch<SetStateAction<string>>;
  releaseHistory: ReleaseRecord[];
  setReleaseHistory: Dispatch<SetStateAction<ReleaseRecord[]>>;

  // 发布与保存状态
  releaseNote: string;
  setReleaseNote: Dispatch<SetStateAction<string>>;
  releaseVersion: string;
  setReleaseVersion: Dispatch<SetStateAction<string>>;
  releaseRiskLevel: string;
  setReleaseRiskLevel: Dispatch<SetStateAction<string>>;
  saveStatus: string;
  setSaveStatus: Dispatch<SetStateAction<string>>;

  // 业务智能体编辑抽屉
  editingAgent: BusinessAgentConfig | null;
  setEditingAgent: Dispatch<SetStateAction<BusinessAgentConfig | null>>;
  editingAgentOpen: boolean;
  setEditingAgentOpen: Dispatch<SetStateAction<boolean>>;
  agentConfigTab: AgentConfigTabKey;
  setAgentConfigTab: Dispatch<SetStateAction<AgentConfigTabKey>>;
  agentTestInput: string;
  setAgentTestInput: Dispatch<SetStateAction<string>>;
  agentTestMessages: Array<{ role: ChatRole; content: string }>;
  setAgentTestMessages: Dispatch<SetStateAction<Array<{ role: ChatRole; content: string }>>>;
  agentTestLoading: boolean;
  setAgentTestLoading: Dispatch<SetStateAction<boolean>>;

  // 数据加载
  loadAdminData: () => Promise<void>;
  saveSection: (url: string, body: unknown, successText: string, failText: string) => Promise<void>;

  // 模型中心操作
  updateModelRecord: (target: ModelRecord, patch: Partial<ModelRecord>) => void;
  addModelRecord: () => void;
  removeModelRecord: (target: ModelRecord) => void;
  setDefaultModel: (target: ModelRecord, checked: boolean) => void;
  saveModelCenter: () => Promise<void>;

  // 各区块保存
  saveMasterConfig: () => Promise<void>;
  saveEditingBusinessAgent: () => Promise<void>;
  savePermissionRules: () => Promise<void>;
  saveRolePolicies: () => Promise<void>;
  saveUserAccounts: () => Promise<void>;
  saveSkillPolicies: () => Promise<void>;
  saveFlowPolicy: () => Promise<void>;
  saveScenarioPacks: () => Promise<void>;
  saveFlowSkillDescriptors: () => Promise<void>;
  saveMcpServers: () => Promise<void>;
  saveUploadedSkills: () => Promise<void>;

  // Master 提示词
  publishMasterPrompt: () => Promise<void>;
  diffMasterPrompt: (version?: string) => Promise<void>;
  rollbackMasterPrompt: (version: string) => Promise<void>;

  // 智能体测试与发布
  runAgentTest: () => Promise<void>;
  publishConfigSnapshot: () => Promise<void>;
}
