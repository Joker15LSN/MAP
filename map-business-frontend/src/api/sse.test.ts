import { describe, expect, it } from 'vitest';
import { parseSseFrames } from './sse';

describe('parseSseFrames 增量 SSE 分帧解析', () => {
  it('解析单帧 event + data', () => {
    const buffer = 'event: start\ndata: {"request_id":"r1","state_id":"s1"}\n\n';
    const { events, remaining } = parseSseFrames(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe('start');
    expect(events[0].data).toEqual({ request_id: 'r1', state_id: 's1' });
    expect(remaining).toBe('');
  });

  it('一次 buffer 含多帧时全部解析', () => {
    const buffer = [
      'event: content_delta',
      'data: {"content":"你好"}',
      '',
      '',
      'event: content_delta',
      'data: {"content":"，世界"}',
      '',
      '',
    ].join('\n');
    const { events, remaining } = parseSseFrames(buffer);
    expect(events).toHaveLength(2);
    expect(events.map((e) => e.event)).toEqual(['content_delta', 'content_delta']);
    expect(events[0].data).toEqual({ content: '你好' });
    expect(events[1].data).toEqual({ content: '，世界' });
    expect(remaining).toBe('');
  });

  it('未闭合半帧保留在 remaining 等待追加', () => {
    const { events, remaining } = parseSseFrames('event: done\ndata: {"content":"答');
    expect(events).toHaveLength(0);
    expect(remaining).toContain('event: done');
    expect(remaining).toContain('"content":"答');
  });

  it('半帧追加后完成解析(分帧缓冲续接)', () => {
    const first = parseSseFrames('event: done\ndata: {"content":"答');
    const second = parseSseFrames(`${first.remaining}案"}\n\n`);
    expect(second.events).toHaveLength(1);
    expect(second.events[0].event).toBe('done');
    expect(second.events[0].data).toEqual({ content: '答案' });
    expect(second.remaining).toBe('');
  });

  it('支持 CRLF 行结束符', () => {
    const buffer = 'event: meta\r\ndata: {"phase":"scene_selected"}\r\n\r\n';
    const { events } = parseSseFrames(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].data).toEqual({ phase: 'scene_selected' });
  });

  it('多行 data: 行按 \n 连接合并为单个 JSON', () => {
    const buffer = 'event: done\ndata: {"content":"a"\ndata: ,"flow":true}\n\n';
    const { events } = parseSseFrames(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe('done');
    expect(events[0].data).toEqual({ content: 'a', flow: true });
  });

  it('缺 event 或缺 data 的帧跳过', () => {
    const buffer = [
      'data: {"content":"no event line"}',
      '',
      '',
      'event: orphan',
      '',
      '',
      'event: done',
      'data: {"content":"ok"}',
      '',
      '',
    ].join('\n');
    const { events } = parseSseFrames(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe('done');
  });

  it('非法 JSON 的帧跳过但后续帧仍可解析', () => {
    const buffer = [
      'event: bad',
      'data: not-json{{',
      '',
      '',
      'event: done',
      'data: {"content":"ok"}',
      '',
      '',
    ].join('\n');
    const { events } = parseSseFrames(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe('done');
    expect(events[0].data).toEqual({ content: 'ok' });
  });

  it('data 前缀后的首尾空白被 trim', () => {
    const buffer = 'event: done\ndata:   {"content":"x"}   \n\n';
    const { events } = parseSseFrames(buffer);
    expect(events).toHaveLength(1);
    expect(events[0].data).toEqual({ content: 'x' });
  });

  it('空 buffer 无事件且 remaining 为空', () => {
    const { events, remaining } = parseSseFrames('');
    expect(events).toHaveLength(0);
    expect(remaining).toBe('');
  });
});
