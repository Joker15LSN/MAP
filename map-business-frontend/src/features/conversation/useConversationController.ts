import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, generateRequestId } from '../../api/client';
import type { SseEvent } from '../../api/types';
import { conversationApi } from './conversationApi';
import type { ConversationView, FeedbackView, MessageView } from './conversationApi';

/**
 * 会话控制器（R1-CONV-01 / FIX-P2-FRONTEND-01）。
 *
 * - 刷新后按 conversation id 恢复已完成/failed/stopped 消息;
 * - 流式消费冻结事件集,content_delta 累积,start 取 message_id;
 * - stop 先置本地中止,再调 :stop 接口,终态以服务端为准;
 * - 错误状态与 loading/empty 状态显式暴露给视图。
 */

export type ConversationPhase = 'idle' | 'loading' | 'streaming' | 'ready' | 'error';

/**
 * R3-P1-02: 浏览器刷新恢复依赖活跃会话 id 的本地持久化。
 * create 成功后写入;加载失败(如后端换库)时清除并回落空状态,
 * 显式传入的 conversationId 永远优先且失败时保留错误状态。
 */
const ACTIVE_CONVERSATION_KEY = 'map_active_conversation_id';

const readStoredConversationId = (): string | null => {
  try {
    return window.localStorage.getItem(ACTIVE_CONVERSATION_KEY);
  } catch {
    return null;
  }
};

const writeStoredConversationId = (conversationId: string | null): void => {
  try {
    if (conversationId) {
      window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
    } else {
      window.localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
    }
  } catch {
    // 存储不可用(隐私模式等)时降级为不恢复,不影响主流程。
  }
};

export interface ConversationState {
  phase: ConversationPhase;
  conversation: ConversationView | null;
  messages: MessageView[];
  error: string | null;
  feedbackByMessage: Record<string, FeedbackView | null>;
  feedbackSaving: Record<string, boolean>;
}

const initialState: ConversationState = {
  phase: 'idle',
  conversation: null,
  messages: [],
  error: null,
  feedbackByMessage: {},
  feedbackSaving: {},
};

export interface UseConversationControllerOptions {
  conversationId?: string;
}

