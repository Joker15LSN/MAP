import { useEffect, useMemo, useState } from 'react';
import { Card, Table } from '@agentscope-ai/design';
import { Line } from '@ant-design/plots';
import dayjs from 'dayjs';

import { analyticsApi } from '../api/client';
import { KpiCards } from '../components/KpiCards';
import { FilterState, OverviewData, RequestListItem, ToolMetrics, TrendPoint } from '../types';
import { formatAxisBucketLabel } from '../utils/time';

interface OverviewPageProps {
  filters: FilterState;
  refreshToken: number;
  isDark: boolean;
}

type TrendMetric = '请求量' | '平均耗时(s)' | 'Token总量';

const TREND_METRIC_STYLE: Record<
  TrendMetric,
  { color: string; lineDash: number[]; lineWidth: number; legendDash: 'solid' | 'dashed' | 'dotted' }
> = {
  请求量: { color: '#1D4ED8', lineDash: [0, 0], lineWidth: 2.4, legendDash: 'solid' },
  '平均耗时(s)': { color: '#059669', lineDash: [8, 4], lineWidth: 2.4, legendDash: 'dashed' },
  Token总量: { color: '#F97316', lineDash: [2, 4], lineWidth: 2.6, legendDash: 'dotted' },
};

export const OverviewPage = ({ filters, refreshToken, isDark }: OverviewPageProps) => {
  const [loading, setLoading] = useState(false);
  const [overview, setOverview] = useState<OverviewData>();
  const [trendRows, setTrendRows] = useState<TrendPoint[]>([]);
  const [slowRequests, setSlowRequests] = useState<RequestListItem[]>([]);
  const [failedTools, setFailedTools] = useState<ToolMetrics[]>([]);
  const [visibleMetrics, setVisibleMetrics] = useState<Record<TrendMetric, boolean>>({
    请求量: true,
    '平均耗时(s)': true,
    Token总量: true,
  });

  const sortedTrendRows = useMemo(
    () =>
      [...trendRows].sort(
        (left, right) =>
          dayjs(left.bucket_ts as string).valueOf() - dayjs(right.bucket_ts as string).valueOf(),
      ),
    [trendRows],
  );

  useEffect(() => {
    const fetchAll = async () => {
      setLoading(true);
      try {
        const [overviewResp, trendsResp, requestsResp, toolsResp] = await Promise.all([
          analyticsApi.getOverview(filters),
          analyticsApi.getTrends(filters),
          analyticsApi.getRequests(filters, 1, 10, 'duration_s', 'desc'),
          analyticsApi.getTools(filters, 10),
        ]);

        setOverview(overviewResp && typeof overviewResp === 'object' ? overviewResp : undefined);
        const normalizedRows = Array.isArray(trendsResp) ? trendsResp : [];
        setTrendRows(normalizedRows);

        setSlowRequests(Array.isArray(requestsResp?.items) ? requestsResp.items : []);
        setFailedTools(Array.isArray(toolsResp?.failure_top) ? toolsResp.failure_top : []);
      } catch {
        setOverview(undefined);
        setTrendRows([]);
        setSlowRequests([]);
        setFailedTools([]);
      } finally {
        setLoading(false);
      }
    };

    fetchAll();
  }, [filters, refreshToken]);

  const leftAxisData = useMemo(
    () =>
      sortedTrendRows.map((point) => ({
        metric: 'Token总量' as TrendMetric,
        value: Number(point.token_total || 0),
        request_value: Number(point.total_requests || 0),
        duration_value: Number(point.avg_duration_s || 0),
        token_value: Number(point.token_total || 0),
        bucket_key: String(point.bucket_ts || ''),
        bucket_label: formatAxisBucketLabel(point.bucket_ts as string, filters.granularity),
      })),
    [sortedTrendRows, filters.granularity],
  );

  const requestTrendData = useMemo(
    () =>
      sortedTrendRows.map((point) => ({
        value: Number(point.total_requests || 0),
        bucket_key: String(point.bucket_ts || ''),
      })),
    [sortedTrendRows, filters.granularity],
  );

  const durationTrendData = useMemo(
    () =>
      sortedTrendRows.map((point) => ({
        value: Number(point.avg_duration_s || 0),
        bucket_key: String(point.bucket_ts || ''),
      })),
    [sortedTrendRows, filters.granularity],
  );

  const tokenTrendData = leftAxisData;

  const xDomain = useMemo<string[] | undefined>(() => {
    if (leftAxisData.length === 0) {
      return undefined;
    }
    return leftAxisData.map((item) => String(item.bucket_key));
  }, [leftAxisData]);

  const xLabelMap = useMemo(() => {
    const map = new Map<string, string>();
    leftAxisData.forEach((item) => {
      map.set(String(item.bucket_key), String(item.bucket_label));
    });
    return map;
  }, [leftAxisData]);

  const rightAxisMax = useMemo(() => {
    const values: number[] = [];
    if (visibleMetrics.请求量) {
      values.push(...requestTrendData.map((item) => Number(item.value || 0)));
    }
    if (visibleMetrics['平均耗时(s)']) {
      values.push(...durationTrendData.map((item) => Number(item.value || 0)));
    }
    const maxValue = values.length ? Math.max(...values) : 0;
    return maxValue > 0 ? Math.ceil(maxValue * 1.1) : 1;
  }, [durationTrendData, requestTrendData, visibleMetrics]);

  const trendLegendItems = useMemo(
    () => [
      { label: '请求量' as TrendMetric },
      { label: '平均耗时(s)' as TrendMetric },
      { label: 'Token总量' as TrendMetric },
    ],
    [],
  );

  const toggleMetricVisibility = (metric: TrendMetric) => {
    setVisibleMetrics((prev) => ({
      ...prev,
      [metric]: !prev[metric],
    }));
  };

  const tooltipItems = useMemo(
    () =>
      [
        visibleMetrics.请求量
          ? {
              name: '请求量',
              color: TREND_METRIC_STYLE.请求量.color,
              field: 'request_value',
              valueFormatter: (value: number) => Number(value || 0).toLocaleString(),
            }
          : null,
        visibleMetrics['平均耗时(s)']
          ? {
              name: '平均耗时(s)',
              color: TREND_METRIC_STYLE['平均耗时(s)'].color,
              field: 'duration_value',
              valueFormatter: (value: number) => Number(value || 0).toFixed(2),
            }
          : null,
        visibleMetrics.Token总量
          ? {
              name: 'Token总量',
              color: TREND_METRIC_STYLE.Token总量.color,
              field: 'token_value',
              valueFormatter: (value: number) => Number(value || 0).toLocaleString(),
            }
          : null,
      ].filter(Boolean),
    [visibleMetrics],
  );

  return (
    <div className="page-layout">
      <KpiCards overview={overview} />

      <Card title="趋势总览" loading={loading}>
        <div className="overview-trend-legend">
          {trendLegendItems.map((item) => (
            <button
              key={item.label}
              type="button"
              className={`overview-trend-legend-item ${visibleMetrics[item.label] ? 'active' : 'inactive'}`}
              onClick={() => toggleMetricVisibility(item.label)}
              title={visibleMetrics[item.label] ? `点击隐藏 ${item.label}` : `点击显示 ${item.label}`}
            >
              <span
                className={`overview-trend-legend-line overview-trend-legend-line-${TREND_METRIC_STYLE[item.label].legendDash}`}
                style={{ borderTopColor: TREND_METRIC_STYLE[item.label].color }}
              />
              <span>{item.label}</span>
            </button>
          ))}
        </div>

        <div className="overview-trend-overlay">
          <Line
            data={tokenTrendData}
            xField="bucket_key"
            yField="value"
            color={TREND_METRIC_STYLE.Token总量.color}
            scale={{
              y: { min: 0, nice: true },
              ...(xDomain ? { bucket_key: { domain: xDomain, type: 'cat' as const } } : {}),
            }}
            axis={{
              x: {
                title: false,
                labelAutoHide: true,
                labelFormatter: (value: string) => xLabelMap.get(String(value)) || value,
              },
              y: {
                title: false,
                position: 'left',
                labelFormatter: (v: number) => v.toLocaleString(),
              },
            }}
            line={{
              style: {
                stroke: visibleMetrics.Token总量 ? TREND_METRIC_STYLE.Token总量.color : 'transparent',
                lineDash: TREND_METRIC_STYLE.Token总量.lineDash,
                lineWidth: visibleMetrics.Token总量 ? TREND_METRIC_STYLE.Token总量.lineWidth : 0,
              },
            }}
            tooltip={
              {
                title: 'bucket_label',
                items: tooltipItems,
              } as never
            }
            legend={false}
            point={
              visibleMetrics.Token总量
                ? { shapeField: 'circle', sizeField: 3 }
                : { shapeField: 'circle', sizeField: 0 }
            }
            padding={[24, 56, 40, 56]}
            theme={isDark ? 'classicDark' : 'classic'}
            autoFit
            height={320}
          />

          <div className="overview-trend-overlay-right">
            <Line
              data={requestTrendData}
              xField="bucket_key"
              yField="value"
              color={TREND_METRIC_STYLE.请求量.color}
              scale={{
                y: { domain: [0, rightAxisMax], nice: true },
                ...(xDomain ? { bucket_key: { domain: xDomain, type: 'cat' as const } } : {}),
              }}
              axis={{
                x: {
                  title: false,
                  labelFill: 'transparent',
                  labelAutoHide: true,
                  tick: false,
                  line: false,
                },
                y: {
                  title: false,
                  position: 'right',
                  grid: false,
                  labelFormatter: (v: number) => v.toLocaleString(),
                },
              }}
              line={{
                style: {
                  lineDash: TREND_METRIC_STYLE.请求量.lineDash,
                  lineWidth: visibleMetrics.请求量 ? TREND_METRIC_STYLE.请求量.lineWidth : 0,
                  stroke: visibleMetrics.请求量 ? TREND_METRIC_STYLE.请求量.color : 'transparent',
                },
              }}
              tooltip={false}
              legend={false}
              point={
                visibleMetrics.请求量
                  ? { shapeField: 'circle', sizeField: 3 }
                  : { shapeField: 'circle', sizeField: 0 }
              }
              padding={[24, 56, 40, 56]}
              theme={isDark ? 'classicDark' : 'classic'}
              autoFit
              height={320}
            />
          </div>

          <div className="overview-trend-overlay-right-top">
            <Line
              data={durationTrendData}
              xField="bucket_key"
              yField="value"
              color={TREND_METRIC_STYLE['平均耗时(s)'].color}
              scale={{
                y: { domain: [0, rightAxisMax], nice: true },
                ...(xDomain ? { bucket_key: { domain: xDomain, type: 'cat' as const } } : {}),
              }}
              axis={{
                x: {
                  title: false,
                  labelFill: 'transparent',
                  labelAutoHide: true,
                  tick: false,
                  line: false,
                },
                y: {
                  title: false,
                  position: 'right',
                  labelFill: 'transparent',
                  tick: false,
                  line: false,
                  grid: false,
                },
              }}
              line={{
                style: {
                  lineDash: TREND_METRIC_STYLE['平均耗时(s)'].lineDash,
                  lineWidth: visibleMetrics['平均耗时(s)'] ? TREND_METRIC_STYLE['平均耗时(s)'].lineWidth : 0,
                  stroke: visibleMetrics['平均耗时(s)']
                    ? TREND_METRIC_STYLE['平均耗时(s)'].color
                    : 'transparent',
                },
              }}
              tooltip={false}
              legend={false}
              point={
                visibleMetrics['平均耗时(s)']
                  ? { shapeField: 'circle', sizeField: 3 }
                  : { shapeField: 'circle', sizeField: 0 }
              }
              padding={[24, 56, 40, 56]}
              theme={isDark ? 'classicDark' : 'classic'}
              autoFit
              height={320}
            />
          </div>
        </div>
      </Card>

      <div className="split-grid">
        <Card title="慢请求 Top 10" loading={loading}>
          <Table
            scroll={{ x: 'max-content' }}
            rowKey="request_id"
            pagination={false}
            dataSource={slowRequests}
            columns={[
              { title: 'request_id', dataIndex: 'request_id', key: 'request_id' },
              { title: 'staff_code', dataIndex: 'staff_code', key: 'staff_code' },
              { title: '状态', dataIndex: 'status', key: 'status' },
              { title: '耗时(s)', dataIndex: 'duration_s', key: 'duration_s', render: (value: number) => value.toFixed(2) },
            ]}
          />
        </Card>

        <Card title="失败工具 Top 10" loading={loading}>
          <Table
            scroll={{ x: 'max-content' }}
            rowKey="tool"
            pagination={false}
            dataSource={failedTools}
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
            ]}
          />
        </Card>
      </div>
    </div>
  );
};
