import dayjs from 'dayjs';
import type { ChangeEvent } from 'react';
import { useEffect, useMemo, useState } from 'react';
import { Button, Card, DatePicker, Form, Input, Select } from '@agentscope-ai/design';

import { analyticsApi } from '../api/client';
import { FilterState, Granularity, LogLevel } from '../types';
import { MAIN_FLOW_CONTAINER_OPTIONS, getForcedToolByContainer, inferMainFlowContainer } from '../constants/containers';
import type { MainFlowContainerKey } from '../constants/containers';

interface FilterBarProps {
  filters: FilterState;
  onChange: (patch: Partial<FilterState>) => void;
  onRefresh: () => void;
  loading?: boolean;
}

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '成功', value: 'success' },
  { label: '失败', value: 'failed' },
  { label: '错误', value: 'error' },
];

const granularityOptions = [
  { label: '小时', value: 'hour' },
  { label: '天', value: 'day' },
];

const levelOptions: Array<{ label: string; value: LogLevel }> = [
  { label: '信息 INFO', value: 'INFO' },
  { label: '警告 WARNING', value: 'WARNING' },
  { label: '错误 ERROR', value: 'ERROR' },
  { label: '调试 DEBUG', value: 'DEBUG' },
  { label: '未知 UNKNOWN', value: 'UNKNOWN' },
];

