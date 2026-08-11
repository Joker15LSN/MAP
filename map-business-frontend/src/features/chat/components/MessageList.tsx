import { Button, Tag } from '@agentscope-ai/design';
import type { Ref } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { RequestDetail } from 'map-tree-core';
import type { ChatMessage, ChatMode } from '../../../api/types';
import { extractAgentNamesFromDetail } from '../chatReducer';
import { QUICK_QUESTIONS } from '../constants';

export interface MessageListProps {
  messages: ChatMessage[];
  isStreaming: boolean;
  detail?: RequestDetail;
  chatMode: ChatMode;
  /** flow 模式是否有策略命中数据 */
  hasFlowHitData: boolean;
  /** 是否有可展示的来源条目 */
  hasSourceItems: boolean;
  onViewTrace: () => void;
  onViewSource: () => void;
  onViewFlow: () => void;
  onQuickSend: (query: string) => void;
  /** 滚动容器 ref,供父级实现自动滚动 */
  listRef?: Ref<HTMLDivElement>;
}

/** 聊天消息流:用户/助手气泡、Markdown 渲染、子智能体标签与动作按钮 */
export default function MessageList({
  messages,
  isStreaming,
  detail,
  chatMode,
  hasFlowHitData,
  hasSourceItems,
  onViewTrace,
  onViewSource,
  onViewFlow,
  onQuickSend,
  listRef,
}: MessageListProps) {
  const hasTraceData = Boolean(
    detail && (detail.agent_events.length > 0 || detail.agent_timeline.length > 0 || detail.tool_calls.length > 0),
  );

  return (
    <div className="chat-message-list" ref={listRef}>
      {messages.length === 0 ? (
        <div className="empty-hint">
          <div>输入问题开始问答。</div>
          <div className="quick-list">
            {QUICK_QUESTIONS.map((item) => (
              <button key={item} className="quick-item" onClick={() => onQuickSend(item)}>
                {item}
              </button>
            ))}
          </div>
        </div>
      ) : null}
      {messages.map((item, index) => (
        <div key={item.id} className={`message-row ${item.role}`}>
          <div className="message-role">{item.role === 'user' ? '你' : 'MAP'}</div>
          <div className="message-content">
            {item.role === 'assistant' ? (
              <div className="message-markdown">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    a: ({ ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
                  }}
                >
                  {item.content || (isStreaming ? '思考中...' : '')}
                </ReactMarkdown>
              </div>
            ) : (
              item.content
            )}
          </div>
          {item.role === 'assistant' && (item.content || isStreaming) ? (
            <div className="assistant-actions-row">
              <Button size="small" onClick={onViewTrace} disabled={!hasTraceData}>
                思考过程
              </Button>
              <Button size="small" onClick={onViewSource} disabled={!hasTraceData && !hasSourceItems}>
                查看来源
              </Button>
              {chatMode === 'flow' ? (
                <Button size="small" onClick={onViewFlow} disabled={!hasFlowHitData}>
                  策略命中
                </Button>
              ) : null}
              <Tag>
                {(() => {
                  const agentNames =
                    item.agentNames && item.agentNames.length > 0
                      ? item.agentNames
                      : index === messages.length - 1
                        ? extractAgentNamesFromDetail(detail)
                        : [];
                  return `子智能体：${agentNames.length ? agentNames.join(' / ') : 'MasterAgent 任务调度'}`;
                })()}
              </Tag>
              {index === messages.length - 1 ? (
                <Tag>{detail?.request.status || (isStreaming ? 'running' : 'success')}</Tag>
              ) : null}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
