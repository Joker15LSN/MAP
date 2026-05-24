import { useEffect, useMemo, useState } from 'react';
import { Card, Empty, Table } from '@agentscope-ai/design';
import { Column, Pie } from '@ant-design/plots';

import { analyticsApi } from '../api/client';
import { AgentMetrics, FilterState, ToolMetricsPayload } from '../types';

interface AgentsToolsPageProps {
  filters: FilterState;
  refreshToken: number;
  isDark: boolean;
}

export const AgentsToolsPage = ({ filters, refreshToken, isDark }: AgentsToolsPageProps) => {
  const [loading, setLoading] = useState(false);
  const [agents, setAgents] = useState<AgentMetrics[]>([]);
  const [tools, setTools] = useState<ToolMetricsPayload>({ items: [], failure_top: [] });

  useEffect(() => {
    const run = async () => {
      setLoading(true);
      try {
        const [agentResp, toolResp] = await Promise.all([analyticsApi.getAgents(filters, 20), analyticsApi.getTools(filters, 20)]);
        setAgents(Array.isArray(agentResp) ? agentResp : []);
        setTools(
          toolResp && Array.isArray(toolResp.items) && Array.isArray(toolResp.failure_top)
            ? toolResp
            : { items: [], failure_top: [] },
        );
      } catch {
        setAgents([]);
        setTools({ items: [], failure_top: [] });
      } finally {
        setLoading(false);
      }
    };

    run();
  }, [filters, refreshToken]);

  const agentChartData = useMemo(
    () =>
      agents.map((item) => ({
        agent: item.agent_code,
        avg_duration_s: Number(item.avg_duration_s.toFixed(2)),
      })),
    [agents],
  );

  const toolFailurePie = useMemo(
    () => tools.failure_top.map((item) => ({ tool: item.tool, value: item.failed_count || 0 })),
    [tools],
  );
  const hasMeaningfulAgentDuration = useMemo(
    () => agentChartData.some((item) => Number(item.avg_duration_s) > 0),
    [agentChartData],
  );
  const hasToolFailures = useMemo(() => toolFailurePie.some((item) => item.value > 0), [toolFailurePie]);

  return (
    <div className="page-layout">
      <div className="split-grid">
        <Card title="Agent 平均耗时" loading={loading}>
          {hasMeaningfulAgentDuration ? (
            <Column
              data={agentChartData}
              xField="agent"
              yField="avg_duration_s"
              height={300}
              label={{ position: 'top' }}
              axis={{ y: { labelFormatter: '~s' } }}
              theme={isDark ? 'classicDark' : 'classic'}
              autoFit
            />
          ) : (
            <Empty description="当前筛选范围无有效 Agent 耗时数据" />
          )}
        </Card>

        <Card title="工具失败分布" loading={loading}>
          {hasToolFailures ? (
            <Pie
              data={toolFailurePie}
              angleField="value"
              colorField="tool"
              label={{ text: 'value', style: { fontWeight: 'bold' } }}
              legend={{ color: { title: false, position: 'right' } }}
              theme={isDark ? 'classicDark' : 'classic'}
              autoFit
              height={300}
            />
          ) : (
            <Empty description="当前筛选范围无工具失败记录" />
          )}
        </Card>
      </div>

      <Card title="Agent 指标" loading={loading}>
        <Table
          scroll={{ x: 'max-content' }}
          rowKey="agent_code"
          dataSource={agents}
          pagination={false}
          columns={[
            { title: 'agent_code', dataIndex: 'agent_code', key: 'agent_code' },
            { title: 'agent_name', dataIndex: 'agent_name', key: 'agent_name' },
            { title: '调用次数', dataIndex: 'call_count', key: 'call_count' },
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
              title: '慢调用占比',
              dataIndex: 'slow_call_ratio',
              key: 'slow_call_ratio',
              render: (value: number) => `${(value * 100).toFixed(2)}%`,
            },
          ]}
        />
      </Card>

      <Card title="工具指标" loading={loading}>
        <Table
          scroll={{ x: 'max-content' }}
          rowKey="tool"
          dataSource={tools.items}
          pagination={false}
          columns={[
            { title: 'tool', dataIndex: 'tool', key: 'tool' },
            { title: '调用次数', dataIndex: 'call_count', key: 'call_count' },
            { title: '失败次数', dataIndex: 'failed_count', key: 'failed_count' },
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
          ]}
        />
      </Card>
    </div>
  );
};
