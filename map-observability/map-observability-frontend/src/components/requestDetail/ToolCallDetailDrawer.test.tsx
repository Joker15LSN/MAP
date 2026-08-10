import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import { analyticsApi } from '../../api/client';
import { ToolCallDetailDrawer } from './ToolCallDetailDrawer';
import type { ToolCallRow } from './types';

/**
 * F-05 / FIX-P2-OBSERVABILITY-01 / R2-P2-01:ToolCallDetailDrawer 的
 * loading / partial payload / 完整 happy / 错误 / 无效 tool_call_id /
 * trace 缺失场景。loading 用例的 partial payload 是 R2-P2-01 的失败
 * 复现:修复前组件直接解引用 ``main_flow_logs_page.items`` 抛
 * unhandled TypeError,Vitest exit 1。
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
  it('shows a loading state and survives a partial payload (regression)', async () => {
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
    // Partial contract: no main_flow_logs_page / cbb_logs_page at all.
    // Before R2-P2-01 this resolved payload threw an unhandled TypeError
    // (Cannot read properties of undefined (reading 'items')) and made
    // the whole test process exit 1.
    resolveTrace({ rows: [], total: 0 });
    await waitFor(() => {
      expect(screen.getByText(/暂无可展示的子问题拆分结果|无/)).toBeTruthy();
    });
    // the normalized partial payload is treated as a valid payload: the
    // "no correlation logs" placeholder disappears and empty log tables render
    await waitFor(() => {
      expect(screen.getByText(/主流程日志/)).toBeInTheDocument();
      expect(screen.getByText(/容器日志/)).toBeInTheDocument();
    });
    expect(screen.queryByText('暂无可展示的关联日志')).toBeNull();
  });

  it('renders the full happy payload from the real API contract', async () => {
    (analyticsApi.getToolCallCorrelation as ReturnType<typeof vi.fn>).mockResolvedValue({
      request_id: 'req-1',
      container: 'cbb-text-to-sql-dev',
      main_flow_container: 'map_core-dev',
      tool: 'ask_database',
      time_window: {
        timezone: 'Asia/Shanghai',
        start_local: '2026-08-09 08:00:00',
        end_local: '2026-08-09 08:05:00',
        start_utc: '2026-08-09T00:00:00Z',
        end_utc: '2026-08-09T00:05:00Z',
        start_ns: '1786003200000000000',
        end_ns: '1786003500000000000',
        buffered_start_utc: '2026-08-08T23:59:30Z',
        buffered_end_utc: '2026-08-09T00:05:30Z',
        buffered_start_ns: '1786003170000000000',
        buffered_end_ns: '1786003530000000000',
        buffer_seconds: 30,
      },
      request: { request_id: 'req-1', status: 'success' },
      tool_call: { tool: 'ask_database', tool_id: 'tc-1' },
      tool_call_candidates: [{ tool_id: 'tc-1' }],
      id_resolution: {
        resolved_value: 'tc-1',
        resolved_by: 'tool_id',
        source_hit_counts: { main_flow: 2, cbb: 1 },
      },
      error_summary: {
        alert_count: 1,
        level_breakdown: { ERROR: 1 },
        channel_breakdown: { stderr: 1 },
        signature_breakdown: { 'db timeout': 1 },
        matched_keywords: ['timeout'],
        first_alert_ts_utc: '2026-08-09T00:01:00Z',
        last_alert_ts_utc: '2026-08-09T00:01:00Z',
      },
      main_flow_logs_page: {
        items: [
          {
            ts_ns: '1786003260000000000',
            ts_utc: '2026-08-09T00:01:00Z',
            line: 'master route decision finished',
            rid: 'req-1',
            task_id: 'task-1',
            level: 'INFO',
          },
        ],
        total: 1,
        page: 1,
        page_size: 10,
      },
      cbb_logs_page: {
        items: [
          {
            ts_ns: '1786003261000000000',
            ts_utc: '2026-08-09T00:01:01Z',
            line: 'ask_database query timed out',
            rid: 'req-1',
            level: 'ERROR',
          },
        ],
        total: 1,
        page: 1,
        page_size: 10,
      },
    });
    renderDrawer();
    await waitFor(() => {
      expect(screen.getByText('主流程容器: map_core-dev')).toBeInTheDocument();
    });
    expect(screen.getByText('工具容器: cbb-text-to-sql-dev')).toBeInTheDocument();
    expect(screen.getByText('ID来源: tool_id')).toBeInTheDocument();
    expect(screen.getByText('告警数: 1')).toBeInTheDocument();
    expect(screen.getByText('主流程日志（map_core-dev）')).toBeInTheDocument();
    expect(screen.getByText('工具容器日志（cbb-text-to-sql-dev）')).toBeInTheDocument();
    expect(screen.getByText('master route decision finished')).toBeInTheDocument();
    expect(screen.getByText('ask_database query timed out')).toBeInTheDocument();
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
