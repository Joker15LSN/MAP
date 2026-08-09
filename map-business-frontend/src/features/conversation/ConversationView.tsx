import { useState } from 'react';
import { Button, Input, Tag } from '@agentscope-ai/design';
import type { MessageView } from './conversationApi';
import type { UseConversationControllerOptions } from './useConversationController';
import { useConversationController } from './useConversationController';

/**
 * 会话视图（R1-CONV-01 UI / FIX-P2-FRONTEND-01）。
 *
 * - 创建/恢复会话,刷新后恢复已完成/failed/stopped 消息;
 * - 流式期间显示停止按钮,错误状态可见;
 * - 每个 completed assistant 消息提供键盘可访问的反馈按钮与原因/纠错弹层。
 */

interface ConversationViewProps extends UseConversationControllerOptions {
  controller: ReturnType<typeof useConversationController>;
}

function MessageItem({
  message,
  onFeedback,
  onWithdraw,
  feedback,
  feedbackSaving,
}: {
  message: MessageView;
  onFeedback: (rating: 'helpful' | 'unhelpful') => void;
  onWithdraw: () => void;
  feedback: { rating: string } | null | undefined;
  feedbackSaving: boolean;
}) {
  return (
    <div className={`message-row role-${message.role}`} data-testid={`message-${message.role}`}>
      <div className="message-bubble">
        <p className="message-content">{message.content || (message.status === 'streaming' ? '…' : '')}</p>
        {message.role === 'assistant' ? (
          <div className="message-meta">
            <Tag>
              {message.status}
              {message.stream_error ? ` / ${message.stream_error}` : ''}
            </Tag>
            {message.status === 'completed' ? (
              <span className="feedback-actions">
                <Button
                  size="small"
                  aria-label="有帮助"
                  disabled={feedbackSaving}
                  onClick={() => onFeedback('helpful')}
                >
                  👍 {feedback?.rating === 'helpful' ? '已赞' : ''}
                </Button>
                <Button
                  size="small"
                  aria-label="没有帮助"
                  disabled={feedbackSaving}
                  onClick={() => onFeedback('unhelpful')}
                >
                  👎 {feedback?.rating === 'unhelpful' ? '已踩' : ''}
                </Button>
                {feedback ? (
                  <Button size="small" aria-label="撤回反馈" onClick={onWithdraw}>
                    撤回
                  </Button>
                ) : null}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function ConversationView({ controller }: ConversationViewProps) {
  const { state, input, setInput, create, send, stop, submitFeedback, withdrawFeedback } =
    controller;
  const [showReasons, setShowReasons] = useState<string | null>(null);
  const [reasonOther, setReasonOther] = useState('');
  const [correction, setCorrection] = useState('');
  const [reasonCodes, setReasonCodes] = useState<string[]>([]);
  const [pendingRating, setPendingRating] = useState<'helpful' | 'unhelpful' | null>(null);

  const submit = () => {
    void send();
  };

  const openFeedback = (messageId: string, rating: 'helpful' | 'unhelpful') => {
    if (rating === 'helpful') {
      void submitFeedback(messageId, { rating });
      return;
    }
    setPendingRating(rating);
    setShowReasons(messageId);
    setReasonCodes([]);
    setReasonOther('');
    setCorrection('');
  };

  const confirmUnhelpful = () => {
    if (showReasons && pendingRating) {
      void submitFeedback(showReasons, {
        rating: pendingRating,
        reasonCodes,
        reasonOther: reasonCodes.includes('other') ? reasonOther : undefined,
        correctionText: correction || undefined,
      });
      setShowReasons(null);
      setPendingRating(null);
    }
  };

  return (
    <div className="conversation-view" data-testid="conversation-view">
      {state.phase === 'error' ? (
        <div className="conversation-error" role="alert" data-testid="conversation-error">
          {state.error}
          <Button size="small" onClick={() => void create('global')}>
            重试
          </Button>
        </div>
      ) : null}

      {!state.conversation ? (
        <div className="conversation-empty" data-testid="conversation-empty">
          <p>还没有会话</p>
          <Button onClick={() => void create('global')}>新建会话</Button>
        </div>
      ) : (
        <>
          <div className="conversation-list">
            {state.messages.length === 0 && state.phase !== 'streaming' ? (
              <p className="conversation-empty" data-testid="conversation-empty">
                输入问题开始对话
              </p>
            ) : (
              state.messages.map((message) => (
                <MessageItem
                  key={message.id}
                  message={message}
                  feedback={state.feedbackByMessage[message.id]}
                  feedbackSaving={Boolean(state.feedbackSaving[message.id])}
                  onFeedback={(rating) => openFeedback(message.id, rating)}
                  onWithdraw={() => void withdrawFeedback(message.id)}
                />
              ))
            )}
          </div>

          <div className="conversation-input">
            <Input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onPressEnter={() => submit()}
              placeholder="输入问题…"
              aria-label="输入问题"
            />
            {state.phase === 'streaming' ? (
              <Button onClick={() => void stop()} data-testid="stop-button">
                停止
              </Button>
            ) : (
              <Button type="primary" onClick={submit} disabled={!input.trim()}>
                发送
              </Button>
            )}
          </div>
        </>
      )}

      {showReasons ? (
        <div className="feedback-modal" role="dialog" aria-label="反馈原因">
          <p>为什么这个回答没有帮助？</p>
          {['incorrect', 'outdated', 'no_evidence', 'not_relevant', 'unsafe', 'too_verbose', 'tool_failed', 'other'].map(
            (code) => (
              <label key={code}>
                <input
                  type="checkbox"
                  checked={reasonCodes.includes(code)}
                  onChange={(event) =>
                    setReasonCodes((prev) =>
                      event.target.checked
                        ? [...prev, code]
                        : prev.filter((item) => item !== code),
                    )
                  }
                />
                {code}
              </label>
            ),
          )}
          {reasonCodes.includes('other') ? (
            <Input
              value={reasonOther}
              onChange={(event) => setReasonOther(event.target.value)}
              placeholder="其他原因"
            />
          ) : null}
          <Input
            value={correction}
            onChange={(event) => setCorrection(event.target.value)}
            placeholder="纠错内容(可选)"
          />
          <Button type="primary" onClick={confirmUnhelpful}>
            提交
          </Button>
          <Button onClick={() => setShowReasons(null)}>取消</Button>
        </div>
      ) : null}
    </div>
  );
}
