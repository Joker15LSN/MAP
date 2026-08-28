import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, generateRequestId } from '../../api/client';
import { runApi } from '../../api/runApi';
import { conversationApi } from './conversationApi';
import type { ConversationView, FeedbackView, MessageView } from './conversationApi';
import {
  applyRunEvent,
  createEmptyRunProjection,
  mapRunTerminalToMessageStatus,
} from './runProjection';
import type { RunProjectionState } from './runProjection';

/**
 * 会话控制器（Step 4 / PR-F1+F2 重写）。
 *
 * - 一轮问答通过 `runApi.createTurn` 创建 canonical Run，随后只订阅
 *   `/api/v1/runs/{run_id}/events` 并按 `(run_id,seq)` 投影;
 * - 刷新恢复时对每个带 `run_id` 的 assistant 消息重放事件;
 * - stop 走权威 `runApi.cancelRun`;本地 abort 仅作网络/超时兜底;
 * - generation(版本号)守卫：迟到事件/回调不得覆盖更新的终态。
 */

export type ConversationPhase = 'idle' | 'loading' | 'streaming' | 'ready' | 'error';

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

function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function localMessage(
  id: string,
  conversation: ConversationView,
  role: 'user' | 'assistant',
  content: string,
  requestId: string,
): MessageView {
  return {
    id,
    conversation_id: conversation.id,
    role,
    status: role === 'user' ? 'completed' : 'streaming',
    content,
    request_id: requestId,
    task_id: null,
    decision: null,
    run_id: null,
    stream_error: null,
    error_message: null,
    fallback_used: false,
    created_at: new Date().toISOString(),
    completed_at: role === 'user' ? new Date().toISOString() : null,
  };
}

