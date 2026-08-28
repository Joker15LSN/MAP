import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConversationView } from './ConversationView';
import { useConversationController } from './useConversationController';
import { runApi } from '../../api/runApi';
import type { RunEventEnvelope } from '../../api/runApi';

/**
 * 会话功能测试（Step 4 / PR-F1+F2）。
 *
 * 场景:创建会话、canonical Run 事件流投影、刷新恢复、停止(权威
 * cancelRun + 本地 abort 兜底)、反馈提交/撤回、错误状态。
 */

function Harness({ conversationId }: { conversationId?: string }) {
  const controller = useConversationController({ conversationId });
  return <ConversationView controller={controller} />;
}

function envelope(
  seq: number,
  type: string,
  data: Record<string, unknown> = {},
  runId = 'run-1',
): RunEventEnvelope {
  return {
    schema_version: 1,
    schema_minor: 0,
    event_id: `ev-${seq}`,
    run_id: runId,
    seq,
    type,
    occurred_at: '2026-08-09T00:00:00Z',
    workspace_id: '00000000-0000-0000-0000-000000000001',
    data,
  };
}

async function createConversationAndOpenInput() {
  window.localStorage.clear();
  render(<Harness />);
  expect(screen.getByTestId('conversation-empty')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /新\s*建\s*会\s*话/ }));
  await waitFor(() => {
    expect(screen.getByLabelText('输入问题')).toBeInTheDocument();
  });
}

describe('conversation feature', () => {
  it('creates a conversation and projects run events until run.completed', async () => {
    await createConversationAndOpenInput();

    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    await waitFor(
      () => {
        expect(screen.getAllByText('你好').length).toBeGreaterThan(0);
      },
      { timeout: 3000 },
    );
    expect(screen.getByText(/completed/)).toBeInTheDocument();
  });

  it('restores a conversation from run events after refresh', async () => {
    render(<Harness conversationId="c-1" />);

    await waitFor(() => {
      expect(screen.getAllByText('你好').length).toBeGreaterThan(0);
    });
    expect(screen.getByText(/completed/)).toBeInTheDocument();
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

  it('stops a running stream by calling the authoritative cancelRun API', async () => {
    const createTurnSpy = vi.spyOn(runApi, 'createTurn').mockResolvedValueOnce({
      run_id: 'run-stop',
      user_message_id: 'um-new',
      assistant_message_id: 'm-new',
      status: 'queued',
      replayed: false,
    });
    const replaySpy = vi.spyOn(runApi, 'replayRunEvents').mockImplementationOnce(
      async function* () {
        yield envelope(1, 'run.started', {}, 'run-stop');
        yield envelope(2, 'message.delta', { content: '部分' }, 'run-stop');
        // 不发送终态:等待停止。
        await new Promise(() => undefined);
      },
    );
    const cancelSpy = vi
      .spyOn(runApi, 'cancelRun')
      .mockResolvedValueOnce({ run_id: 'run-stop', accepted: true, status: 'queued' });

    await createConversationAndOpenInput();
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    await waitFor(() => {
      expect(screen.getByTestId('stop-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('stop-button'));

    await waitFor(() => {
      expect(screen.getByText(/stopped/)).toBeInTheDocument();
    });
    expect(cancelSpy).toHaveBeenCalledWith('run-stop', 'stopped by user');

    createTurnSpy.mockRestore();
    replaySpy.mockRestore();
    cancelSpy.mockRestore();
  });

  it('falls back to local abort when cancelRun fails', async () => {
    const createTurnSpy = vi.spyOn(runApi, 'createTurn').mockResolvedValueOnce({
      run_id: 'run-stop',
      user_message_id: 'um-new',
      assistant_message_id: 'm-new',
      status: 'queued',
      replayed: false,
    });
    const replaySpy = vi.spyOn(runApi, 'replayRunEvents').mockImplementationOnce(
      async function* () {
        yield envelope(1, 'run.started', {}, 'run-stop');
        yield envelope(2, 'message.delta', { content: '部分' }, 'run-stop');
        await new Promise(() => undefined);
      },
    );
    const cancelSpy = vi
      .spyOn(runApi, 'cancelRun')
      .mockRejectedValueOnce(new Error('network down'));

    await createConversationAndOpenInput();
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    await waitFor(() => {
      expect(screen.getByTestId('stop-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('stop-button'));

    await waitFor(() => {
      expect(screen.getByText(/stopped/)).toBeInTheDocument();
    });
    expect(cancelSpy).toHaveBeenCalledWith('run-stop', 'stopped by user');

    createTurnSpy.mockRestore();
    replaySpy.mockRestore();
    cancelSpy.mockRestore();
  });

  it('stop clicked before run id arrives still calls cancelRun once the turn is created', async () => {
    const createTurnSpy = vi.spyOn(runApi, 'createTurn').mockImplementationOnce(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
      return {
        run_id: 'run-stop',
        user_message_id: 'um-new',
        assistant_message_id: 'm-new',
        status: 'queued',
        replayed: false,
      };
    });
    const replaySpy = vi.spyOn(runApi, 'replayRunEvents').mockImplementationOnce(
      async function* () {
        yield envelope(1, 'run.started', {}, 'run-stop');
        await new Promise(() => undefined);
      },
    );
    const cancelSpy = vi
      .spyOn(runApi, 'cancelRun')
      .mockResolvedValueOnce({ run_id: 'run-stop', accepted: true, status: 'queued' });

    await createConversationAndOpenInput();
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    await waitFor(() => {
      expect(screen.getByTestId('stop-button')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('stop-button'));

    await waitFor(() => {
      expect(cancelSpy).toHaveBeenCalledWith('run-stop', 'stopped by user');
    });
    await waitFor(() => {
      expect(screen.getByText(/stopped/)).toBeInTheDocument();
    });

    createTurnSpy.mockRestore();
    replaySpy.mockRestore();
    cancelSpy.mockRestore();
  });

  it('marks the message failed when createTurn fails', async () => {
    const createTurnSpy = vi
      .spyOn(runApi, 'createTurn')
      .mockRejectedValueOnce(new Error('turn create failed'));

    await createConversationAndOpenInput();
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    await waitFor(() => {
      expect(screen.getByText(/failed/)).toBeInTheDocument();
    });
    expect(screen.getByText(/STREAM_CORE_ERROR/)).toBeInTheDocument();
    createTurnSpy.mockRestore();
  });
});
