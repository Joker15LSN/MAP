import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Collapse, Drawer, Table, Tag } from '@agentscope-ai/design';

import { RequestCallTree } from './RequestCallTree';
import { ErrorAlert } from './AsyncState';
import { AgentTimelinePanel } from './requestDetail/AgentTimelinePanel';
import { LLMTracePanel } from './requestDetail/LLMTracePanel';
import { ToolCallsPanel } from './requestDetail/ToolCallsPanel';
import { ScenePanel } from './requestDetail/ScenePanel';
import { ToolCallDetailDrawer } from './requestDetail/ToolCallDetailDrawer';
import { DETAIL_PANEL_KEYS } from './requestDetail/types';
import type { RequestDetailDrawerProps, ToolCallRow } from './requestDetail/types';
import { getToolCallIdentity, mergeToolCallRows, toText } from './requestDetail/utils';

export const RequestDetailDrawer = ({
  open,
  loading,
  detail,
  errorMessage,
  activeContainer,
  activeLevels,
  onClose,
}: RequestDetailDrawerProps) => {
  const [selectedToolCall, setSelectedToolCall] = useState<ToolCallRow>();
  const [expandedDetailKeys, setExpandedDetailKeys] = useState<string[]>([]);

  const requestId = String(detail?.request?.request_id || '');
  const llmCallRows = useMemo(() => detail?.llm_calls || [], [detail?.llm_calls]);
  const mergedToolCallRows = useMemo<ToolCallRow[]>(() => {
    const sourceRows = (detail?.tool_calls || []).map((row) => row as ToolCallRow);
    if (sourceRows.length === 0) {
      return [];
    }
    const groups = new Map<string, ToolCallRow[]>();
    sourceRows.forEach((row) => {
      const key = getToolCallIdentity(row);
      if (!groups.has(key)) {
        groups.set(key, []);
      }
      groups.get(key)!.push(row);
    });
    const mergedRows = Array.from(groups.values()).map((rows) => mergeToolCallRows(rows));
    mergedRows.sort((a, b) => toText(a.ts, '').localeCompare(toText(b.ts, '')));
    return mergedRows;
  }, [detail?.tool_calls]);

  useEffect(() => {
    if (!open) {
      setSelectedToolCall(undefined);
      setExpandedDetailKeys([]);
    }
  }, [open]);

  const closeDetailDrawer = () => {
    setSelectedToolCall(undefined);
    onClose();
  };

  const allDetailExpanded = expandedDetailKeys.length === DETAIL_PANEL_KEYS.length;

  return (
    <>
      <Drawer title="请求详情" open={open} onClose={closeDetailDrawer} width={1000}>
        <ErrorAlert message={errorMessage} className="table-gap-bottom" />
        {detail ? (
          <div className="detail-layout">
            <Card title="请求摘要" loading={loading}>
              <div className="summary-row">
                <Tag>request_id: {detail.request.request_id}</Tag>
                <Tag>状态: {detail.request.status || 'unknown'}</Tag>
                <Tag>耗时: {detail.request.duration_s.toFixed(2)}s</Tag>
                <Tag>Token: {detail.request.token_total}</Tag>
                <Tag>LLM 调用: {detail.summary?.llm_call_count ?? llmCallRows.length}</Tag>
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
                  onClick={() => setExpandedDetailKeys(allDetailExpanded ? [] : [...DETAIL_PANEL_KEYS])}
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
                    children: <AgentTimelinePanel rows={detail.agent_timeline} />,
                  },
                  {
                    key: 'llm',
                    label: `LLM 调用轨迹（${llmCallRows.length}）`,
                    children: <LLMTracePanel rows={llmCallRows} requestId={requestId} />,
                  },
                  {
                    key: 'tools',
                    label: `工具调用（${mergedToolCallRows.length}）`,
                    children: <ToolCallsPanel rows={mergedToolCallRows} onSelectToolCall={setSelectedToolCall} />,
                  },
                  {
                    key: 'scene',
                    label: '场景识别原始数据',
                    children: <ScenePanel sceneResult={detail.request.scene_result} />,
                  },
                ]}
              />
            </Card>
          </div>
        ) : !loading ? (
          <Alert type="warning" showIcon message="未获取到请求详情数据" />
        ) : null}
      </Drawer>

      <ToolCallDetailDrawer
        detail={detail}
        toolCall={selectedToolCall}
        requestId={requestId}
        activeContainer={activeContainer}
        activeLevels={activeLevels}
        onClose={() => setSelectedToolCall(undefined)}
      />
    </>
  );
};
