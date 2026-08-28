import type { RunEventEnvelope } from '../../api/runApi';

/**
 * 纯函数 Run 事件投影（Step 4 / PR-F1+F2）。
 *
 * 规则与 BFF `app/turns/projection.py` 保持一致：
 * - 按 `(run_id, seq)` 去重（SSE 至少一次投递，重连续传会重复发尾帧）;
 * - `message.delta` 追加内容增量;
 * - `step.completed` 携带全文，是内容权威;
 * - 首个 `run.*` 终态恰好渲染一次，迟到的终态事件被丢弃。
 */

export type RunTerminalStatus = 'completed' | 'failed' | 'cancelled' | 'timed_out';

export interface RunProjectionState {
  runId: string;
  lastSeq: number;
  content: string;
  terminalStatus: RunTerminalStatus | null;
  terminalSeen: boolean;
  stepCompletedSeen: boolean;
}

const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set<RunTerminalStatus>([
  'completed',
  'failed',
  'cancelled',
  'timed_out',
]);

export const createEmptyRunProjection = (runId: string): RunProjectionState => ({
  runId,
  lastSeq: 0,
  content: '',
  terminalStatus: null,
  terminalSeen: false,
  stepCompletedSeen: false,
});

export const applyRunEvent = (
  state: RunProjectionState,
  envelope: RunEventEnvelope,
): RunProjectionState => {
  if (envelope.run_id !== state.runId) {
    return state;
  }
  // 去重：事件必须严格前进；重连续传时尾部已见过的帧直接丢弃。
  if (envelope.seq <= state.lastSeq) {
    return state;
  }

  const next: RunProjectionState = {
    ...state,
    lastSeq: envelope.seq,
  };

  if (envelope.type === 'message.delta') {
    const delta = envelope.data.content;
    if (typeof delta === 'string' && !next.stepCompletedSeen) {
      next.content += delta;
    }
    return next;
  }

  if (envelope.type === 'step.completed') {
    const fullText = envelope.data.content;
    if (typeof fullText === 'string') {
      next.content = fullText;
      next.stepCompletedSeen = true;
    }
    return next;
  }

  if (envelope.type.startsWith('run.')) {
    const status = envelope.type.slice('run.'.length);
    if (TERMINAL_RUN_STATUSES.has(status) && !next.terminalSeen) {
      next.terminalStatus = status as RunTerminalStatus;
      next.terminalSeen = true;
    }
    // 非终态 run.* 不改变投影状态；迟到终态被上面的条件丢弃。
  }

  return next;
};

/** 把 Run 终态映射为消息级展示状态（UI 兼容旧 MessageView.status）。 */
export const mapRunTerminalToMessageStatus = (
  terminal: RunTerminalStatus,
): 'completed' | 'failed' | 'stopped' => {
  switch (terminal) {
    case 'completed':
      return 'completed';
    case 'cancelled':
      return 'stopped';
    case 'failed':
    case 'timed_out':
      return 'failed';
  }
};
