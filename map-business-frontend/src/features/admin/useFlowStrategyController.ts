import { useEffect, useMemo, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { fetchJson } from '../../api/client';
import type {
  FlowPolicyConfig,
  FlowRequestConfig,
  FlowRuntimeSnapshot,
  FlowSkillDescriptor,
  McpServerConfig,
  ScenarioPackConfig,
  UploadedSkill,
} from '../../api/types';
import { cloneFlowRequestConfig, FLOW_MODE_DEFAULT_CONFIG, toFlowRequestConfig } from '../chat/flowConfig';
import { normalizeFlowSkillDescriptor } from './businessAgent';

/**
 * Flow 策略与运行时快照的共享控制器。
 *
 * 聊天端(流式请求构造、会话覆写)与管理端(心流策略页、运行时快照加载)
 * 共同消费 flowPolicy / flowRuntimeSnapshot,此处统一持有并负责加载,
 * 避免 App shell 继续承载业务数据状态。
 */
export interface FlowStrategyController {
  // 策略与运行时快照(共享数据)
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

  // 会话级 flow 策略选择与覆写
  flowUseRuntimePolicy: boolean;
  setFlowUseRuntimePolicy: Dispatch<SetStateAction<boolean>>;
  flowSessionOverride: FlowRequestConfig;
  setFlowSessionOverride: Dispatch<SetStateAction<FlowRequestConfig>>;
  flowOverrideOpen: boolean;
  setFlowOverrideOpen: Dispatch<SetStateAction<boolean>>;

  // 派生配置
  runtimeFlowRequestConfig: FlowRequestConfig;
  effectiveFlowRequestConfig: FlowRequestConfig;
  handleFlowUseRuntimePolicyChange: (checked: boolean) => void;
  resetFlowOverride: () => void;

  loadFlowRuntimeSnapshot: () => Promise<void>;
}

export function useFlowStrategyController(): FlowStrategyController {
  const [flowPolicy, setFlowPolicy] = useState<FlowPolicyConfig | null>(null);
  const [flowRuntimeSnapshot, setFlowRuntimeSnapshot] = useState<FlowRuntimeSnapshot | null>(null);
  const [scenarioPacks, setScenarioPacks] = useState<ScenarioPackConfig[]>([]);
  const [flowSkillDescriptors, setFlowSkillDescriptors] = useState<FlowSkillDescriptor[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerConfig[]>([]);
  const [uploadedSkills, setUploadedSkills] = useState<UploadedSkill[]>([]);
  const [flowUseRuntimePolicy, setFlowUseRuntimePolicy] = useState(true);
  const [flowSessionOverride, setFlowSessionOverride] = useState<FlowRequestConfig>(FLOW_MODE_DEFAULT_CONFIG);
  const [flowOverrideOpen, setFlowOverrideOpen] = useState(false);

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

  const loadFlowRuntimeSnapshot = async () => {
    try {
      const snapshot = await fetchJson<FlowRuntimeSnapshot>('/api/admin/flow-runtime-snapshot');
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

  // 挂载时拉取一次运行时快照,供聊天端 flow 请求与管理端展示使用
  useEffect(() => {
    void loadFlowRuntimeSnapshot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleFlowUseRuntimePolicyChange = (checked: boolean) => {
    setFlowUseRuntimePolicy(checked);
    if (checked) {
      setFlowSessionOverride(cloneFlowRequestConfig(runtimeFlowRequestConfig));
    }
  };

  const resetFlowOverride = () => {
    setFlowSessionOverride(cloneFlowRequestConfig(runtimeFlowRequestConfig));
  };

  return {
    flowPolicy,
    setFlowPolicy,
    flowRuntimeSnapshot,
    setFlowRuntimeSnapshot,
    scenarioPacks,
    setScenarioPacks,
    flowSkillDescriptors,
    setFlowSkillDescriptors,
    mcpServers,
    setMcpServers,
    uploadedSkills,
    setUploadedSkills,
    flowUseRuntimePolicy,
    setFlowUseRuntimePolicy,
    flowSessionOverride,
    setFlowSessionOverride,
    flowOverrideOpen,
    setFlowOverrideOpen,
    runtimeFlowRequestConfig,
    effectiveFlowRequestConfig,
    handleFlowUseRuntimePolicyChange,
    resetFlowOverride,
    loadFlowRuntimeSnapshot,
  };
}
