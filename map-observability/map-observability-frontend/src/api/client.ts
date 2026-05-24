import axios from 'axios';

import {
  AgentMetrics,
  CorrelationErrorsPayload,
  CorrelationRidPayload,
  FilterState,
  FridayChatEvent,
  FridayChatRequest,
  FridayConfig,
  LogLevel,
  OverviewData,
  RequestDetail,
  RequestListPayload,
  TimeAlignPayload,
  ToolCallCorrelationPayload,
  ToolMetricsPayload,
  TrendPoint,
  UserMetrics,
} from '../types';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api/v1';

const client = axios.create({
  baseURL: API_BASE_URL,
  timeout: 90000,
});

const appendParam = (params: URLSearchParams, key: string, value?: string | number) => {
  if (value === undefined || value === null || value === '') {
    return;
  }
  params.append(key, String(value));
};

const appendArrayParam = (params: URLSearchParams, key: string, values?: string[]) => {
  if (!values || values.length === 0) {
    return;
  }
  params.append(key, values.join(','));
};

const buildApiUrl = (path: string) => {
  const normalizedBase = API_BASE_URL.endsWith('/') ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
};

export const buildFilterQuery = (filters: FilterState): URLSearchParams => {
  const params = new URLSearchParams();
  appendParam(params, 'start_ts', filters.startTs);
  appendParam(params, 'end_ts', filters.endTs);
  appendParam(params, 'staff_code', filters.staffCode);
  appendParam(params, 'session_id', filters.sessionId);
  appendParam(params, 'request_id', filters.requestId);
  appendParam(params, 'query_like', filters.queryLike);
  appendParam(params, 'status', filters.status);
  appendParam(params, 'agent_code', filters.agentCode);
  appendParam(params, 'tool', filters.tool);
  appendParam(params, 'container', filters.container);
  appendParam(params, 'granularity', filters.granularity);
  appendArrayParam(params, 'log_levels', filters.logLevels);
  return params;
};

