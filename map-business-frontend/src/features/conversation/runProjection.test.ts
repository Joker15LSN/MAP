import { describe, expect, it } from 'vitest';
import {
  applyRunEvent,
  createEmptyRunProjection,
  mapRunTerminalToMessageStatus,
} from './runProjection';
import type { RunEventEnvelope } from '../../api/runApi';

function envelope(
  runId: string,
  seq: number,
  type: string,
  data: Record<string, unknown> = {},
): RunEventEnvelope {
  return {
    schema_version: 1,
    schema_minor: 0,
    event_id: `ev-${runId}-${seq}`,
    run_id: runId,
    seq,
    type,
    occurred_at: '2026-08-09T00:00:00Z',
    workspace_id: '00000000-0000-0000-0000-000000000001',
    data,
  };
}

describe('runProjection', () => {
  it('dedupes by (run_id, seq) and appends message.delta', () => {
    let state = createEmptyRunProjection('run-1');
    state = applyRunEvent(state, envelope('run-1', 1, 'run.started'));
    state = applyRunEvent(state, envelope('run-1', 2, 'message.delta', { content: '你' }));
    state = applyRunEvent(state, envelope('run-1', 2, 'message.delta', { content: '重复' }));
    state = applyRunEvent(state, envelope('run-1', 3, 'message.delta', { content: '好' }));

    expect(state.lastSeq).toBe(3);
    expect(state.content).toBe('你好');
    expect(state.terminalSeen).toBe(false);
  });

  it('treats step.completed full text as authoritative', () => {
    let state = createEmptyRunProjection('run-1');
    state = applyRunEvent(state, envelope('run-1', 1, 'message.delta', { content: '你' }));
    state = applyRunEvent(state, envelope('run-1', 2, 'step.completed', { content: '你好啊' }));
    state = applyRunEvent(state, envelope('run-1', 3, 'message.delta', { content: '迟到' }));

    expect(state.content).toBe('你好啊');
    expect(state.stepCompletedSeen).toBe(true);
  });

  it('renders the first run.* terminal exactly once and drops late terminals', () => {
    let state = createEmptyRunProjection('run-1');
    state = applyRunEvent(state, envelope('run-1', 1, 'run.started'));
    state = applyRunEvent(state, envelope('run-1', 2, 'run.completed'));
    state = applyRunEvent(state, envelope('run-1', 3, 'run.failed', { code: 'LATE' }));

    expect(state.terminalSeen).toBe(true);
    expect(state.terminalStatus).toBe('completed');
  });

  it('ignores events from another run id', () => {
    const state = createEmptyRunProjection('run-1');
    const next = applyRunEvent(state, envelope('run-2', 1, 'message.delta', { content: 'x' }));
    expect(next).toBe(state);
  });

  it('maps run terminals to message statuses', () => {
    expect(mapRunTerminalToMessageStatus('completed')).toBe('completed');
    expect(mapRunTerminalToMessageStatus('cancelled')).toBe('stopped');
    expect(mapRunTerminalToMessageStatus('failed')).toBe('failed');
    expect(mapRunTerminalToMessageStatus('timed_out')).toBe('failed');
  });
});
