import { http, HttpResponse, delay } from 'msw';

/**
 * 业务前端测试用的 MSW handlers（FIX-P2-FRONTEND-01）。
 *
 * 覆盖:会话创建/详情/流式 SSE、反馈、admin 保存成功/失败、网络异常、
 * 非 JSON 错误体。错误响应使用标准 envelope {code,message,details,request_id}。
 */

const STREAM_FRAMES = [
  'event: start\ndata: {"message_id":"m-1","conversation_id":"c-1","user_message_id":"um-1"}\n\n',
  'event: content_delta\ndata: {"content":"你"}\n\n',
  'event: content_delta\ndata: {"content":"好"}\n\n',
  'event: done\ndata: {"message_id":"m-1","content":"你好","status":"completed","task_id":"t-1"}\n\n',
].join('');

export const handlers = [
  // ---- conversations (new API) ----
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
          status: 'completed',
          content: '你好',
          request_id: 'req-1',
          task_id: 't-1',
          decision: null,
          stream_error: null,
          error_message: null,
          fallback_used: false,
          created_at: '2026-08-09T00:00:00Z',
          completed_at: '2026-08-09T00:00:01Z',
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

  http.post('/api/v1/conversations/c-1/messages:stream', () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 1; i <= STREAM_FRAMES.length; i += 1) {
          controller.enqueue(encoder.encode(STREAM_FRAMES.slice(0, i)));
        }
        controller.close();
      },
    });
    return new HttpResponse(stream, {
      headers: { 'Content-Type': 'text/event-stream' },
    });
  }),

  // ---- feedback (new API) ----
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
  // R3-P2-01: useAdminController 挂载时会初始化 GET summary/full-config，
  // 缺少 handler 会产生 MSW unmatched 错误日志。
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
  full: STREAM_FRAMES,
  noDone:
    'event: start\ndata: {"message_id":"m-1"}\n\nevent: content_delta\ndata: {"content":"hi"}\n\n',
};
