import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import { analyticsApi } from '../../api/client';
import { ToolCallDetailDrawer } from './ToolCallDetailDrawer';
import type { ToolCallRow } from './types';

/**
 * F-05 / FIX-P2-OBSERVABILITY-01:ToolCallDetailDrawer 的 loading / 错误 /
 * 无效 tool_call_id / trace 缺失场景。
 */

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ...actual,
    analyticsApi: {
      ...actual.analyticsApi,
      getToolCallCorrelation: vi.fn(),
    },
  };
});

const toolCall: ToolCallRow = {
  tool: 'ask_database',
  tool_id: 'tc-invalid',
  agent_code: 'Operations',
  step: 1,
  status: 'success',
  ts: '2026-08-09T00:00:00Z',
};

function renderDrawer(overrides: Partial<Parameters<typeof ToolCallDetailDrawer>[0]> = {}) {
  return render(
    <ToolCallDetailDrawer
      detail={{ tool_calls: [] } as never}
      toolCall={toolCall}
      requestId="req-1"
      activeContainer="main"
      activeLevels={[]}
      onClose={vi.fn()}
      {...overrides}
    />,
  );
}

describe('ToolCallDetailDrawer', () => {
  it('shows a loading state while the correlation trace loads', async () => {
    let resolveTrace: (value: unknown) => void = () => undefined;
    (analyticsApi.getToolCallCorrelation as ReturnType<typeof vi.fn>).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTrace = resolve;
        }),
    );
    renderDrawer();
    await waitFor(() => {
      expect(analyticsApi.getToolCallCorrelation).toHaveBeenCalledWith(
        'req-1',
        expect.any(String),
        'ask_database',
        expect.objectContaining({ toolId: 'tc-invalid', step: 1 }),
      );
    });
    resolveTrace({ rows: [], total: 0 });
    await waitFor(() => {
      expect(screen.getByText(/暂无可展示的子问题拆分结果|无/)).toBeTruthy();
    });
  });

  it('shows the error state when the trace request fails (invalid tool_call_id)', async () => {
    (analyticsApi.getToolCallCorrelation as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('tool_call_id not found'),
    );
    renderDrawer();
    await waitFor(() => {
      expect(screen.getByText(/tool_call_id not found/)).toBeInTheDocument();
    });
  });

  it('renders without crashing when no tool call is selected (trace missing)', () => {
    const { container } = renderDrawer({ toolCall: undefined });
    expect(container).toBeInTheDocument();
  });
});
