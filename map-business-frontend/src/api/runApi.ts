import {
  ApiError,
  buildIdHeaders,
  fetchJson,
  parseResponse,
  postJson,
} from './client';
import { parseSseFrames } from './sse';

/**
 * Canonical Run API client (Step 4 / PR-F1+F2).
 *
 * 前端只依赖 BFF 的 `/api/v1`：
 * - `createTurn` 启动一轮会话（返回 durable run/message 三元组）;
 * - `getRun` / `cancelRun` 读写 Run 生命周期;
 * - `replayRunEvents` 逐帧解析 `/runs/{run_id}/events` 的 SSE，并把错误帧
 *   投影为 `RunStreamError`。SSE 帧/错误投影只允许出现在本文件。
 */

export interface TurnCreated {
  run_id: string;
  user_message_id: string;
  assistant_message_id: string;
  status: string;
  replayed: boolean;
}

export interface RunView {
  run_id: string;
  workspace_id: string;
  principal_id: string;
  conversation_id: string | null;
  status: string;
  command: {
    kind: string;
    payload: Record<string, unknown>;
    snapshot: Record<string, unknown>;
  };
  last_seq: number;
  cancel_requested: boolean;
  error_code: string | null;
}

export interface CancelRunReceipt {
  run_id: string;
  accepted: boolean;
  status: string;
}

export interface RunEventEnvelope {
  schema_version: number;
  schema_minor: number;
  event_id: string;
  run_id: string;
  seq: number;
  type: string;
  occurred_at: string;
  workspace_id: string;
  data: Record<string, unknown>;
  [key: string]: unknown;
}

export class RunStreamError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
    this.name = 'RunStreamError';
  }
}

export interface CreateTurnInput {
  requestId?: string;
  idempotencyKey: string;
  signal?: AbortSignal;
}

export interface ReplayRunEventsOptions {
  afterSeq?: number;
  signal?: AbortSignal;
}

export const runApi = {
  createTurn(
    conversationId: string,
    query: string,
    input: CreateTurnInput,
  ): Promise<TurnCreated> {
    return postJson<TurnCreated>(
      `/api/v1/conversations/${conversationId}/turns`,
      { query, request_id: input.requestId || null },
      {
        headers: { 'Idempotency-Key': input.idempotencyKey },
        signal: input.signal,
      },
    );
  },

  getRun(runId: string, signal?: AbortSignal): Promise<RunView> {
    return fetchJson<RunView>(`/api/v1/runs/${runId}`, { signal });
  },

  cancelRun(
    runId: string,
    reason = '',
    signal?: AbortSignal,
  ): Promise<CancelRunReceipt> {
    return postJson<CancelRunReceipt>(
      `/api/v1/runs/${runId}:cancel`,
      { reason },
      { signal },
    );
  },

  /**
   * 逐帧读取 run event SSE。EOF 是正常结束（调用方按 `afterSeq` 重连续传），
   * `event: error` 帧投影为 `RunStreamError`，HTTP 错误投影为 `ApiError`。
   */
  async *replayRunEvents(
    runId: string,
    options: ReplayRunEventsOptions = {},
  ): AsyncGenerator<RunEventEnvelope> {
    const { afterSeq = 0, signal } = options;
    const params = new URLSearchParams();
    if (afterSeq > 0) {
      params.set('after_seq', String(afterSeq));
    }
    const query = params.size > 0 ? `?${params.toString()}` : '';
    const response = await fetch(`/api/v1/runs/${runId}/events${query}`, {
      method: 'GET',
      headers: buildIdHeaders(),
      signal,
    });

    if (!response.ok) {
      throw await parseResponse<never>(response);
    }
    if (!response.body) {
      throw new ApiError(200, 'run event stream returned an empty body');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSseFrames(buffer);
        buffer = parsed.remaining;

        for (const frame of parsed.events) {
          if (frame.event === 'error') {
            const code = String(frame.data.code || 'RUN_STREAM_ERROR');
            const message = String(frame.data.message || 'run event stream error');
            throw new RunStreamError(code, message);
          }
          yield frame.data as unknown as RunEventEnvelope;
        }
      }
    } finally {
      reader.releaseLock();
    }
  },
};
