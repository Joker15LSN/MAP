import { useMemo } from 'react';
import { Alert, Table, Tag } from '@agentscope-ai/design';
import { Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';
import dayjs from 'dayjs';
import timezone from 'dayjs/plugin/timezone';
import utc from 'dayjs/plugin/utc';

dayjs.extend(utc);
dayjs.extend(timezone);

const DISPLAY_TZ = 'Asia/Shanghai';
const OUTPUT_FORMAT = 'YYYY-MM-DD HH:mm:ss.SSS';

interface TimeTextPair {
  localText: string;
  utcText: string;
}

const formatIsoTimePair = (rawValue?: string | null, tzName = DISPLAY_TZ): TimeTextPair => {
  if (!rawValue) {
    return { localText: '-', utcText: '-' };
  }
  const parsed = dayjs(rawValue);
  if (!parsed.isValid()) {
    return { localText: String(rawValue), utcText: String(rawValue) };
  }
  return {
    localText: `${parsed.tz(tzName).format(OUTPUT_FORMAT)} (UTC+8)`,
    utcText: `${parsed.utc().format(OUTPUT_FORMAT)} (UTC)`,
  };
};

export interface RequestDetail {
  request: {
    request_id: string;
    state_id?: string;
    session_id?: string;
    staff_code?: string;
    query?: string;
    status?: string;
    error?: string;
    start_ts?: string;
    end_ts?: string;
    duration_s: number;
    token_total: number;
    scene_result?: Record<string, unknown>;
  };
  agent_timeline: Array<Record<string, unknown>>;
  agent_events: Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>;
  summary: {
    agent_event_count: number;
    tool_call_count: number;
  };
}

type AnyRecord = Record<string, unknown>;

interface SubQuestionNode {
  question: string;
  inputText: string;
  status: 'success' | 'failed' | 'unknown';
  resultText: string;
  rows: AnyRecord[];
}

interface MergedToolNode extends AnyRecord {
  key: string;
  tool: string;
  toolId: string;
  agentCode: string;
  step?: number;
  status: string;
  startTs?: string;
  endTs?: string;
  durationS?: number;
  args?: unknown;
  output?: unknown;
  subQuestions: SubQuestionNode[];
}

interface SubAgentResultNode {
  key: string;
  agentCode: string;
  agentName: string;
  status: string;
  content: string;
  error: string;
  source: string;
  toolResultsCount: number;
  exitReason: string;
}

const asRecord = (value: unknown): AnyRecord | undefined => {
  if (typeof value === 'object' && value !== null) {
    return value as AnyRecord;
  }
  return undefined;
};

const asArray = (value: unknown): unknown[] => {
  if (Array.isArray(value)) {
    return value;
  }
  return [];
};

const stringValue = (value: unknown, fallback = ''): string => {
  if (typeof value === 'string') {
    return value.trim() || fallback;
  }
  if (value === undefined || value === null) {
    return fallback;
  }
  return String(value);
};

const numberValue = (value: unknown): number | undefined => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return undefined;
  }
  return numeric;
};

const truncateText = (value: string, max = 120): string => {
  if (value.length <= max) {
    return value;
  }
  return `${value.slice(0, max)}...`;
};

