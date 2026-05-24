import { Card, Progress, Statistic, Tag } from '@agentscope-ai/design';

import { OverviewData } from '../types';

interface KpiCardsProps {
  overview?: OverviewData;
}

const formatPercent = (value: number) => `${(value * 100).toFixed(2)}%`;
const safeNumber = (value: unknown, fallback = 0): number => {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
};

export const KpiCards = ({ overview }: KpiCardsProps) => {
  if (!overview) {
    return null;
  }

  const totalRequests = safeNumber(overview.total_requests);
  const successRate = safeNumber(overview.success_rate);
  const durationAvg = safeNumber(overview.duration_s?.avg);
  const durationP90 = safeNumber(overview.duration_s?.p90);
  const durationP95 = safeNumber(overview.duration_s?.p95);
  const tokenTotal = safeNumber(overview.token?.total);
  const tokenPerRequest = safeNumber(overview.token?.avg_per_request);
  const tokenPerSuccess = safeNumber(overview.token?.efficiency_per_success_request);
  const toolPerRequest = safeNumber(overview.tool_calls?.per_request);
  const toolTotal = safeNumber(overview.tool_calls?.total);
  const bigScene = safeNumber(overview.scene_confidence_avg?.big_scene);
  const subScene = safeNumber(overview.scene_confidence_avg?.sub_scene);

  return (
    <div className="kpi-grid">
      <Card>
        <Statistic title="请求总量" value={totalRequests} />
      </Card>

      <Card>
        <Statistic title="成功率" value={formatPercent(successRate)} />
        <Progress percent={Number((successRate * 100).toFixed(2))} />
      </Card>

      <Card>
        <Statistic title="平均耗时(s)" value={durationAvg.toFixed(2)} />
        <div className="kpi-tags">
          <Tag>P90: {durationP90.toFixed(2)}s</Tag>
          <Tag>P95: {durationP95.toFixed(2)}s</Tag>
        </div>
      </Card>

      <Card>
        <Statistic title="Token 总量" value={tokenTotal.toFixed(0)} />
        <div className="kpi-tags">
          <Tag>单请求: {tokenPerRequest.toFixed(2)}</Tag>
          <Tag>单成功请求: {tokenPerSuccess.toFixed(2)}</Tag>
        </div>
      </Card>

      <Card>
        <Statistic title="每请求工具调用数" value={toolPerRequest.toFixed(2)} />
        <div className="kpi-tags">
          <Tag>工具调用总数: {toolTotal}</Tag>
        </div>
      </Card>

      <Card>
        <Statistic title="场景置信度" value={bigScene.toFixed(2)} />
        <div className="kpi-tags">
          <Tag>大类: {bigScene.toFixed(2)}</Tag>
          <Tag>子类: {subScene.toFixed(2)}</Tag>
        </div>
      </Card>
    </div>
  );
};
