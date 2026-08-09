import { describe, expect, it } from 'vitest';
import { act, render, renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { useAdminController } from './useAdminController';
import { useFlowStrategyController } from './useFlowStrategyController';

/**
 * Admin 统一 API client 错误路径测试（FIX-P2-FRONTEND-01）。
 *
 * - 保存 500(标准 envelope detail):saveStatus 显示失败文案;
 * - 保存 500 但响应体非 JSON:不抛未捕获异常,显示失败文案;
 * - 网络异常:显示失败文案;
 * - 业务/admin 请求均经 apiRequest,无散落 fetch。
 */

const EMPTY_FLOW = {
  flowStrategy: null,
  scenarioPolicy: null,
  uploadedSkills: [],
  skillPolicies: [],
  flowSkillDescriptors: [],
  loading: false,
  error: null,
  hasFlowData: false,
  loaded: false,
} as unknown as ReturnType<typeof useFlowStrategyController>;

function makeController() {
  const { result } = renderHook(() => useAdminController(EMPTY_FLOW));
  return result;
}

describe('admin save error paths', () => {
  it('marks the section as failed when the API returns 500 (envelope)', async () => {
    const controller = makeController();
    await act(async () => {
      await controller.current.saveSection(
        '/api/admin/model-center-fail',
        { large_models: [] },
        'ok',
        '保存失败-500',
      );
    });
    expect(controller.current.saveStatus).toBe('保存失败-500');
  });

  it('marks the section as failed when the API returns a non-JSON error body', async () => {
    const controller = makeController();
    await act(async () => {
      await controller.current.saveSection(
        '/api/admin/model-center-html',
        { large_models: [] },
        'ok',
        '保存失败-html',
      );
    });
    expect(controller.current.saveStatus).toBe('保存失败-html');
  });

  it('marks the section as failed on network errors', async () => {
    server.use(
      http.put('/api/admin/model-center', () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    const controller = makeController();
    await act(async () => {
      await controller.current.saveSection(
        '/api/admin/model-center',
        { large_models: [] },
        'ok',
        '保存失败-网络',
      );
    });
    expect(controller.current.saveStatus).toBe('保存失败-网络');
  });

  it('saves successfully when the API returns 200', async () => {
    const controller = makeController();
    await act(async () => {
      await controller.current.saveSection(
        '/api/admin/model-center',
        { large_models: [] },
        '已保存',
        '保存失败',
      );
    });
    expect(controller.current.saveStatus).toBe('已保存');
  });
});
