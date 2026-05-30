import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Input, Select, Table, Tag } from '@agentscope-ai/design';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { analyticsApi } from '../api/client';
import { FilterState, FridayAction, FridayConversation, FridayEvidenceItem, FridayMessage, FridayReport } from '../types';
import { inferMainFlowContainer, MainFlowContainerKey } from '../constants/containers';

type FridayHistoryMessage = { role: 'user' | 'assistant' | 'system'; content: string };

interface FridayPageProps {
  filters: FilterState;
  onRunAction: (action: FridayAction) => void;
}

const STORAGE_CONVERSATIONS_KEY = 'map_friday_conversations';
const STORAGE_ACTIVE_ID_KEY = 'map_friday_active_conversation';
const STORAGE_FRIDAY_ENV_KEY = 'map_friday_env';

type FridayEnv = 'dev' | 'test' | 'ubddev';

const FRIDAY_ENV_TO_CONTAINER: Record<FridayEnv, MainFlowContainerKey> = {
  dev: 'map_core-dev',
  test: 'map_core-test',
  ubddev: 'map_core-preprod',
};

const CONTAINER_TO_FRIDAY_ENV: Record<MainFlowContainerKey, FridayEnv> = {
  'map_core-dev': 'dev',
  'map_core-test': 'test',
  'map_core-preprod': 'ubddev',
};

const FRIDAY_ENV_OPTIONS: Array<{ label: string; value: FridayEnv }> = [
  { label: 'dev', value: 'dev' },
  { label: 'test', value: 'test' },
  { label: 'ubddev', value: 'ubddev' },
];

const normalizeFridayEnv = (value?: string): FridayEnv | undefined => {
  if (value === 'dev' || value === 'test' || value === 'ubddev') {
    return value;
  }
  return undefined;
};

const QUICK_QUESTIONS = [
  '定位最近 30 天慢调用并给优化建议',
  '定位最近 30 天 ERROR/WARNING 的主要原因',
  '请分析 request_id=xxx 的关键链路问题',
];

const makeConversation = (title = '新会话'): FridayConversation => {
  const now = new Date().toISOString();
  return {
    id: `conv_${Math.random().toString(36).slice(2, 10)}`,
    title,
    created_at: now,
    updated_at: now,
    messages: [],
  };
};

const trimTitle = (message: string) => {
  const normalized = (message || '').trim();
  if (!normalized) {
    return '新会话';
  }
  return normalized.length > 24 ? `${normalized.slice(0, 24)}...` : normalized;
};

const parseMessageHistory = (messages: FridayMessage[]): FridayHistoryMessage[] =>
  messages
    .filter((item) => item.role === 'user' || item.role === 'assistant' || item.role === 'system')
    .filter((item) => String(item.content || '').trim().length > 0)
    .map((item) => ({ role: item.role, content: item.content }));

const formatCount = (value: unknown) => {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) {
    return '0';
  }
  return String(number);
};

const formatReportTime = (value?: string) => {
  if (!value) {
    return '-';
  }
  return value.replace('T', ' ').slice(0, 19);
};

