import type { RequestDetail } from 'map-tree-core';
import type {
  ChatHistoryItem,
  ChatMessage,
  ChatModeState,
  SourceReferenceItem,
} from '../../api/types';

/**
 * 聊天会话状态更新逻辑(纯 reducer)。
 *
 * 将 App.tsx 中散落在 handleSend / applyMetaEvent / stopStreaming 内的
 * setMessages / setChatHistory / setDetail 更新逻辑收敛为可独立测试的纯函数。
 */

export const nowIso = () => new Date().toISOString();

export const sanitizeText = (value: unknown): string => {
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

export const dedupeStrings = (items: string[]): string[] => Array.from(new Set(items.filter(Boolean)));

export const extractAgentNamesFromRows = (rows: Record<string, unknown>[]): string[] =>
  dedupeStrings(rows.map((row) => sanitizeText(row.agent_name) || sanitizeText(row.agent_code)).filter(Boolean));

export const extractAgentNamesFromDetail = (requestDetail: RequestDetail | undefined): string[] => {
  if (!requestDetail?.agent_timeline?.length) {
    return [];
  }
  return dedupeStrings(
    requestDetail.agent_timeline
      .map((row) => sanitizeText(row?.agent_name) || sanitizeText(row?.agent_code))
      .filter(Boolean),
  );
};

export const toHistoryPayload = (messages: ChatMessage[]) =>
  messages.map((item) => ({
    role: item.role,
    content: item.content,
  }));

export const createEmptyChatModeState = (): ChatModeState => ({
  chatHistory: [],
  activeHistoryId: null,
  messages: [],
  inputValue: '',
  detail: undefined,
});

export const createBaseDetail = (query: string): RequestDetail => ({
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

export const deriveSourcesFromDetail = (
  requestDetail: RequestDetail | undefined,
  answer: string,
): SourceReferenceItem[] => {
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

const appendDeltaToLastAssistant = (messages: ChatMessage[], delta: string): ChatMessage[] => {
  const cloned = [...messages];
  const last = cloned[cloned.length - 1];
  if (!last || last.role !== 'assistant') {
    return messages;
  }
  cloned[cloned.length - 1] = {
    ...last,
    content: `${last.content}${delta}`,
  };
  return cloned;
};

const setAssistantContent = (messages: ChatMessage[], content: string, force: boolean): ChatMessage[] => {
  const cloned = [...messages];
  const last = cloned[cloned.length - 1];
  if (!last || last.role !== 'assistant') {
    return messages;
  }
  if (!force && !content) {
    return messages;
  }
  cloned[cloned.length - 1] = {
    ...last,
    content,
  };
  return cloned;
};

const appendAgentNamesToLastAssistant = (messages: ChatMessage[], agentNames: string[]): ChatMessage[] => {
  const cloned = [...messages];
  const last = cloned[cloned.length - 1];
  if (!last || last.role !== 'assistant') {
    return messages;
  }
  cloned[cloned.length - 1] = {
    ...last,
    agentNames: dedupeStrings([...(last.agentNames || []), ...agentNames]),
  };
  return cloned;
};

export type ChatAction =
  | { type: 'send'; userMessage: ChatMessage; assistantMessage: ChatMessage; historyItem: ChatHistoryItem }
  | { type: 'appendDelta'; delta: string; historyId: string }
  | { type: 'meta'; meta: Record<string, unknown> }
  | { type: 'start'; requestId: string; stateId: string }
  | { type: 'done'; historyId: string; finalContent: string; flowDoneMeta?: unknown }
  | { type: 'stop' }
  | { type: 'loadHistory'; item: ChatHistoryItem }
  | { type: 'reset' }
  | { type: 'setInputValue'; value: string }
  | { type: 'setActiveHistoryId'; id: string | null };

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

const applyMetaToDetail = (current: RequestDetail, meta: Record<string, unknown>): RequestDetail => {
  const phase = sanitizeText(meta.phase);
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
        ...(((next.request as unknown as Record<string, unknown>).flow_step_verdicts as unknown[]) || []),
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
};

export const chatReducer = (state: ChatModeState, action: ChatAction): ChatModeState => {
  switch (action.type) {
    case 'send': {
      return {
        ...state,
        chatHistory: [action.historyItem, ...state.chatHistory],
        activeHistoryId: action.historyItem.id,
        messages: [...state.messages, action.userMessage, action.assistantMessage],
        detail: createBaseDetail(action.userMessage.content),
      };
    }
    case 'appendDelta': {
      if (!action.delta) {
        return state;
      }
      return {
        ...state,
        messages: appendDeltaToLastAssistant(state.messages, action.delta),
        chatHistory: state.chatHistory.map((item) =>
          item.id === action.historyId ? { ...item, answer: `${item.answer}${action.delta}` } : item,
        ),
      };
    }
    case 'meta': {
      const meta = action.meta;
      const phase = sanitizeText(meta.phase);
      let messages = state.messages;

      if (phase === 'agent_result') {
        const rows = Array.isArray(meta.agents) ? (meta.agents as Record<string, unknown>[]) : [];
        const agentNames = extractAgentNamesFromRows(rows);
        if (agentNames.length > 0) {
          messages = appendAgentNamesToLastAssistant(messages, agentNames);
        }
      }

      if (!state.detail) {
        return { ...state, messages };
      }

      return {
        ...state,
        messages,
        detail: applyMetaToDetail(state.detail, meta),
      };
    }
    case 'start': {
      if (!state.detail) {
        return state;
      }
      return {
        ...state,
        detail: {
          ...state.detail,
          request: {
            ...state.detail.request,
            request_id: action.requestId || state.detail.request.request_id,
            state_id: action.stateId,
          },
        },
      };
    }
    case 'done': {
      if (!state.detail) {
        return state;
      }
      let next: RequestDetail = {
        ...state.detail,
        request: {
          ...state.detail.request,
          status: 'success',
        },
      };
      if (action.flowDoneMeta && typeof action.flowDoneMeta === 'object') {
        const doneMeta = action.flowDoneMeta as Record<string, unknown>;
        next = {
          ...next,
          request: {
            ...next.request,
            flow_done_meta: doneMeta.flow,
          } as unknown as RequestDetail['request'],
        };
      }
      return {
        ...state,
        messages: setAssistantContent(state.messages, action.finalContent, false),
        chatHistory: state.chatHistory.map((item) =>
          item.id === action.historyId
            ? {
                ...item,
                answer: action.finalContent || item.answer,
                detail: next,
                sources: deriveSourcesFromDetail(next, action.finalContent || item.answer),
              }
            : item,
        ),
        detail: next,
      };
    }
    case 'stop': {
      if (!state.detail) {
        return state;
      }
      return {
        ...state,
        detail: {
          ...state.detail,
          request: {
            ...state.detail.request,
            status: 'stopped',
          },
        },
      };
    }
    case 'loadHistory': {
      const item = action.item;
      const messages: ChatMessage[] = [
        { id: `${item.id}-q`, role: 'user', content: item.question },
        {
          id: `${item.id}-a`,
          role: 'assistant',
          content: item.answer,
          agentNames: extractAgentNamesFromDetail(item.detail),
        },
      ];
      return {
        ...state,
        messages,
        detail: item.detail,
        activeHistoryId: item.id,
      };
    }
    case 'reset': {
      return {
        ...state,
        messages: [],
        detail: undefined,
        inputValue: '',
        activeHistoryId: null,
      };
    }
    case 'setInputValue': {
      return { ...state, inputValue: action.value };
    }
    case 'setActiveHistoryId': {
      return { ...state, activeHistoryId: action.id };
    }
    default:
      return state;
  }
};
