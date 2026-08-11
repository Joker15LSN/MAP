import { Table, Tag } from '@agentscope-ai/design';

import type { LLMCallRecord } from '../../types';
import { dateRender } from './utils';

interface LLMTracePanelProps {
  rows: LLMCallRecord[];
  requestId: string;
}

export const LLMTracePanel = ({ rows, requestId }: LLMTracePanelProps) => (
  <Table
    className="llm-trace-table"
    scroll={{ x: 'max-content' }}
    rowKey={(row: LLMCallRecord) => `${row.state_id || requestId}-${row.seq ?? '-'}-${row.start_ts || row.ts || ''}`}
    pagination={false}
    dataSource={rows}
    columns={[
      { title: '#', dataIndex: 'seq', key: 'seq', width: 70 },
      { title: 'agent', dataIndex: 'agent_code', key: 'agent_code', width: 150 },
      { title: 'component', dataIndex: 'component', key: 'component', width: 150 },
      { title: 'phase', dataIndex: 'phase', key: 'phase', width: 170 },
      { title: 'step', dataIndex: 'step', key: 'step', width: 160 },
      { title: 'model', dataIndex: 'model', key: 'model', width: 180 },
      {
        title: '状态',
        dataIndex: 'status',
        key: 'status',
        width: 110,
        render: (value: unknown) => <Tag>{String(value || 'unknown')}</Tag>,
      },
      {
        title: '耗时(s)',
        dataIndex: 'duration_s',
        key: 'duration_s',
        width: 110,
        render: (value: unknown) => Number(value || 0).toFixed(2),
      },
      {
        title: 'Token',
        dataIndex: 'usage',
        key: 'usage',
        width: 160,
        render: (value: unknown) => {
          const usage = (value || {}) as Record<string, unknown>;
          return String(usage.total_tokens ?? usage.total ?? usage.completion_tokens ?? '-');
        },
      },
      { title: '开始(UTC+8)', dataIndex: 'start_ts', key: 'start_ts', width: 220, render: dateRender },
      {
        title: '提示摘要',
        dataIndex: 'prompt_summary',
        key: 'prompt_summary',
        width: 360,
        render: (value: unknown) => <div className="log-line-full">{String(value || '-')}</div>,
      },
      {
        title: '错误',
        dataIndex: 'error',
        key: 'error',
        width: 280,
        render: (value: unknown) => <div className="log-line-full">{String(value || '-')}</div>,
      },
    ]}
  />
);