const EvidenceView = ({ evidence }: { evidence?: FridayEvidenceItem }) => {
  if (!evidence) {
    return null;
  }

  const slowCalls = Array.isArray(evidence.slow_calls_top) ? evidence.slow_calls_top : [];
  const errors = Array.isArray(evidence.error_clusters_top) ? evidence.error_clusters_top : [];
  const trace = evidence.request_trace;

  return (
    <div className="friday-evidence">
      <div className="friday-evidence-title">诊断证据</div>
      <div className="summary-row">
        <Tag>诊断意图: {String(evidence.intent || '-')}</Tag>
        <Tag>范围: 最近 {formatCount(evidence.scope?.lookback_days)} 天</Tag>
        <Tag>慢阈值: {formatCount(evidence.scope?.slow_threshold_s)}s</Tag>
      </div>

      {slowCalls.length > 0 ? (
        <div className="friday-evidence-block">
          <div className="friday-evidence-subtitle">慢调用 Top</div>
          <ul className="friday-evidence-list">
            {slowCalls.slice(0, 5).map((row, idx) => (
              <li key={`slow-${idx}`}>
                {String(row.request_id || '-')}
                {' | '}
                {String(row.container || '-')}
                {' | '}
                {formatCount(row.duration_s)}s
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {errors.length > 0 ? (
        <div className="friday-evidence-block">
          <div className="friday-evidence-subtitle">错误聚类 Top</div>
          <ul className="friday-evidence-list">
            {errors.slice(0, 5).map((row, idx) => (
              <li key={`error-${idx}`}>
                {String(row.error_type || '-')}
                {' | '}
                {String(row.container || '-')}
                {' | 次数: '}
                {formatCount(row.count)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {trace ? (
        <div className="friday-evidence-block">
          <div className="friday-evidence-subtitle">指定请求追踪</div>
          <ul className="friday-evidence-list">
            <li>request_id: {String(trace.request_id || '-')}</li>
            <li>container: {String(trace.container || '-')}</li>
            <li>root_cause_hint: {String(trace.root_cause_hint || '-')}</li>
            <li>error_hits: {formatCount(trace.error_hits)}</li>
          </ul>
        </div>
      ) : null}
    </div>
  );
};

export const FridayPage = ({ filters, onRunAction }: FridayPageProps) => {
  const [conversations, setConversations] = useState<FridayConversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string>('');
  const [inputValue, setInputValue] = useState('');
  const [sending, setSending] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [selectedEnv, setSelectedEnv] = useState<FridayEnv>(() => {
    const fallbackEnv = CONTAINER_TO_FRIDAY_ENV[inferMainFlowContainer(filters.container) as MainFlowContainerKey] || 'dev';
    try {
      const cached = window.localStorage.getItem(STORAGE_FRIDAY_ENV_KEY);
      return normalizeFridayEnv(cached || undefined) || fallbackEnv;
    } catch {
      return fallbackEnv;
    }
  });
  const [reports, setReports] = useState<FridayReport[]>([]);
  const [selectedReport, setSelectedReport] = useState<FridayReport>();
  const [reportsLoading, setReportsLoading] = useState(false);
  const [reportRunning, setReportRunning] = useState<'weekly' | 'monthly' | ''>('');
  const [reportError, setReportError] = useState('');

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_CONVERSATIONS_KEY);
      const parsed = raw ? (JSON.parse(raw) as FridayConversation[]) : [];
      if (Array.isArray(parsed) && parsed.length > 0) {
        setConversations(parsed);
        const storedActive = window.localStorage.getItem(STORAGE_ACTIVE_ID_KEY);
        const active = parsed.find((item) => item.id === storedActive) || parsed[0];
        setActiveConversationId(active.id);
        return;
      }
    } catch {
      // ignore local cache parse errors
    }

    const initial = makeConversation('默认会话');
    setConversations([initial]);
    setActiveConversationId(initial.id);
  }, []);

  useEffect(() => {
    if (conversations.length === 0) {
      return;
    }
    window.localStorage.setItem(STORAGE_CONVERSATIONS_KEY, JSON.stringify(conversations));
  }, [conversations]);

  useEffect(() => {
    if (!activeConversationId) {
      return;
    }
    window.localStorage.setItem(STORAGE_ACTIVE_ID_KEY, activeConversationId);
  }, [activeConversationId]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_FRIDAY_ENV_KEY, selectedEnv);
  }, [selectedEnv]);

  const selectedContainer = FRIDAY_ENV_TO_CONTAINER[selectedEnv];

  const activeConversation = useMemo(
    () => conversations.find((item) => item.id === activeConversationId),
    [conversations, activeConversationId],
  );

  const loadReports = async (preferredReportId?: string) => {
    setReportsLoading(true);
    setReportError('');
    try {
      const items = await analyticsApi.listFridayReports(20);
      setReports(items);
      const target = items.find((item) => item.report_id === preferredReportId) || items[0];
      if (target) {
        const detail = await analyticsApi.getFridayReport(target.report_id);
        setSelectedReport(detail);
      } else {
        setSelectedReport(undefined);
      }
    } catch (error) {
      setReportError(String((error as Error)?.message || error));
    } finally {
      setReportsLoading(false);
    }
  };

  const openReport = async (reportId: string) => {
    setReportsLoading(true);
    setReportError('');
    try {
      const detail = await analyticsApi.getFridayReport(reportId);
      setSelectedReport(detail);
    } catch (error) {
      setReportError(String((error as Error)?.message || error));
    } finally {
      setReportsLoading(false);
    }
  };

  const runReport = async (reportType: 'weekly' | 'monthly') => {
    setReportRunning(reportType);
    setReportError('');
    try {
      const generated = await analyticsApi.runFridayReport(reportType);
      setSelectedReport(generated);
      await loadReports(generated.report_id);
    } catch (error) {
      setReportError(String((error as Error)?.message || error));
    } finally {
      setReportRunning('');
    }
  };

  useEffect(() => {
    loadReports();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const updateConversation = (conversationId: string, updater: (conversation: FridayConversation) => FridayConversation) => {
    setConversations((prev) =>
      prev.map((item) => {
        if (item.id !== conversationId) {
          return item;
        }
        return updater(item);
      }),
    );
  };

  const createConversation = () => {
    const next = makeConversation('新会话');
    setConversations((prev) => [next, ...prev]);
    setActiveConversationId(next.id);
    setInputValue('');
    setErrorMessage('');
  };

  const clearActiveConversation = () => {
    if (!activeConversation || sending) {
      return;
    }
    updateConversation(activeConversation.id, (item) => ({
      ...item,
      updated_at: new Date().toISOString(),
      messages: [],
    }));
    setErrorMessage('');
    setInputValue('');
  };

  const sendMessage = async (message: string) => {
    const text = (message || '').trim();
    if (!text || sending) {
      return;
    }

    let current = activeConversation;
    if (!current) {
      current = makeConversation(trimTitle(text));
      setConversations((prev) => [current as FridayConversation, ...prev]);
      setActiveConversationId((current as FridayConversation).id);
    }

    const nowIso = new Date().toISOString();
    const userMessage: FridayMessage = {
      id: `msg_u_${Math.random().toString(36).slice(2, 10)}`,
      role: 'user',
      content: text,
      created_at: nowIso,
    };

    const assistantMessageId = `msg_a_${Math.random().toString(36).slice(2, 10)}`;
    const assistantMessage: FridayMessage = {
      id: assistantMessageId,
      role: 'assistant',
      content: '',
      created_at: nowIso,
      streaming: true,
    };

    const historyForRequest = parseMessageHistory([...(current.messages || []), userMessage]);

    updateConversation(current.id, (item) => ({
      ...item,
      title: item.messages.length === 0 ? trimTitle(text) : item.title,
      updated_at: nowIso,
      messages: [...item.messages, userMessage, assistantMessage],
    }));

    setInputValue('');
    setSending(true);
    setErrorMessage('');

    try {
      await analyticsApi.streamFridayChat(
        {
          message: text,
          conversation_id: current.id,
          history: historyForRequest,
          context_overrides: {
            container: selectedContainer,
            request_id: filters.requestId || undefined,
            rid: filters.requestId || undefined,
          },
        },
        (event) => {
          updateConversation(current!.id, (item) => {
            const nextMessages = item.messages.map((msg) => {
              if (msg.id !== assistantMessageId) {
                return msg;
              }

              if (event.type === 'token') {
                return {
                  ...msg,
                  progress: undefined,
                  content: `${msg.content}${String(event.data.text || '')}`,
                };
              }
              if (event.type === 'progress') {
                return {
                  ...msg,
                  progress: String(event.data.message || '正在处理中...'),
                };
              }
              if (event.type === 'evidence') {
                return {
                  ...msg,
                  evidence: event.data as FridayEvidenceItem,
                };
              }
              if (event.type === 'actions') {
                const items = Array.isArray(event.data.items) ? (event.data.items as FridayAction[]) : [];
                return {
                  ...msg,
                  actions: items,
                };
              }
              if (event.type === 'error') {
                return {
                  ...msg,
                  progress: undefined,
                  error: String(event.data.message || 'Friday 返回异常'),
                  streaming: false,
                };
              }
              if (event.type === 'done') {
                return {
                  ...msg,
                  progress: undefined,
                  streaming: false,
                };
              }
              return msg;
            });

            return {
              ...item,
              updated_at: new Date().toISOString(),
              messages: nextMessages,
            };
          });
        },
      );
    } catch (error) {
      const detail = String((error as Error)?.message || error);
      setErrorMessage(detail);
      updateConversation(current.id, (item) => ({
        ...item,
        updated_at: new Date().toISOString(),
        messages: item.messages.map((msg) =>
          msg.id === assistantMessageId
            ? {
                ...msg,
                streaming: false,
                error: detail,
              }
            : msg,
        ),
      }));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="friday-workspace">
      <Card className="friday-session-panel" title="会话列表" extra={<Button onClick={createConversation}>新建会话</Button>}>
        <div className="friday-session-list">
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              className={`friday-session-item ${conversation.id === activeConversationId ? 'active' : ''}`}
              onClick={() => {
                setActiveConversationId(conversation.id);
                setErrorMessage('');
              }}
            >
              <div className="friday-session-title">{conversation.title || '新会话'}</div>
              <div className="friday-session-time">{conversation.updated_at.replace('T', ' ').slice(0, 19)}</div>
            </button>
          ))}
        </div>
      </Card>

      <div className="friday-chat-panel">
        <Card
          title="Friday 对话诊断"
          extra={(
            <div className="friday-card-extra">
              <Tag>默认范围：最近 30 天</Tag>
              <Button
                onClick={clearActiveConversation}
                disabled={sending || !activeConversation || activeConversation.messages.length === 0}
              >
                清空当前会话
              </Button>
            </div>
          )}
        >
          <div className="friday-quick-actions">
            {QUICK_QUESTIONS.map((item) => (
              <Button key={item} onClick={() => sendMessage(item)} disabled={sending}>
                {item}
              </Button>
            ))}
          </div>

          {errorMessage ? <Alert className="table-gap-top" type="error" showIcon message={errorMessage} /> : null}

          <div className="friday-chat-list">
            {(activeConversation?.messages || []).map((message) => (
              <div key={message.id} className={`friday-chat-item ${message.role === 'user' ? 'user' : 'assistant'}`}>
                <div className="friday-chat-meta">{message.role === 'user' ? '你' : 'Friday'}</div>
                <div className="friday-chat-bubble">
                  {message.streaming && message.progress ? <div className="friday-chat-progress">{message.progress}</div> : null}
                  {message.content ? (
                    message.role === 'assistant' ? (
                      <div className="friday-chat-markdown">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {message.content}
                        </ReactMarkdown>
                      </div>
                    ) : (
                      <div className="friday-chat-content">{message.content}</div>
                    )
                  ) : (
                    <div className="friday-chat-content">{message.streaming ? '正在分析，请稍候...' : '-'}</div>
                  )}
                  {message.error ? <Alert className="table-gap-top" type="error" showIcon message={message.error} /> : null}
                  {message.evidence ? <EvidenceView evidence={message.evidence} /> : null}
                  {message.actions && message.actions.length > 0 ? (
                    <div className="friday-action-row">
                      {message.actions.map((action, idx) => (
                        <Button key={`${message.id}-action-${idx}`} type="primary" onClick={() => onRunAction(action)}>
                          {action.label || '执行动作'}
                        </Button>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </div>

          <div className="friday-input-row">
            <div className="friday-input-controls">
              <Select
                value={selectedEnv}
                options={FRIDAY_ENV_OPTIONS}
                className="friday-env-select"
                onChange={(value: FridayEnv) => setSelectedEnv(value)}
              />
            </div>
            <Input.TextArea
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              placeholder="输入你要诊断的问题，例如：最近为何调用变慢？"
              autoSize={{ minRows: 2, maxRows: 6 }}
            />
            <Button
              className="action-btn-primary"
              type="primary"
              loading={sending}
              onClick={() => sendMessage(inputValue)}
            >
              发送
            </Button>
          </div>
        </Card>
      </div>

      <Card
        className="friday-report-panel"
        title="周报 / 月报"
        loading={reportsLoading}
        extra={(
          <div className="friday-card-extra">
            <Button loading={reportRunning === 'weekly'} onClick={() => runReport('weekly')}>生成周报</Button>
            <Button loading={reportRunning === 'monthly'} onClick={() => runReport('monthly')}>生成月报</Button>
            <Button onClick={() => loadReports()}>刷新</Button>
          </div>
        )}
      >
        {reportError ? <Alert className="table-gap-bottom" type="error" showIcon message={reportError} /> : null}
        <Table
          className="friday-report-table"
          size="small"
          rowKey={(row: FridayReport) => row.report_id}
          pagination={false}
          scroll={{ x: 'max-content', y: 220 }}
          dataSource={reports}
          columns={[
            {
              title: '报告',
              dataIndex: 'title',
              key: 'title',
              width: 180,
              render: (value: string, row: FridayReport) => (
                <Button type="link" onClick={() => openReport(row.report_id)}>
                  {value || row.report_id}
                </Button>
              ),
            },
            { title: '类型', dataIndex: 'report_type', key: 'report_type', width: 90 },
            {
              title: '生成时间',
              dataIndex: 'generated_at',
              key: 'generated_at',
              width: 150,
              render: formatReportTime,
            },
          ]}
        />

        {selectedReport ? (
          <div className="friday-report-detail">
            <div className="summary-row">
              <Tag>{selectedReport.report_type === 'monthly' ? '月报' : '周报'}</Tag>
              <Tag>状态: {selectedReport.status || 'unknown'}</Tag>
              <Tag>失败请求: {formatCount(selectedReport.summary?.failed_request_count)}</Tag>
              <Tag>慢 LLM: {formatCount(selectedReport.summary?.slow_llm_count)}</Tag>
            </div>
            <div className="friday-report-period">
              {formatReportTime(selectedReport.period_start)} 至 {formatReportTime(selectedReport.period_end)}
            </div>
            {selectedReport.markdown ? (
              <div className="friday-report-markdown">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {selectedReport.markdown}
                </ReactMarkdown>
              </div>
            ) : (
              <pre className="raw-json">{JSON.stringify(selectedReport, null, 2)}</pre>
            )}
          </div>
        ) : (
          <Alert className="table-gap-top" type="info" showIcon message="暂无报告，可手动生成周报或月报。" />
        )}
      </Card>
    </div>
  );
};
