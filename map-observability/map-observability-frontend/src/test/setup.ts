import '@testing-library/jest-dom/vitest';

// rc-table 在 jsdom 下测量滚动条需要带 pseudoElt 参数的 getComputedStyle
const originalGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (elt: Element, pseudoElt?: string | null) =>
  originalGetComputedStyle(elt, pseudoElt ?? null);

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