export const analyticsApi = {
  async getOverview(filters: FilterState): Promise<OverviewData> {
    const params = buildFilterQuery(filters);
    const { data } = await client.get<OverviewData>(`/overview?${params.toString()}`);
    return data;
  },

  async getTrends(filters: FilterState): Promise<TrendPoint[]> {
    const params = buildFilterQuery(filters);
    const { data } = await client.get<TrendPoint[]>(`/trends?${params.toString()}`);
    return data;
  },

  async getUsers(filters: FilterState, topN = 20): Promise<UserMetrics[]> {
    const params = buildFilterQuery(filters);
    appendParam(params, 'top_n', topN);
    const { data } = await client.get<UserMetrics[]>(`/users?${params.toString()}`);
    return data;
  },

  async getAgents(filters: FilterState, topN = 20): Promise<AgentMetrics[]> {
    const params = buildFilterQuery(filters);
    appendParam(params, 'top_n', topN);
    const { data } = await client.get<AgentMetrics[]>(`/agents?${params.toString()}`);
    return data;
  },

  async getTools(filters: FilterState, topN = 20): Promise<ToolMetricsPayload> {
    const params = buildFilterQuery(filters);
    appendParam(params, 'top_n', topN);
    const { data } = await client.get<ToolMetricsPayload>(`/tools?${params.toString()}`);
    return data;
  },

  async getRequests(
    filters: FilterState,
    page = 1,
    pageSize = 10,
    sortBy = 'start_ts',
    sortOrder = 'desc',
  ): Promise<RequestListPayload> {
    const params = buildFilterQuery(filters);
    appendParam(params, 'page', page);
    appendParam(params, 'page_size', pageSize);
    appendParam(params, 'sort_by', sortBy);
    appendParam(params, 'sort_order', sortOrder);
    const { data } = await client.get<RequestListPayload>(`/requests?${params.toString()}`);
    return data;
  },

  async getRequestDetail(requestId: string, container?: string): Promise<RequestDetail> {
    const params = new URLSearchParams();
    appendParam(params, 'container', container);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    const { data } = await client.get<RequestDetail>(`/requests/${requestId}${suffix}`);
    return data;
  },

  async exportRequestsJsonl(filters: FilterState, requestIds?: string[]): Promise<Blob> {
    const params = buildFilterQuery(filters);
    appendParam(params, 'sort_by', 'start_ts');
    appendParam(params, 'sort_order', 'desc');
    appendParam(params, 'request_ids', requestIds?.join(','));
    const { data } = await client.get<Blob>(`/requests/export/jsonl?${params.toString()}`, {
      responseType: 'blob',
    });
    return data;
  },

  async getTimeAlign(
    startLocal: string,
    endLocal: string,
    tz = 'Asia/Shanghai',
    bufferSeconds = 120,
  ): Promise<TimeAlignPayload> {
    const params = new URLSearchParams();
    appendParam(params, 'start_local', startLocal);
    appendParam(params, 'end_local', endLocal);
    appendParam(params, 'tz', tz);
    appendParam(params, 'buffer_seconds', bufferSeconds);
    const { data } = await client.get<TimeAlignPayload>(`/correlation/time-align?${params.toString()}`);
    return data;
  },

  async getRidCorrelation(
    requestId: string,
    container: string,
    windowSec = 120,
    levels: LogLevel[] = [],
    page = 1,
    pageSize = 10,
  ): Promise<CorrelationRidPayload> {
    const params = new URLSearchParams();
    appendParam(params, 'container', container);
    appendParam(params, 'window_sec', windowSec);
    appendParam(params, 'page', page);
    appendParam(params, 'page_size', pageSize);
    appendArrayParam(params, 'levels', levels);
    const { data } = await client.get<CorrelationRidPayload>(`/correlation/rid/${requestId}?${params.toString()}`);
    return data;
  },

  async getCorrelationErrors(
    startLocal: string,
    endLocal: string,
    container: string,
    keywords = '',
    tz = 'Asia/Shanghai',
    bufferSeconds = 120,
    levels: LogLevel[] = [],
    page = 1,
    pageSize = 10,
    staffCode?: string,
    sessionId?: string,
    requestId?: string,
  ): Promise<CorrelationErrorsPayload> {
    const params = new URLSearchParams();
    appendParam(params, 'container', container);
    appendParam(params, 'start_local', startLocal);
    appendParam(params, 'end_local', endLocal);
    appendParam(params, 'tz', tz);
    appendParam(params, 'keywords', keywords);
    appendParam(params, 'buffer_seconds', bufferSeconds);
    appendParam(params, 'page', page);
    appendParam(params, 'page_size', pageSize);
    appendParam(params, 'staff_code', staffCode);
    appendParam(params, 'session_id', sessionId);
    appendParam(params, 'request_id', requestId);
    appendArrayParam(params, 'levels', levels);
    const { data } = await client.get<CorrelationErrorsPayload>(`/correlation/errors?${params.toString()}`);
    return data;
  },

  async getToolCallCorrelation(
    requestId: string,
    container: string,
    tool: string,
    options?: {
      toolId?: string;
      step?: number;
      levels?: LogLevel[];
      page?: number;
      pageSize?: number;
      windowSec?: number;
    },
  ): Promise<ToolCallCorrelationPayload> {
    const params = new URLSearchParams();
    appendParam(params, 'request_id', requestId);
    appendParam(params, 'container', container);
    appendParam(params, 'tool', tool);
    appendParam(params, 'tool_id', options?.toolId);
    appendParam(params, 'step', options?.step);
    appendParam(params, 'page', options?.page ?? 1);
    appendParam(params, 'page_size', options?.pageSize ?? 10);
    appendParam(params, 'window_sec', options?.windowSec ?? 120);
    appendArrayParam(params, 'levels', options?.levels || []);
    const { data } = await client.get<ToolCallCorrelationPayload>(`/correlation/tool-call?${params.toString()}`);
    return data;
  },

  async getFridayConfig(): Promise<FridayConfig> {
    const { data } = await client.get<FridayConfig>('/friday/config');
    return data;
  },

  async updateFridayConfig(baseUrl: string, model: string): Promise<FridayConfig> {
    const { data } = await client.put<FridayConfig>('/friday/config', {
      base_url: baseUrl,
      model,
    });
    return data;
  },

  async streamFridayChat(
    payload: FridayChatRequest,
    onEvent: (event: FridayChatEvent) => void,
  ): Promise<void> {
    const response = await fetch(buildApiUrl('/friday/chat'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const errorText = await response.text();
      let detail = errorText;
      try {
        const parsed = JSON.parse(errorText);
        detail = String(parsed?.detail || errorText);
      } catch {
        detail = errorText;
      }
      throw new Error(detail || `HTTP ${response.status}`);
    }

    if (!response.body) {
      throw new Error('Friday 流式响应为空');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    const emitChunk = (chunk: string) => {
      const block = chunk.trim();
      if (!block) {
        return;
      }

      let eventType: FridayChatEvent['type'] = 'token';
      const dataLines: string[] = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) {
          eventType = line.slice(6).trim() as FridayChatEvent['type'];
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trim());
        }
      }

      const rawData = dataLines.join('\n');
      if (!rawData) {
        return;
      }

      let parsedData: Record<string, unknown>;
      try {
        parsedData = JSON.parse(rawData) as Record<string, unknown>;
      } catch {
        parsedData = { text: rawData };
      }
      onEvent({ type: eventType, data: parsedData });
    };

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      buffer = buffer.replace(/\r\n/g, '\n');

      let idx = buffer.indexOf('\n\n');
      while (idx >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        emitChunk(chunk);
        idx = buffer.indexOf('\n\n');
      }

      if (done) {
        if (buffer.trim()) {
          emitChunk(buffer);
        }
        break;
      }
    }
  },
};
