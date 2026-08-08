import { describe, expect, it } from 'vitest';

import {
  buildDynamicColumns,
  buildSubQuestionResult,
  extractSubQuestionRows,
  extractToolQueryRequest,
  getToolCallIdentity,
  getToolQueryRequestLabel,
  isAskDatabaseTool,
  isEfficiencyPiTool,
  isWenshuTool,
  mergeToolCallRows,
  normalizeRowsForTable,
  parseJsonIfPossible,
  resolveToolTraceContainer,
  statusClass,
  statusLabel,
  toNumber,
  toRecordLoose,
  toText,
  truncateText,
} from './utils';

describe('requestDetail utils: basic value helpers', () => {
  it('toText trims strings and falls back', () => {
    expect(toText('  hello  ', 'fb')).toBe('hello');
    expect(toText(undefined, 'fb')).toBe('fb');
    expect(toText(null, 'fb')).toBe('fb');
    expect(toText(42)).toBe('42');
  });

  it('toNumber parses finite numbers only', () => {
    expect(toNumber(3.5)).toBe(3.5);
    expect(toNumber('12')).toBe(12);
    expect(toNumber('abc')).toBeUndefined();
    expect(toNumber(Number.NaN)).toBeUndefined();
  });

  it('truncateText shortens long strings with suffix', () => {
    expect(truncateText('abc', 3)).toBe('abc');
    expect(truncateText('abcdef', 3)).toBe('abc...');
  });

  it('parseJsonIfPossible leaves non-JSON strings untouched', () => {
    expect(parseJsonIfPossible('plain')).toBe('plain');
    expect(parseJsonIfPossible('{"a":1}')).toEqual({ a: 1 });
    expect(parseJsonIfPossible('[1,2]')).toEqual([1, 2]);
  });

  it('toRecordLoose parses JSON object strings', () => {
    expect(toRecordLoose('{"a":1}')).toEqual({ a: 1 });
    expect(toRecordLoose('nope')).toBeUndefined();
  });
});

describe('requestDetail utils: tool classification', () => {
  it('detects typed tools by name', () => {
    expect(isWenshuTool('wenshu_agent')).toBe(true);
    expect(isWenshuTool('ask_database_agent')).toBe(false);
    expect(isAskDatabaseTool('ask_database_agent')).toBe(true);
    expect(isEfficiencyPiTool('efficiency_pi_agent')).toBe(true);
    expect(isEfficiencyPiTool('search_mounted_kb_agent')).toBe(false);
  });

  it('builds typed query request labels', () => {
    expect(getToolQueryRequestLabel('wenshu_agent')).toBe('查询请求（payload）');
    expect(getToolQueryRequestLabel('ask_database_agent')).toBe('查询请求（SQL）');
    expect(getToolQueryRequestLabel('efficiency_pi_agent')).toBe('查询请求（nGQL）');
    expect(getToolQueryRequestLabel('other')).toBe('查询请求');
  });

  it('extracts SQL from ask_database args', () => {
    const sql = extractToolQueryRequest('ask_database_agent', { sql: 'SELECT 1' }, null);
    expect(sql).toBe('SELECT 1');
  });

  it('extracts nGQL list from efficiency_pi output data source', () => {
    const args = { question: 'q' };
    const output = { data_source: { data: [{ generated_ngql: 'MATCH (n)' }] } };
    expect(extractToolQueryRequest('efficiency_pi_agent', args, output)).toBe('MATCH (n)');
  });
});

