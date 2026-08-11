import { ReactNode } from 'react';

import { formatIsoTimePair } from '../../utils/time';
import { inferCbbContainerByTool, isCbbContainer } from '../../constants/containers';
import type { ContainerKey } from '../../constants/containers';
import type { CorrelationLogItem, ToolCallCorrelationPayload } from '../../types';
import { PAGE_SIZE } from './types';
import type { GenericRecord, ToolCallRow } from './types';

export const dateRender = (value: unknown) => {
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

export const stringifyValue = (value: unknown): string => {
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

export const formatLevelMap = (value?: Record<string, number>) => {
  if (!value || Object.keys(value).length === 0) {
    return '-';
  }
  return Object.entries(value)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}:${v}`)
    .join(' | ');
};

export const toNumber = (value: unknown): number | undefined => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return undefined;
  }
  return numeric;
};

export const toRecord = (value: unknown): GenericRecord | undefined => {
  if (typeof value === 'object' && value !== null) {
    return value as GenericRecord;
  }
  return undefined;
};

export const toArray = (value: unknown): unknown[] => {
  if (Array.isArray(value)) {
    return value;
  }
  return [];
};

export const toText = (value: unknown, fallback = ''): string => {
  if (typeof value === 'string') {
    return value.trim() || fallback;
  }
  if (value === undefined || value === null) {
    return fallback;
  }
  return String(value);
};

export const truncateText = (value: string, max = 220): string => {
  if (value.length <= max) {
    return value;
  }
  return `${value.slice(0, max)}...`;
};

export const hasOwn = (item: GenericRecord, key: string): boolean => Object.prototype.hasOwnProperty.call(item, key);

export const compactValue = (value: unknown): string => {
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

export const compactDisplayValue = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  return compactValue(value);
};

export const appendNonEmpty = (target: GenericRecord, key: string, value: unknown) => {
  const displayValue = compactDisplayValue(value);
  if (displayValue) {
    target[key] = displayValue;
  }
};

export const flattenResultRecords = (value: unknown): GenericRecord[] => {
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

export const flattenDataRows = (value: unknown): unknown[] => {
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

export const formatAsPrettyJson = (value: unknown): string => {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

export const normalizeToolName = (tool: unknown): string => toText(tool, '').toLowerCase();
export const isWenshuTool = (tool: unknown): boolean => normalizeToolName(tool).includes('wenshu');
export const isAskDatabaseTool = (tool: unknown): boolean => normalizeToolName(tool).includes('ask_database');
export const isEfficiencyPiTool = (tool: unknown): boolean => normalizeToolName(tool).includes('efficiency_pi');

export const findValueByKeysDeep = (value: unknown, keys: string[], depth = 0): unknown => {
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

export const extractTypedQueryRequestText = (tool: unknown, value: unknown): string => {
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

export const extractToolQueryRequest = (tool: unknown, args: unknown, output: unknown): string => {
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

export const getToolQueryRequestLabel = (tool: unknown): string => {
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

export const summarizeInputFromRecord = (record: GenericRecord | undefined, max = 280): string => {
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

export const summarizeInputFromUnknown = (value: unknown, max = 280): string => {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return truncateText(value.replace(/\s+/g, ' '), max);
  }
  return summarizeInputFromRecord(toRecordLoose(value), max);
};

export const parseJsonIfPossible = (value: unknown): unknown => {
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

export const toRecordLoose = (value: unknown): GenericRecord | undefined => {
  return toRecord(parseJsonIfPossible(value));
};

export const toArrayLoose = (value: unknown): unknown[] => {
  const parsed = parseJsonIfPossible(value);
  return Array.isArray(parsed) ? parsed : [];
};

export const extractNestedToolCallRows = (value: unknown): GenericRecord[] => {
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

export const flattenResultRows = (value: unknown): GenericRecord[] => {
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

export const extractSubQuestionRows = (tool: unknown, subQuestion: GenericRecord): GenericRecord[] => {
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

export const buildSummaryTextForSubQuestion = (item: GenericRecord): string => {
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

export const normalizeRowsForTable = (rows: GenericRecord[]): GenericRecord[] =>
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

export const buildDynamicColumns = (rows: GenericRecord[]) => {
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

export const getToolCallIdentity = (row: ToolCallRow): string => {
  const agentCode = toText(row.agent_code, 'unknown_agent');
  const tool = toText(row.tool, 'unknown_tool');
  const toolId = toText(row.tool_id, 'unknown_id');
  const step = Number.isFinite(Number(row.step)) ? String(Math.trunc(Number(row.step))) : '-1';
  return `${agentCode}|${tool}|${toolId}|${step}`;
};

export const mergeToolCallRows = (rows: ToolCallRow[]): ToolCallRow => {
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

export const statusLabel = (status: unknown): string => {
  const normalized = toText(status, 'unknown').toLowerCase();
  if (normalized === 'success') {
    return '成功';
  }
  if (normalized === 'failed' || normalized === 'error') {
    return '失败';
  }
  return '未知';
};

export const statusClass = (status: unknown): string => {
  const normalized = toText(status, 'unknown').toLowerCase();
  if (normalized === 'success') {
    return 'status-success';
  }
  if (normalized === 'failed' || normalized === 'error') {
    return 'status-failed';
  }
  return 'status-unknown';
};

export const buildSubQuestionResult = (item: GenericRecord): string => {
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

export const escapeRegExp = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export const highlightText = (text: string, keyword: string): ReactNode => {
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

export const resolveToolTraceContainer = (tool: string, activeContainer: ContainerKey): ContainerKey => {
  if (isCbbContainer(activeContainer)) {
    return activeContainer;
  }
  return inferCbbContainerByTool(tool, activeContainer) || activeContainer;
};

/**
 * R2-P2-01: normalize the tool-call correlation payload BEFORE storing it in
 * state. Partial/loading-stage payloads (or future contract drift) must never
 * reach the render path with ``main_flow_logs_page`` / ``cbb_logs_page``
 * undefined — dereferencing ``.items`` there threw an unhandled TypeError
 * and made ``npm test`` exit 1.
 */
export const normalizeToolTracePayload = (raw: unknown): ToolCallCorrelationPayload | undefined => {
  const record = toRecordLoose(raw);
  if (!record) {
    return undefined;
  }
  const normalizeLogPage = (value: unknown, pageFallback: number) => {
    const pageRecord = toRecordLoose(value);
    return {
      items: toArrayLoose(pageRecord?.items) as CorrelationLogItem[],
      total: toNumber(pageRecord?.total) ?? 0,
      page: toNumber(pageRecord?.page) ?? pageFallback,
      page_size: toNumber(pageRecord?.page_size) ?? PAGE_SIZE,
    };
  };
  const errorSummary = toRecordLoose(record.error_summary);
  return {
    ...record,
    request_id: toText(record.request_id),
    container: toText(record.container),
    main_flow_container: toText(record.main_flow_container),
    tool: toText(record.tool),
    time_window: (toRecordLoose(record.time_window) || {}) as unknown as ToolCallCorrelationPayload['time_window'],
    request: toRecordLoose(record.request) || {},
    tool_call: toRecordLoose(record.tool_call) || {},
    tool_call_candidates: toArrayLoose(record.tool_call_candidates) as Array<Record<string, unknown>>,
    id_resolution: (toRecordLoose(record.id_resolution) || {}) as ToolCallCorrelationPayload['id_resolution'],
    error_summary: {
      alert_count: toNumber(errorSummary?.alert_count) ?? 0,
      level_breakdown: toRecordLoose(errorSummary?.level_breakdown) || {},
      channel_breakdown: toRecordLoose(errorSummary?.channel_breakdown) || {},
      signature_breakdown: toRecordLoose(errorSummary?.signature_breakdown),
      matched_keywords: toArrayLoose(errorSummary?.matched_keywords) as string[],
      first_alert_ts_utc: toText(errorSummary?.first_alert_ts_utc) || undefined,
      last_alert_ts_utc: toText(errorSummary?.last_alert_ts_utc) || undefined,
    },
    main_flow_logs_page: normalizeLogPage(record.main_flow_logs_page, 1),
    cbb_logs_page: normalizeLogPage(record.cbb_logs_page, 1),
  } as ToolCallCorrelationPayload;
};
