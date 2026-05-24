import { useEffect, useMemo, useState } from 'react';
import { Card, Table } from '@agentscope-ai/design';
import { Column, DualAxes } from '@ant-design/plots';

import { analyticsApi } from '../api/client';
import { FilterState, UserMetrics } from '../types';

interface UsersPageProps {
  filters: FilterState;
  refreshToken: number;
  isDark: boolean;
}

export const UsersPage = ({ filters, refreshToken, isDark }: UsersPageProps) => {
  const [loading, setLoading] = useState(false);
  const [users, setUsers] = useState<UserMetrics[]>([]);

  useEffect(() => {
    const run = async () => {
      setLoading(true);
      try {
        const result = await analyticsApi.getUsers(filters, 20);
        setUsers(Array.isArray(result) ? result : []);
      } catch {
        setUsers([]);
      } finally {
        setLoading(false);
      }
    };

    run();
  }, [filters, refreshToken]);

  const chartData = useMemo(
    () => users.map((item) => ({ staff_code: item.staff_code, request_count: item.request_count, success_rate: item.success_rate * 100 })),
    [users],
  );

  return (
    <div className="page-layout">
      <Card title="用户请求量" loading={loading}>
        <Column
          data={chartData}
          xField="staff_code"
          yField="request_count"
          label={{ position: 'top' }}
          theme={isDark ? 'classicDark' : 'classic'}
          autoFit
          height={320}
        />
      </Card>

      <Card title="用户成功率与 P95 耗时" loading={loading}>
        <DualAxes
          data={[
            users.map((item) => ({ key: item.staff_code, value: Number((item.success_rate * 100).toFixed(2)) })),
            users.map((item) => ({ key: item.staff_code, value: Number(item.p95_duration_s.toFixed(2)) })),
          ]}
          xField="key"
          yField="value"
          geometryOptions={[
            { geometry: 'column', color: '#5B8FF9' },
            { geometry: 'line', color: '#F6BD16' },
          ]}
          theme={isDark ? 'classicDark' : 'classic'}
          autoFit
          height={320}
        />
      </Card>

      <Card title="用户指标明细" loading={loading}>
        <Table
          scroll={{ x: 'max-content' }}
          rowKey="staff_code"
          dataSource={users}
          pagination={false}
          columns={[
            { title: 'staff_code', dataIndex: 'staff_code', key: 'staff_code' },
            { title: '请求数', dataIndex: 'request_count', key: 'request_count' },
            {
              title: '成功率',
              dataIndex: 'success_rate',
              key: 'success_rate',
              render: (value: number) => `${(value * 100).toFixed(2)}%`,
            },
            {
              title: '平均耗时(s)',
              dataIndex: 'avg_duration_s',
              key: 'avg_duration_s',
              render: (value: number) => value.toFixed(2),
            },
            {
              title: 'P95 耗时(s)',
              dataIndex: 'p95_duration_s',
              key: 'p95_duration_s',
              render: (value: number) => value.toFixed(2),
            },
            {
              title: 'Token 总量',
              dataIndex: 'token_total',
              key: 'token_total',
              render: (value: number) => value.toFixed(0),
            },
            {
              title: '每请求工具调用',
              dataIndex: 'tool_calls_per_request',
              key: 'tool_calls_per_request',
              render: (value: number) => value.toFixed(2),
            },
          ]}
        />
      </Card>
    </div>
  );
};
