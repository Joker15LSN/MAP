import { deleteJson, fetchJson, postJson, putJson, streamSseEvents } from '../../api/client';
import type { SseEvent } from '../../api/types';

/**
 * 新 conversation API 客户端（R1-CONV-01 / FIX-P2-FRONTEND-01）。
 *
 * - 创建会话支持 Idempotency-Key（同键同 body 重放返回原会话）;
 * - 流式接口按冻结事件集 start/meta/content_delta/done/error 消费;
 * - 反馈使用当前事实模型 PUT/DELETE。
 */

export interface ConversationView {
  id: string;
  workspace_id: string;
  mode: string;
  title: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  last_message_at: string | null;
  messages: MessageView[];
}

export interface MessageView {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant';
  status: 'pending' | 'streaming' | 'completed' | 'failed' | 'stopped';
  content: string;
  request_id: string | null;
  task_id: string | null;
  decision: Record<string, unknown> | null;
  stream_error: string | null;
  error_message: string | null;
  fallback_used: boolean;
  created_at: string | null;
  completed_at: string | null;
}

export interface FeedbackView {
  id: string;
  message_id: string;
  conversation_id: string | null;
  rating: 'helpful' | 'unhelpful';
  reason_codes: string[];
  reason_other: string | null;
  correction_text: string | null;
  status: string;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateConversationInput {
  mode: 'global' | 'flow';
  title?: string;
  idempotencyKey?: string;
}

export interface FeedbackInput {
  rating: 'helpful' | 'unhelpful';
  reasonCodes?: string[];
  reasonOther?: string;
  correctionText?: string;
}

export const conversationApi = {
  create(input: CreateConversationInput): Promise<ConversationView> {
    return postJson<ConversationView>(
      '/api/v1/conversations',
      { mode: input.mode, title: input.title || '新会话' },
      input.idempotencyKey
        ? { headers: { 'Idempotency-Key': input.idempotencyKey } }
        : undefined,
    );
  },

  get(id: string): Promise<ConversationView> {
    return fetchJson<ConversationView>(`/api/v1/conversations/${id}`);
  },

  async* stream(
    conversationId: string,
    query: string,
    requestId: string,
    signal?: AbortSignal,
  ): AsyncGenerator<SseEvent> {
    yield* streamSseEvents({
      endpoint: `/api/v1/conversations/${conversationId}/messages:stream`,
      payload: { query, request_id: requestId },
      signal,
    });
  },

  stop(messageId: string, init?: RequestInit): Promise<MessageView> {
    return postJson<MessageView>(`/api/v1/messages/${messageId}:stop`, {}, init);
  },

  submitFeedback(messageId: string, input: FeedbackInput): Promise<FeedbackView> {
    return putJson<FeedbackView>(`/api/v1/messages/${messageId}/feedback`, {
      rating: input.rating,
      reason_codes: input.reasonCodes || [],
      reason_other: input.reasonOther || null,
      correction_text: input.correctionText || null,
    });
  },

  getFeedback(messageId: string): Promise<FeedbackView | null> {
    return fetchJson<FeedbackView | null>(`/api/v1/messages/${messageId}/feedback`);
  },

  withdrawFeedback(messageId: string): Promise<{ status: string }> {
    return deleteJson<{ status: string }>(`/api/v1/messages/${messageId}/feedback`);
  },
};