function abortError(): Error {
  return new Error('The operation was aborted.');
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

export function useConversationController(options: UseConversationControllerOptions = {}) {
  const [storedId] = useState<string | null>(() =>
    options.conversationId ? null : readStoredConversationId(),
  );
  const conversationId = options.conversationId ?? storedId ?? undefined;
  const restoredFromStorage = !options.conversationId && Boolean(storedId);
  const [state, setState] = useState<ConversationState>(initialState);
  const abortRef = useRef<AbortController | null>(null);
  // 当前流式 assistant 消息的渲染 id（本地占位 -> 服务端真实 id）。
  const streamingMessageIdRef = useRef<string | null>(null);
  // createTurn 返回前的等待句柄：stop 点击早于 run_id 到达时,等它解析。
  const runIdReadyRef = useRef<Promise<string | null> | null>(null);
  // 当前流式 run 的 durable identity；createTurn 返回后非空。
  const currentRunIdRef = useRef<string | null>(null);
  // generation/版本号:每次 send 自增;stop 成功提交权威终态时再次自增,
  // 使旧的流回调 / GET 回调因版本不匹配而被丢弃。
  const turnRef = useRef(0);
  const [input, setInput] = useState('');

  const create = useCallback(async (mode: 'global' | 'flow' = 'global', title?: string) => {
    setState((prev) => ({ ...prev, phase: 'loading', error: null }));
    try {
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
      const messages = [...conversation.messages];

      for (const message of messages) {
        if (message.role === 'assistant' && message.run_id) {
          try {
            const projection = await replayRunToTerminalOrHead(message.run_id);
            message.content = projection.content;
            if (projection.terminalSeen && projection.terminalStatus) {
              message.status = mapRunTerminalToMessageStatus(projection.terminalStatus);
              message.completed_at = message.completed_at || new Date().toISOString();
            }
          } catch {
            // 投影失败保留 DB 中的 message 事实;不阻塞恢复。
          }
        }
        if (message.role === 'assistant' && message.status === 'completed') {
          try {
            feedbackByMessage[message.id] = await conversationApi.getFeedback(message.id);
          } catch {
            feedbackByMessage[message.id] = null;
          }
        }
      }

      setState((prev) => ({
        ...prev,
        phase: 'ready',
        conversation,
        messages,
        feedbackByMessage,
        error: null,
      }));
    } catch (error) {
      if (restoredFromStorage) {
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
    const userMessage = localMessage(
      `local-user-${requestId}`,
      conversation,
      'user',
      query,
      requestId,
    );
    const assistantPlaceholder = localMessage(
      `local-assistant-${requestId}`,
      conversation,
      'assistant',
      '',
      requestId,
    );
    const abort = new AbortController();
    abortRef.current = abort;
    streamingMessageIdRef.current = assistantPlaceholder.id;
    currentRunIdRef.current = null;
    let resolveRunId: (id: string | null) => void = () => {};
    runIdReadyRef.current = new Promise<string | null>((resolve) => {
      resolveRunId = resolve;
    });

    setState((prev) => ({
      ...prev,
      phase: 'streaming',
      messages: [...prev.messages, userMessage, assistantPlaceholder],
      error: null,
    }));

    let assistantId = assistantPlaceholder.id;

    const adoptServerIds = (
      localUserId: string,
      localAssistantId: string,
      serverUserId: string,
      serverAssistantId: string,
    ) => {
      setState((prev) => ({
        ...prev,
        messages: prev.messages.map((m) => {
          if (m.id === localUserId) {
            return { ...m, id: serverUserId };
          }
          if (m.id === localAssistantId) {
            return { ...m, id: serverAssistantId };
          }
          return m;
        }),
      }));
    };

    const updateAssistantMessage = (messageId: string, projection: RunProjectionState) => {
      setState((prev) => ({
        ...prev,
        messages: prev.messages.map((m) =>
          m.id === messageId
            ? {
                ...m,
                content: projection.content,
                run_id: projection.runId || m.run_id,
              }
            : m,
        ),
      }));
    };

    const finalizeTerminal = (
      messageId: string,
      projection: RunProjectionState | null,
      status: MessageView['status'],
      streamError?: string,
      errorMessage?: string,
    ) => {
      setState((prev) => ({
        ...prev,
        phase: 'ready',
        messages: prev.messages.map((m) =>
          m.id === messageId
            ? {
                ...m,
                status,
                content: projection ? projection.content : m.content,
                run_id: projection ? projection.runId : m.run_id,
                stream_error: streamError || null,
                error_message: errorMessage || null,
                completed_at: new Date().toISOString(),
              }
            : m,
        ),
      }));
    };

    const refreshConversation = async (id: string, expectedTurn: number) => {
      try {
        const view = await conversationApi.get(id);
        if (turnRef.current === expectedTurn) {
          setState((cur) => ({ ...cur, conversation: view }));
        }
      } catch {
        // 元信息刷新失败不覆盖消息事实。
      }
    };

    try {
      const created = await runApi.createTurn(conversation.id, query, {
        requestId,
        idempotencyKey: generateRequestId(),
        signal: abort.signal,
      });
      if (turnRef.current !== turn) {
        return;
      }
      assistantId = created.assistant_message_id;
      streamingMessageIdRef.current = assistantId;
      currentRunIdRef.current = created.run_id;
      resolveRunId(created.run_id);
      adoptServerIds(
        userMessage.id,
        assistantPlaceholder.id,
        created.user_message_id,
        created.assistant_message_id,
      );

      let projection = createEmptyRunProjection(created.run_id);
      // 订阅 run events;快照 EOF 后按 after_seq 重连续传,直到终态。
      while (true) {
        if (abort.signal.aborted) {
          throw abortError();
        }
        for await (const envelope of runApi.replayRunEvents(created.run_id, {
          afterSeq: projection.lastSeq,
          signal: abort.signal,
        })) {
          if (turnRef.current !== turn) {
            return;
          }
          projection = applyRunEvent(projection, envelope);
          updateAssistantMessage(assistantId, projection);
          if (projection.terminalSeen && projection.terminalStatus) {
            finalizeTerminal(
              assistantId,
              projection,
              mapRunTerminalToMessageStatus(projection.terminalStatus),
            );
            void refreshConversation(conversation.id, turn);
            return;
          }
        }
        // EOF without terminal: the run is still producing facts; resume
        // from the last seen seq after a short backoff. 断流不重跑。
        if (turnRef.current !== turn) {
          return;
        }
        await sleep(250);
        if (turnRef.current !== turn) {
          return;
        }
      }
    } catch (error) {
      if (turnRef.current !== turn) {
        return;
      }
      if (abort.signal.aborted) {
        finalizeTerminal(assistantId, null, 'stopped', 'STREAM_ABORTED');
      } else {
        finalizeTerminal(
          assistantId,
          null,
          'failed',
          'STREAM_CORE_ERROR',
          describeError(error),
        );
      }
    } finally {
      if (abortRef.current === abort) {
        abortRef.current = null;
        streamingMessageIdRef.current = null;
        currentRunIdRef.current = null;
        runIdReadyRef.current = null;
      }
    }
  }, [input, state.conversation]);

  const stop = useCallback(async () => {
    let runId = currentRunIdRef.current;
    if (!runId && runIdReadyRef.current) {
      runId = await Promise.race([
        runIdReadyRef.current,
        new Promise<string | null>((resolve) => setTimeout(() => resolve(null), 8000)),
      ]);
    }
    if (runId) {
      try {
        await runApi.cancelRun(runId, 'stopped by user');
        // 权威路径:run cancel 命令已提交,worker 会 settle run.cancelled。
        // 本地立即给出 stopped 终态,并让 generation 守卫丢弃迟到事件。
        turnRef.current += 1;
        abortRef.current?.abort();
        const messageId = streamingMessageIdRef.current;
        if (messageId) {
          setState((prev) => ({
            ...prev,
            phase: 'ready',
            messages: prev.messages.map((m) =>
              m.id === messageId
                ? {
                    ...m,
                    status: 'stopped' as const,
                    stream_error: 'STREAM_ABORTED' as const,
                    completed_at: new Date().toISOString(),
                  }
                : m,
            ),
            error: null,
          }));
        }
        return;
      } catch {
        // 网络/超时失败:落到本地 abort 兜底。
      }
    }
    // 兜底:仅本地 abort(run_id 迟迟不到或服务端 cancel 失败)。
    abortRef.current?.abort();
    const messageId = streamingMessageIdRef.current;
    if (messageId) {
      setState((prev) => ({
        ...prev,
        messages: prev.messages.map((m) =>
          m.id === messageId
            ? {
                ...m,
                status: 'stopped' as const,
                stream_error: 'STREAM_ABORTED' as const,
                completed_at: new Date().toISOString(),
              }
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

async function replayRunToTerminalOrHead(runId: string): Promise<RunProjectionState> {
  let projection = createEmptyRunProjection(runId);
  while (true) {
    let advanced = false;
    for await (const envelope of runApi.replayRunEvents(runId, {
      afterSeq: projection.lastSeq,
    })) {
      advanced = true;
      projection = applyRunEvent(projection, envelope);
    }
    if (!advanced || projection.terminalSeen) {
      return projection;
    }
    await sleep(250);
  }
}
