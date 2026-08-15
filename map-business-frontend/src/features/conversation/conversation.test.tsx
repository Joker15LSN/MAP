import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConversationView } from './ConversationView';
import { useConversationController } from './useConversationController';
import { conversationApi } from './conversationApi';
import type { MessageView } from './conversationApi';

/**
 * 会话功能测试（R1-CONV-01 UI / FIX-P2-FRONTEND-01 / S4-05）。
 *
 * 场景:创建会话、流式消息、刷新恢复、停止(含服务端 stop 接口、
 * completed 竞态、本地 abort 兜底、generation 守卫)、反馈提交/撤回、
 * 错误状态。
 */

function Harness({ conversationId }: { conversationId?: string }) {
  const controller = useConversationController({ conversationId });
  return <ConversationView controller={controller} />;
}

function assistantMessage(overrides: Partial<MessageView> = {}): MessageView {
  return {
    id: 'm-new',
    conversation_id: 'c-1',
    role: 'assistant',
    status: 'stopped',
    content: '部分',
    request_id: 'req-1',
    task_id: null,
    decision: null,
    stream_error: 'STREAM_ABORTED',
    error_message: 'stopped by user',
    fallback_used: false,
    created_at: '2026-08-09T00:00:00Z',
    completed_at: '2026-08-09T00:00:01Z',
    ...overrides,
  };
}

