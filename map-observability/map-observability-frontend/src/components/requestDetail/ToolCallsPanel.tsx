import { Button, Table } from '@agentscope-ai/design';

import { dateRender, getToolCallIdentity } from './utils';
import type { ToolCallRow } from './types';

interface ToolCallsPanelProps {
  rows: ToolCallRow[];
  onSelectToolCall: (row: ToolCallRow) => void;
}

export const ToolCallsPanel = ({ rows, onSelectToolCall }: ToolCallsPanelProps) => (
  <Table
    scroll={{ x: 'max-content' }}
    rowKey={(row: Record<string, unknown>) => getToolCallIdentity(row as ToolCallRow)}
    pagination={false}
    dataSource={rows}
    columns={[
      {
        title: 'tool',
        dataIndex: 'tool',
        key: 'tool',
        render: (value: unknown, row: ToolCallRow) => (
          <Button type="link" onClick={() => onSelectToolCall(row)}>
            {String(value || '-')}
          </Button>
        ),
      },
      {
        title: 'tool_id',
        dataIndex: 'tool_id',
        key: 'tool_id',
        render: (value: unknown, row: ToolCallRow) => (
          <Button type="link" onClick={() => onSelectToolCall(row)}>
            {String(value || '-')}
          </Button>
        ),
      },
      { title: 'agent_code', dataIndex: 'agent_code', key: 'agent_code' },
      { title: '步骤', dataIndex: 'step', key: 'step' },
      { title: '状态', dataIndex: 'status', key: 'status', render: (value: unknown) => value || 'unknown' },
      { title: '时间(UTC+8)', dataIndex: 'ts', key: 'ts', render: dateRender },
      {
        title: '操作',
        key: 'action',
        render: (_: unknown, row: ToolCallRow) => (
          <Button type="link" onClick={() => onSelectToolCall(row)}>
            查看详情
          </Button>
        ),
      },
    ]}
  />
);
