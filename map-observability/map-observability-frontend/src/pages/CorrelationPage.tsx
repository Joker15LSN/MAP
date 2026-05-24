import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Drawer, Input, Table, Tag } from '@agentscope-ai/design';

import { analyticsApi } from '../api/client';
import { CorrelationErrorsPayload, CorrelationLogItem, CorrelationRidPayload, FilterState, TimeAlignPayload } from '../types';
import { formatIsoTimePair } from '../utils/time';

interface CorrelationPageProps {
  filters: FilterState;
  refreshToken: number;
}

const PAGE_SIZE = 10;

const formatLevelMap = (value?: Record<string, number>) => {
  if (!value || Object.keys(value).length === 0) {
    return '-';
  }
  return Object.entries(value)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}:${v}`)
    .join(' | ');
};

export const CorrelationPage = ({ filters, refreshToken }: CorrelationPageProps) => {
  const [alignLoading, setAlignLoading] = useState(false);
  const [alignPayload, setAlignPayload] = useState<TimeAlignPayload>();
  const [alignError, setAlignError] = useState('');

  const [ridInput, setRidInput] = useState(filters.requestId || '');
  const [ridLoading, setRidLoading] = useState(false);
  const [ridPayload, setRidPayload] = useState<CorrelationRidPayload>();
  const [ridError, setRidError] = useState('');
  const [ridPage, setRidPage] = useState(1);

  const [keywordInput, setKeywordInput] = useState('error,exception,failed,traceback,timeout');
  const [errorLoading, setErrorLoading] = useState(false);
  const [errorsPayload, setErrorsPayload] = useState<CorrelationErrorsPayload>();
  const [clusterError, setClusterError] = useState('');
  const [clusterPage, setClusterPage] = useState(1);

  const [selectedLogLine, setSelectedLogLine] = useState<string>();

  useEffect(() => {
    if (filters.requestId) {
      setRidInput(filters.requestId);
    }
  }, [filters.requestId]);

  const runTimeAlign = async () => {
    setAlignLoading(true);
    setAlignError('');
    try {
      const payload = await analyticsApi.getTimeAlign(filters.startTs, filters.endTs, 'Asia/Shanghai', 120);
      setAlignPayload(payload);
    } catch (error) {
      setAlignError(String((error as Error)?.message || error));
    } finally {
      setAlignLoading(false);
    }
  };

  const runRidTrace = async (page = 1) => {
    if (!ridInput.trim()) {
      return;
    }
    setRidLoading(true);
    setRidError('');
    setRidPage(page);
    try {
      const payload = await analyticsApi.getRidCorrelation(
        ridInput.trim(),
        filters.container,
        120,
        filters.logLevels || [],
        page,
        PAGE_SIZE,
      );
      setRidPayload(payload);
    } catch (error) {
      setRidError(String((error as Error)?.message || error));
    } finally {
      setRidLoading(false);
    }
  };

  const runErrorClustering = async (page = 1) => {
    setErrorLoading(true);
    setClusterError('');
    setClusterPage(page);
    try {
      const payload = await analyticsApi.getCorrelationErrors(
        filters.startTs,
        filters.endTs,
        filters.container,
        keywordInput,
        'Asia/Shanghai',
        120,
        filters.logLevels || [],
        page,
        PAGE_SIZE,
        filters.staffCode,
        filters.sessionId,
        filters.requestId,
      );
      setErrorsPayload(payload);
    } catch (error) {
      setClusterError(String((error as Error)?.message || error));
    } finally {
      setErrorLoading(false);
    }
  };

  useEffect(() => {
    runTimeAlign();
    runErrorClustering(1);
    if (filters.requestId) {
      runRidTrace(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.startTs, filters.endTs, filters.container, filters.staffCode, filters.sessionId, filters.requestId, filters.logLevels, refreshToken]);

  const logRows = useMemo<CorrelationLogItem[]>(
    () => ridPayload?.logs_page?.items || ridPayload?.loki_logs || [],
    [ridPayload?.logs_page?.items, ridPayload?.loki_logs],
  );
  const startAlignPair = useMemo(
    () => formatIsoTimePair(alignPayload?.start_local || alignPayload?.start_utc),
    [alignPayload?.start_local, alignPayload?.start_utc],
  );
  const endAlignPair = useMemo(
    () => formatIsoTimePair(alignPayload?.end_local || alignPayload?.end_utc),
    [alignPayload?.end_local, alignPayload?.end_utc],
  );
  const formatNs = (value?: string) => {
    if (!value) {
      return '-';
    }
    const normalized = value.replace(/,/g, '');
    return normalized.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  };

  const traceNodes = ridPayload?.trace_chain?.nodes || [];
  const traceEdges = ridPayload?.trace_chain?.edges || [];
  const edgeText = traceEdges.slice(0, 5).map((edge) => `${edge.from} -> ${edge.to} (${edge.count})`).join(' ; ');
  const mainChainAlerts = ridPayload?.main_chain_highlights?.alert_logs || [];
  const mainChainAids = ridPayload?.main_chain_highlights?.main_chain_aids || [];
  const mainChainLevelBreakdown = ridPayload?.main_chain_highlights?.level_breakdown || {};
  const mainChainAlertCount = ridPayload?.main_chain_highlights?.alert_count || 0;

  return (
    <div className="page-layout">
      <Alert
        type="info"
        showIcon
        message="排查 SOP"
        description="1) Grafana 选 container + 时间 2) 时间对齐 3) Mongo 按 UTC 查 request_id 4) Loki 按 rid/sid/aid/parid 联查 5) 输出归因与优化建议。"
      />

      <Card
        title="时间对齐（Grafana 本地时间 -> Mongo UTC + Loki ns）"
        extra={
          <div className="inline-actions correlation-extra-actions">
            <Button className="action-btn-primary" type="primary" loading={alignLoading} onClick={runTimeAlign}>
              时间对齐
            </Button>
          </div>
        }
      >
        {alignError ? <Alert type="error" showIcon message={alignError} className="table-gap-bottom" /> : null}
        <div className="time-align-grid">
          <div className="time-align-item">
            <div className="time-align-label">容器</div>
            <div className="time-align-value">{filters.container}</div>
          </div>
          <div className="time-align-item">
            <div className="time-align-label">开始时间 (UTC+8)</div>
            <div className="time-align-value">{startAlignPair.localText}</div>
            <div className="time-align-sub">{startAlignPair.utcText}</div>
          </div>
          <div className="time-align-item">
            <div className="time-align-label">结束时间 (UTC+8)</div>
            <div className="time-align-value">{endAlignPair.localText}</div>
            <div className="time-align-sub">{endAlignPair.utcText}</div>
          </div>
          <div className="time-align-item">
            <div className="time-align-label">Start ns</div>
            <div className="time-align-value">{formatNs(alignPayload?.start_ns)}</div>
          </div>
          <div className="time-align-item">
            <div className="time-align-label">End ns</div>
            <div className="time-align-value">{formatNs(alignPayload?.end_ns)}</div>
          </div>
          <div className="time-align-item">
            <div className="time-align-label">缓冲窗口</div>
            <div className="time-align-value">{alignPayload ? `±${alignPayload.buffer_seconds}s` : '-'}</div>
          </div>
        </div>
      </Card>

      <Card
        title="RID 关联追踪"
        extra={
          <div className="inline-actions">
            <Input
              value={ridInput}
              onChange={(event) => setRidInput(event.target.value)}
              placeholder="输入 request_id / rid"
              style={{ width: 320 }}
            />
            <Button className="action-btn-primary" type="primary" loading={ridLoading} onClick={() => runRidTrace(1)}>
              一键追踪
            </Button>
          </div>
        }
      >
        {ridError ? <Alert type="error" showIcon message={ridError} className="table-gap-bottom" /> : null}
        <div className="summary-row">
          <Tag>根因建议: {ridPayload?.root_cause_hint || '-'}</Tag>
          <Tag>日志总数: {ridPayload?.log_summary.total_logs ?? 0}</Tag>
          <Tag>错误命中: {ridPayload?.log_summary.error_hits ?? 0}</Tag>
          <Tag>RID 命中: {String(ridPayload?.correlation_checks?.rid_match_count ?? 0)}</Tag>
          <Tag>SID 命中: {String(ridPayload?.correlation_checks?.sid_match_count ?? 0)}</Tag>
          <Tag>级别分布: {formatLevelMap(ridPayload?.log_summary?.level_breakdown)}</Tag>
        </div>

        <Card className="table-gap-top" title="主链路告警（ERROR / WARNING）" bordered={false}>
          <div className="summary-row">
            <Tag>主链路 AID: {(mainChainAids || []).join(' -> ') || '-'}</Tag>
            <Tag>告警数: {mainChainAlertCount}</Tag>
            <Tag>告警级别分布: {formatLevelMap(mainChainLevelBreakdown)}</Tag>
          </div>
          {mainChainAlertCount === 0 ? (
            <Alert
              className="table-gap-top"
              type="success"
              showIcon
              message="主链路未发现 ERROR/WARNING 日志"
            />
          ) : (
            <Table
              className="table-gap-top main-chain-alert-table"
              scroll={{ x: 'max-content' }}
              rowKey={(row: CorrelationLogItem) => `main-${String(row.ts_ns)}-${String(row.line)}`}
              pagination={false}
              dataSource={mainChainAlerts}
              columns={[
                {
                  title: '时间(UTC+8)',
                  dataIndex: 'ts_utc',
                  key: 'ts_utc',
                  width: 220,
                  render: (value: string) => formatIsoTimePair(value).localText,
                },
                { title: '级别', dataIndex: 'level', key: 'level', width: 90 },
                { title: 'aid', dataIndex: 'aid', key: 'aid', width: 160 },
                {
                  title: '日志行',
                  dataIndex: 'line',
                  key: 'line',
                  render: (value: string) => (
                    <span
                      className="log-line-preview log-line-clickable"
                      title={value}
                      onClick={() => setSelectedLogLine(String(value || ''))}
                    >
                      {value}
                    </span>
                  ),
                },
                {
                  title: '操作',
                  key: 'action',
                  width: 90,
                  render: (_: unknown, row: CorrelationLogItem) => (
                    <Button type="link" onClick={() => setSelectedLogLine(String(row.raw_line || row.line || ''))}>
                      查看
                    </Button>
                  ),
                },
              ]}
            />
          )}
        </Card>

        <Table
          className="table-gap-top rid-log-table"
          scroll={{ x: 'max-content' }}
          rowKey={(row: CorrelationLogItem) => `${String(row.ts_ns)}-${String(row.line)}`}
          dataSource={logRows}
          rowClassName={(row: CorrelationLogItem) => {
            if (!row?.is_main_chain || !row?.is_alert) {
              return '';
            }
            if (row.level === 'ERROR') {
              return 'main-chain-alert-row error-row';
            }
            if (row.level === 'WARNING') {
              return 'main-chain-alert-row warning-row';
            }
            return 'main-chain-alert-row';
          }}
          pagination={{
            current: ridPayload?.logs_page?.page || ridPage,
            pageSize: PAGE_SIZE,
            total: ridPayload?.logs_page?.total || 0,
            showSizeChanger: false,
            onChange: (page) => runRidTrace(page),
          }}
          columns={[
            {
              title: '日志时间(UTC+8)',
              dataIndex: 'ts_utc',
              key: 'ts_utc',
              width: 220,
              render: (value: string) => {
                const pair = formatIsoTimePair(value);
                return (
                  <div className="time-cell" title={pair.utcText}>
                    <div>{pair.localText}</div>
                    <div className="time-cell-sub">{pair.utcText}</div>
                  </div>
                );
              },
            },
            { title: '级别', dataIndex: 'level', key: 'level', width: 70 },
            { title: 'rid', dataIndex: 'rid', key: 'rid', width: 160 },
            { title: 'sid', dataIndex: 'sid', key: 'sid', width: 160 },
            { title: 'aid', dataIndex: 'aid', key: 'aid', width: 120 },
            { title: 'parid', dataIndex: 'parid', key: 'parid', width: 120 },
            {
              title: '日志行',
              dataIndex: 'line',
              key: 'line',
              render: (value: string) => (
                <span className="log-line-preview" title={value}>
                  {value}
                </span>
              ),
            },
            {
              title: '操作',
              key: 'action',
              width: 80,
              render: (_: unknown, row: CorrelationLogItem) => (
                <Button type="link" onClick={() => setSelectedLogLine(String(row.raw_line || row.line || ''))}>
                  查看
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Card title="链路溯源（rid -> sid -> aid/parid）">
        <div className="summary-row">
          <Tag>session_id: {ridPayload?.trace_chain?.session_id || '-'}</Tag>
          <Tag>根节点数: {ridPayload?.trace_chain?.root_nodes?.length || 0}</Tag>
          <Tag>孤立父节点: {ridPayload?.trace_chain?.unresolved_parents?.length || 0}</Tag>
          <Tag>
            Mongo 映射: AID总数 {String((ridPayload?.trace_chain?.mongo_link_stats?.aid_total as number) || 0)}
          </Tag>
        </div>
        {edgeText ? <div className="time-align-sub">父子关系样例: {edgeText}</div> : null}
        <Table
          className="table-gap-top trace-chain-table"
          scroll={{ x: 'max-content' }}
          rowKey={(row: Record<string, unknown>) => String(row.aid)}
          tableLayout="fixed"
          dataSource={traceNodes as unknown as Record<string, unknown>[]}
          pagination={{ pageSize: PAGE_SIZE, showSizeChanger: false }}
          columns={[
            {
              title: 'aid',
              dataIndex: 'aid',
              key: 'aid',
              width: 120,
              render: (value: string) => (
                <span className="cell-ellipsis" title={value}>
                  {value || '-'}
                </span>
              ),
            },
            {
              title: 'parid',
              dataIndex: 'parid',
              key: 'parid',
              width: 120,
              render: (value: string) => (
                <span className="cell-ellipsis" title={value}>
                  {value || '-'}
                </span>
              ),
            },
            {
              title: '首日志时间(UTC+8)',
              dataIndex: 'first_ts_utc',
              key: 'first_ts_utc',
              width: 180,
              render: (value: string) => {
                const localText = formatIsoTimePair(value).localText;
                return (
                  <span className="cell-ellipsis" title={localText}>
                    {localText}
                  </span>
                );
              },
            },
            {
              title: '末日志时间(UTC+8)',
              dataIndex: 'last_ts_utc',
              key: 'last_ts_utc',
              width: 180,
              render: (value: string) => {
                const localText = formatIsoTimePair(value).localText;
                return (
                  <span className="cell-ellipsis" title={localText}>
                    {localText}
                  </span>
                );
              },
            },
            { title: '日志条数', dataIndex: 'log_count', key: 'log_count', width: 90 },
            {
              title: '级别分布',
              dataIndex: 'level_breakdown',
              key: 'level_breakdown',
              width: 170,
              render: (value: Record<string, number>) => {
                const text = formatLevelMap(value);
                return (
                  <span className="cell-ellipsis" title={text}>
                    {text}
                  </span>
                );
              },
            },
            {
              title: 'Mongo Agent',
              dataIndex: 'mongo_agent_codes',
              key: 'mongo_agent_codes',
              width: 160,
              render: (value: string[]) => (
                <span className="cell-ellipsis" title={(value || []).join(', ')}>
                  {(value || []).join(', ') || '-'}
                </span>
              ),
            },
            {
              title: 'Mongo Tool',
              dataIndex: 'mongo_tools',
              key: 'mongo_tools',
              width: 160,
              render: (value: string[]) => (
                <span className="cell-ellipsis" title={(value || []).join(', ')}>
                  {(value || []).join(', ') || '-'}
                </span>
              ),
            },
          ]}
        />
      </Card>

      <Card
        title="错误聚类"
        extra={
          <div className="inline-actions">
            <Input
              value={keywordInput}
              onChange={(event) => setKeywordInput(event.target.value)}
              placeholder="error,exception,failed,traceback,timeout"
              style={{ width: 340 }}
            />
            <Button className="action-btn-primary" type="primary" loading={errorLoading} onClick={() => runErrorClustering(1)}>
              更新聚类
            </Button>
          </div>
        }
      >
        {clusterError ? <Alert type="error" showIcon message={clusterError} className="table-gap-bottom" /> : null}
        <Table
          scroll={{ x: 'max-content' }}
          tableLayout="fixed"
          rowKey={(row: Record<string, unknown>) => String(row.error_type)}
          dataSource={(errorsPayload?.clusters_page?.items || errorsPayload?.clusters || []) as unknown as Record<string, unknown>[]}
          pagination={{
            current: errorsPayload?.clusters_page?.page || clusterPage,
            pageSize: PAGE_SIZE,
            total: errorsPayload?.clusters_page?.total || 0,
            showSizeChanger: false,
            onChange: (page) => runErrorClustering(page),
          }}
          columns={[
            { title: '异常类型', dataIndex: 'error_type', key: 'error_type', width: 200 },
            { title: '次数', dataIndex: 'count', key: 'count', width: 90 },
            {
              title: '级别分布',
              dataIndex: 'level_breakdown',
              key: 'level_breakdown',
              render: (value: Record<string, number>) => formatLevelMap(value),
            },
            {
              title: '首次时间(UTC+8)',
              dataIndex: 'first_ts_utc',
              key: 'first_ts_utc',
              width: 300,
              render: (value: string) => {
                const pair = formatIsoTimePair(value);
                return (
                  <div className="time-cell" title={pair.utcText}>
                    <div>{pair.localText}</div>
                    <div className="time-cell-sub">{pair.utcText}</div>
                  </div>
                );
              },
            },
            {
              title: '末次时间(UTC+8)',
              dataIndex: 'last_ts_utc',
              key: 'last_ts_utc',
              width: 300,
              render: (value: string) => {
                const pair = formatIsoTimePair(value);
                return (
                  <div className="time-cell" title={pair.utcText}>
                    <div>{pair.localText}</div>
                    <div className="time-cell-sub">{pair.utcText}</div>
                  </div>
                );
              },
            },
            {
              title: '样例 RID',
              dataIndex: 'sample_request_ids',
              key: 'sample_request_ids',
              width: 320,
              ellipsis: true,
              render: (value: string[]) => (
                <span className="cell-ellipsis" title={(value || []).join(', ')}>
                  {(value || []).join(', ') || '-'}
                </span>
              ),
            },
          ]}
        />
      </Card>

      <Drawer title="原始日志行" open={Boolean(selectedLogLine)} onClose={() => setSelectedLogLine(undefined)} width={900}>
        <pre className="raw-json raw-log-line">{selectedLogLine || ''}</pre>
      </Drawer>
    </div>
  );
};
