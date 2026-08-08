import type { FlowPolicyConfig, FlowRequestConfig } from '../../api/types';

export const FLOW_MODE_DEFAULT_CONFIG: FlowPolicyConfig = {
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

export const toFlowRequestConfig = (
  flowPolicy: FlowPolicyConfig | null | undefined,
): FlowRequestConfig => {
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

export const cloneFlowRequestConfig = (config: FlowRequestConfig): FlowRequestConfig => ({
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
