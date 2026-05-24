import type { ContainerKey } from './constants/containers';

export type Granularity = 'hour' | 'day';
export type LogLevel = 'INFO' | 'WARNING' | 'ERROR' | 'DEBUG' | 'UNKNOWN';

export interface FilterState {
  startTs: string;
  endTs: string;
  container: ContainerKey;
  queryLike?: string;
  staffCode?: string;
  sessionId?: string;
  requestId?: string;
  status?: string;
  agentCode?: string;
  tool?: string;
  granularity: Granularity;
  logLevels?: LogLevel[];
}

export interface OverviewData {
  total_requests: number;
  success_requests: number;
  success_rate: number;
  error_rate: number;
  duration_s: {
    avg: number;
    p50: number;
    p90: number;
    p95: number;
    max: number;
  };
  token: {
    total: number;
    avg_per_request: number;
    efficiency_per_success_request: number;
  };
  tool_calls: {
    total: number;
    per_request: number;
  };
  scene_confidence_avg: {
    big_scene: number;
    sub_scene: number;
  };
}

export interface TrendPoint {
  bucket_ts: string;
  total_requests: number;
  success_rate: number;
  avg_duration_s: number;
  token_total: number;
}

export interface UserMetrics {
  staff_code: string;
  request_count: number;
  success_rate: number;
  avg_duration_s: number;
  p95_duration_s: number;
  token_total: number;
  tool_calls_per_request: number;
}

export interface AgentMetrics {
  agent_code: string;
  agent_name: string;
  call_count: number;
  success_rate: number;
  avg_duration_s: number;
  slow_call_ratio: number;
}

export interface ToolMetrics {
  tool: string;
  call_count: number;
  success_rate: number;
  avg_duration_s: number;
  failed_count: number;
}

export interface ToolMetricsPayload {
  items: ToolMetrics[];
  failure_top: ToolMetrics[];
}

export interface RequestListItem {
  request_id: string;
  session_id: string;
  staff_code: string;
  status: string;
  duration_s: number;
  start_ts: string;
  end_ts: string;
  query: string;
  agents_called: string[];
  token_total: number;
  tool_call_count: number;
}

export interface RequestListPayload {
  total: number;
  page: number;
  page_size: number;
  items: RequestListItem[];
}

export interface RequestDetail {
  request: {
    request_id: string;
    state_id?: string;
    session_id?: string;
    staff_code?: string;
    query?: string;
    status?: string;
    error?: string;
    start_ts?: string;
    end_ts?: string;
    duration_s: number;
    token_total: number;
    scene_result?: Record<string, unknown>;
  };
  agent_timeline: Array<Record<string, unknown>>;
  agent_events: Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>;
  summary: {
    agent_event_count: number;
    tool_call_count: number;
  };
}

export interface TimeAlignPayload {
  timezone: string;
  start_local: string;
  end_local: string;
  start_utc: string;
  end_utc: string;
  start_ns: string;
  end_ns: string;
  buffered_start_utc: string;
  buffered_end_utc: string;
  buffered_start_ns: string;
  buffered_end_ns: string;
  buffer_seconds: number;
}

export interface CorrelationLogItem {
  ts_ns: string;
  ts_utc?: string;
  line: string;
  raw_line?: string;
  stream?: Record<string, unknown>;
  rid?: string;
  task_id?: string;
  request_id?: string;
  req_id?: string;
  sid?: string;
  aid?: string;
  parid?: string;
  level?: LogLevel;
  correlation_id?: string;
  correlation_id_source?: string;
  is_main_chain?: boolean;
  is_alert?: boolean;
}

export interface MainChainHighlights {
  main_chain_aids: string[];
  alert_count: number;
  level_breakdown: Record<string, number>;
  alert_logs: CorrelationLogItem[];
}

export interface TraceChainNode {
  aid: string;
  parid: string;
  first_ts_utc?: string;
  last_ts_utc?: string;
  log_count: number;
  level_breakdown: Record<string, number>;
  sid_candidates: string[];
  mongo_agent_codes: string[];
  mongo_agent_names: string[];
  mongo_tools: string[];
  source_hits: {
    logs: number;
    agent_payload: number;
    tool_call: number;
  };
}

