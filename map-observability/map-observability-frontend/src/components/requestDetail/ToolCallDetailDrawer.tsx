import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Drawer, Input, Table, Tag } from '@agentscope-ai/design';
import { Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';

import { analyticsApi } from '../../api/client';
import type { ContainerKey } from '../../constants/containers';
import { isCbbContainer } from '../../constants/containers';
import type { CorrelationLogItem, LLMCallRecord, LogLevel, RequestDetail, ToolCallCorrelationPayload } from '../../types';
import {
  buildDynamicColumns,
  buildSubQuestionResult,
  buildSummaryTextForSubQuestion,
  dateRender,
  extractSubQuestionRows,
  extractToolQueryRequest,
  extractTypedQueryRequestText,
  flattenResultRecords,
  formatLevelMap,
  getToolCallIdentity,
  getToolQueryRequestLabel,
  highlightText,
  isAskDatabaseTool,
  isEfficiencyPiTool,
  isWenshuTool,
  mergeToolCallRows,
  normalizeRowsForTable,
  resolveToolTraceContainer,
  statusClass,
  statusLabel,
  stringifyValue,
  summarizeInputFromUnknown,
  toArrayLoose,
  toNumber,
  toRecordLoose,
  toText,
  truncateText,
} from './utils';
import { PAGE_SIZE } from './types';
import type { GenericRecord, SubQuestionResultBlock, ToolCallRow } from './types';

interface ToolCallDetailDrawerProps {
  detail?: RequestDetail;
  toolCall?: ToolCallRow;
  requestId: string;
  activeContainer: ContainerKey;
  activeLevels: LogLevel[];
  onClose: () => void;
}

export const ToolCallDetailDrawer = ({
  detail,
  toolCall,
  requestId,
  activeContainer,
  activeLevels,
  onClose,
}: ToolCallDetailDrawerProps) => {
  const [toolTraceLoading, setToolTraceLoading] = useState(false);
  const [toolTraceError, setToolTraceError] = useState('');
  const [toolTracePayload, setToolTracePayload] = useState<ToolCallCorrelationPayload>();
  const [toolTracePage, setToolTracePage] = useState(1);
  const [selectedRawLogLine, setSelectedRawLogLine] = useState<string>();
  const [toolTreeKeyword, setToolTreeKeyword] = useState('');

  const resolvedToolCall = useMemo(() => {
    if (!toolCall) {
      return undefined;
    }
    const identity = getToolCallIdentity(toolCall);
    const sourceRows = (detail?.tool_calls || [])
      .filter((row) => getToolCallIdentity(row as ToolCallRow) === identity)
      .map((row) => row as ToolCallRow);
    if (sourceRows.length === 0) {
      return toolCall;
    }
    return mergeToolCallRows(sourceRows);
  }, [detail?.tool_calls, toolCall]);

  const loadToolTrace = async (toolCall: ToolCallRow, page = 1) => {
    const tool = String(toolCall.tool || '');
    if (!requestId || !tool) {
      return;
    }

    const container = resolveToolTraceContainer(tool, activeContainer);
    const toolId = toolCall.tool_id ? String(toolCall.tool_id) : undefined;
    const step = toNumber(toolCall.step);

    setToolTraceLoading(true);
    setToolTraceError('');
    setToolTracePage(page);
    try {
      const payload = await analyticsApi.getToolCallCorrelation(requestId, container, tool, {
        toolId,
        step,
        levels: activeLevels,
        page,
        pageSize: PAGE_SIZE,
      });
      setToolTracePayload(payload);
    } catch (error) {
      setToolTracePayload(undefined);
      setToolTraceError(String((error as Error)?.message || error));
    } finally {
      setToolTraceLoading(false);
    }
  };

  useEffect(() => {
    if (!resolvedToolCall || !requestId) {
      setToolTracePayload(undefined);
      setToolTraceError('');
      setToolTracePage(1);
      return;
    }
    setToolTreeKeyword('');
    loadToolTrace(resolvedToolCall, 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedToolCall, requestId, activeContainer, JSON.stringify(activeLevels)]);

  const closeToolCallDrawer = () => {
    setToolTracePayload(undefined);
    setToolTraceError('');
    setToolTraceLoading(false);
    setToolTracePage(1);
    onClose();
  };

  const summaryTags = useMemo(() => {
    if (!toolTracePayload) {
      return [] as string[];
    }
    const containerTagLabel = isCbbContainer(toolTracePayload.container) ? '工具容器' : '关联容器';
    return [
      `主流程容器: ${toolTracePayload.main_flow_container || '-'}`,
      `${containerTagLabel}: ${toolTracePayload.container || '-'}`,
      `ID来源: ${toolTracePayload.id_resolution?.resolved_by || '-'}`,
      `告警数: ${toolTracePayload.error_summary?.alert_count || 0}`,
      `级别分布: ${formatLevelMap(toolTracePayload.error_summary?.level_breakdown)}`,
    ];
  }, [toolTracePayload]);

  const resolvedToolQueryRequest = useMemo(
    () => (resolvedToolCall ? extractToolQueryRequest(resolvedToolCall.tool, resolvedToolCall.args, resolvedToolCall.output) : ''),
    [resolvedToolCall],
  );
  const resolvedToolQueryRequestLabel = useMemo(
    () => getToolQueryRequestLabel(resolvedToolCall?.tool),
    [resolvedToolCall?.tool],
  );
  const isStructuredSubQuestionTool = useMemo(
    () => isWenshuTool(resolvedToolCall?.tool) || isAskDatabaseTool(resolvedToolCall?.tool) || isEfficiencyPiTool(resolvedToolCall?.tool),
    [resolvedToolCall?.tool],
  );
  const subQuestionResultBlocks = useMemo<SubQuestionResultBlock[]>(() => {
    if (!resolvedToolCall) {
      return [];
    }

    const toolName = resolvedToolCall.tool;
    const argsRecord = toRecordLoose(resolvedToolCall.args);
    const fallbackQuestion = toText(argsRecord?.query, '');
    const outputRecord = toRecordLoose(resolvedToolCall.output);
    const dataSource = toRecordLoose(outputRecord?.data_source);
    const subQuestions = flattenResultRecords(dataSource?.data);

    if (subQuestions.length === 0) {
      const summary = toText(outputRecord?.error, '') || toText(outputRecord?.content, '') || '';
      return [{
        key: `${toText(resolvedToolCall.tool_id, '-')}-fallback`,
        question: fallbackQuestion || '未拆分子问题',
        status: toText(resolvedToolCall.status, 'unknown'),
        queryRequest: extractToolQueryRequest(toolName, resolvedToolCall.args, resolvedToolCall.output),
        summary,
        rows: [],
      }];
    }

    return subQuestions.map((item, index) => {
      const question = toText(item.query, '') || toText(item.question, '') || fallbackQuestion || `子问题 ${index + 1}`;
      const status = item.error
        ? 'failed'
        : toText(item.success, '') === 'false'
          ? 'failed'
          : toText(resolvedToolCall.status, 'unknown');
      const queryRequest = extractTypedQueryRequestText(toolName, item) || extractToolQueryRequest(toolName, resolvedToolCall.args, resolvedToolCall.output);
      const rows = extractSubQuestionRows(toolName, item);
      const summary = buildSummaryTextForSubQuestion(item);

      return {
        key: `${toText(resolvedToolCall.tool_id, '-')}-${index}`,
        question,
        status,
        queryRequest,
        summary,
        rows,
      };
    });
  }, [resolvedToolCall]);

  const toolSubQuestionTree = useMemo(() => {
    if (!resolvedToolCall) {
      return [] as DataNode[];
    }
    const toolId = toText(resolvedToolCall.tool_id, '-');
    const toolName = toText(resolvedToolCall.tool, '-');
    const argsRecord = toRecordLoose(resolvedToolCall.args);
    const fallbackQuestion = toText(argsRecord?.query, '');
    const outputRecord = toRecordLoose(resolvedToolCall.output);
    const dataSource = toRecordLoose(outputRecord?.data_source);
    const subQuestions = flattenResultRecords(dataSource?.data);
    const toolInput = summarizeInputFromUnknown(resolvedToolCall.args, 360);
    const toolQueryRequest = resolvedToolQueryRequest;

    const children: DataNode[] = [];
    if (subQuestions.length > 0) {
      subQuestions.forEach((item, index) => {
        const entry = item;
        const question = toText(entry.query, '') || toText(entry.question, '') || fallbackQuestion || `子问题 ${index + 1}`;
        const subInput = extractTypedQueryRequestText(resolvedToolCall.tool, entry) || question;
        const subRows = normalizeRowsForTable(extractSubQuestionRows(resolvedToolCall.tool, entry));
        const entryStatus = entry.error
          ? 'failed'
          : toText(entry.success, '') === 'false'
            ? 'failed'
            : statusClass(resolvedToolCall.status) === 'status-failed'
              ? 'failed'
              : 'success';
        const resultText = buildSubQuestionResult(entry);
        children.push({
          key: `subq-${toolId}-${index}`,
          title: (
            <div className="request-tree-node-block">
              <div className="request-tree-title-wrap">
                <span className="request-tree-sub-text">子问题 {index + 1}: {highlightText(question, toolTreeKeyword)}</span>
                <span className={`request-tree-status ${statusClass(entryStatus)}`}>{statusLabel(entryStatus)}</span>
              </div>
              <div className="request-tree-meta-text">{highlightText(`输入: ${subInput}`, toolTreeKeyword)}</div>
              <div className="request-tree-meta-text">{highlightText(`输出: ${resultText}`, toolTreeKeyword)}</div>
              {subRows.length > 0 ? (
                <div className="table-gap-top">
                  <Table
                    size="small"
                    scroll={{ x: 'max-content' }}
                    rowKey={(row: Record<string, unknown>) => String(row.__idx)}
                    pagination={false}
                    dataSource={subRows}
                    columns={buildDynamicColumns(subRows)}
                  />
                </div>
              ) : null}
            </div>
          ),
        });
      });
    } else {
      const outputError = toText(outputRecord?.error, '');
      const outputContent = toText(outputRecord?.content, '');
      const fallbackResultText = outputError
        ? `工具错误: ${truncateText(outputError.replace(/\s+/g, ' '), 220)}`
        : outputContent
          ? `工具返回: ${truncateText(outputContent.replace(/\s+/g, ' '), 220)}`
          : outputRecord
            ? '工具已返回结果，未发现可拆分子问题结构'
            : '仅记录调用参数，暂无返回结果';
      children.push({
        key: `subq-${toolId}-single`,
        title: (
          <div className="request-tree-node-block">
            <div className="request-tree-title-wrap">
              <span className="request-tree-sub-text">
                子问题 1: {highlightText(fallbackQuestion || '未拆分子问题', toolTreeKeyword)}
              </span>
              <span className={`request-tree-status ${statusClass(resolvedToolCall.status)}`}>
                {statusLabel(resolvedToolCall.status)}
              </span>
            </div>
            <div className="request-tree-meta-text">{highlightText(`输入: ${fallbackQuestion || '未记录输入'}`, toolTreeKeyword)}</div>
            <div className="request-tree-meta-text">{highlightText(`输出: ${fallbackResultText}`, toolTreeKeyword)}</div>
          </div>
        ),
      });
    }

    return [
      {
        key: `tool-root-${toolId}`,
        title: (
          <div className="request-tree-node-block">
            <div className="request-tree-title-wrap">
              <span className="request-tree-main-text">tool_id: {toolId}</span>
              <Tag>tool: {toolName}</Tag>
              <Tag>子问题数: {children.length}</Tag>
            </div>
            {toolInput ? <div className="request-tree-meta-text">输入: {highlightText(toolInput, toolTreeKeyword)}</div> : null}
            {toolQueryRequest ? (
              <div className="request-tree-meta-text">查询请求: {highlightText(toolQueryRequest, toolTreeKeyword)}</div>
            ) : null}
          </div>
        ),
        children,
      },
    ] as DataNode[];
  }, [resolvedToolCall, resolvedToolQueryRequest, toolTreeKeyword]);

  const renderLogTable = (title: string, rows: CorrelationLogItem[], total: number, onPageChange: (page: number) => void) => (
    <Card title={title} loading={toolTraceLoading}>
      <Table
        className="tool-trace-log-table"
        scroll={{ x: 'max-content' }}
        rowKey={(row: CorrelationLogItem) => `${String(row.ts_ns)}-${String(row.line)}`}
        dataSource={rows}
        pagination={{
          current: toolTracePage,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          onChange: onPageChange,
        }}
        columns={[
          {
            title: '日志时间(UTC+8)',
            dataIndex: 'ts_utc',
            key: 'ts_utc',
            width: 220,
            render: (value: string) => dateRender(value),
          },
          { title: '级别', dataIndex: 'level', key: 'level', width: 90 },
          {
            title: '日志行',
            dataIndex: 'line',
            key: 'line',
            render: (value: string) => (
              <div className="log-line-full">
                {value}
              </div>
            ),
          },
          { title: 'rid', dataIndex: 'rid', key: 'rid', width: 170 },
          { title: 'task_id', dataIndex: 'task_id', key: 'task_id', width: 190 },
          {
            title: '操作',
            key: 'action',
            width: 90,
            render: (_: unknown, row: CorrelationLogItem) => (
              <Button type="link" onClick={() => setSelectedRawLogLine(String(row.raw_line || row.line || ''))}>
                查看原文
              </Button>
            ),
          },
        ]}
        rowClassName={(row: CorrelationLogItem) => {
          if (row.level === 'ERROR') {
            return 'tool-trace-alert-row error-row';
          }
          if (row.level === 'WARNING') {
            return 'tool-trace-alert-row warning-row';
          }
          return '';
        }}
      />
    </Card>
  );

  return (
    <>
      <Drawer
        title="工具调用详情"
        open={Boolean(resolvedToolCall)}
        onClose={closeToolCallDrawer}
        width={1120}
      >
        {resolvedToolCall ? (
          <div className="detail-layout">
            <Card title="基础信息">
              <div className="summary-row">
                <Tag>tool: {String(resolvedToolCall.tool || '-')}</Tag>
                <Tag>tool_id: {String(resolvedToolCall.tool_id || '-')}</Tag>
                <Tag>step: {String(resolvedToolCall.step ?? '-')}</Tag>
                <Tag>status: {String(resolvedToolCall.status || 'unknown')}</Tag>
                <Tag>duration_s: {String(resolvedToolCall.duration_s ?? '-')}</Tag>
                <Tag>agent_code: {String(resolvedToolCall.agent_code || '-')}</Tag>
                <Tag>agent_id: {String(resolvedToolCall.agent_id || '-')}</Tag>
              </div>
              <div className="table-gap-top">{dateRender(resolvedToolCall.ts)}</div>
            </Card>

            <Card title="调用参数（args）">
              <pre className="raw-json tool-call-json">{stringifyValue(resolvedToolCall.args)}</pre>
            </Card>

            <Card title="调用结果（output）">
              <pre className="raw-json tool-call-json">{stringifyValue(resolvedToolCall.output)}</pre>
            </Card>

            {resolvedToolQueryRequest ? (
              <Card title={resolvedToolQueryRequestLabel}>
                <pre className="raw-json tool-call-json">{resolvedToolQueryRequest}</pre>
              </Card>
            ) : null}

            <Card
              title="子问题树（tool_id 维度）"
              extra={
                <Input
                  allowClear
                  value={toolTreeKeyword}
                  onChange={(event) => setToolTreeKeyword(event.target.value)}
                  placeholder="输入关键字，高亮问题和结果"
                  className="tool-subquestion-search"
                />
              }
            >
              {toolSubQuestionTree.length > 0 ? (
                <div className="request-call-tree-wrap">
                  <Tree
                    className="request-call-tree tool-subquestion-tree"
                    showLine={{ showLeafIcon: false }}
                    selectable={false}
                    defaultExpandAll
                    treeData={toolSubQuestionTree}
                  />
                </div>
              ) : (
                <Alert type="info" showIcon message="暂无可展示的子问题拆分结果" />
              )}
            </Card>

            {isStructuredSubQuestionTool ? (
              <Card title="子问题数据结果（表格）">
                {subQuestionResultBlocks.length === 0 ? (
                  <Alert type="info" showIcon message="暂无可展示的子问题数据结果" />
                ) : (
                  <div className="detail-layout">
                    {subQuestionResultBlocks.map((block, index) => {
                      const normalizedRows = normalizeRowsForTable(block.rows);
                      const columns = buildDynamicColumns(normalizedRows);
                      return (
                        <Card key={block.key} title={`子问题 ${index + 1}`} className="table-gap-top">
                          <div className="request-tree-meta-text">问题: {block.question}</div>
                          <div className="request-tree-meta-text">状态: {statusLabel(block.status)}</div>
                          {block.queryRequest ? (
                            <>
                              <div className="request-tree-meta-text">{resolvedToolQueryRequestLabel}:</div>
                              <pre className="raw-json tool-call-json">{block.queryRequest}</pre>
                            </>
                          ) : null}
                          {block.summary ? (
                            <div className="request-tree-meta-text">总结: {block.summary}</div>
                          ) : (
                            <div className="request-tree-meta-text">总结: -</div>
                          )}
                          {normalizedRows.length > 0 ? (
                            <Table
                              className="table-gap-top"
                              scroll={{ x: 'max-content' }}
                              rowKey={(row: Record<string, unknown>) => String(row.__idx)}
                              pagination={false}
                              dataSource={normalizedRows}
                              columns={columns}
                            />
                          ) : (
                            <Alert className="table-gap-top" type="info" showIcon message="该子问题未返回结构化表格数据" />
                          )}
                        </Card>
                      );
                    })}
                  </div>
                )}
              </Card>
            ) : null}

            <Card
              title="错误摘要与关联日志（主流程 + 工具容器）"
              loading={toolTraceLoading}
              extra={
                <Button
                  type="primary"
                  className="action-btn-primary"
                  onClick={() => resolvedToolCall && loadToolTrace(resolvedToolCall, toolTracePage)}
                >
                  刷新日志
                </Button>
              }
            >
              {toolTraceError ? <Alert type="error" showIcon message={toolTraceError} className="table-gap-bottom" /> : null}
              <div className="summary-row">
                {summaryTags.map((item) => (
                  <Tag key={item}>{item}</Tag>
                ))}
              </div>
              {!toolTracePayload && !toolTraceLoading && !toolTraceError ? (
                <Alert className="table-gap-top" type="info" showIcon message="暂无可展示的关联日志" />
              ) : null}
            </Card>

            {toolTracePayload
              ? renderLogTable(
                  `主流程日志（${toolTracePayload.main_flow_container}）`,
                  toolTracePayload.main_flow_logs_page.items || [],
                  toolTracePayload.main_flow_logs_page.total || 0,
                  (page) => resolvedToolCall && loadToolTrace(resolvedToolCall, page),
                )
              : null}

            {toolTracePayload
              ? renderLogTable(
                  `${isCbbContainer(toolTracePayload.container) ? '工具容器日志' : '关联容器日志'}（${toolTracePayload.container}）`,
                  toolTracePayload.cbb_logs_page.items || [],
                  toolTracePayload.cbb_logs_page.total || 0,
                  (page) => resolvedToolCall && loadToolTrace(resolvedToolCall, page),
                )
              : null}
          </div>
        ) : null}
      </Drawer>

      <Drawer title="原始日志行" open={Boolean(selectedRawLogLine)} onClose={() => setSelectedRawLogLine(undefined)} width={900}>
        <pre className="raw-json raw-log-line">{selectedRawLogLine || ''}</pre>
      </Drawer>
    </>
  );
};
