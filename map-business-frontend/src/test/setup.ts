import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll } from 'vitest';
import { cleanup } from '@testing-library/react';
import { server } from './server';

/**
 * R2-P2-02 / R4-P2-01: 仅隔离已知、不可在本仓库修复的第三方告警,其余
 * console.error 全部保留(不吞错误)。每条临时隔离均记录
 * package/version/owner/到期日(review_until),到期必须复审:
 *  1. `[antd: Tooltip] overlayClassName deprecated`
 *     来源: antd@5.29.3 内部经 @agentscope-ai/design@1.0.32 调用;
 *     owner: frontend; review_until: 2026-11-30;
 *     待上游升级至 classNames API 后移除本过滤。
 *  2. `Warning: forwardRef render functions accept exactly two parameters`
 *     来源: react-dom@18.3.1 对第三方组件(如 @agentscope-ai/design@1.0.32
 *     内部 forwardRef 用法)的告警;React 19 已移除该 API 形式;
 *     owner: frontend; review_until: 2026-11-30。
 * 过滤边界: 仅按完整告警前缀匹配,任何其它文本一律放行。
 */
const KNOWN_THIRD_PARTY_WARNINGS = [
  'Warning: [antd: Tooltip] `overlayClassName` is deprecated',
  'Warning: forwardRef render functions accept exactly two parameters',
];

const originalConsoleError = console.error.bind(console);
// R3-P2-01: 未预期的 console.error 直接 fail 当前用例（含 MSW
// unhandled/unmatched 诊断 —— server.listen 使用 onUnhandledRequest:'error'，
// 其报错也经 console.error 输出）。预期的错误必须在用例内显式 spy/捕获。
const unexpectedConsoleErrors: string[] = [];
console.error = (...args: unknown[]) => {
  const first = typeof args[0] === 'string' ? args[0] : '';
  if (KNOWN_THIRD_PARTY_WARNINGS.some((prefix) => first.startsWith(prefix))) {
    return;
  }
  unexpectedConsoleErrors.push(args.map((item) => String(item)).join(' '));
  originalConsoleError(...args);
};

// R3-P2-01: unhandled rejection 同样直接 fail，不依赖进程级警告。
const unhandledRejections: string[] = [];
const onUnhandledRejection = (event: PromiseRejectionEvent) => {
  unhandledRejections.push(String(event.reason));
};

// 每个用例后清理 DOM,避免用例间互相污染
afterEach(() => {
  cleanup();
  if (unexpectedConsoleErrors.length > 0) {
    const detail = unexpectedConsoleErrors.splice(0).join('\n---\n');
    throw new Error(`unexpected console.error during test:\n${detail}`);
  }
  if (unhandledRejections.length > 0) {
    const detail = unhandledRejections.splice(0).join('\n---\n');
    throw new Error(`unhandled promise rejection during test:\n${detail}`);
  }
});

// MSW:所有测试默认走 mock handlers(FIX-P2-FRONTEND-01 测试底座)
beforeAll(() => {
  window.addEventListener('unhandledrejection', onUnhandledRejection);
  server.listen({ onUnhandledRequest: 'error' });
});
afterEach(() => server.resetHandlers());
afterAll(() => {
  window.removeEventListener('unhandledrejection', onUnhandledRejection);
  server.close();
});

// jsdom 环境缺少的浏览器 API,组件测试需要的 polyfill
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

if (typeof window !== 'undefined' && !window.ResizeObserver) {
  window.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

if (typeof window !== 'undefined' && !window.scrollTo) {
  window.scrollTo = () => {};
}