describe('requestDetail utils: tool call rows', () => {
  it('getToolCallIdentity normalizes fields', () => {
    expect(getToolCallIdentity({ agent_code: 'A', tool: 'T', tool_id: 'X', step: '2' })).toBe('A|T|X|2');
    expect(getToolCallIdentity({ step: 'not-a-number' })).toBe('unknown_agent|unknown_tool|unknown_id|-1');
  });

  it('mergeToolCallRows merges duplicated rows keeping earliest ts and non-null args/output', () => {
    const merged = mergeToolCallRows([
      { tool: 'T', tool_id: 'X', status: 'started', ts: '2026-01-01T10:00:00Z', args: null, output: 'first' },
      { tool: 'T', tool_id: 'X', status: 'success', ts: '2026-01-01T10:00:05Z', args: { q: 1 }, output: null },
    ]);
    expect(merged.status).toBe('success');
    expect(merged.args).toEqual({ q: 1 });
    expect(merged.output).toBe('first');
    expect(merged.ts).toBe('2026-01-01T10:00:00Z');
    expect(merged.end_ts).toBe('2026-01-01T10:00:05Z');
  });
});

describe('requestDetail utils: status helpers', () => {
  it('statusLabel maps statuses to Chinese', () => {
    expect(statusLabel('success')).toBe('成功');
    expect(statusLabel('failed')).toBe('失败');
    expect(statusLabel('error')).toBe('失败');
    expect(statusLabel('pending')).toBe('未知');
  });

  it('statusClass maps statuses to css classes', () => {
    expect(statusClass('success')).toBe('status-success');
    expect(statusClass('failed')).toBe('status-failed');
    expect(statusClass('pending')).toBe('status-unknown');
  });
});

describe('requestDetail utils: sub-question summary', () => {
  it('summarizes web search hits', () => {
    const text = buildSubQuestionResult({
      items: [{ name: '文档A', summary: '摘要' }],
    });
    expect(text).toContain('检索命中 1 条');
    expect(text).toContain('文档A');
  });

  it('summarizes empty result', () => {
    expect(buildSubQuestionResult({ result: '[[]]' })).toBe('返回空结果');
    expect(buildSubQuestionResult({})).toBe('已执行，暂无可读结果');
  });
});

describe('requestDetail utils: table rows and columns', () => {
  it('normalizeRowsForTable adds __idx and stringifies objects', () => {
    const rows = normalizeRowsForTable([{ a: 1, b: { c: 2 }, d: null }]);
    expect(rows[0].__idx).toBe(1);
    expect(rows[0].b).toContain('"c": 2');
    expect(rows[0].d).toBe('');
  });

  it('buildDynamicColumns builds # plus one column per key', () => {
    const columns = buildDynamicColumns([{ name: 'x', value: 1 }]);
    const titles = columns.map((col: { title: string }) => col.title);
    expect(titles).toEqual(['#', 'name', 'value']);
  });

  it('extractSubQuestionRows unwraps tool_call_results rows', () => {
    const rows = extractSubQuestionRows(
      'ask_database_agent',
      {
        data: {
          tool_call_results: [
            {
              tool_name: 'sql_runner',
              arguments: { question: 'q' },
              data: [[{ a: 1 }]],
            },
          ],
        },
      },
    );
    // 原逻辑:先展开 tool_call_results 为结构化行,再递归展开其 data 原始行
    expect(rows.length).toBe(2);
    expect(rows[0].tool_name).toBe('sql_runner');
    expect(rows[0].internal_step).toBe(1);
  });
});

describe('requestDetail utils: trace container resolution', () => {
  it('keeps cbb container as-is', () => {
    expect(resolveToolTraceContainer('wenshu_agent', 'cbb-text-to-metrics-dev')).toBe('cbb-text-to-metrics-dev');
  });

  it('infers cbb container from tool for a main-flow container', () => {
    expect(resolveToolTraceContainer('wenshu_agent', 'map_core-dev')).toBe('cbb-text-to-metrics-dev');
    expect(resolveToolTraceContainer('ask_database_agent', 'map_core-test')).toBe('cbb-text-to-sql-test');
  });

  it('falls back to the active main-flow container for unknown tools', () => {
    expect(resolveToolTraceContainer('unknown_tool', 'map_core-dev')).toBe('map_core-dev');
  });
});
