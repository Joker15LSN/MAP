import { Table } from '@agentscope-ai/design';

import { dateRender } from './utils';

interface AgentTimelinePanelProps {
  rows: Array<Record<string, unknown>>;
}

export const AgentTimelinePanel = ({ rows }: AgentTimelinePanelProps) => (
  <Table
    scroll={{ x: 'max-content' }}
    rowKey={(row: Record<string, unknown>) => `${row.state_id}-${row.agent_code}-${row.seq}`}
    pagination={false}
    dataSource={rows}
    columns={[
      { title: 'agent_code', dataIndex: 'agent_code', key: 'agent_code' },
      { title: '组件', dataIndex: 'component', key: 'component' },
      { title: '开始(UTC+8)', dataIndex: 'start_ts', key: 'start_ts', render: dateRender },
      { title: '结束(UTC+8)', dataIndex: 'end_ts', key: 'end_ts', render: dateRender },
      {
        title: '耗时(s)',
        dataIndex: 'duration_s',
        key: 'duration_s',
        render: (value: unknown) => Number(value || 0).toFixed(2),
      },
      { title: '状态', dataIndex: 'status', key: 'status', render: (value: unknown) => value || 'unknown' },
    ]}
  />
);