describe('conversation feature', () => {
  it('creates a conversation and streams content deltas until done', async () => {
    render(<Harness />);

    // empty state -> create
    expect(screen.getByTestId('conversation-empty')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /新\s*建\s*会\s*话/ }));

    await waitFor(() => {
      expect(screen.getByLabelText('输入问题')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    // streaming accumulates deltas and completes
    await waitFor(
      () => {
        expect(screen.getAllByText('你好').length).toBeGreaterThan(0);
      },
      { timeout: 3000 },
    );
    expect(screen.getByText(/completed/)).toBeInTheDocument();
  });

  it('restores a conversation (with feedback) after refresh', async () => {
    render(<Harness conversationId="c-1" />);

    await waitFor(() => {
      expect(screen.getAllByText('你好').length).toBeGreaterThan(0);
    });
    // assistant message shows the completed tag and a feedback button
    const buttons = screen.getAllByRole('button', { name: /有帮助/ });
    expect(buttons.length).toBeGreaterThan(0);
  });

  it('shows a standard error for a missing conversation', async () => {
    render(<Harness conversationId="c-missing" />);
    await waitFor(() => {
      expect(screen.getByTestId('conversation-error')).toBeInTheDocument();
    });
    expect(screen.getByTestId('conversation-error').textContent).toContain('RESOURCE_NOT_FOUND');
  });

  it('submits and withdraws feedback on a completed assistant message', async () => {
    render(<Harness conversationId="c-1" />);
    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /有帮助/ }).length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getAllByRole('button', { name: '有帮助' })[0]);
    await waitFor(() => {
      expect(screen.getByText(/已\s*赞/)).toBeInTheDocument();
    });

    // withdraw
    fireEvent.click(screen.getByRole('button', { name: '撤回反馈' }));
    await waitFor(() => {
      expect(screen.queryByText(/已\s*赞/)).not.toBeInTheDocument();
    });
  });

  it('opens the reason dialog for unhelpful feedback', async () => {
    render(<Harness conversationId="c-1" />);
    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /没有帮助/ }).length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getAllByRole('button', { name: '没有帮助' })[0]);
    expect(screen.getByRole('dialog', { name: '反馈原因' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }));
    expect(screen.queryByRole('dialog', { name: '反馈原因' })).not.toBeInTheDocument();
  });

  it('stops a running stream by calling the server stop API with the real message id', async () => {
    const streamSpy = vi.spyOn(conversationApi, 'stream').mockImplementationOnce(
      async function* () {
        yield { event: 'start', data: { message_id: 'm-new', conversation_id: 'c-1' } };
        yield { event: 'content_delta', data: { content: '部分' } };
        // 不发送 done:等待停止
        await new Promise(() => undefined);
      },
    );
    const stopSpy = vi
      .spyOn(conversationApi, 'stop')
      .mockResolvedValueOnce(assistantMessage({ status: 'stopped', content: '部分' }));

    render(<Harness conversationId="c-1" />);
    await waitFor(() => {
      expect(screen.getByLabelText('输入问题')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    await waitFor(() => {
      expect(screen.getByTestId('stop-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('stop-button'));

    await waitFor(() => {
      expect(screen.getByText(/stopped/)).toBeInTheDocument();
    });
    // 关键断言:停止必须先打到服务端,且使用 start 里的真实 message_id。
    expect(stopSpy).toHaveBeenCalledWith('m-new');
    expect(stopSpy).toHaveBeenCalledTimes(1);

    streamSpy.mockRestore();
    stopSpy.mockRestore();
  });

  it('keeps completed when the server stop returns completed (done won the race)', async () => {
    const streamSpy = vi.spyOn(conversationApi, 'stream').mockImplementationOnce(
      async function* () {
        yield { event: 'start', data: { message_id: 'm-new', conversation_id: 'c-1' } };
        yield { event: 'content_delta', data: { content: '部分' } };
        await new Promise(() => undefined);
      },
    );
    const stopSpy = vi.spyOn(conversationApi, 'stop').mockResolvedValueOnce(
      assistantMessage({
        status: 'completed',
        content: '竞态已完成',
        stream_error: null,
        error_message: null,
      }),
    );

    render(<Harness conversationId="c-1" />);
    await waitFor(() => {
      expect(screen.getByLabelText('输入问题')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));
    await waitFor(() => {
      expect(screen.getByTestId('stop-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('stop-button'));

    // 服务端 done 先赢并显式返回 completed:终态不得回退成 stopped。
    await waitFor(() => {
      expect(screen.getByText('竞态已完成')).toBeInTheDocument();
    });
    expect(stopSpy).toHaveBeenCalledWith('m-new');
    expect(screen.queryByText(/stopped/)).not.toBeInTheDocument();

    streamSpy.mockRestore();
    stopSpy.mockRestore();
  });

  it('falls back to local abort when the server stop API fails', async () => {
    const streamSpy = vi.spyOn(conversationApi, 'stream').mockImplementationOnce(
      async function* () {
        yield { event: 'start', data: { message_id: 'm-new', conversation_id: 'c-1' } };
        yield { event: 'content_delta', data: { content: '部分' } };
        await new Promise(() => undefined);
      },
    );
    const stopSpy = vi
      .spyOn(conversationApi, 'stop')
      .mockRejectedValueOnce(new Error('network down'));

    render(<Harness conversationId="c-1" />);
    await waitFor(() => {
      expect(screen.getByLabelText('输入问题')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));
    await waitFor(() => {
      expect(screen.getByTestId('stop-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('stop-button'));

    // 服务端 stop 失败 -> 本地 abort 兜底,消息仍标记为 stopped。
    await waitFor(() => {
      expect(screen.getByText(/stopped/)).toBeInTheDocument();
    });
    expect(stopSpy).toHaveBeenCalledWith('m-new');

    streamSpy.mockRestore();
    stopSpy.mockRestore();
  });

  it('ignores a stale done after stop commits stopped (generation guard)', async () => {
    const streamSpy = vi.spyOn(conversationApi, 'stream').mockImplementationOnce(
      async function* () {
        yield { event: 'start', data: { message_id: 'm-new', conversation_id: 'c-1' } };
        yield { event: 'content_delta', data: { content: '部分' } };
        // 延迟后仍送达一个迟到的 done:必须被 generation 守卫丢弃。
        await new Promise((resolve) => setTimeout(resolve, 300));
        yield {
          event: 'done',
          data: { message_id: 'm-new', status: 'completed', content: '迟到的完成' },
        };
      },
    );
    const stopSpy = vi
      .spyOn(conversationApi, 'stop')
      .mockResolvedValueOnce(assistantMessage({ status: 'stopped', content: '部分' }));

    render(<Harness conversationId="c-1" />);
    await waitFor(() => {
      expect(screen.getByLabelText('输入问题')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));
    await waitFor(() => {
      expect(screen.getByTestId('stop-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('stop-button'));

    // stop 已提交 stopped;迟到的 completed 不得覆盖它。
    await waitFor(
      () => {
        expect(screen.getByText(/stopped/)).toBeInTheDocument();
      },
      { timeout: 2000 },
    );
    expect(stopSpy).toHaveBeenCalledWith('m-new');
    expect(screen.queryByText('迟到的完成')).not.toBeInTheDocument();

    streamSpy.mockRestore();
    stopSpy.mockRestore();
  });

  it('marks the message failed when the stream ends without done', async () => {
    const streamSpy = vi
      .spyOn(conversationApi, 'stream')
      .mockImplementationOnce(async function* () {
        yield { event: 'content_delta', data: { content: '半截' } };
      });
    render(<Harness conversationId="c-1" />);
    await waitFor(() => {
      expect(screen.getByLabelText('输入问题')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    await waitFor(() => {
      expect(screen.getByText(/failed/)).toBeInTheDocument();
    });
    expect(screen.getByText(/STREAM_EOF_WITHOUT_DONE/)).toBeInTheDocument();
    streamSpy.mockRestore();
  });
});