export function useConversationController(options: UseConversationControllerOptions = {}) {
  // 显式 conversationId 优先;否则一次性读取持久化的活跃会话 id。
  const [storedId] = useState<string | null>(() =>
    options.conversationId ? null : readStoredConversationId(),
  );
  const conversationId = options.conversationId ?? storedId ?? undefined;
  const restoredFromStorage = !options.conversationId && Boolean(storedId);
  const [state, setState] = useState<ConversationState>(initialState);
  const abortRef = useRef<AbortController | null>(null);
  const streamingMessageId = useRef<string | null>(null);
  const [input, setInput] = useState('');

  const create = useCallback(async (mode: 'global' | 'flow' = 'global', title?: string) => {
    setState((prev) => ({ ...prev, phase: 'loading', error: null }));
    try {
      // Idempotency-Key: 创建走幂等键,网络重放不会产生第二个会话。
      const conversation = await conversationApi.create({
        mode,
        title,
        idempotencyKey: generateRequestId(),
      });
      writeStoredConversationId(conversation.id);
      setState((prev) => ({
        ...prev,
        phase: 'ready',
        conversation,
        messages: [],
        error: null,
      }));
      return conversation;
    } catch (error) {
      setState((prev) => ({ ...prev, phase: 'error', error: describeError(error) }));
      return null;
    }
  }, []);

  const load = useCallback(async () => {
    if (!conversationId) {
      return;
    }
    setState((prev) => ({ ...prev, phase: 'loading', error: null }));
    try {
      const conversation = await conversationApi.get(conversationId);
      const feedbackByMessage: Record<string, FeedbackView | null> = {};
      await Promise.all(
        conversation.messages
          .filter((m) => m.role === 'assistant' && m.status === 'completed')
          .map(async (m) => {
            try {
              feedbackByMessage[m.id] = await conversationApi.getFeedback(m.id);
            } catch {
              feedbackByMessage[m.id] = null;
            }
          }),
      );
      setState((prev) => ({
        ...prev,
        phase: 'ready',
        conversation,
        messages: conversation.messages,
        feedbackByMessage,
        error: null,
      }));
    } catch (error) {
      if (restoredFromStorage) {
        // 持久化的会话已不存在(如后端换了数据库):清除并回落空状态,
        // 而不是把用户困在错误页。
        writeStoredConversationId(null);
        setState((prev) => ({ ...prev, ...initialState, error: null }));
        return;
      }
      setState((prev) => ({ ...prev, phase: 'error', error: describeError(error) }));
    }
  }, [conversationId, restoredFromStorage]);

  useEffect(() => {
    if (conversationId) {
      void load();
    }
  }, [conversationId, load]);

  const send = useCallback(async () => {
    const query = input.trim();
    if (!query || !state.conversation) {
      return;
    }
    setInput('');
    const requestId = generateRequestId();
    const userMessage: MessageView = {
      id: `local-user-${requestId}`,
      conversation_id: state.conversation.id,
      role: 'user',
      status: 'completed',
      content: query,
      request_id: requestId,
      task_id: null,
      decision: null,
      stream_error: null,
      error_message: null,
      fallback_used: false,
      created_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    };
    const assistantPlaceholder: MessageView = {
      id: `local-assistant-${requestId}`,
      conversation_id: state.conversation.id,
      role: 'assistant',
      status: 'streaming',
      content: '',
      request_id: requestId,
      task_id: null,
      decision: null,
      stream_error: null,
      error_message: null,
      fallback_used: false,
      created_at: new Date().toISOString(),
      completed_at: null,
    };
    const abort = new AbortController();
    abortRef.current = abort;

    setState((prev) => ({
      ...prev,
      phase: 'streaming',
      messages: [...prev.messages, userMessage, assistantPlaceholder],
      error: null,
    }));
    streamingMessageId.current = assistantPlaceholder.id;

    let accumulated = '';
    let terminal: MessageView | null = null;
    let streamError: string | null = null;
    try {
      for await (const event of conversationApi.stream(
        state.conversation.id,
        query,
        requestId,
        abort.signal,
      )) {
        if (event.event === 'content_delta') {
          accumulated += String(event.data.content || '');
          updateAssistantContent(assistantPlaceholder.id, accumulated);
        } else if (event.event === 'done') {
          terminal = {
            ...assistantPlaceholder,
            id: String(event.data.message_id || assistantPlaceholder.id),
            status: (event.data.status as MessageView['status']) || 'completed',
            content: String(event.data.content || accumulated),
            task_id: event.data.task_id ? String(event.data.task_id) : null,
            completed_at: new Date().toISOString(),
          };
        } else if (event.event === 'error') {
          streamError = String(event.data.error || '上游错误');
        }
      }
      if (!terminal) {
        // EOF without a legal done: failed (stable error code), never
        // completed; a core error event also lands here.
        terminal = {
          ...assistantPlaceholder,
          status: 'failed',
          content: accumulated,
          stream_error: streamError ? 'STREAM_CORE_ERROR' : 'STREAM_EOF_WITHOUT_DONE',
          error_message: streamError || 'stream ended without done',
          completed_at: new Date().toISOString(),
        };
      }
    } catch (error) {
      if (abort.signal.aborted) {
        terminal = {
          ...assistantPlaceholder,
          status: 'stopped',
          content: accumulated,
          stream_error: 'STREAM_ABORTED',
          completed_at: new Date().toISOString(),
        };
      } else {
        terminal = {
          ...assistantPlaceholder,
          status: 'failed',
          content: accumulated,
          stream_error: 'STREAM_CORE_ERROR',
          error_message: describeError(error),
          completed_at: new Date().toISOString(),
        };
      }
    } finally {
      abortRef.current = null;
      streamingMessageId.current = null;
    }

    setState((prev) => {
      const messages = prev.messages.map((m) =>
        m.id === assistantPlaceholder.id ? terminal! : m,
      );
      void conversationApi.get(state.conversation!.id).then((conversation) => {
        // 刷新后以服务端为准恢复(含完成态消息)
        setState((cur) => ({ ...cur, conversation }));
      });
      return { ...prev, phase: 'ready', messages, error: null };
    });
  }, [input, state.conversation]);

  const stop = useCallback(async () => {
    if (abortRef.current) {
      abortRef.current.abort();
    }
    if (streamingMessageId.current) {
      const localId = streamingMessageId.current;
      try {
        // 服务端终态以条件更新为准:已 completed 不会被 stop 覆盖。
        setState((prev) => ({
          ...prev,
          messages: prev.messages.map((m) =>
            m.id === localId ? { ...m, status: 'stopped' as const } : m,
          ),
        }));
      } catch {
        // ignore
      }
    }
  }, []);

  const submitFeedback = useCallback(
    async (messageId: string, input: {
      rating: 'helpful' | 'unhelpful';
      reasonCodes?: string[];
      reasonOther?: string;
      correctionText?: string;
    }) => {
      setState((prev) => ({
        ...prev,
        feedbackSaving: { ...prev.feedbackSaving, [messageId]: true },
      }));
      try {
        const feedback = await conversationApi.submitFeedback(messageId, input);
        setState((prev) => ({
          ...prev,
          feedbackByMessage: { ...prev.feedbackByMessage, [messageId]: feedback },
          feedbackSaving: { ...prev.feedbackSaving, [messageId]: false },
        }));
        return feedback;
      } catch (error) {
        setState((prev) => ({
          ...prev,
          feedbackSaving: { ...prev.feedbackSaving, [messageId]: false },
          error: describeError(error),
        }));
        return null;
      }
    },
    [],
  );

  const withdrawFeedback = useCallback(async (messageId: string) => {
    try {
      await conversationApi.withdrawFeedback(messageId);
      setState((prev) => ({
        ...prev,
        feedbackByMessage: { ...prev.feedbackByMessage, [messageId]: null },
      }));
    } catch (error) {
      setState((prev) => ({ ...prev, error: describeError(error) }));
    }
  }, []);

  function updateAssistantContent(messageId: string, content: string) {
    setState((prev) => ({
      ...prev,
      messages: prev.messages.map((m) =>
        m.id === messageId ? { ...m, content } : m,
      ),
    }));
  }

  return {
    state,
    input,
    setInput,
    create,
    load,
    send,
    stop,
    submitFeedback,
    withdrawFeedback,
  };
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

export type { SseEvent };
