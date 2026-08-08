import { useMemo, useRef, useState } from 'react';
import type { RequestDetail } from 'map-tree-core';
import { fetchJson, streamSseEvents } from '../../api/client';
import type { ChatHistoryItem, ChatMessage, ChatMode, ChatModeState, TracePanelMode } from '../../api/types';
import {
  chatReducer,
  createEmptyChatModeState,
  deriveSourcesFromDetail,
  sanitizeText,
  toHistoryPayload,
  type ChatAction,
} from './chatReducer';
import { cloneFlowRequestConfig } from './flowConfig';
import type { ChatViewProps } from './ChatView';
import type { FlowStrategyController } from '../admin/useFlowStrategyController';

export interface ChatControllerResult {
  chatProps: ChatViewProps;
  chatHistory: ChatHistoryItem[];
  activeHistoryId: string | null;
  handleNewChat: () => void;
  handleSelectHistory: (item: ChatHistoryItem) => void;
}

/**
 * 聊天会话控制器:持有会话状态(消息流/历史/输入/溯源面板)与
 * SSE 流式发送逻辑,向 App shell 暴露纯 props 接口。
 */
export function useChatController(flow: FlowStrategyController): ChatControllerResult {
  const [chatMode, setChatMode] = useState<ChatMode>('global');
  const [chatModeState, setChatModeState] = useState<Record<ChatMode, ChatModeState>>({
    global: createEmptyChatModeState(),
    flow: createEmptyChatModeState(),
  });
  const [isStreaming, setIsStreaming] = useState(false);
  const [tracePanelOpen, setTracePanelOpen] = useState(false);
  const [tracePanelMode, setTracePanelMode] = useState<TracePanelMode>('trace');
  const [expandedInputOpen, setExpandedInputOpen] = useState(false);
  const [expandedInputValue, setExpandedInputValue] = useState('');
  const streamAbortRef = useRef<AbortController | null>(null);

  const detail = chatModeState[chatMode].detail;
  const messages = chatModeState[chatMode].messages;
  const inputValue = chatModeState[chatMode].inputValue;
  const chatHistory = chatModeState[chatMode].chatHistory;
  const activeHistoryId = chatModeState[chatMode].activeHistoryId;

  const setChatHistory = (updater: ChatHistoryItem[] | ((current: ChatHistoryItem[]) => ChatHistoryItem[])) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextChatHistory = typeof updater === 'function' ? updater(modeState.chatHistory) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          chatHistory: nextChatHistory,
        },
      };
    });
  };

  const setActiveHistoryId = (updater: string | null | ((current: string | null) => string | null)) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextActiveHistoryId = typeof updater === 'function' ? updater(modeState.activeHistoryId) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          activeHistoryId: nextActiveHistoryId,
        },
      };
    });
  };

  const setMessages = (updater: ChatMessage[] | ((current: ChatMessage[]) => ChatMessage[])) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextMessages = typeof updater === 'function' ? updater(modeState.messages) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          messages: nextMessages,
        },
      };
    });
  };

  const setInputValue = (updater: string | ((current: string) => string)) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextInputValue = typeof updater === 'function' ? updater(modeState.inputValue) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          inputValue: nextInputValue,
        },
      };
    });
  };

  const setDetail = (
    updater: RequestDetail | undefined | ((current: RequestDetail | undefined) => RequestDetail | undefined),
  ) => {
    setChatModeState((current) => {
      const modeState = current[chatMode];
      const nextDetail = typeof updater === 'function' ? updater(modeState.detail) : updater;
      return {
        ...current,
        [chatMode]: {
          ...modeState,
          detail: nextDetail,
        },
      };
    });
  };

  /** SSE 事件驱动的会话状态更新统一走 chatReducer */
  const dispatchChat = (action: ChatAction) => {
    setChatModeState((current) => ({
      ...current,
      [chatMode]: chatReducer(current[chatMode], action),
    }));
  };

  const latestAssistantContent = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'assistant') {
        return messages[i].content;
      }
    }
    return '';
  }, [messages]);

  const activeHistoryItem = useMemo(
    () => chatHistory.find((item) => item.id === activeHistoryId) || null,
    [chatHistory, activeHistoryId],
  );

  const traceSourceItems = useMemo(() => {
    if (activeHistoryItem?.sources?.length) {
      return activeHistoryItem.sources;
    }
    return deriveSourcesFromDetail(detail, latestAssistantContent);
  }, [activeHistoryItem, detail, latestAssistantContent]);

  const stopStreaming = () => {
    if (!streamAbortRef.current) {
      return;
    }
    streamAbortRef.current.abort();
    streamAbortRef.current = null;
    setIsStreaming(false);
    dispatchChat({ type: 'stop' });
  };

  const handleSend = async (query: string) => {
    const trimmed = query.trim();
    if (!trimmed || isStreaming) {
      return;
    }
    const requestMode = chatMode;
    const streamEndpoint = requestMode === 'flow' ? '/api/chat/stream/flow/v1' : '/api/chat/stream/v2';
    const syncEndpoint = requestMode === 'flow' ? '/api/chat/flow/v1' : '/api/chat';
    const flowConfigForRequest =
      requestMode === 'flow'
        ? cloneFlowRequestConfig(flow.flowUseRuntimePolicy ? flow.runtimeFlowRequestConfig : flow.effectiveFlowRequestConfig)
        : undefined;
    const requestPayload = {
      query: trimmed,
      history: toHistoryPayload(messages),
      ...(requestMode === 'flow' ? { flow_config: flowConfigForRequest } : {}),
    };

    const historyId = `h-${Date.now()}`;

    const userMessage: ChatMessage = {
      id: `u-${Date.now()}`,
      role: 'user',
      content: trimmed,
    };
    const assistantMessage: ChatMessage = {
      id: `a-${Date.now()}`,
      role: 'assistant',
      content: '',
      agentNames: [],
    };

    dispatchChat({
      type: 'send',
      userMessage,
      assistantMessage,
      historyItem: {
        id: historyId,
        question: trimmed,
        answer: '',
        created_at: new Date().toISOString(),
        sources: [],
      },
    });
    setTracePanelOpen(false);
    setTracePanelMode('trace');
    setInputValue('');
    setIsStreaming(true);
    const controller = new AbortController();
    streamAbortRef.current = controller;

    try {
      for await (const frame of streamSseEvents({
        endpoint: streamEndpoint,
        payload: requestPayload,
        signal: controller.signal,
      })) {
        if (frame.event === 'start') {
          dispatchChat({
            type: 'start',
            requestId: sanitizeText(frame.data.request_id),
            stateId: sanitizeText(frame.data.state_id),
          });
        }

        if (frame.event === 'meta') {
          dispatchChat({ type: 'meta', meta: frame.data });
        }

        if (frame.event === 'content_delta') {
          const delta = sanitizeText(frame.data.content);
          if (delta) {
            dispatchChat({ type: 'appendDelta', delta, historyId });
          }
        }

        if (frame.event === 'done') {
          dispatchChat({
            type: 'done',
            historyId,
            finalContent: sanitizeText(frame.data.content),
            flowDoneMeta: requestMode === 'flow' ? frame.data.meta : undefined,
          });
        }

        if (frame.event === 'error') {
          throw new Error(sanitizeText(frame.data.error) || 'stream error');
        }
      }
    } catch (error) {
      const aborted = error instanceof Error && error.name === 'AbortError';
      if (aborted) {
        setChatHistory((historyRows) =>
          historyRows.map((item) =>
            item.id === historyId
              ? {
                  ...item,
                  detail: detail
                    ? {
                        ...detail,
                        request: {
                          ...detail.request,
                          status: 'stopped',
                        },
                      }
                    : item.detail,
                }
              : item,
          ),
        );
        return;
      }
      const syncResponse = await fetchJson<{ content?: string }>(syncEndpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestPayload),
      });
      const content = sanitizeText(syncResponse.content);
      setMessages((current) => {
        const cloned = [...current];
        const last = cloned[cloned.length - 1];
        if (!last || last.role !== 'assistant') {
          return current;
        }
        cloned[cloned.length - 1] = {
          ...last,
          content,
        };
        return cloned;
      });
      setDetail((current) => {
        if (!current) {
          return current;
        }
        const next = {
          ...current,
          request: {
            ...current.request,
            status: 'success',
          },
        };
        setChatHistory((historyRows) =>
          historyRows.map((item) =>
            item.id === historyId
              ? {
                  ...item,
                  answer: content,
                  detail: next,
                  sources: deriveSourcesFromDetail(next, content),
                }
              : item,
          ),
        );
        return {
          ...next,
        };
      });
    } finally {
      streamAbortRef.current = null;
      setIsStreaming(false);
    }
  };

  const handleNewChat = () => {
    dispatchChat({ type: 'reset' });
    setTracePanelOpen(false);
  };

  const handleSelectHistory = (item: ChatHistoryItem) => {
    dispatchChat({ type: 'loadHistory', item });
    setTracePanelOpen(false);
    setTracePanelMode('trace');
  };

  const chatProps: ChatViewProps = {
    chatMode,
    onChatModeChange: (mode) => setChatMode(mode),
    messages,
    isStreaming,
    inputValue,
    onInputChange: (value) => setInputValue(value),
    onSend: (query) => void handleSend(query),
    onStop: () => stopStreaming(),
    detail,
    tracePanelOpen,
    tracePanelMode,
    onTracePanelOpenChange: (open) => setTracePanelOpen(open),
    onTracePanelModeChange: (mode) => setTracePanelMode(mode),
    traceSourceItems,
    latestAssistantContent,
    flowUseRuntimePolicy: flow.flowUseRuntimePolicy,
    onFlowUseRuntimePolicyChange: flow.handleFlowUseRuntimePolicyChange,
    flowRuntimeSnapshotUpdatedAt: flow.flowRuntimeSnapshot?.updated_at,
    flowSessionOverride: flow.flowSessionOverride,
    onFlowSessionOverrideChange: flow.setFlowSessionOverride,
    flowOverrideOpen: flow.flowOverrideOpen,
    onFlowOverrideOpenChange: (open) => flow.setFlowOverrideOpen(open),
    onFlowOverrideReset: flow.resetFlowOverride,
    expandedInputOpen,
    expandedInputValue,
    onExpandedInputOpenChange: (open) => setExpandedInputOpen(open),
    onExpandedInputValueChange: (value) => setExpandedInputValue(value),
  };

  return {
    chatProps,
    chatHistory,
    activeHistoryId,
    handleNewChat,
    handleSelectHistory,
  };
}