const safeParseObject = (value: string): AnyRecord | undefined => {
  try {
    const parsed = JSON.parse(value);
    return asRecord(parsed);
  } catch {
    return undefined;
  }
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

const asRecordLoose = (value: unknown): AnyRecord | undefined => {
  return asRecord(parseJsonIfPossible(value));
};

const asArrayLoose = (value: unknown): unknown[] => {
  const parsed = parseJsonIfPossible(value);
  return Array.isArray(parsed) ? parsed : [];
};

const normalizeSummary = (value: unknown): string => {
  const raw = stringValue(value, '');
  if (!raw) {
    return '';
  }
  return truncateText(raw.replace(/\s+/g, ' ').trim());
};

const countRowsFromSingleResult = (value: unknown): number => {
  if (!Array.isArray(value)) {
    return 0;
  }
  if (value.length === 1 && Array.isArray(value[0])) {
    return value[0].length;
  }
  return value.reduce((total, item) => {
    if (Array.isArray(item)) {
      return total + item.length;
    }
    return total;
  }, 0);
};

const hasOwn = (item: AnyRecord, key: string): boolean => Object.prototype.hasOwnProperty.call(item, key);

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

const appendNonEmpty = (target: AnyRecord, key: string, value: unknown) => {
  const displayValue = compactDisplayValue(value);
  if (displayValue) {
    target[key] = displayValue;
  }
};

const flattenResultRecords = (value: unknown): AnyRecord[] => {
  const results: AnyRecord[] = [];
  const visit = (entry: unknown) => {
    const parsed = parseJsonIfPossible(entry);
    if (Array.isArray(parsed)) {
      parsed.forEach(visit);
      return;
    }
    const record = asRecord(parsed);
    if (record) {
      results.push(record);
    }
  };
  asArrayLoose(value).forEach(visit);
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
  asArrayLoose(value).forEach(visit);
  return rows;
};

const extractNestedToolCallRows = (value: unknown): AnyRecord[] => {
  const record = asRecordLoose(value);
  const toolCallResults = asArrayLoose(record?.tool_call_results);
  if (toolCallResults.length === 0) {
    return [];
  }

  return toolCallResults.map((item, index) => {
    const callRecord = asRecordLoose(item) || {};
    const argsRecord = asRecordLoose(callRecord.arguments) || asRecordLoose(callRecord.args) || {};
    const row: AnyRecord = { internal_step: index + 1 };

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

const flattenResultRows = (value: unknown): AnyRecord[] => {
  const rows: AnyRecord[] = [];
  const visit = (entry: unknown) => {
    const parsed = parseJsonIfPossible(entry);
    if (Array.isArray(parsed)) {
      parsed.forEach(visit);
      return;
    }
    const record = asRecord(parsed);
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

const extractSubQuestionRows = (subQuestion: AnyRecord): AnyRecord[] => {
  const rows: AnyRecord[] = [];
  rows.push(...flattenResultRows(subQuestion.data));

  const dataRecord = asRecordLoose(subQuestion.data);
  const toolCallResults = asArrayLoose(dataRecord?.tool_call_results);
  for (const item of toolCallResults) {
    const itemRecord = asRecordLoose(item);
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
  return rows;
};

const normalizeRowsForTable = (rows: AnyRecord[]): AnyRecord[] =>
  rows.map((row, index) => {
    const normalized: AnyRecord = { __idx: index + 1 };
    for (const [key, value] of Object.entries(row)) {
      if (value === undefined || value === null) {
        normalized[key] = '';
      } else if (typeof value === 'object') {
        normalized[key] = compactValue(value);
      } else {
        normalized[key] = value;
      }
    }
    return normalized;
  });

const buildDynamicColumns = (rows: AnyRecord[]) => {
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
    { title: '#', dataIndex: '__idx', key: '__idx', width: 60 },
    ...keys.map((key) => ({
      title: key,
      dataIndex: key,
      key,
      render: (value: unknown) => {
        const text = stringValue(value, '-');
        return (
          <span className="query-cell-wrap" title={text}>
            {truncateText(text, 260)}
          </span>
        );
      },
    })),
  ];
};

const toCompactJson = (value: unknown): string => {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const formatAsPrettyJson = (value: unknown): string => {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const normalizeToolName = (tool: unknown): string => stringValue(tool, '').toLowerCase();
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
  const record = asRecordLoose(value);
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
  const jsonLikeValue = typeof value === 'string' ? value : formatAsPrettyJson(value);
  if (isWenshuTool(tool)) {
    const payload = findValueByKeysDeep(value, ['metric_ql', 'metricQl', 'payload', 'request_payload', 'requestPayload']);
    if (payload !== undefined && payload !== null) {
      return formatAsPrettyJson(payload);
    }
    return jsonLikeValue;
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

const extractToolQueryRequest = (toolCall: MergedToolNode): string => {
  const fromArgs = extractTypedQueryRequestText(toolCall.tool, toolCall.args);
  if (fromArgs) {
    return fromArgs;
  }
  const outputRecord = asRecordLoose(toolCall.output);
  const dataSource = asRecordLoose(outputRecord?.data_source);
  const sourceItems = flattenResultRecords(dataSource?.data);
  for (const item of sourceItems) {
    const fromItem = extractTypedQueryRequestText(toolCall.tool, item);
    if (fromItem) {
      return fromItem;
    }
  }
  return '';
};

const summarizeInputFromRecord = (record: AnyRecord | undefined, max = 280): string => {
  if (!record) {
    return '';
  }

  const preferredKeys = ['query', 'question', 'user_query', 'content', 'subquestion', 'current_subquestion', 'prompt'];
  for (const key of preferredKeys) {
    const raw = stringValue(record[key], '');
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
  return summarizeInputFromRecord(asRecordLoose(value), max);
};

const buildSubQuestionResultText = (item: AnyRecord): string => {
  const errorText = normalizeSummary(item.error);
  if (errorText) {
    return `错误: ${errorText}`;
  }

  const webItems = asArrayLoose(item.items);
  if (webItems.length > 0) {
    const firstItem = asRecordLoose(webItems[0]);
    const firstTitle = stringValue(firstItem?.name, '') || stringValue(firstItem?.title, '');
    const firstSummary = stringValue(firstItem?.summary, '');
    const snippet = firstTitle || firstSummary || '无可展示摘要';
    return `检索命中 ${webItems.length} 条，首条: ${truncateText(snippet.replace(/\s+/g, ' '), 220)}`;
  }

  const chunkContent = stringValue(item.chunk_content, '');
  if (chunkContent) {
    const docTitle = stringValue(item.title, '未命名文档');
    return `知识库命中: ${truncateText(docTitle, 60)}，片段: ${truncateText(chunkContent.replace(/\s+/g, ' '), 220)}`;
  }

  const resultTextFromSearch = stringValue(item.result_text, '');
  if (resultTextFromSearch) {
    return `检索摘要: ${truncateText(resultTextFromSearch.replace(/\s+/g, ' '), 220)}`;
  }

  const summaryText = normalizeSummary(item.summary);
  if (summaryText) {
    return summaryText;
  }

  const directDataRows = flattenDataRows(item.data);
  if (hasOwn(item, 'data') && Array.isArray(item.data)) {
    if (directDataRows.length === 0) {
      return '返回空结果';
    }
    const sample = truncateText(compactValue(directDataRows[0]).replace(/\s+/g, ' '), 220);
    return sample ? `返回 ${directDataRows.length} 条，示例: ${sample}` : `返回 ${directDataRows.length} 条`;
  }

  const dataRecord = asRecordLoose(item.data);
  const toolCallResults = asArrayLoose(dataRecord?.tool_call_results);
  if (toolCallResults.length > 0) {
    let toolCount = 0;
    let rows = 0;
    for (const call of toolCallResults) {
      const callRecord = asRecordLoose(call);
      if (!callRecord) {
        continue;
      }
      toolCount += 1;
      rows += countRowsFromSingleResult(callRecord.data);
    }
    return `执行工具 ${toolCount} 个，返回数据行 ${rows} 条`;
  }

  const resultText = normalizeSummary(item.result);
  if (resultText) {
    if (resultText === '[[]]') {
      return '返回空结果';
    }
    return `结果: ${resultText}`;
  }

  return '已执行，暂无摘要';
};

const extractToolOutputSummary = (toolCall: MergedToolNode): string => {
  const outputRecord = asRecordLoose(toolCall.output);
  if (!outputRecord) {
    return '';
  }

  const errorText = stringValue(outputRecord.error, '');
  if (errorText) {
    return `工具错误: ${truncateText(errorText.replace(/\s+/g, ' '), 220)}`;
  }

  const contentText = stringValue(outputRecord.content, '');
  if (contentText) {
    return `工具返回: ${truncateText(contentText.replace(/\s+/g, ' '), 220)}`;
  }

  const dataSource = asRecordLoose(outputRecord.data_source);
  const dataItems = asArrayLoose(dataSource?.data);
  if (dataItems.length > 0) {
    return `工具返回 ${dataItems.length} 条子问题结果`;
  }

  const successFlag = outputRecord.success;
  if (typeof successFlag === 'boolean') {
    return successFlag ? '工具调用成功，未返回可展示内容' : '工具调用失败，未返回详细错误';
  }

  return '工具已返回结果对象';
};

const extractSubQuestions = (toolCall: MergedToolNode): SubQuestionNode[] => {
  const outputRecord = asRecordLoose(toolCall.output);
  const argsRecord = asRecordLoose(toolCall.args);
  const fallbackQuestion = stringValue(argsRecord?.query, '');
  const toolQueryRequest = extractToolQueryRequest(toolCall);
  const dataSource = asRecordLoose(outputRecord?.data_source);
  const sourceItems = flattenResultRecords(dataSource?.data);

  if (sourceItems.length === 0) {
    const outputError = stringValue(outputRecord?.error, '');
    const outputContent = stringValue(outputRecord?.content, '');
    const fallbackResultText = outputError
      ? `工具错误: ${truncateText(outputError.replace(/\s+/g, ' '), 220)}`
      : outputContent
        ? `工具返回: ${truncateText(outputContent.replace(/\s+/g, ' '), 220)}`
        : outputRecord
          ? '工具已返回结果对象，但未返回可拆分子问题'
          : '仅记录调用参数，暂无返回结果';
    return [
      {
        question: fallbackQuestion || '未拆分子问题',
        inputText: toolQueryRequest || fallbackQuestion || '未记录输入',
        status: toolCall.status === 'success' ? 'success' : (toolCall.status === 'failed' || toolCall.status === 'error') ? 'failed' : 'unknown',
        resultText: fallbackResultText,
        rows: [],
      },
    ];
  }

  return sourceItems.map((entry, index) => {
    const record = entry;
    const question = stringValue(record.query, '') || stringValue(record.question, '') || fallbackQuestion || `子问题 ${index + 1}`;
    const hasError = stringValue(record.error, '') !== '';
    const successFlag = typeof record.success === 'boolean' ? record.success : undefined;
    const fallbackStatus = toolCall.status === 'success' ? 'success' : (toolCall.status === 'failed' || toolCall.status === 'error') ? 'failed' : 'unknown';

    let status: SubQuestionNode['status'] = fallbackStatus;
    if (hasError) {
      status = 'failed';
    } else if (successFlag === true) {
      status = 'success';
    } else if (successFlag === false) {
      status = 'failed';
    }

    return {
      question,
      inputText: extractTypedQueryRequestText(toolCall.tool, record) || question,
      status,
      resultText: buildSubQuestionResultText(record),
      rows: extractSubQuestionRows(record),
    };
  });
};

const extractSubAgentInputMap = (
  agentEvents: Array<Record<string, unknown>>,
  tools: MergedToolNode[],
): Map<string, string> => {
  const inputMap = new Map<string, Set<string>>();
  const addInput = (agentCode: string, inputSummary: string) => {
    const normalized = inputSummary.trim();
    if (!agentCode || !normalized) {
      return;
    }
    if (!inputMap.has(agentCode)) {
      inputMap.set(agentCode, new Set<string>());
    }
    inputMap.get(agentCode)!.add(normalized);
  };

  for (const event of agentEvents) {
    const stage = stringValue(event.stage, '');
    const agentCode = stringValue(event.agent_code, '');
    if (!agentCode || stage !== 'start' || agentCode === 'GlobalDomainOrchestrator') {
      continue;
    }
    const payload = asRecord(event.payload);
    const inputRecord = asRecord(payload?.input);
    addInput(agentCode, summarizeInputFromRecord(inputRecord));
  }

  for (const tool of tools) {
    addInput(tool.agentCode, summarizeInputFromUnknown(tool.args));
  }

  const flattened = new Map<string, string>();
  for (const [agentCode, values] of inputMap.entries()) {
    const mergedText = Array.from(values).join(' | ');
    flattened.set(agentCode, truncateText(mergedText.replace(/\s+/g, ' '), 420));
  }
  return flattened;
};

const extractHistoryToolErrors = (history: unknown[]): string[] => {
  const errors: string[] = [];
  for (const item of history) {
    const record = asRecord(item);
    if (!record) {
      continue;
    }
    if (stringValue(record.role, '') !== 'tool') {
      continue;
    }
    const rawContent = stringValue(record.content, '');
    if (!rawContent) {
      continue;
    }
    const parsed = safeParseObject(rawContent);
    const errorText = stringValue(parsed?.error, '');
    if (errorText) {
      errors.push(errorText);
    }
  }
  return Array.from(new Set(errors));
};

const extractSubAgentContentFromOutput = (outputValue: unknown): string => {
  if (typeof outputValue === 'string') {
    return truncateText(outputValue.replace(/\s+/g, ' '), 400);
  }
  const outputRecord = asRecord(outputValue);
  if (!outputRecord) {
    return '';
  }

  const directContent = stringValue(outputRecord.content, '');
  if (directContent) {
    return truncateText(directContent.replace(/\s+/g, ' '), 400);
  }

  const errorText = stringValue(outputRecord.error, '');
  if (errorText) {
    return `错误: ${truncateText(errorText.replace(/\s+/g, ' '), 320)}`;
  }

  const dataSource = asRecord(outputRecord.data_source);
  const history = asArray(dataSource?.history);
  if (history.length > 0) {
    const toolErrors = extractHistoryToolErrors(history);
    if (toolErrors.length > 0) {
      return `工具返回错误: ${truncateText(toolErrors.join(' | ').replace(/\s+/g, ' '), 380)}`;
    }

    for (let i = history.length - 1; i >= 0; i -= 1) {
      const record = asRecord(history[i]);
      if (!record) {
        continue;
      }
      if (stringValue(record.role, '') !== 'assistant') {
        continue;
      }
      const content = stringValue(record.content, '');
      if (content) {
        return truncateText(content.replace(/\s+/g, ' '), 380);
      }
    }
  }

  return '';
};

const extractSubAgentResults = (agentEvents: Array<Record<string, unknown>>): SubAgentResultNode[] => {
  const results = new Map<string, SubAgentResultNode>();

  for (const event of agentEvents) {
    const payload = asRecord(event.payload);
    const input = asRecord(payload?.input);
    const dispatchResults = asArray(input?.effective_dispatch_results);
    if (dispatchResults.length === 0) {
      continue;
    }

    for (const item of dispatchResults) {
      const record = asRecord(item) || {};
      const agentCode = stringValue(record.agent_code, 'unknown_agent');
      const content = stringValue(record.content, '');
      const key = `${agentCode}|${content}`;
      const errorText = stringValue(record.error, '');
      const successFlag = record.success;
      const status = typeof successFlag === 'boolean'
        ? (successFlag ? 'success' : 'failed')
        : (errorText ? 'failed' : 'unknown');
      const exit = asRecord(record.exit);

      if (!results.has(key)) {
        results.set(key, {
          key,
          agentCode,
          agentName: stringValue(record.agent_name, ''),
          status,
          content: truncateText(content.replace(/\s+/g, ' '), 420),
          error: truncateText(errorText.replace(/\s+/g, ' '), 220),
          source: stringValue(record.response_source, ''),
          toolResultsCount: asArray(record.tool_results).length,
          exitReason: stringValue(exit?.reason, ''),
        });
      }
    }
  }

  for (const event of agentEvents) {
    const agentCode = stringValue(event.agent_code, '');
    const stage = stringValue(event.stage, '');
    if (!agentCode || agentCode === 'GlobalDomainOrchestrator' || stage !== 'end') {
      continue;
    }
    const payload = asRecord(event.payload);
    const output = payload?.output;
    const content = extractSubAgentContentFromOutput(output);
    if (!content) {
      continue;
    }

    const key = `${agentCode}|${content}`;
    if (results.has(key)) {
      continue;
    }

    const outputRecord = asRecord(output);
    const outputError = stringValue(outputRecord?.error, '');
    const eventStatus = stringValue(event.status, '');
    const status = eventStatus || (outputError ? 'failed' : 'unknown');
    results.set(key, {
      key,
      agentCode,
      agentName: stringValue(event.agent_name, ''),
      status,
      content,
      error: truncateText(outputError.replace(/\s+/g, ' '), 220),
      source: 'agent_events.payload.output',
      toolResultsCount: 0,
      exitReason: '',
    });
  }

  return Array.from(results.values()).sort((a, b) => a.agentCode.localeCompare(b.agentCode));
};

const extractMasterAgentResult = (detail: RequestDetail): string => {
  const events = detail.agent_events || [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    const agentCode = stringValue(event.agent_code, '');
    const stage = stringValue(event.stage, '');
    if (agentCode !== 'GlobalDomainOrchestrator' || stage !== 'end') {
      continue;
    }
    const payload = asRecord(event.payload);
    const outputValue = payload?.output;
    if (typeof outputValue === 'string' && outputValue.trim()) {
      return truncateText(outputValue.replace(/\s+/g, ' '), 900);
    }
    const outputRecord = asRecord(outputValue);
    if (outputRecord) {
      const contentText = stringValue(outputRecord.content, '');
      if (contentText) {
        return truncateText(contentText.replace(/\s+/g, ' '), 900);
      }
      const errorText = stringValue(outputRecord.error, '');
      if (errorText) {
        return `错误: ${truncateText(errorText.replace(/\s+/g, ' '), 900)}`;
      }
    }
  }

  const requestError = stringValue(detail.request.error, '');
  if (requestError) {
    return `请求错误: ${truncateText(requestError.replace(/\s+/g, ' '), 900)}`;
  }
  return '';
};

const mergeToolCalls = (toolCalls: Array<Record<string, unknown>>): MergedToolNode[] => {
  const merged = new Map<string, MergedToolNode>();

  for (const row of toolCalls) {
    const tool = stringValue(row.tool, 'unknown_tool');
    const toolId = stringValue(row.tool_id, `${tool}-unknown`);
    const agentCode = stringValue(row.agent_code, 'unknown_agent');
    const stepRaw = numberValue(row.step);
    const step = stepRaw === undefined ? undefined : Math.trunc(stepRaw);
    const mergeKey = `${agentCode}|${tool}|${toolId}|${step ?? -1}`;
    const statusText = stringValue(row.status, '');
    const ts = stringValue(row.ts, '');

    if (!merged.has(mergeKey)) {
      merged.set(mergeKey, {
        key: mergeKey,
        tool,
        toolId,
        agentCode,
        step,
        status: statusText || 'unknown',
        startTs: ts || undefined,
        endTs: ts || undefined,
        durationS: numberValue(row.duration_s),
        args: row.args,
        output: row.output,
        subQuestions: [],
      });
    }

    const target = merged.get(mergeKey)!;
    if (statusText) {
      target.status = statusText;
    }
    if ((target.args === undefined || target.args === null) && row.args !== undefined && row.args !== null) {
      target.args = row.args;
    }
    if ((target.output === undefined || target.output === null) && row.output !== undefined && row.output !== null) {
      target.output = row.output;
    }

    const duration = numberValue(row.duration_s);
    if (duration !== undefined) {
      target.durationS = duration;
    }

    if (ts) {
      if (!target.startTs || ts < target.startTs) {
        target.startTs = ts;
      }
      if (!target.endTs || ts > target.endTs) {
        target.endTs = ts;
      }
    }
  }

  const results = Array.from(merged.values());
  for (const item of results) {
    item.subQuestions = extractSubQuestions(item);
  }

  results.sort((a, b) => {
    const at = stringValue(a.startTs, '');
    const bt = stringValue(b.startTs, '');
    if (at && bt && at !== bt) {
      return at.localeCompare(bt);
    }
    return a.key.localeCompare(b.key);
  });
  return results;
};

const buildSceneNodes = (sceneResult: Record<string, unknown> | undefined): DataNode[] => {
  if (!sceneResult) {
    return [];
  }

  const bigScenes = asArray((sceneResult as AnyRecord).big_scenes);
  const subScenes = asArray((sceneResult as AnyRecord).sub_scenes);
  const subSceneMap = new Map<string, string[]>();

  for (const item of subScenes) {
    const record = asRecord(item) || {};
    const bigSceneName = stringValue(record.big_scene, 'unknown');
    const names = asArray(record.sub_scenes)
      .map((name) => stringValue(name, ''))
      .filter(Boolean);
    if (!subSceneMap.has(bigSceneName)) {
      subSceneMap.set(bigSceneName, []);
    }
    subSceneMap.get(bigSceneName)!.push(...names);
  }

  const seen = new Set<string>();
  const sceneNodes: DataNode[] = [];

  bigScenes.forEach((item, bigIndex) => {
    const record = asRecord(item) || {};
    const bigSceneName = stringValue(record.big_scene, 'unknown');
    const confidence = numberValue(record.confidence);
    const subSceneNames = Array.from(new Set(subSceneMap.get(bigSceneName) || []));
    seen.add(bigSceneName);

    sceneNodes.push({
      key: `scene-${bigSceneName}-${bigIndex}`,
      title: (
        <div className="request-tree-title-wrap">
          <span className="request-tree-main-text">大场景: {bigSceneName}</span>
          {confidence !== undefined ? <Tag>置信度: {confidence.toFixed(2)}</Tag> : null}
          <Tag>小场景数: {subSceneNames.length}</Tag>
        </div>
      ),
      children: subSceneNames.map((name, index) => ({
        key: `sub-scene-${bigSceneName}-${bigIndex}-${name}-${index}`,
        title: (
          <div className="request-tree-title-wrap">
            <span className="request-tree-sub-text">小场景: {name}</span>
          </div>
        ),
      })),
    });
  });

  for (const [bigSceneName, names] of subSceneMap.entries()) {
    if (seen.has(bigSceneName)) {
      continue;
    }
    const uniqueNames = Array.from(new Set(names));
    sceneNodes.push({
      key: `scene-${bigSceneName}`,
      title: (
        <div className="request-tree-title-wrap">
          <span className="request-tree-main-text">大场景: {bigSceneName}</span>
          <Tag>小场景数: {uniqueNames.length}</Tag>
        </div>
      ),
      children: uniqueNames.map((name, index) => ({
        key: `sub-scene-${bigSceneName}-${name}-${index}`,
        title: <span className="request-tree-sub-text">小场景: {name}</span>,
      })),
    });
  }

  return sceneNodes;
};

const statusLabel = (status: string): string => {
  if (status === 'success') {
    return '成功';
  }
  if (status === 'failed' || status === 'error') {
    return '失败';
  }
  return status || '未知';
};

const statusClassName = (status: string): string => {
  if (status === 'success') {
    return 'status-success';
  }
  if (status === 'failed' || status === 'error') {
    return 'status-failed';
  }
  return 'status-unknown';
};

const buildAgentToolNodes = (tools: MergedToolNode[], subAgentInputMap: Map<string, string>): DataNode[] => {
  const agentMap = new Map<string, MergedToolNode[]>();
  for (const item of tools) {
    if (!agentMap.has(item.agentCode)) {
      agentMap.set(item.agentCode, []);
    }
    agentMap.get(item.agentCode)!.push(item);
  }

  const sortedAgents = Array.from(agentMap.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  return sortedAgents.map(([agentCode, agentTools]) => {
    const subAgentInput = subAgentInputMap.get(agentCode) || '';
    return ({
      key: `agent-${agentCode}`,
      title: (
        <div className="request-tree-node-block">
          <div className="request-tree-title-wrap">
            <span className="request-tree-main-text">sub-agent: {agentCode}</span>
            <Tag>工具数: {agentTools.length}</Tag>
          </div>
          {subAgentInput ? <div className="request-tree-meta-text">输入: {subAgentInput}</div> : null}
        </div>
      ),
      children: agentTools.map((tool) => {
        const durationText = tool.durationS !== undefined ? `${tool.durationS.toFixed(2)}s` : '-';
        const endPair = tool.endTs ? formatIsoTimePair(tool.endTs) : undefined;
        const toolInput = summarizeInputFromUnknown(tool.args, 360);
        const toolQueryRequest = extractToolQueryRequest(tool);
        const queryRequestText = toolQueryRequest ? truncateText(toCompactJson(toolQueryRequest).replace(/\s+/g, ' '), 900) : '';
        const outputSummary = extractToolOutputSummary(tool);
        return {
          key: `tool-${tool.key}`,
          title: (
            <div className="request-tree-node-block">
              <div className="request-tree-title-wrap">
                <span className="request-tree-main-text">工具: {tool.tool}</span>
                <span className={`request-tree-status ${statusClassName(tool.status)}`}>{statusLabel(tool.status)}</span>
                <Tag>tool_id: {tool.toolId}</Tag>
                <Tag>step: {tool.step ?? '-'}</Tag>
                <Tag>子问题: {tool.subQuestions.length}</Tag>
                <Tag>耗时: {durationText}</Tag>
              </div>
              {toolInput ? <div className="request-tree-meta-text">输入: {toolInput}</div> : null}
              {queryRequestText ? <div className="request-tree-meta-text">查询请求: {queryRequestText}</div> : null}
              {endPair ? <div className="request-tree-meta-text">结束时间: {endPair.localText}</div> : null}
              {outputSummary ? <div className="request-tree-meta-text">输出: {outputSummary}</div> : null}
            </div>
          ),
          children: tool.subQuestions.map((subQuestion, index) => {
            const normalizedRows = normalizeRowsForTable(subQuestion.rows || []);
            return ({
              key: `subq-${tool.key}-${index}`,
              title: (
                <div className="request-tree-node-block">
                  <div className="request-tree-title-wrap">
                    <span className="request-tree-sub-text">
                      子问题 {index + 1}: {subQuestion.question}
                    </span>
                    <span className={`request-tree-status ${statusClassName(subQuestion.status)}`}>{statusLabel(subQuestion.status)}</span>
                  </div>
                  <div className="request-tree-meta-text">输入: {subQuestion.inputText}</div>
                  <div className="request-tree-meta-text">输出: {subQuestion.resultText}</div>
                  {normalizedRows.length > 0 ? (
                    <div className="table-gap-top">
                      <Table
                        size="small"
                        scroll={{ x: 'max-content' }}
                        rowKey={(row: Record<string, unknown>) => String(row.__idx)}
                        pagination={false}
                        dataSource={normalizedRows}
                        columns={buildDynamicColumns(normalizedRows)}
                      />
                    </div>
                  ) : null}
                </div>
              ),
            });
          }),
        };
      }),
    });
  });
};

const buildSubAgentResultNodes = (
  agentEvents: Array<Record<string, unknown>>,
  subAgentInputMap: Map<string, string>,
): DataNode[] => {
  const subAgentResults = extractSubAgentResults(agentEvents);
  if (subAgentResults.length === 0) {
    return [{ key: 'subagent-result-empty', title: '未记录可展示的 sub-agent 返回结果' }];
  }

  return subAgentResults.map((item, index) => ({
    key: `subagent-result-${item.agentCode}-${index}`,
    title: (
      <div className="request-tree-node-block">
        <div className="request-tree-title-wrap">
          <span className="request-tree-main-text">sub-agent: {item.agentCode}</span>
          {item.agentName ? <Tag>{item.agentName}</Tag> : null}
          <span className={`request-tree-status ${statusClassName(item.status)}`}>{statusLabel(item.status)}</span>
          {item.source ? <Tag>source: {item.source}</Tag> : null}
          {item.toolResultsCount > 0 ? <Tag>tool_results: {item.toolResultsCount}</Tag> : null}
          {item.exitReason ? <Tag>exit: {item.exitReason}</Tag> : null}
        </div>
        {subAgentInputMap.get(item.agentCode) ? (
          <div className="request-tree-meta-text">输入: {subAgentInputMap.get(item.agentCode)}</div>
        ) : null}
        <div className="request-tree-meta-text">输出: {item.content || '无文本返回'}</div>
        {item.error ? <div className="request-tree-meta-text">错误: {item.error}</div> : null}
      </div>
    ),
  }));
};

interface RequestCallTreeProps {
  detail?: RequestDetail;
}

export const RequestCallTree = ({ detail }: RequestCallTreeProps) => {
  const treeData = useMemo<DataNode[]>(() => {
    if (!detail) {
      return [];
    }
    const sceneNodes = buildSceneNodes(detail.request.scene_result);
    const mergedTools = mergeToolCalls(detail.tool_calls || []);
    const subAgentInputMap = extractSubAgentInputMap(detail.agent_events || [], mergedTools);
    const agentNodes = buildAgentToolNodes(mergedTools, subAgentInputMap);
    const subAgentResultNodes = buildSubAgentResultNodes(detail.agent_events || [], subAgentInputMap);
    const masterResult = extractMasterAgentResult(detail);

    return [
      {
        key: `request-root-${detail.request.request_id}`,
        title: (
          <div className="request-tree-title-wrap">
            <span className="request-tree-main-text">request_id: {detail.request.request_id}</span>
            <Tag>状态: {detail.request.status || 'unknown'}</Tag>
          </div>
        ),
        children: [
          {
            key: `scene-root-${detail.request.request_id}`,
            title: (
              <div className="request-tree-title-wrap">
                <span className="request-tree-main-text">场景识别</span>
                <Tag>大场景数: {sceneNodes.length}</Tag>
              </div>
            ),
            children: sceneNodes.length > 0 ? sceneNodes : [{ key: 'scene-empty', title: '无场景数据' }],
          },
          {
            key: `agent-root-${detail.request.request_id}`,
            title: (
              <div className="request-tree-title-wrap">
                <span className="request-tree-main-text">sub-agent 调用链路</span>
                <Tag>sub-agent 数: {agentNodes.length}</Tag>
              </div>
            ),
            children: agentNodes.length > 0 ? agentNodes : [{ key: 'agent-empty', title: '无工具调用数据' }],
          },
          {
            key: `sub-agent-result-root-${detail.request.request_id}`,
            title: (
              <div className="request-tree-title-wrap">
                <span className="request-tree-main-text">sub-agent 返回结果</span>
                <Tag>结果数: {subAgentResultNodes.length}</Tag>
              </div>
            ),
            children: subAgentResultNodes,
          },
          {
            key: `master-result-root-${detail.request.request_id}`,
            title: (
              <div className="request-tree-title-wrap">
                <span className="request-tree-main-text">master-agent 最终结果</span>
                <Tag>{masterResult ? '已提取' : '无结果'}</Tag>
              </div>
            ),
            children: [
              {
                key: `master-result-node-${detail.request.request_id}`,
                title: (
                  <div className="request-tree-node-block">
                    <div className="request-tree-meta-text">{masterResult || '未记录 master-agent 可展示输出'}</div>
                  </div>
                ),
              },
            ],
          },
        ],
      },
    ];
  }, [detail]);

  if (!detail) {
    return <Alert type="info" showIcon message="暂无请求详情，无法生成调用链路树" />;
  }

  return (
    <div className="request-call-tree-wrap">
      <Tree className="request-call-tree" showLine={{ showLeafIcon: false }} selectable={false} defaultExpandAll treeData={treeData} />
    </div>
  );
};
