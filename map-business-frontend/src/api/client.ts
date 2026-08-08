import { parseSseFrames } from './sse';
import type { SseEvent } from './types';

/**
 * 统一 fetch 封装。
 *
 * - `apiRequest`  返回原始 Response,供需要区分 ok/非 ok 分支的调用方使用。
 * - `fetchJson`   期望 2xx 并解析 JSON,否则抛出错误。
 * - `postJson` / `putJson` 封装常见写操作(请求路径、参数与历史实现完全一致)。
 * - `streamSseEvents` 按 SSE 帧流式 yield 事件,支持中途 abort。
 */
export const apiRequest = (url: string, init?: RequestInit): Promise<Response> => fetch(url, init);

export const fetchJson = async <T>(url: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(url, init);
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`);
  }
  return (await response.json()) as T;
};

export const postJson = <T>(url: string, body: unknown, init?: RequestInit): Promise<T> =>
  fetchJson<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    ...init,
  });

export const putJson = <T>(url: string, body: unknown, init?: RequestInit): Promise<T> =>
  fetchJson<T>(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    ...init,
  });

export interface StreamSseOptions {
  endpoint: string;
  payload: unknown;
  signal?: AbortSignal;
}

/**
 * 以 SSE 帧为粒度消费聊天流式接口,逐帧 yield 事件。
 * 内部使用 parseSseFrames 处理多行 data 与半帧缓冲。
 */
export async function* streamSseEvents(options: StreamSseOptions): AsyncGenerator<SseEvent> {
  const { endpoint, payload, signal } = options;
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    signal,
    body: JSON.stringify(payload),
  });

  if (!response.ok || !response.body) {
    throw new Error(`stream request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let sseBuffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    sseBuffer += decoder.decode(value, { stream: true });
    const parsed = parseSseFrames(sseBuffer);
    sseBuffer = parsed.remaining;

    for (const frame of parsed.events) {
      yield frame;
    }
  }
}
