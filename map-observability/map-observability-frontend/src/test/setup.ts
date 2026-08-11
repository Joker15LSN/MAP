import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// R2-P2-01: vitest 配置 globals=false,@testing-library/react 不会自动
// 注册 cleanup;不手动卸载时 DOM 会跨用例累积,造成后续用例命中重复
// 元素甚至假阳性通过。(cleanup 已并入上方 fail-on-unexpected-error 钩子)

/**
 * R2-P2-02: 仅隔离已知、不可在本仓库修复的第三方告警,其余 console.error
 * 全部保留(不吞错误)。每条过滤均记录来源包与精确版本:
 *  1. `[antd: Tooltip] overlayClassName deprecated`
 *     来源: antd@5.29.3 内部经 @agentscope-ai/design@1.0.32 调用;
 *     待上游升级至 classNames API 后移除本过滤。
 *  2. `Warning: forwardRef render functions accept exactly two parameters`
 *     来源: react-dom@18.3.1 对第三方组件(如 @agentscope-ai/design@1.0.32
 *     内部 forwardRef 用法)的告警。
 * 过滤边界: 仅按完整告警前缀匹配,任何其它文本一律放行。
 */
const KNOWN_THIRD_PARTY_WARNINGS = [
  'Warning: [antd: Tooltip] `overlayClassName` is deprecated',
  'Warning: forwardRef render functions accept exactly two parameters',
  // R3-P2-01: @emotion/cache@11.14.0 的 SSR 伪类告警（部分构建经
  // console.error 通道输出）；jsdom 客户端环境从不 SSR，无意义且
  // 不可在上游关闭，按完整前缀精确隔离。
  'The pseudo class ":first-child" is potentially unsafe',
];

const originalConsoleError = console.error.bind(console);
// R3-P2-01: 未预期的 console.error 直接 fail 当前用例，不再只“保留输出”。
// 预期的错误必须在用例内显式 spy/捕获。
const unexpectedConsoleErrors: string[] = [];
console.error = (...args: unknown[]) => {
  const first = typeof args[0] === 'string' ? args[0] : '';
  if (KNOWN_THIRD_PARTY_WARNINGS.some((prefix) => first.startsWith(prefix))) {
    return;
  }
  unexpectedConsoleErrors.push(args.map((item) => String(item)).join(' '));
  originalConsoleError(...args);
};

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
