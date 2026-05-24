import { type Key, useEffect, useState } from 'react';
import { Alert, Button, Card, Table, Tag } from '@agentscope-ai/design';

import { analyticsApi } from '../api/client';
import { RequestDetail, RequestListPayload, FilterState } from '../types';
import { RequestDetailDrawer } from '../components/RequestDetailDrawer';
import { formatIsoTimePair } from '../utils/time';

interface RequestsPageProps {
  filters: FilterState;
  refreshToken: number;
  openRequestSignal?: {
    requestId: string;
    nonce: number;
  } | null;
  onRequestSignalConsumed?: () => void;
}

export const RequestsPage = ({
  filters,
  refreshToken,
  openRequestSignal,
  onRequestSignalConsumed,
}: RequestsPageProps) => {
  const [loading, setLoading] = useState(false);
  const [payload, setPayload] = useState<RequestListPayload>({ total: 0, page: 1, page_size: 10, items: [] });
  const [errorMessage, setErrorMessage] = useState('');
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<RequestDetail>();
  const [detailErrorMessage, setDetailErrorMessage] = useState('');
  const [selectedRequestIds, setSelectedRequestIds] = useState<string[]>([]);
  const [exportingScope, setExportingScope] = useState<'selected' | 'all' | ''>('');

  const loadRequests = async (page = payload.page, pageSize = 10) => {
    setLoading(true);
    setErrorMessage('');
    try {
      const result = await analyticsApi.getRequests(filters, page, pageSize, 'start_ts', 'desc');
      setPayload(
        result && Array.isArray(result.items)
          ? result
          : { total: 0, page, page_size: pageSize, items: [] },
      );
    } catch (error) {
      setPayload({ total: 0, page, page_size: pageSize, items: [] });
      setErrorMessage(String((error as Error)?.message || error));
    } finally {
      setLoading(false);
    }
  };

  const loadDetail = async (requestId: string) => {
    setDetailOpen(true);
    setDetailLoading(true);
    setDetailErrorMessage('');
    try {
      const result = await analyticsApi.getRequestDetail(requestId, filters.container);
      setDetail(result);
    } catch (error) {
      setDetail(undefined);
      setDetailErrorMessage(String((error as Error)?.message || error));
    } finally {
      setDetailLoading(false);
    }
  };

  const downloadJsonl = (blob: Blob, scope: 'selected' | 'all') => {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `map-qa-${scope}-${timestamp}.jsonl`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
  };

  const exportJsonl = async (scope: 'selected' | 'all') => {
    if (scope === 'selected' && selectedRequestIds.length === 0) {
      return;
    }

    setExportingScope(scope);
    setErrorMessage('');
    try {
      const blob = await analyticsApi.exportRequestsJsonl(
        filters,
        scope === 'selected' ? selectedRequestIds : undefined,
      );
      if (blob.size === 0) {
        throw new Error('当前筛选条件下没有可导出的问答记录');
      }
      downloadJsonl(blob, scope);
    } catch (error) {
      setErrorMessage(String((error as Error)?.message || error));
    } finally {
      setExportingScope('');
    }
  };

  useEffect(() => {
    setSelectedRequestIds([]);
    loadRequests(1, 10);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, refreshToken]);

  useEffect(() => {
    if (!openRequestSignal?.requestId) {
      return;
    }
    loadDetail(openRequestSignal.requestId);
    onRequestSignalConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequestSignal?.nonce]);

  const renderTimeCell = (value: string) => {
    const pair = formatIsoTimePair(value);
    return (
      <div className="time-cell" title={pair.utcText}>
        <div>{pair.localText}</div>
        <div className="time-cell-sub">{pair.utcText}</div>
      </div>
    );
  };

  return (
    <>
      <Card
        title="请求检索"
        loading={loading}
        extra={
          <div className="request-list-actions">
            <Tag>已选 {selectedRequestIds.length}</Tag>
            <Button
              disabled={selectedRequestIds.length === 0 || Boolean(exportingScope)}
              loading={exportingScope === 'selected'}
              onClick={() => exportJsonl('selected')}
            >
              导出选中 JSONL
            </Button>
            <Button
              type="primary"
              disabled={Boolean(exportingScope)}
              loading={exportingScope === 'all'}
              onClick={() => exportJsonl('all')}
            >
              导出筛选全部 JSONL
            </Button>
          </div>
        }
      >
        {errorMessage ? <Alert type="error" showIcon message={errorMessage} className="table-gap-bottom" /> : null}
        <Table
          scroll={{ x: 'max-content' }}
          rowKey="request_id"
          rowSelection={{
            selectedRowKeys: selectedRequestIds,
            preserveSelectedRowKeys: true,
            onChange: (keys: Key[]) => setSelectedRequestIds(keys.map((key) => String(key))),
          }}
          dataSource={payload.items}
          pagination={{
            current: payload.page,
            pageSize: 10,
            total: payload.total,
            showSizeChanger: false,
            onChange: (page) => loadRequests(page, 10),
          }}
          columns={[
            {
              title: 'request_id',
              dataIndex: 'request_id',
              key: 'request_id',
              render: (value: string) => (
                <Button type="link" onClick={() => loadDetail(value)}>
                  {value}
                </Button>
              ),
            },
            {
              title: 'query',
              dataIndex: 'query',
              key: 'query',
              width: 420,
              render: (value: string) => (
                <span className="query-cell-wrap" title={value || ''}>
                  {value || '-'}
                </span>
              ),
            },
            { title: 'staff_code', dataIndex: 'staff_code', key: 'staff_code' },
            { title: 'session_id', dataIndex: 'session_id', key: 'session_id' },
            {
              title: '状态',
              dataIndex: 'status',
              key: 'status',
              render: (value: string) => <Tag>{value || 'unknown'}</Tag>,
            },
            {
              title: '耗时(s)',
              dataIndex: 'duration_s',
              key: 'duration_s',
              render: (value: number) => value.toFixed(2),
            },
            {
              title: 'Token',
              dataIndex: 'token_total',
              key: 'token_total',
              render: (value: number) => value.toFixed(0),
            },
            { title: '工具调用数', dataIndex: 'tool_call_count', key: 'tool_call_count' },
            {
              title: '开始时间(UTC+8)',
              dataIndex: 'start_ts',
              key: 'start_ts',
              render: (value: string) => renderTimeCell(value),
            },
          ]}
        />
      </Card>

      <RequestDetailDrawer
        open={detailOpen}
        loading={detailLoading}
        detail={detail}
        errorMessage={detailErrorMessage}
        activeContainer={filters.container}
        activeLevels={filters.logLevels || []}
        onClose={() => {
          setDetailOpen(false);
          setDetail(undefined);
          setDetailErrorMessage('');
        }}
      />
    </>
  );
};