export const FilterBar = ({ filters, onChange, onRefresh, loading }: FilterBarProps) => {
  const [agentOptions, setAgentOptions] = useState<Array<{ label: string; value: string }>>([{ label: '全部 Agent', value: '' }]);
  const [toolOptions, setToolOptions] = useState<Array<{ label: string; value: string }>>([{ label: '全部工具', value: '' }]);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const forcedTool = useMemo(() => getForcedToolByContainer(filters.container), [filters.container]);
  const selectedMainFlowContainer = useMemo(
    () => inferMainFlowContainer(filters.container),
    [filters.container],
  );

  const optionFilters = useMemo(
    () => ({
      ...filters,
      agentCode: '',
      tool: '',
    }),
    [
      filters.startTs,
      filters.endTs,
      filters.container,
      filters.status,
      filters.staffCode,
      filters.sessionId,
      filters.requestId,
      filters.queryLike,
      filters.granularity,
      filters.logLevels,
    ],
  );

  useEffect(() => {
    if (forcedTool && filters.tool !== forcedTool) {
      onChange({ tool: forcedTool });
    }
  }, [forcedTool, filters.tool, onChange]);

  useEffect(() => {
    let cancelled = false;

    const fetchOptions = async () => {
      if (forcedTool) {
        setAgentOptions([{ label: '全部 Agent', value: '' }]);
        setToolOptions([{ label: forcedTool, value: forcedTool }]);
        return;
      }
      setOptionsLoading(true);
      try {
        const [agentsResp, toolsResp] = await Promise.all([
          analyticsApi.getAgents(optionFilters, 100),
          analyticsApi.getTools(optionFilters, 100),
        ]);
        if (cancelled) {
          return;
        }

        const agentCodes = Array.from(new Set(agentsResp.map((item) => item.agent_code).filter(Boolean))).sort();
        const toolNames = Array.from(new Set(toolsResp.items.map((item) => item.tool).filter(Boolean))).sort();

        const nextAgentOptions = [{ label: '全部 Agent', value: '' }, ...agentCodes.map((value) => ({ label: value, value }))];
        const nextToolOptions = [{ label: '全部工具', value: '' }, ...toolNames.map((value) => ({ label: value, value }))];

        if (filters.agentCode && !agentCodes.includes(filters.agentCode)) {
          nextAgentOptions.push({ label: filters.agentCode, value: filters.agentCode });
        }
        if (filters.tool && !toolNames.includes(filters.tool)) {
          nextToolOptions.push({ label: filters.tool, value: filters.tool });
        }

        setAgentOptions(nextAgentOptions);
        setToolOptions(nextToolOptions);
      } catch {
        if (cancelled) {
          return;
        }
        setAgentOptions([{ label: '全部 Agent', value: '' }]);
        setToolOptions([{ label: '全部工具', value: '' }]);
      } finally {
        if (!cancelled) {
          setOptionsLoading(false);
        }
      }
    };

    fetchOptions();
    return () => {
      cancelled = true;
    };
  }, [forcedTool, optionFilters, filters.agentCode, filters.tool]);

  return (
    <Card title="筛选条件" className="filter-card">
      <Form layout="vertical" className="filter-form">
        <Form.Item label="容器">
          <Select
            value={selectedMainFlowContainer}
            options={MAIN_FLOW_CONTAINER_OPTIONS}
            onChange={(value: MainFlowContainerKey) => onChange({ container: value })}
          />
        </Form.Item>

        <Form.Item label="开始时间">
          <DatePicker
            showTime
            value={filters.startTs ? dayjs(filters.startTs) : undefined}
            onChange={(value) => onChange({ startTs: value ? value.toISOString() : '' })}
          />
        </Form.Item>

        <Form.Item label="结束时间">
          <DatePicker
            showTime
            value={filters.endTs ? dayjs(filters.endTs) : undefined}
            onChange={(value) => onChange({ endTs: value ? value.toISOString() : '' })}
          />
        </Form.Item>

        <Form.Item label="粒度">
          <Select
            value={filters.granularity}
            options={granularityOptions}
            onChange={(value: Granularity) => onChange({ granularity: value })}
          />
        </Form.Item>

        <Form.Item label="请求状态">
          <Select value={filters.status || ''} options={statusOptions} onChange={(value: string) => onChange({ status: value })} />
        </Form.Item>

        <Form.Item label="日志级别">
          <Select
            mode="multiple"
            allowClear
            value={filters.logLevels || []}
            options={levelOptions}
            optionFilterProp="label"
            onChange={(value: LogLevel[]) => onChange({ logLevels: value || [] })}
            placeholder="选择日志级别"
          />
        </Form.Item>

        <Form.Item label="staff_code">
          <Input
            value={filters.staffCode || ''}
            placeholder="例如 0120250028"
            onChange={(event: ChangeEvent<HTMLInputElement>) => onChange({ staffCode: event.target.value })}
          />
        </Form.Item>

        <Form.Item label="session_id">
          <Input
            value={filters.sessionId || ''}
            placeholder="输入 session_id"
            onChange={(event: ChangeEvent<HTMLInputElement>) => onChange({ sessionId: event.target.value })}
          />
        </Form.Item>

        <Form.Item label="request_id">
          <Input
            value={filters.requestId || ''}
            placeholder="输入 request_id"
            onChange={(event: ChangeEvent<HTMLInputElement>) => onChange({ requestId: event.target.value })}
          />
        </Form.Item>

        <Form.Item label="query_like">
          <Input
            value={filters.queryLike || ''}
            placeholder="按用户query模糊搜索（不区分大小写）"
            onChange={(event: ChangeEvent<HTMLInputElement>) => onChange({ queryLike: event.target.value })}
          />
        </Form.Item>

        <Form.Item label="agent_code">
          <Select
            showSearch
            virtual={false}
            value={filters.agentCode || ''}
            options={agentOptions}
            loading={optionsLoading}
            optionFilterProp="label"
            onChange={(value: string) => onChange({ agentCode: value })}
          />
        </Form.Item>

        <Form.Item label="tool">
          <Select
            showSearch
            virtual={false}
            value={forcedTool || filters.tool || ''}
            options={toolOptions}
            loading={optionsLoading}
            optionFilterProp="label"
            disabled={Boolean(forcedTool)}
            onChange={(value: string) => onChange({ tool: value })}
          />
        </Form.Item>
      </Form>

      <div className="filter-actions">
        <Button type="primary" loading={loading} onClick={onRefresh}>
          刷新
        </Button>
      </div>
      <div className="timezone-hint">当前展示时区：Asia/Shanghai（查询参数按 UTC 发送）</div>
      {forcedTool ? (
        <div className="timezone-hint">
          当前容器已强制映射 tool：{forcedTool}
        </div>
      ) : null}
    </Card>
  );
};
