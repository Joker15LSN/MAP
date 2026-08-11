import type { RequestDetail } from 'map-tree-core';
import { sanitizeText } from './chatReducer';

export interface FlowHitData {
  flowConfig: unknown;
  flowSnapshot: unknown;
  flowPolicyHit: unknown;
  matchedScenarios: Record<string, unknown>[];
  fallbackReason: string;
  flowGraph: unknown;
  skillAuthorization: Record<string, unknown>[];
  nodeResults: Record<string, unknown>[];
  stepVerdicts: Record<string, unknown>[];
  repairEvents: Record<string, unknown>[];
  flowDoneMeta: unknown;
}

export const computeFlowHitData = (detail: RequestDetail | undefined): FlowHitData | null => {
  if (!detail) {
    return null;
  }
  const requestRecord = (detail.request || {}) as unknown as Record<string, unknown>;
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
};
