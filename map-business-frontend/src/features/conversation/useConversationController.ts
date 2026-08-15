import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, generateRequestId } from '../../api/client';
import type { SseEvent } from '../../api/types';
import { conversationApi } from './conversationApi';
import type { ConversationView, FeedbackView, MessageView } from './conversationApi';

/**
 * 会话控制器（R1-CONV-01 / FIX-P2-FRONTEND-01 / S4-05）。
 *
 * - 刷新后按 conversation id 恢复已完成/failed/stopped 消息;
 * - 流式消费冻结事件集,content_delta 累积,start 立即保存真实 message_id;
 * - stop 先调服务端 :stop 接口(条件更新为准),本地 abort 仅作超时/网络兜底;
 * - generation(版本号)防止旧的 GET/流回调覆盖更新的 stopped 终态;
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
  // 当前流式消息的渲染 id:先为本地占位 id,收到 start 后换成真实服务端 id。
  const streamingMessageId = useRef<string | null>(null);
  // 真实服务端 message id:仅在收到 start 后非空,用于 stop 接口调用。
  const serverMessageIdRef = useRef<string | null>(null);
  // start 尚未到达时被点击 stop 的等待句柄:resolve 后拿到真实服务端 id,
  // 避免「只 abort 本地 SSE、服务端继续执行」的竞态。
  const serverIdReadyRef = useRef<Promise<string> | null>(null);
  // generation/版本号:每次 send 自增;stop 成功提交权威终态时再次自增,
  // 使旧的流回调 / GET 回调因版本不匹配而被丢弃。
  const turnRef = useRef(0);
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
    const conversation = state.conversation;
    setInput('');
    const requestId = generateRequestId();
    const turn = ++turnRef.current;
    const userMessage: MessageView = {
      id: `local-user-${requestId}`,
      conversation_id: conversation.id,
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
      conversation_id: conversation.id,
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
    // 当前这条 assistant 消息的渲染 id:先占位,start 后切换为真实 id。
    let assistantId = assistantPlaceholder.id;
    const abort = new AbortController();
    abortRef.current = abort;
    streamingMessageId.current = assistantPlaceholder.id;
    serverMessageIdRef.current = null;
    let resolveServerId: (id: string) => void = () => {};
    serverIdReadyRef.current = new Promise<string>((resolve) => {
      resolveServerId = resolve;
    });

    setState((prev) => ({
      ...prev,
      phase: 'streaming',
      messages: [...prev.messages, userMessage, assistantPlaceholder],
      error: null,
    }));

    let accumulated = '';
    let terminal: MessageView | null = null;
    let streamError: string | null = null;
    try {
      for await (const event of conversationApi.stream(
        conversation.id,
        query,
        requestId,
        abort.signal,
      )) {
        if (turnRef.current !== turn) {
          // stop 已提交权威终态:丢弃本 turn 剩余的流事件。
          break;
        }
        if (event.event === 'start') {
          const serverId = String(event.data.message_id || '');
          if (serverId) {
            assistantId = serverId;
            streamingMessageId.current = serverId;
            serverMessageIdRef.current = serverId;
            resolveServerId(serverId);
            adoptMessageId(assistantPlaceholder.id, serverId);
          }
        } else if (event.event === 'content_delta') {
          accumulated += String(event.data.content || '');
          updateAssistantContent(assistantId, accumulated);
        } else if (event.event === 'done') {
          terminal = {
            ...assistantPlaceholder,
            id: String(event.data.message_id || assistantId),
            status: (event.data.status as MessageView['status']) || 'completed',
            content: String(event.data.content || accumulated),
            task_id: event.data.task_id ? String(event.data.task_id) : null,
            completed_at: new Date().toISOString(),
          };
        } else if (event.event === 'error') {
          streamError = String(event.data.error || '上游错误');
        }
      }
      if (turnRef.current !== turn) {
        return;
      }
      if (!terminal) {
        // EOF without a legal done: failed (stable error code), never
        // completed; a core error event also lands here.
        terminal = {
          ...assistantPlaceholder,
          id: assistantId,
          status: 'failed',
          content: accumulated,
          stream_error: streamError ? 'STREAM_CORE_ERROR' : 'STREAM_EOF_WITHOUT_DONE',
          error_message: streamError || 'stream ended without done',
          completed_at: new Date().toISOString(),
        };
      }
    } catch (error) {
      if (turnRef.current !== turn) {
        return;
      }
      if (abort.signal.aborted) {
        // 本地 abort 兜底:服务端停止未走通(超时/网络),或 start 尚未到达。
        terminal = {
          ...assistantPlaceholder,
          id: assistantId,
          status: 'stopped',
          content: accumulated,
          stream_error: 'STREAM_ABORTED',
          completed_at: new Date().toISOString(),
        };
      } else {
        terminal = {
          ...assistantPlaceholder,
          id: assistantId,
          status: 'failed',
          content: accumulated,
          stream_error: 'STREAM_CORE_ERROR',
          error_message: describeError(error),
          completed_at: new Date().toISOString(),
        };
      }
    } finally {
      // 仅当本 turn 仍持有 controller 时清理,避免清掉新一轮 send 的引用。
      if (abortRef.current === abort) {
        abortRef.current = null;
        streamingMessageId.current = null;
        serverMessageIdRef.current = null;
        serverIdReadyRef.current = null;
      }
    }

    setState((prev) => {
      const messages = prev.messages.map((m) =>
        m.id === assistantPlaceholder.id || m.id === assistantId ? terminal! : m,
      );
      // 服务端刷新只更新 conversation 元信息;带 generation 守卫,旧回调
      // 不得覆盖更新的 stopped 终态。
      void conversationApi.get(conversation.id).then((conversationView) => {
        if (turnRef.current === turn) {
          setState((cur) => ({ ...cur, conversation: conversationView }));
        }
      });
      return { ...prev, phase: 'ready', messages, error: null };
    });
  }, [input, state.conversation]);

  const stop = useCallback(async () => {
    // S4-05 race fix: stop 可能早于 start 事件被点击。先短暂等待真实服务端
    // id(最多 2.5s),拿到后走权威路径;等待超时才落到本地 abort 兜底。
    let serverId = serverMessageIdRef.current;
    if (!serverId && serverIdReadyRef.current) {
      serverId = await Promise.race([
        serverIdReadyRef.current,
        new Promise<string | null>((resolve) => setTimeout(() => resolve(null), 2500)),
      ]);
    }
    if (serverId) {
      try {
        // 权威路径:服务端条件更新与终态为准。done 先赢时接口显式返回
        // completed,此处不得把终态回退成 streaming/completed。8s 超时
        // 保证 stop 永不悬挂(失败落到本地 abort 兜底)。
        const terminal = await conversationApi.stop(serverId, {
          signal: AbortSignal.timeout(8000),
        });
        turnRef.current += 1; // 使本 turn 的流/GET 回调失效
        serverMessageIdRef.current = null;
        streamingMessageId.current = null;
        setState((prev) => ({
          ...prev,
          phase: 'ready',
          messages: prev.messages.map((m) => (m.id === serverId ? terminal : m)),
          error: null,
        }));
        return;
      } catch {
        // 网络/超时失败:落到本地 abort 兜底。
      }
    }
    // 兜底:仅本地 abort(start 迟迟不到或服务端停止失败)。
    abortRef.current?.abort();
    const localId = streamingMessageId.current;
    if (localId) {
      setState((prev) => ({
        ...prev,
        messages: prev.messages.map((m) =>
          m.id === localId
            ? { ...m, status: 'stopped' as const, stream_error: 'STREAM_ABORTED' as const }
            : m,
        ),
      }));
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

  function adoptMessageId(localId: string, serverId: string) {
    // start 携带真实 message_id:把本地占位 id 换成服务端 id,后续
    // content_delta / done / stop 都按真实 id 定位。
    setState((prev) => ({
      ...prev,
      messages: prev.messages.map((m) => (m.id === localId ? { ...m, id: serverId } : m)),
    }));
  }

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
