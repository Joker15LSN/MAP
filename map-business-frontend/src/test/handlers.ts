import { http, HttpResponse, delay } from 'msw';

/**
 * 业务前端测试用的 MSW handlers（Step 4 / PR-F1+F2 更新）。
 *
 * 覆盖:会话创建/详情、canonical Run 事件 SSE、turn 创建、run cancel、
 * 反馈、admin 保存成功/失败、网络异常、非 JSON 错误体。
 * 错误响应使用标准 envelope {code,message,details,request_id}。
 */

interface RunEventFixture {
  seq: number;
  type: string;
  data: Record<string, unknown>;
}

const RUN_EVENTS: RunEventFixture[] = [
  { seq: 1, type: 'run.started', data: {} },
  { seq: 2, type: 'attempt.started', data: { attempt: 1 } },
  { seq: 3, type: 'message.delta', data: { content: '你' } },
  { seq: 4, type: 'message.delta', data: { content: '好' } },
  { seq: 5, type: 'step.completed', data: { content: '你好', result: {} } },
  { seq: 6, type: 'attempt.completed', data: { attempt: 1 } },
  { seq: 7, type: 'run.completed', data: {} },
];

function runEventsToSse(afterSeq = 0): string {
  return RUN_EVENTS.filter((event) => event.seq > afterSeq)
    .map((event) => {
      const envelope = {
        schema_version: 1,
        schema_minor: 0,
        event_id: `ev-${event.seq}`,
        run_id: 'run-1',
        seq: event.seq,
        type: event.type,
        occurred_at: '2026-08-09T00:00:00Z',
        workspace_id: '00000000-0000-0000-0000-000000000001',
        data: event.data,
      };
      return `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(envelope)}\n\n`;
    })
    .join('');
}

