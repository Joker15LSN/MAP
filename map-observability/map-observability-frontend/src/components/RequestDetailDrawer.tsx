import { ReactNode, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Drawer, Input, Table, Tag } from '@agentscope-ai/design';
import { Collapse, Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';

import { analyticsApi } from '../api/client';
import { CorrelationLogItem, LLMCallRecord, LogLevel, RequestDetail, ToolCallCorrelationPayload } from '../types';
import { formatIsoTimePair } from '../utils/time';
import { inferCbbContainerByTool, isCbbContainer } from '../constants/containers';
import type { ContainerKey } from '../constants/containers';
import { RequestCallTree } from './RequestCallTree';

interface RequestDetailDrawerProps {
  open: boolean;
  loading: boolean;
  detail?: RequestDetail;
  errorMessage?: string;
  activeContainer: ContainerKey;
  activeLevels: LogLevel[];
  onClose: () => void;
}

type ToolCallRow = Record<string, unknown>;
type GenericRecord = Record<string, unknown>;
interface SubQuestionResultBlock {
  key: string;
  question: string;
  status: string;
  queryRequest: string;
  summary: string;
  rows: GenericRecord[];
}
const PAGE_SIZE = 10;
const DETAIL_PANEL_KEYS = ['timeline', 'llm', 'tools', 'scene'] as const;

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

const stringifyValue = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '-';
  }
  if (typeof value === 'string') {
    return value || '-';
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const formatLevelMap = (value?: Record<string, number>) => {
  if (!value || Object.keys(value).length === 0) {
    return '-';
  }
  return Object.entries(value)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}:${v}`)
    .join(' | ');
};

const toNumber = (value: unknown): number | undefined => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return undefined;
  }
  return numeric;
};

const toRecord = (value: unknown): GenericRecord | undefined => {
  if (typeof value === 'object' && value !== null) {
    return value as GenericRecord;
  }
  return undefined;
};

const toArray = (value: unknown): unknown[] => {
  if (Array.isArray(value)) {
    return value;
  }
  return [];
};

const toText = (value: unknown, fallback = ''): string => {
  if (typeof value === 'string') {
    return value.trim() || fallback;
  }
  if (value === undefined || value === null) {
    return fallback;
  }
  return String(value);
};

const truncateText = (value: string, max = 220): string => {
  if (value.length <= max) {
    return value;
  }
  return `${value.slice(0, max)}...`;
};

const hasOwn = (item: GenericRecord, key: string): boolean => Object.prototype.hasOwnProperty.call(item, key);

const compactValue = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const compactDisplayValue = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  return compactValue(value);
};

const appendNonEmpty = (target: GenericRecord, key: string, value: unknown) => {
  const displayValue = compactDisplayValue(value);
  if (displayValue) {
    target[key] = displayValue;
  }
};

const flattenResultRecords = (value: unknown): GenericRecord[] => {
  const results: GenericRecord[] = [];
  const visit = (entry: unknown) => {
    const parsed = parseJsonIfPossible(entry);
    if (Array.isArray(parsed)) {
      parsed.forEach(visit);
      return;
    }
    const record = toRecord(parsed);
    if (record) {
      results.push(record);
    }
  };
  toArrayLoose(value).forEach(visit);
  return results;
};

const flattenDataRows = (value: unknown): unknown[] => {
  const rows: unknown[] = [];
  const visit = (entry: unknown) => {
    const parsed = parseJsonIfPossible(entry);
    if (Array.isArray(parsed)) {
      parsed.forEach(visit);
      return;
    }
    if (parsed !== undefined && parsed !== null) {
      rows.push(parsed);
    }
  };
  toArrayLoose(value).forEach(visit);
  return rows;
};

const formatAsPrettyJson = (value: unknown): string => {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const normalizeToolName = (tool: unknown): string => toText(tool, '').toLowerCase();
const isWenshuTool = (tool: unknown): boolean => normalizeToolName(tool).includes('wenshu');
const isAskDatabaseTool = (tool: unknown): boolean => normalizeToolName(tool).includes('ask_database');
const isEfficiencyPiTool = (tool: unknown): boolean => normalizeToolName(tool).includes('efficiency_pi');

const findValueByKeysDeep = (value: unknown, keys: string[], depth = 0): unknown => {
  if (depth > 6 || value === undefined || value === null) {
    return undefined;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const hit = findValueByKeysDeep(item, keys, depth + 1);
      if (hit !== undefined && hit !== null) {
        return hit;
      }
    }
    return undefined;
  }
  const record = toRecordLoose(value);
  if (!record) {
    return undefined;
  }
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) {
      return record[key];
    }
  }
  for (const nested of Object.values(record)) {
    const hit = findValueByKeysDeep(nested, keys, depth + 1);
    if (hit !== undefined && hit !== null) {
      return hit;
    }
  }
  return undefined;
};

const extractTypedQueryRequestText = (tool: unknown, value: unknown): string => {
  if (value === undefined || value === null) {
    return '';
  }
  if (isWenshuTool(tool)) {
    const payload = findValueByKeysDeep(value, ['metric_ql', 'metricQl', 'payload', 'request_payload', 'requestPayload']);
    if (payload !== undefined && payload !== null) {
      return formatAsPrettyJson(payload);
    }
    if (typeof value === 'string') {
      return value;
    }
    return formatAsPrettyJson(value);
  }
  if (isAskDatabaseTool(tool)) {
    const sqlValue = findValueByKeysDeep(value, ['sql', 'query_sql', 'generated_sql', 'final_sql', 'executed_sql', 'statement']);
    if (typeof sqlValue === 'string') {
      return sqlValue;
    }
    return sqlValue ? formatAsPrettyJson(sqlValue) : '';
  }
  if (isEfficiencyPiTool(tool)) {
    const ngqlValue = findValueByKeysDeep(value, [
      'ngql_list',
      'ngql',
      'query_ngql',
      'generated_ngql',
      'final_ngql',
      'executed_ngql',
      'statement',
    ]);
    if (typeof ngqlValue === 'string') {
      return ngqlValue;
    }
    return ngqlValue ? formatAsPrettyJson(ngqlValue) : '';
  }
  return '';
};

const extractToolQueryRequest = (tool: unknown, args: unknown, output: unknown): string => {
  const fromArgs = extractTypedQueryRequestText(tool, args);
  if (fromArgs) {
    return fromArgs;
  }
  const outputRecord = toRecordLoose(output);
  const dataSource = toRecordLoose(outputRecord?.data_source);
  const sourceItems = flattenResultRecords(dataSource?.data);
  for (const item of sourceItems) {
    const fromItem = extractTypedQueryRequestText(tool, item);
    if (fromItem) {
      return fromItem;
    }
  }
  return '';
};

const getToolQueryRequestLabel = (tool: unknown): string => {
  if (isWenshuTool(tool)) {
    return '查询请求（payload）';
  }
  if (isAskDatabaseTool(tool)) {
    return '查询请求（SQL）';
  }
  if (isEfficiencyPiTool(tool)) {
    return '查询请求（nGQL）';
  }
  return '查询请求';
};

const summarizeInputFromRecord = (record: GenericRecord | undefined, max = 280): string => {
  if (!record) {
    return '';
  }
  const preferredKeys = ['query', 'question', 'user_query', 'content', 'subquestion', 'current_subquestion', 'prompt'];
  for (const key of preferredKeys) {
    const raw = toText(record[key], '');
    if (raw) {
      return truncateText(raw.replace(/\s+/g, ' '), max);
    }
  }
  const compact = compactValue(record).replace(/\s+/g, ' ').trim();
  return compact ? truncateText(compact, max) : '';
};

const summarizeInputFromUnknown = (value: unknown, max = 280): string => {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return truncateText(value.replace(/\s+/g, ' '), max);
  }
  return summarizeInputFromRecord(toRecordLoose(value), max);
};

const parseJsonIfPossible = (value: unknown): unknown => {
  if (typeof value !== 'string') {
    return value;
  }
  const raw = value.trim();
  if (!raw || (!raw.startsWith('{') && !raw.startsWith('['))) {
    return value;
  }
  try {
    return JSON.parse(raw);
  } catch {
    return value;
  }
};

const toRecordLoose = (value: unknown): GenericRecord | undefined => {
  return toRecord(parseJsonIfPossible(value));
};

const toArrayLoose = (value: unknown): unknown[] => {
  const parsed = parseJsonIfPossible(value);
  return Array.isArray(parsed) ? parsed : [];
};

const extractNestedToolCallRows = (value: unknown): GenericRecord[] => {
  const record = toRecordLoose(value);
  const toolCallResults = toArrayLoose(record?.tool_call_results);
  if (toolCallResults.length === 0) {
    return [];
  }

  return toolCallResults.map((item, index) => {
    const callRecord = toRecordLoose(item) || {};
    const argsRecord = toRecordLoose(callRecord.arguments) || toRecordLoose(callRecord.args) || {};
    const row: GenericRecord = { internal_step: index + 1 };

    appendNonEmpty(row, 'tool_name', callRecord.tool_name || callRecord.tool || callRecord.name);
    appendNonEmpty(row, 'question', argsRecord.question || callRecord.question);
    appendNonEmpty(row, 'staff_code', argsRecord.staff_code);
    appendNonEmpty(row, 'request_id', argsRecord.request_id || callRecord.request_id);
    appendNonEmpty(row, 'task_id', argsRecord.task_id || callRecord.task_id);

    appendNonEmpty(
      row,
      'ngql',
      callRecord.ngql_list || findValueByKeysDeep(callRecord, [
        'ngql',
        'query_ngql',
        'generated_ngql',
        'final_ngql',
        'executed_ngql',
        'statement',
      ]),
    );
    appendNonEmpty(
      row,
      'sql',
      findValueByKeysDeep(callRecord, ['sql', 'query_sql', 'generated_sql', 'final_sql', 'executed_sql']),
    );
    appendNonEmpty(
      row,
      'payload',
      findValueByKeysDeep(callRecord, ['metric_ql', 'metricQl', 'payload', 'request_payload', 'requestPayload']),
    );
    appendNonEmpty(row, 'data', callRecord.data ?? callRecord.result ?? callRecord.output ?? callRecord.content);
    appendNonEmpty(row, 'error', callRecord.error);
    appendNonEmpty(row, 'execution_time_ms', callRecord.execution_time_ms);
    appendNonEmpty(row, 'standard_names', argsRecord.standard_names);

    return row;
  });
};

const flattenResultRows = (value: unknown): GenericRecord[] => {
  const rows: GenericRecord[] = [];
  const visit = (entry: unknown) => {
    const parsed = parseJsonIfPossible(entry);
    if (Array.isArray(parsed)) {
      parsed.forEach(visit);
      return;
    }
    const record = toRecord(parsed);
    if (record) {
      const nestedToolRows = extractNestedToolCallRows(record);
      if (nestedToolRows.length > 0) {
        rows.push(...nestedToolRows);
        return;
      }
      rows.push(record);
    }
  };
  visit(value);
  return rows;
};

const extractSubQuestionRows = (tool: unknown, subQuestion: GenericRecord): GenericRecord[] => {
  const rows: GenericRecord[] = [];
  rows.push(...flattenResultRows(subQuestion.data));

  const dataRecord = toRecordLoose(subQuestion.data);
  const toolCallResults = toArrayLoose(dataRecord?.tool_call_results);
  for (const item of toolCallResults) {
    const itemRecord = toRecordLoose(item);
    rows.push(...flattenResultRows(itemRecord?.data));
  }

  if (rows.length > 0) {
    return rows;
  }

  rows.push(...flattenResultRows(parseJsonIfPossible(subQuestion.result)));
  if (rows.length > 0) {
    return rows;
  }

  rows.push(...flattenResultRows(parseJsonIfPossible(subQuestion.content)));
  if (rows.length > 0) {
    return rows;
  }

  if (isWenshuTool(tool) || isAskDatabaseTool(tool) || isEfficiencyPiTool(tool)) {
    return [];
  }
  return rows;
};

const buildSummaryTextForSubQuestion = (item: GenericRecord): string => {
  const summary = toText(item.summary, '');
  if (summary) {
    return summary;
  }
  const resultText = toText(item.result_text, '');
  if (resultText) {
    return resultText;
  }
  const content = toText(item.content, '');
  if (content) {
    return content;
  }
  const result = toText(item.result, '');
  if (result) {
    return result;
  }
  const fallback = buildSubQuestionResult(item);
  return fallback === '已执行，暂无可读结果' ? '' : fallback;
};

const normalizeRowsForTable = (rows: GenericRecord[]): GenericRecord[] =>
  rows.map((row, index) => {
    const normalized: GenericRecord = { __idx: index + 1 };
    for (const [key, value] of Object.entries(row)) {
      if (value === undefined || value === null) {
        normalized[key] = '';
      } else if (typeof value === 'object') {
        normalized[key] = stringifyValue(value);
      } else {
        normalized[key] = value;
      }
    }
    return normalized;
  });

const buildDynamicColumns = (rows: GenericRecord[]) => {
  const keys: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (key === '__idx') {
        continue;
      }
      if (!keys.includes(key)) {
        keys.push(key);
      }
    }
  }
  return [
    {
      title: '#',
      dataIndex: '__idx',
      key: '__idx',
      width: 60,
    },
    ...keys.map((key) => ({
      title: key,
      dataIndex: key,
      key,
      render: (value: unknown) => {
        const text = stringifyValue(value);
        return (
          <span className="query-cell-wrap" title={text}>
            {truncateText(text, 260)}
          </span>
        );
      },
    })),
  ];
};

const getToolCallIdentity = (row: ToolCallRow): string => {
  const agentCode = toText(row.agent_code, 'unknown_agent');
  const tool = toText(row.tool, 'unknown_tool');
  const toolId = toText(row.tool_id, 'unknown_id');
  const step = Number.isFinite(Number(row.step)) ? String(Math.trunc(Number(row.step))) : '-1';
  return `${agentCode}|${tool}|${toolId}|${step}`;
};

const mergeToolCallRows = (rows: ToolCallRow[]): ToolCallRow => {
  if (rows.length === 0) {
    return {};
  }
  const merged: ToolCallRow = { ...rows[0] };
  let minTs = toText(merged.ts, '');
  let maxTs = toText(merged.ts, '');

  for (const row of rows) {
    const status = toText(row.status, '');
    if (status) {
      merged.status = status;
    }

    if ((merged.args === undefined || merged.args === null) && row.args !== undefined && row.args !== null) {
      merged.args = row.args;
    }
    if ((merged.output === undefined || merged.output === null) && row.output !== undefined && row.output !== null) {
      merged.output = row.output;
    }

    const duration = toNumber(row.duration_s);
    if (duration !== undefined) {
      merged.duration_s = duration;
    }

    const ts = toText(row.ts, '');
    if (ts) {
      if (!minTs || ts < minTs) {
        minTs = ts;
      }
      if (!maxTs || ts > maxTs) {
        maxTs = ts;
      }
    }
  }

  if (minTs) {
    merged.ts = minTs;
  }
  if (maxTs) {
    merged.end_ts = maxTs;
  }
  return merged;
};

const statusLabel = (status: unknown): string => {
  const normalized = toText(status, 'unknown').toLowerCase();
  if (normalized === 'success') {
    return '成功';
  }
  if (normalized === 'failed' || normalized === 'error') {
    return '失败';
  }
  return '未知';
};

const statusClass = (status: unknown): string => {
  const normalized = toText(status, 'unknown').toLowerCase();
  if (normalized === 'success') {
    return 'status-success';
  }
  if (normalized === 'failed' || normalized === 'error') {
    return 'status-failed';
  }
  return 'status-unknown';
};

const buildSubQuestionResult = (item: GenericRecord): string => {
  const error = toText(item.error, '');
  if (error) {
    return `错误: ${truncateText(error, 180)}`;
  }

  const webItems = toArrayLoose(item.items);
  if (webItems.length > 0) {
    const firstItem = toRecordLoose(webItems[0]);
    const firstTitle = toText(firstItem?.name, '') || toText(firstItem?.title, '');
    const firstSummary = toText(firstItem?.summary, '');
    const snippet = firstTitle || firstSummary || '无可展示摘要';
    return `检索命中 ${webItems.length} 条，首条: ${truncateText(snippet.replace(/\s+/g, ' '), 220)}`;
  }

  const chunkContent = toText(item.chunk_content, '');
  if (chunkContent) {
    const docTitle = toText(item.title, '未命名文档');
    return `知识库命中: ${truncateText(docTitle, 60)}，片段: ${truncateText(chunkContent.replace(/\s+/g, ' '), 220)}`;
  }

  const resultTextFromSearch = toText(item.result_text, '');
  if (resultTextFromSearch) {
    return `检索摘要: ${truncateText(resultTextFromSearch.replace(/\s+/g, ' '), 220)}`;
  }

  const directDataRows = flattenDataRows(item.data);
  if (hasOwn(item, 'data') && Array.isArray(item.data)) {
    if (directDataRows.length === 0) {
      return '返回空结果';
    }
    const sample = truncateText(compactValue(directDataRows[0]).replace(/\s+/g, ' '), 220);
    return sample ? `返回 ${directDataRows.length} 条，示例: ${sample}` : `返回 ${directDataRows.length} 条`;
  }

  const dataRecord = toRecordLoose(item.data);
  const toolCallResults = toArrayLoose(dataRecord?.tool_call_results);
  if (toolCallResults.length > 0) {
    let returnedRows = 0;
    let firstSample = '';
    let firstToolError = '';
    for (const row of toolCallResults) {
      const resultRecord = toRecordLoose(row);
      const dataItems = toArrayLoose(resultRecord?.data);
      const toolError = toText(resultRecord?.error, '');
      if (!firstToolError && toolError) {
        firstToolError = toolError;
      }
      returnedRows += dataItems.reduce<number>((sum: number, value: unknown) => {
        if (Array.isArray(value)) {
          if (!firstSample && value.length > 0) {
            firstSample = truncateText(stringifyValue(value[0]).replace(/\s+/g, ' '), 180);
          }
          return sum + value.length;
        }
        return sum;
      }, 0);
    }
    if (firstToolError) {
      return `工具错误: ${truncateText(firstToolError, 180)}`;
    }
    if (returnedRows > 0) {
      return firstSample ? `返回 ${returnedRows} 条，示例: ${firstSample}` : `返回 ${returnedRows} 条`;
    }
    return '返回空结果';
  }

  const summary = toText(item.summary, '');
  if (summary) {
    return truncateText(summary.replace(/\s+/g, ' '), 220);
  }

  const result = toText(item.result, '');
  if (result === '[[]]') {
    return '返回空结果';
  }
  if (result) {
    const normalizedResult = result.replace(/\s+/g, ' ');
    if (normalizedResult.startsWith('[{') || normalizedResult.startsWith('[[') || normalizedResult.startsWith('{')) {
      return `结果片段: ${truncateText(normalizedResult, 180)}`;
    }
    return `结果: ${truncateText(normalizedResult, 180)}`;
  }

  return '已执行，暂无可读结果';
};

const escapeRegExp = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const highlightText = (text: string, keyword: string): ReactNode => {
  const trimmedKeyword = keyword.trim();
  if (!trimmedKeyword) {
    return text;
  }
  const pattern = new RegExp(`(${escapeRegExp(trimmedKeyword)})`, 'ig');
  const parts = text.split(pattern);
  return parts.map((part, index) => {
    if (part.toLowerCase() === trimmedKeyword.toLowerCase()) {
      return (
        <mark key={`${part}-${index}`} className="request-tree-highlight">
          {part}
        </mark>
      );
    }
    return <span key={`${part}-${index}`}>{part}</span>;
  });
};

const resolveToolTraceContainer = (tool: string, activeContainer: ContainerKey): ContainerKey => {
  if (isCbbContainer(activeContainer)) {
    return activeContainer;
  }
  return inferCbbContainerByTool(tool, activeContainer) || activeContainer;
};

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
  const [toolTraceLoading, setToolTraceLoading] = useState(false);
  const [toolTraceError, setToolTraceError] = useState('');
  const [toolTracePayload, setToolTracePayload] = useState<ToolCallCorrelationPayload>();
  const [toolTracePage, setToolTracePage] = useState(1);
  const [selectedRawLogLine, setSelectedRawLogLine] = useState<string>();
  const [expandedDetailKeys, setExpandedDetailKeys] = useState<string[]>([]);
  const [toolTreeKeyword, setToolTreeKeyword] = useState('');

  const requestId = String(detail?.request?.request_id || '');
  const llmCallRows = useMemo<LLMCallRecord[]>(() => detail?.llm_calls || [], [detail?.llm_calls]);
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

  const resolvedToolCall = useMemo(() => {
    if (!selectedToolCall) {
      return undefined;
    }
    const identity = getToolCallIdentity(selectedToolCall);
    const sourceRows = (detail?.tool_calls || [])
      .filter((row) => getToolCallIdentity(row as ToolCallRow) === identity)
      .map((row) => row as ToolCallRow);
    if (sourceRows.length === 0) {
      return selectedToolCall;
    }
    return mergeToolCallRows(sourceRows);
  }, [detail?.tool_calls, selectedToolCall]);

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
    if (!open) {
      setSelectedToolCall(undefined);
      setToolTracePayload(undefined);
      setToolTraceError('');
      setToolTraceLoading(false);
      setToolTracePage(1);
      setSelectedRawLogLine(undefined);
      setExpandedDetailKeys([]);
      setToolTreeKeyword('');
    }
  }, [open]);

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
    setSelectedToolCall(undefined);
    setToolTracePayload(undefined);
    setToolTraceError('');
    setToolTraceLoading(false);
    setToolTracePage(1);
  };

  const closeDetailDrawer = () => {
    closeToolCallDrawer();
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

  const allDetailExpanded = expandedDetailKeys.length === DETAIL_PANEL_KEYS.length;
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
      <Drawer title="请求详情" open={open} onClose={closeDetailDrawer} width={1000}>
        {errorMessage ? <Alert type="error" showIcon message={errorMessage} className="table-gap-bottom" /> : null}
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
                    key: 'llm',
                    label: `LLM 调用轨迹（${llmCallRows.length}）`,
                    children: (
                      <Table
                        className="llm-trace-table"
                        scroll={{ x: 'max-content' }}
                        rowKey={(row: LLMCallRecord) => `${row.state_id || requestId}-${row.seq ?? '-'}-${row.start_ts || row.ts || ''}`}
                        pagination={false}
                        dataSource={llmCallRows}
                        columns={[
                          { title: '#', dataIndex: 'seq', key: 'seq', width: 70 },
                          { title: 'agent', dataIndex: 'agent_code', key: 'agent_code', width: 150 },
                          { title: 'component', dataIndex: 'component', key: 'component', width: 150 },
                          { title: 'phase', dataIndex: 'phase', key: 'phase', width: 170 },
                          { title: 'step', dataIndex: 'step', key: 'step', width: 160 },
                          { title: 'model', dataIndex: 'model', key: 'model', width: 180 },
                          {
                            title: '状态',
                            dataIndex: 'status',
                            key: 'status',
                            width: 110,
                            render: (value: unknown) => <Tag>{String(value || 'unknown')}</Tag>,
                          },
                          {
                            title: '耗时(s)',
                            dataIndex: 'duration_s',
                            key: 'duration_s',
                            width: 110,
                            render: (value: unknown) => Number(value || 0).toFixed(2),
                          },
                          {
                            title: 'Token',
                            dataIndex: 'usage',
                            key: 'usage',
                            width: 160,
                            render: (value: unknown) => {
                              const usage = (value || {}) as Record<string, unknown>;
                              return String(usage.total_tokens ?? usage.total ?? usage.completion_tokens ?? '-');
                            },
                          },
                          { title: '开始(UTC+8)', dataIndex: 'start_ts', key: 'start_ts', width: 220, render: dateRender },
                          {
                            title: '提示摘要',
                            dataIndex: 'prompt_summary',
                            key: 'prompt_summary',
                            width: 360,
                            render: (value: unknown) => <div className="log-line-full">{String(value || '-')}</div>,
                          },
                          {
                            title: '错误',
                            dataIndex: 'error',
                            key: 'error',
                            width: 280,
                            render: (value: unknown) => <div className="log-line-full">{String(value || '-')}</div>,
                          },
                        ]}
                      />
                    ),
                  },
                  {
                    key: 'tools',
                    label: `工具调用（${mergedToolCallRows.length}）`,
                    children: (
                      <Table
                        scroll={{ x: 'max-content' }}
                        rowKey={(row: Record<string, unknown>) => getToolCallIdentity(row as ToolCallRow)}
                        pagination={false}
                        dataSource={mergedToolCallRows}
                        columns={[
                          {
                            title: 'tool',
                            dataIndex: 'tool',
                            key: 'tool',
                            render: (value: unknown, row: ToolCallRow) => (
                              <Button type="link" onClick={() => setSelectedToolCall(row)}>
                                {String(value || '-')}
                              </Button>
                            ),
                          },
                          {
                            title: 'tool_id',
                            dataIndex: 'tool_id',
                            key: 'tool_id',
                            render: (value: unknown, row: ToolCallRow) => (
                              <Button type="link" onClick={() => setSelectedToolCall(row)}>
                                {String(value || '-')}
                              </Button>
                            ),
                          },
                          { title: 'agent_code', dataIndex: 'agent_code', key: 'agent_code' },
                          { title: '步骤', dataIndex: 'step', key: 'step' },
                          { title: '状态', dataIndex: 'status', key: 'status', render: (value: unknown) => value || 'unknown' },
                          { title: '时间(UTC+8)', dataIndex: 'ts', key: 'ts', render: dateRender },
                          {
                            title: '操作',
                            key: 'action',
                            render: (_: unknown, row: ToolCallRow) => (
                              <Button type="link" onClick={() => setSelectedToolCall(row)}>
                                查看详情
                              </Button>
                            ),
                          },
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
          </div>
        ) : !loading ? (
          <Alert type="warning" showIcon message="未获取到请求详情数据" />
        ) : null}
      </Drawer>

      <Drawer
        title="工具调用详情"
        open={Boolean(selectedToolCall)}
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
