import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConversationView } from './ConversationView';
import { useConversationController } from './useConversationController';
import { conversationApi } from './conversationApi';

/**
 * 会话功能测试（R1-CONV-01 UI / FIX-P2-FRONTEND-01）。
 *
 * 场景:创建会话、流式消息、刷新恢复、停止、反馈提交/撤回、错误状态。
 */

function Harness({ conversationId }: { conversationId?: string }) {
  const controller = useConversationController({ conversationId });
  return <ConversationView controller={controller} />;
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

  it('stops a running stream and marks the message stopped', async () => {
    const streamSpy = vi.spyOn(conversationApi, 'stream').mockImplementationOnce(
      async function* () {
        yield { event: 'content_delta', data: { content: '部分' } };
        // 不发送 done:等待停止
        await new Promise(() => undefined);
      },
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

    await waitFor(() => {
      expect(screen.getByText(/stopped/)).toBeInTheDocument();
    });
    streamSpy.mockRestore();
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