function runEventsStream(request: Request): HttpResponse<ReadableStream<Uint8Array>> {
  const url = new URL(request.url);
  const afterSeq = Number(url.searchParams.get('after_seq') || 0);
  const body = runEventsToSse(afterSeq);
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return new HttpResponse<ReadableStream<Uint8Array>>(stream, {
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

export const handlers = [
  // ---- conversations ----
  http.post('/api/v1/conversations', async ({ request }) => {
    const idempotencyKey = request.headers.get('Idempotency-Key');
    if (idempotencyKey === 'conflict-key') {
      return HttpResponse.json(
        {
          code: 'IDEMPOTENCY_CONFLICT',
          message: 'idempotency key reused with a different request body',
          details: null,
          request_id: 'req-1',
        },
        { status: 409 },
      );
    }
    await delay(10);
    return HttpResponse.json(
      {
        id: 'c-1',
        workspace_id: '00000000-0000-0000-0000-000000000001',
        mode: 'global',
        title: '新会话',
        status: 'active',
        created_at: '2026-08-09T00:00:00Z',
        updated_at: '2026-08-09T00:00:00Z',
        last_message_at: null,
      },
      { status: 201 },
    );
  }),

  http.get('/api/v1/conversations/c-1', () =>
    HttpResponse.json({
      id: 'c-1',
      workspace_id: '00000000-0000-0000-0000-000000000001',
      mode: 'global',
      title: '新会话',
      status: 'active',
      created_at: '2026-08-09T00:00:00Z',
      updated_at: '2026-08-09T00:00:00Z',
      last_message_at: '2026-08-09T00:00:01Z',
      messages: [
        {
          id: 'um-1',
          conversation_id: 'c-1',
          role: 'user',
          status: 'completed',
          content: '你好',
          request_id: 'req-1',
          task_id: null,
          decision: null,
          run_id: null,
          stream_error: null,
          error_message: null,
          fallback_used: false,
          created_at: '2026-08-09T00:00:00Z',
          completed_at: '2026-08-09T00:00:00Z',
        },
        {
          id: 'm-1',
          conversation_id: 'c-1',
          role: 'assistant',
          status: 'streaming',
          content: '',
          request_id: 'req-1',
          task_id: 't-1',
          decision: null,
          run_id: 'run-1',
          stream_error: null,
          error_message: null,
          fallback_used: false,
          created_at: '2026-08-09T00:00:00Z',
          completed_at: null,
        },
      ],
    }),
  ),

  http.get('/api/v1/conversations/c-missing', () =>
    HttpResponse.json(
      {
        code: 'RESOURCE_NOT_FOUND',
        message: 'conversation not found',
        details: null,
        request_id: 'req-404',
      },
      { status: 404 },
    ),
  ),

  // ---- canonical turns / runs ----
  http.post('/api/v1/conversations/c-1/turns', () =>
    HttpResponse.json(
      {
        run_id: 'run-1',
        user_message_id: 'um-new',
        assistant_message_id: 'm-new',
        status: 'queued',
        replayed: false,
      },
      { status: 201 },
    ),
  ),

  http.get('/api/v1/runs/run-1/events', ({ request }) => runEventsStream(request)),

  http.get('/api/v1/runs/run-1', () =>
    HttpResponse.json({
      run_id: 'run-1',
      workspace_id: '00000000-0000-0000-0000-000000000001',
      principal_id: 'local-admin',
      conversation_id: 'c-1',
      status: 'completed',
      command: {
        kind: 'conversation_turn',
        payload: { query: '你好' },
        snapshot: {},
      },
      last_seq: 7,
      cancel_requested: false,
      error_code: null,
    }),
  ),

  http.post('/api/v1/runs/run-1:cancel', () =>
    HttpResponse.json({
      run_id: 'run-1',
      accepted: true,
      status: 'queued',
    }),
  ),

  // ---- feedback ----
  http.put('/api/v1/messages/m-1/feedback', () =>
    HttpResponse.json({
      id: 'f-1',
      message_id: 'm-1',
      conversation_id: 'c-1',
      rating: 'helpful',
      reason_codes: [],
      reason_other: null,
      correction_text: null,
      status: 'open',
      version: 1,
      created_at: '2026-08-09T00:00:00Z',
      updated_at: '2026-08-09T00:00:00Z',
    }),
  ),
  http.get('/api/v1/messages/m-1/feedback', () =>
    HttpResponse.json({
      id: 'f-1',
      message_id: 'm-1',
      conversation_id: 'c-1',
      rating: 'helpful',
      reason_codes: [],
      reason_other: null,
      correction_text: null,
      status: 'open',
      version: 1,
      created_at: '2026-08-09T00:00:00Z',
      updated_at: '2026-08-09T00:00:00Z',
    }),
  ),
  http.delete('/api/v1/messages/m-1/feedback', () =>
    HttpResponse.json({ status: 'withdrawn' }),
  ),

  // ---- admin (legacy) ----
  http.get('/api/admin/summary', () =>
    HttpResponse.json({
      updated_at: '2026-08-09T00:00:00Z',
      master_version: 'v1',
      business_agent_count: 0,
      business_agent_enabled_count: 0,
      permission_rule_count: 0,
      knowledge_binding_count: 0,
      skill_enabled_count: 0,
      release_count: 0,
      model_count: 0,
      user_count: 0,
      user_enabled_count: 0,
      mcp_server_count: 0,
      skill_count: 0,
    }),
  ),
  http.get('/api/admin/full-config', () =>
    HttpResponse.json({
      updated_at: '2026-08-09T00:00:00Z',
      model_center: {
        large_models: [],
        asr_models: [],
        tts_models: [],
        embedding_models: [],
        rerank_models: [],
      },
    }),
  ),
  http.put('/api/admin/model-center', () =>
    HttpResponse.json({ large_models: [], asr_models: [], tts_models: [], embedding_models: [], rerank_models: [] }),
  ),
  http.put('/api/admin/model-center-fail', () =>
    HttpResponse.json(
      { detail: 'admin state write failed; previous file kept intact' },
      { status: 500 },
    ),
  ),
  http.put('/api/admin/model-center-html', () =>
    new HttpResponse('<html>not json</html>', { status: 500 }),
  ),
];

export const sseFixtures = {
  full: runEventsToSse(0),
};
