import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// R2-P2-01: vitest 配置 globals=false,@testing-library/react 不会自动
// 注册 cleanup;不手动卸载时 DOM 会跨用例累积,造成后续用例命中重复
// 元素甚至假阳性通过。(cleanup 已并入上方 fail-on-unexpected-error 钩子)

/**
 * R2-P2-02: 仅隔离已知、不可在本仓库修复的第三方告警,其余 console.error
 * 全部保留(不吞错误)。每条过滤均记录来源包、精确版本与到期时间
 * (expiresAt): 到期后过滤自动失效,告警重新落入 fail-on-unexpected-error,
 * 使 E2E/单测自动失败,倒逼完成升级/修复 (S2-08)。
 *  1. `[antd: Tooltip] overlayClassName deprecated`
 *     来源: antd@5.29.3 内部经 @agentscope-ai/design@1.0.32 调用;
 *     待上游升级至 classNames API 后移除本过滤。
 *  2. `Warning: forwardRef render functions accept exactly two parameters`
 *     来源: react-dom@18.3.1 对第三方组件(如 @agentscope-ai/design@1.0.32
 *     内部 forwardRef 用法)的告警。
 * 过滤边界: 仅按完整告警前缀匹配,任何其它文本一律放行。
 */
const KNOWN_THIRD_PARTY_WARNINGS: Array<{ prefix: string; expiresAt: string }> = [
  {
    prefix: 'Warning: [antd: Tooltip] `overlayClassName` is deprecated',
    expiresAt: '2026-12-31',
  },
  {
    prefix: 'Warning: forwardRef render functions accept exactly two parameters',
    expiresAt: '2026-12-31',
  },
  // R3-P2-01: @emotion/cache@11.14.0 的 SSR 伪类告警（部分构建经
  // console.error 通道输出）；jsdom 客户端环境从不 SSR，无意义且
  // 不可在上游关闭，按完整前缀精确隔离。
  {
    prefix: 'The pseudo class ":first-child" is potentially unsafe',
    expiresAt: '2026-12-31',
  },
];

function quarantineFor(message: string): { prefix: string; expiresAt: string } | null {
  const entry = KNOWN_THIRD_PARTY_WARNINGS.find((item) =>
    message.startsWith(item.prefix),
  );
  if (!entry) {
    return null;
  }
  // S2-08: an expired quarantine no longer suppresses the warning, so the
  // fail-on-unexpected-error hook trips and the suite fails.
  return new Date(entry.expiresAt).getTime() > Date.now() ? entry : null;
}

const originalConsoleError = console.error.bind(console);
// R3-P2-01: 未预期的 console.error 直接 fail 当前用例，不再只“保留输出”。
// 预期的错误必须在用例内显式 spy/捕获。
const unexpectedConsoleErrors: string[] = [];
console.error = (...args: unknown[]) => {
  const first = typeof args[0] === 'string' ? args[0] : '';
  if (quarantineFor(first) !== null) {
    return;
  }
  unexpectedConsoleErrors.push(args.map((item) => String(item)).join(' '));
  originalConsoleError(...args);
};

// ---- S2-08: no undeclared network access in tests ---------------------------
// 单测必须离线运行。@agentscope-ai/design 的空态插图 (Empty/Illustrate)
// 从 gw.alicdn.com 拉取 SVG：这里将其本地化(mock 为最小合法 SVG),
// 消除外网请求、DNS 错误 stderr 噪声与离线环境的不稳定性；其余任何
// 对真实外部资源的 fetch/XHR 请求一律立即失败 (fail-loudly)。需要在
// 用例内声明网络行为的，必须自行 mock。
const LOCALIZED_EXTERNAL_SVG = /^https:\/\/gw\.alicdn\.com\/.*\.svg($|\?)/;
const MINIMAL_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>';

if (typeof globalThis.fetch !== 'undefined') {
  globalThis.fetch = ((input: unknown) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : String((input as { url?: string })?.url ?? input);
    if (LOCALIZED_EXTERNAL_SVG.test(url)) {
      return Promise.resolve(
        new Response(MINIMAL_SVG, {
          status: 200,
          headers: { 'Content-Type': 'image/svg+xml' },
        }),
      );
    }
    throw new Error(
      `tests must not fetch external resources (undeclared network access): ${url}`,
    );
  }) as typeof fetch;
}
if (typeof window !== 'undefined' && window.XMLHttpRequest) {
  const OriginalXHR = window.XMLHttpRequest;
  const BlockedXHR = function (this: XMLHttpRequest) {
    const xhr = new OriginalXHR();
    const originalOpen = xhr.open.bind(xhr) as (
      ...args: unknown[]
    ) => unknown;
    xhr.open = function (
      method: string,
      url: string | URL,
      ...rest: unknown[]
    ) {
      const target = String(url);
      if (!target.startsWith('/') && !target.startsWith('http://localhost')) {
        throw new Error(
          `tests must not open external resources (undeclared network access): ${target}`,
        );
      }
      return originalOpen(method, url, ...rest) as void;
    } as XMLHttpRequest['open'];
    return xhr;
  } as unknown as typeof XMLHttpRequest;
  window.XMLHttpRequest = BlockedXHR;
}

/**
 * R3-P2-01: console.warn 通道同样精确隔离 SSR 伪类告警（来源同上）。
 * 仅按完整前缀匹配，其它 console.warn 一律放行。
 */
const KNOWN_SSR_PSEUDO_WARNING =
  'The pseudo class ":first-child" is potentially unsafe';
const originalConsoleWarn = console.warn.bind(console);
console.warn = (...args: unknown[]) => {
  const first = typeof args[0] === 'string' ? args[0] : '';
  if (first.startsWith(KNOWN_SSR_PSEUDO_WARNING)) {
    return;
  }
  originalConsoleWarn(...args);
};

// R3-P2-01: unhandled rejection 同样直接 fail。
const unhandledRejections: string[] = [];
const onUnhandledRejection = (event: PromiseRejectionEvent) => {
  unhandledRejections.push(String(event.reason));
};
window.addEventListener('unhandledrejection', onUnhandledRejection);

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

// rc-table/rc-util 在 jsdom 下测量滚动条时会带 pseudoElt 参数调用
// getComputedStyle；jsdom 对 pseudoElt 分支报 Not implemented（且该错误
// 经 virtualConsole 直接进 stderr，无法被 console shim 拦截）。因此
// 带 pseudo 元素时不转发 pseudoElt，返回元素自身样式即可满足测量需求。
const originalGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (elt: Element, pseudoElt?: string | null) =>
  pseudoElt ? originalGetComputedStyle(elt) : originalGetComputedStyle(elt, pseudoElt);

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
