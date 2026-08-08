import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import type { RequestDetail } from 'map-tree-core';
import MessageList from './MessageList';
import type { ChatMessage } from '../../../api/types';

const baseProps = {
  messages: [],
  isStreaming: false,
  detail: undefined,
  chatMode: 'global' as const,
  hasFlowHitData: false,
  hasSourceItems: false,
  onViewTrace: () => {},
  onViewSource: () => {},
  onViewFlow: () => {},
  onQuickSend: () => {},
  listRef: undefined,
};

describe('MessageList 消息流', () => {
  it('空消息时展示输入提示与快捷问题列表', () => {
    render(<MessageList {...baseProps} />);
    expect(screen.getByText(/输入问题开始问答/)).toBeTruthy();
    expect(screen.getByText('介绍一下中国杭州')).toBeTruthy();
    expect(screen.getAllByRole('button').length).toBeGreaterThan(0);
  });

  it('点击快捷问题触发 onQuickSend', () => {
    const onQuickSend = vi.fn();
    render(<MessageList {...baseProps} onQuickSend={onQuickSend} />);
    fireEvent.click(screen.getByText('杭州有哪些代表性产业？'));
    expect(onQuickSend).toHaveBeenCalledTimes(1);
    expect(onQuickSend).toHaveBeenCalledWith('杭州有哪些代表性产业？');
  });

  it('渲染用户与助手消息内容', () => {
    const messages: ChatMessage[] = [
      { id: 'u1', role: 'user', content: '杭州天气如何？' },
      { id: 'a1', role: 'assistant', content: '今天晴，适合出行。' },
    ];
    render(<MessageList {...baseProps} messages={messages} />);
    expect(screen.getByText('杭州天气如何？')).toBeTruthy();
    expect(screen.getByText(/今天晴，适合出行。/)).toBeTruthy();
  });

  it('助手消息附带操作按钮,点击思考过程触发 onViewTrace', () => {
    const onViewTrace = vi.fn();
    const detail: RequestDetail = {
      request: { request_id: 'r1', duration_s: 1, token_total: 1, status: 'success' },
      agent_timeline: [],
      agent_events: [{ action: 'tool_call' }],
      tool_calls: [],
      summary: { agent_event_count: 1, tool_call_count: 0 },
    };
    const messages: ChatMessage[] = [
      { id: 'a1', role: 'assistant', content: '回答内容', agentNames: ['general_qa_agent'] },
    ];
    render(<MessageList {...baseProps} messages={messages} detail={detail} onViewTrace={onViewTrace} />);
    fireEvent.click(screen.getByRole('button', { name: '思考过程' }));
    expect(onViewTrace).toHaveBeenCalledTimes(1);
  });

  it('flow 模式下展示策略命中按钮', () => {
    const onViewFlow = vi.fn();
    const messages: ChatMessage[] = [
      { id: 'a1', role: 'assistant', content: '回答内容', agentNames: [] },
    ];
    render(
      <MessageList
        {...baseProps}
        messages={messages}
        chatMode="flow"
        hasFlowHitData={true}
        onViewFlow={onViewFlow}
      />,
    );
    const flowBtn = screen.getByText('策略命中');
    expect(flowBtn).toBeTruthy();
    fireEvent.click(flowBtn);
    expect(onViewFlow).toHaveBeenCalledTimes(1);
  });

  it('无溯源数据时思考过程按钮禁用', () => {
    const messages: ChatMessage[] = [{ id: 'a1', role: 'assistant', content: '回答内容' }];
    render(<MessageList {...baseProps} messages={messages} />);
    const traceBtn = screen.getByRole('button', { name: '思考过程' });
    expect((traceBtn as HTMLButtonElement).disabled).toBe(true);
  });
});
