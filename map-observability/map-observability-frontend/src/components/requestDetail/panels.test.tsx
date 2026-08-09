import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { AgentTimelinePanel } from './AgentTimelinePanel';
import { LLMTracePanel } from './LLMTracePanel';
import { ToolCallsPanel } from './ToolCallsPanel';
import { ScenePanel } from './ScenePanel';
import type { ToolCallRow } from './types';

/**
 * F-05 / FIX-P2-OBSERVABILITY-01:各 detail panel 的 happy/empty/error 测试。
 */

describe('AgentTimelinePanel', () => {
  const rows = [
    {
      state_id: 's-1',
      agent_code: 'Operations',
      component: 'planner',
      start_ts: '2026-08-09T00:00:00Z',
      end_ts: '2026-08-09T00:00:05Z',
      duration_s: 5,
      status: 'success',
    },
  ];

  it('renders rows with formatted duration and status fallback', () => {
    render(<AgentTimelinePanel rows={rows} />);
    expect(screen.getByText('Operations')).toBeInTheDocument();
    expect(screen.getByText('5.00')).toBeInTheDocument();
    expect(screen.getByText('success')).toBeInTheDocument();
  });

  it('renders an empty table without crashing', () => {
    const { container } = render(<AgentTimelinePanel rows={[]} />);
    expect(container.querySelector('table')).toBeInTheDocument();
    expect(screen.getAllByText('agent_code').length).toBeGreaterThan(0);
  });

  it('falls back to unknown status when status is missing', () => {
    render(<AgentTimelinePanel rows={[{ state_id: 's-2', agent_code: 'A', duration_s: null }]} />);
    expect(screen.getByText('unknown')).toBeInTheDocument();
  });
});

describe('LLMTracePanel', () => {
  const rows = [
    {
      state_id: 's-1',
      seq: 1,
      agent_code: 'Master',
      component: 'router',
      phase: 'route',
      step: '1',
      model: 'deepseek-v4-flash',
      status: 'ok',
      duration_s: 1.5,
      usage: { total_tokens: 123 },
      start_ts: '2026-08-09T00:00:00Z',
      prompt_summary: 'route prompt',
      error: undefined,
    },
  ];

  it('renders a trace row with token totals and prompt summary', () => {
    render(<LLMTracePanel rows={rows} requestId="req-1" />);
    expect(screen.getByText('deepseek-v4-flash')).toBeInTheDocument();
    expect(screen.getByText('123')).toBeInTheDocument();
    expect(screen.getByText('route prompt')).toBeInTheDocument();
  });

  it('renders an empty trace table (trace missing) without crashing', () => {
    const { container } = render(<LLMTracePanel rows={[]} requestId="req-1" />);
    expect(container.querySelector('table')).toBeInTheDocument();
    expect(screen.getAllByText('提示摘要').length).toBeGreaterThan(0);
  });

  it('shows error text in the error column', () => {
    render(
      <LLMTracePanel
        rows={[{ ...rows[0], error: 'upstream timeout' }]}
        requestId="req-1"
      />,
    );
    expect(screen.getByText('upstream timeout')).toBeInTheDocument();
  });
});

describe('ToolCallsPanel', () => {
  const rows: ToolCallRow[] = [
    {
      tool: 'ask_database',
      tool_id: 'tc-1',
      agent_code: 'Operations',
      step: '1',
      status: 'success',
      ts: '2026-08-09T00:00:00Z',
    },
  ];

  it('renders rows and invokes onSelectToolCall for deep-link navigation', () => {
    const onSelect = vi.fn();
    render(<ToolCallsPanel rows={rows} onSelectToolCall={onSelect} />);
    fireEvent.click(screen.getByText('查看详情'));
    expect(onSelect).toHaveBeenCalledWith(rows[0]);
    fireEvent.click(screen.getByText('ask_database'));
    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it('renders an empty tool list without crashing', () => {
    const { container } = render(<ToolCallsPanel rows={[]} onSelectToolCall={vi.fn()} />);
    expect(container.querySelector('table')).toBeInTheDocument();
    expect(screen.getAllByText('tool').length).toBeGreaterThan(0);
  });
});

describe('ScenePanel', () => {
  it('pretty-prints the scene result JSON', () => {
    const { container } = render(<ScenePanel sceneResult={{ mode: 'auto', agents: ['A'] }} />);
    expect(container.textContent).toContain('mode');
    expect(container.textContent).toContain('auto');
  });

  it('renders an empty object when no scene result exists', () => {
    render(<ScenePanel />);
    expect(screen.getByText(/\{\}/)).toBeInTheDocument();
  });
});
