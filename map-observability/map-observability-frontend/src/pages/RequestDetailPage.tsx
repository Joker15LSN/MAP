import { useEffect, useState } from 'react';
import { Alert, Button, Card, Table, Tag } from '@agentscope-ai/design';
import { Collapse } from 'antd';

import { analyticsApi } from '../api/client';
import { RequestDetail } from '../types';
import { formatIsoTimePair } from '../utils/time';
import { RequestCallTree } from '../components/RequestCallTree';

interface RequestDetailPageProps {
  requestId?: string;
  refreshToken: number;
  detailToken: number;
}

const dateRender = (value: unknown) => {
  if (!value) {
    return '-';
  }
  const pair = formatIsoTimePair(String(value));
  return (
    <div className="time-cell" title={pair.utcText}>
      <div>{pair.localText}</div>
      <div className="time-cell-sub">{pair.utcText}</div>
    </div>
  );
};

export const RequestDetailPage = ({ requestId, refreshToken, detailToken }: RequestDetailPageProps) => {
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<RequestDetail>();
  const [errorMessage, setErrorMessage] = useState('');
  const [expandedDetailKeys, setExpandedDetailKeys] = useState<string[]>([]);

  useEffect(() => {
    const run = async () => {
      if (!requestId) {
        setDetail(undefined);
        setErrorMessage('');
        setExpandedDetailKeys([]);
        return;
      }
      setExpandedDetailKeys([]);
      setLoading(true);
      setErrorMessage('');
      try {
        const result = await analyticsApi.getRequestDetail(requestId);
        setDetail(result);
      } catch (error) {
        setDetail(undefined);
        setErrorMessage(String((error as Error)?.message || error));
      } finally {
        setLoading(false);
      }
    };

    run();
  }, [requestId, refreshToken, detailToken]);

  const allDetailExpanded = expandedDetailKeys.length === 3;

  if (!requestId) {
    return (
      <Card title="请求详情">
        <Alert type="info" showIcon message="请先在“请求检索”中点击 request_id，再在此查看完整链路详情。" />
      </Card>
    );
  }

  return (
    <div className="detail-layout">
      {errorMessage ? <Alert type="error" showIcon message={errorMessage} /> : null}
      {detail ? (
        <>
          <Card title="请求摘要" loading={loading}>
            <div className="summary-row">
              <Tag>request_id: {detail.request.request_id}</Tag>
              <Tag>状态: {detail.request.status || 'unknown'}</Tag>
              <Tag>耗时: {detail.request.duration_s.toFixed(2)}s</Tag>
              <Tag>Token: {detail.request.token_total}</Tag>
            </div>
            <div className="detail-query">{detail.request.query || '-'}</div>
          </Card>

          <Card title="调用链路树（大场景/小场景 + sub-agent 工具子问题）" loading={loading}>
            <RequestCallTree detail={detail} />
          </Card>

          <Card
            title="详细明细（默认收起）"
            loading={loading}
            extra={
              <Button
                type="link"
                onClick={() => setExpandedDetailKeys(allDetailExpanded ? [] : ['timeline', 'tools', 'scene'])}
              >
                {allDetailExpanded ? '全部收起' : '全部展开'}
              </Button>
            }
          >
            <Collapse
              className="request-detail-collapse"
              activeKey={expandedDetailKeys}
              onChange={(keys) => {
                const nextKeys = Array.isArray(keys) ? keys.map((key) => String(key)) : [String(keys)];
                setExpandedDetailKeys(nextKeys);
              }}
              items={[
                {
                  key: 'timeline',
                  label: `Agent 时间线（${detail.agent_timeline.length}）`,
                  children: (
                    <Table
                      scroll={{ x: 'max-content' }}
                      rowKey={(row: Record<string, unknown>) => `${row.state_id}-${row.agent_code}-${row.seq}`}
                      pagination={false}
                      dataSource={detail.agent_timeline}
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
                  ),
                },
                {
                  key: 'tools',
                  label: `工具调用（${detail.tool_calls.length}）`,
                  children: (
                    <Table
                      scroll={{ x: 'max-content' }}
                      rowKey={(row: Record<string, unknown>) => `${row.tool_id}-${row.step}`}
                      pagination={false}
                      dataSource={detail.tool_calls}
                      columns={[
                        { title: 'tool', dataIndex: 'tool', key: 'tool' },
                        { title: 'agent_code', dataIndex: 'agent_code', key: 'agent_code' },
                        { title: '步骤', dataIndex: 'step', key: 'step' },
                        { title: '状态', dataIndex: 'status', key: 'status', render: (value: unknown) => value || 'unknown' },
                        { title: '时间(UTC+8)', dataIndex: 'ts', key: 'ts', render: dateRender },
                      ]}
                    />
                  ),
                },
                {
                  key: 'scene',
                  label: '场景识别原始数据',
                  children: <pre className="raw-json">{JSON.stringify(detail.request.scene_result || {}, null, 2)}</pre>,
                },
              ]}
            />
          </Card>
        </>
      ) : (
        <Card title="请求详情" loading={loading}>
          <Alert type="warning" showIcon message={`未获取到 request_id=${requestId} 的详情数据`} />
        </Card>
      )}
    </div>
  );
};
