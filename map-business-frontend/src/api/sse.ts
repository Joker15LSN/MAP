import type { SseEvent } from './types';

export interface SseParseResult {
  /** 解析出的完整事件帧 */
  events: SseEvent[];
  /** 未闭合的半帧,留待下一次追加解析 */
  remaining: string;
}

/**
 * 增量解析 SSE 文本流。
 *
 * - 以空行（`\n\n` 或 `\r\n\r\n`）作为帧分隔,支持多行 `data:` 行(按 SSE 规范以 `\n` 连接)。
 * - 未遇到空行的尾部视为半帧,原样放入 `remaining`,由调用方保留并与后续数据拼接。
 * - 缺少 event/data 字段、或 data 非合法 JSON 的帧视为错误帧,直接跳过(与历史行为一致)。
 */
export const parseSseFrames = (buffer: string): SseParseResult => {
  const normalized = buffer.replace(/\r\n/g, '\n');
  const frames = normalized.split('\n\n');
  const remaining = frames.pop() ?? '';
  const events: SseEvent[] = [];

  for (const frame of frames) {
    const lines = frame.split('\n');
    const eventLine = lines.find((line) => line.startsWith('event:'));
    const dataLines = lines.filter((line) => line.startsWith('data:'));
    if (!eventLine || dataLines.length === 0) {
      continue;
    }
    const eventName = eventLine.slice('event:'.length).trim();
    const payloadText = dataLines.map((line) => line.slice('data:'.length).trim()).join('\n');
    try {
      const data = JSON.parse(payloadText) as Record<string, unknown>;
      events.push({ event: eventName, data });
    } catch {
      // 错误帧(非法 JSON)跳过,保持流继续
    }
  }

  return { events, remaining };
};