export interface TraceChainPayload {
  request_id: string;
  session_id: string;
  nodes: TraceChainNode[];
  edges: Array<{ from: string; to: string; count: number }>;
  root_nodes: string[];
  unresolved_parents: string[];
  mongo_link_stats: Record<string, unknown>;
}

export interface CorrelationRidPayload {
  container: string;
  request_id: string;
  time_window: TimeAlignPayload;
  request: Record<string, unknown>;
  agent_timeline: Array<Record<string, unknown>>;
  agent_events: Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>;
  loki_logs: CorrelationLogItem[];
  logs_page: {
    items: CorrelationLogItem[];
    total: number;
    page: number;
    page_size: number;
  };
  trace_chain: TraceChainPayload;
  main_chain_highlights?: MainChainHighlights;
  log_summary: {
    total_logs: number;
    error_hits: number;
    matched_keywords: string[];
    level_breakdown: Record<string, number>;
  };
  correlation_checks: Record<string, unknown>;
  root_cause_hint: string;
}

export interface ErrorClusterItem {
  error_type: string;
  count: number;
  first_ts_utc?: string;
  last_ts_utc?: string;
  sample_request_ids: string[];
  sample_lines: string[];
  level_breakdown?: Record<string, number>;
}

export interface CorrelationErrorsPayload {
  container: string;
  time_window: TimeAlignPayload;
  keywords: string[];
  levels?: LogLevel[];
  total_logs: number;
  clusters: ErrorClusterItem[];
  clusters_page: {
    items: ErrorClusterItem[];
    total: number;
    page: number;
    page_size: number;
  };
}

export interface ToolCallCorrelationPayload {
  request_id: string;
  container: string;
  main_flow_container: string;
  tool: string;
  time_window: TimeAlignPayload;
  request: Record<string, unknown>;
  tool_call: Record<string, unknown>;
  tool_call_candidates: Array<Record<string, unknown>>;
  id_resolution: {
    resolved_value?: string;
    resolved_by?: string;
    source_hit_counts?: Record<string, number>;
  };
  error_summary: {
    alert_count: number;
    level_breakdown: Record<string, number>;
    channel_breakdown: Record<string, number>;
    signature_breakdown?: Record<string, number>;
    matched_keywords?: string[];
    first_alert_ts_utc?: string;
    last_alert_ts_utc?: string;
  };
  main_flow_logs_page: {
    items: CorrelationLogItem[];
    total: number;
    page: number;
    page_size: number;
  };
  cbb_logs_page: {
    items: CorrelationLogItem[];
    total: number;
    page: number;
    page_size: number;
  };
}

export type FridayActionType = 'open_request_detail' | 'open_traces';

export interface FridayAction {
  type: FridayActionType;
  label: string;
  request_id?: string;
  container?: string;
}

export interface FridayEvidenceItem {
  scope?: {
    lookback_days?: number;
    timezone?: string;
    main_containers?: string[];
    slow_threshold_s?: number;
  };
  intent?: string;
  request_id?: string;
  slow_calls_top?: Array<Record<string, unknown>>;
  error_clusters_top?: Array<Record<string, unknown>>;
  request_trace?: Record<string, unknown> | null;
}

export interface FridayMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  created_at: string;
  evidence?: FridayEvidenceItem;
  actions?: FridayAction[];
  error?: string;
  progress?: string;
  streaming?: boolean;
}

export interface FridayConversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: FridayMessage[];
}

export interface FridayConfig {
  configured: boolean;
  base_url: string;
  model: string;
  restart_required: boolean;
  active_base_url?: string;
  active_model?: string;
  config_file?: string;
}

export interface FridayChatRequest {
  message: string;
  conversation_id?: string;
  history?: Array<{ role: 'user' | 'assistant' | 'system'; content: string }>;
  context_overrides?: {
    container?: string;
    request_id?: string;
    rid?: string;
  };
}

export type FridayChatEventType = 'token' | 'evidence' | 'actions' | 'progress' | 'done' | 'error';

export interface FridayChatEvent {
  type: FridayChatEventType;
  data: Record<string, unknown>;
}
