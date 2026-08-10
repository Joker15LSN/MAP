import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { RequestDetailDrawer } from './RequestDetailDrawer';
import type { RequestDetail } from '../types';

/**
 * R2-P2-01:请求详情容器的 loading / empty / request error(invalid ID) /
 * missing trace / happy 矩阵。panel 的真实 request error 发生在此层
 * (API 失败 → ErrorAlert),panel 组件本身是纯展示。
 */

const detailFixture: RequestDetail = {
  request: {
    request_id: 'req-1',
    state_id: 'state-1',
    session_id: 'sess-1',
    staff_code: 'demo',
    query: '查询昨天的指标',
    status: 'success',
    start_ts: '2026-08-09T00:00:00Z',
    end_ts: '2026-08-09T00:00:08Z',
    duration_s: 8,
    token_total: 1234,
    scene_result: { mode: 'auto', agents: ['Operations'] },
  },
  agent_timeline: [
    {
      state_id: 'state-1',
      agent_code: 'Operations',
      component: 'planner',
      start_ts: '2026-08-09T00:00:00Z',
      end_ts: '2026-08-09T00:00:05Z',
      duration_s: 5,
      status: 'success',
    },
  ],
  agent_events: [],
  tool_calls: [
    {
      tool: 'ask_database',
      tool_id: 'tc-1',
      agent_code: 'Operations',
      step: 1,
      status: 'success',
      ts: '2026-08-09T00:00:02Z',
    },
  ],
  llm_calls: [
    {
      state_id: 'state-1',
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
    },
  ],
  summary: { agent_event_count: 1, tool_call_count: 1, llm_call_count: 1 },
};

function renderDrawer(overrides: Partial<Parameters<typeof RequestDetailDrawer>[0]> = {}) {
  return render(
    <RequestDetailDrawer
      open
      loading={false}
      detail={detailFixture}
      activeContainer={'map_core-dev' as never}
      activeLevels={[]}
      onClose={vi.fn()}
      {...overrides}
    />,
  );
}

describe('RequestDetailDrawer', () => {
  it('renders a loading skeleton without error alerts (loading)', () => {
    renderDrawer({ loading: true, detail: undefined });
    expect(screen.queryByText('未获取到请求详情数据')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows the missing-trace warning when no detail arrives (empty)', () => {
    renderDrawer({ detail: undefined });
    expect(screen.getByText('未获取到请求详情数据')).toBeInTheDocument();
  });

  it('surfaces a real request error, e.g. invalid request_id 404 (request error)', () => {
    renderDrawer({ detail: undefined, errorMessage: 'request_id not found: req-invalid' });
    expect(screen.getByText('request_id not found: req-invalid')).toBeInTheDocument();
    expect(screen.getByText('未获取到请求详情数据')).toBeInTheDocument();
  });

  it('renders the summary and all four detail panels (happy)', () => {
    renderDrawer();
    // request_id 同时出现在摘要 Tag 与调用链路树中,属于真实重复
    expect(screen.getAllByText('request_id: req-1').length).toBeGreaterThan(0);
    expect(screen.getByText('耗时: 8.00s')).toBeInTheDocument();
    expect(screen.getByText('Token: 1234')).toBeInTheDocument();

    // panels are collapsed by default; expand them all
    fireEvent.click(screen.getByText('全部展开'));

    // AgentTimelinePanel row(也可能出现在调用链路树中)
    expect(screen.getAllByText('Operations').length).toBeGreaterThan(0);
    // LLMTracePanel row
    expect(screen.getByText('deepseek-v4-flash')).toBeInTheDocument();
    // ToolCallsPanel row
    expect(screen.getAllByText('ask_database').length).toBeGreaterThan(0);
    // ScenePanel pretty-printed JSON
    expect(screen.getByText(/"mode": "auto"/)).toBeInTheDocument();
  });

  it('renders collapsed panel labels with row counts before expanding', () => {
    renderDrawer();
    expect(screen.getByText('Agent 时间线（1）')).toBeInTheDocument();
    expect(screen.getByText('LLM 调用轨迹（1）')).toBeInTheDocument();
    expect(screen.getByText('工具调用（1）')).toBeInTheDocument();
    expect(screen.getByText('场景识别原始数据')).toBeInTheDocument();
  });
});
