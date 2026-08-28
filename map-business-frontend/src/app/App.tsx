import { Suspense, lazy, useEffect, useState } from 'react';
import { Button, ConfigProvider, Switch, Tag, carbonDarkTheme, carbonTheme } from '@agentscope-ai/design';
import type { ViewMode } from '../api/types';

import AppSidebar from '../components/AppSidebar';
import { useAdminController } from '../features/admin/useAdminController';
import { useFlowStrategyController } from '../features/admin/useFlowStrategyController';
import { useConversationController } from '../features/conversation/useConversationController';

const LazyConversationView = lazy(async () => {
  const module = await import('../features/conversation/ConversationView');
  return { default: module.ConversationView };
});

const LazyAdminView = lazy(() => import('../features/admin/AdminView'));

const THEME_STORAGE_KEY = 'map_theme_mode';

/**
 * 应用外壳(Application Shell)。
 *
 * 职责边界:
 * - 持有 shell 级 UI 状态(视图切换、侧栏折叠、主题);
 * - 前台默认挂载 ConversationView（canonical Run 路径）;
 * - 渲染 主题 Provider + 侧栏 + 头部 + 视图。
 *
 * 业务数据加载、SSE 流式、配置保存等均下沉到对应 controller/features,
 * 本文件不再包含表格与 fetch 逻辑。
 */
export default function App() {
  const [viewMode, setViewMode] = useState<ViewMode>('chat');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [isDark, setIsDark] = useState<boolean>(() => {
    if (typeof window === 'undefined') {
      return false;
    }
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === 'dark') {
      return true;
    }
    if (stored === 'light') {
      return false;
    }
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  });

  const flow = useFlowStrategyController();
  const admin = useAdminController(flow);
  const conversation = useConversationController({});

  useEffect(() => {
    document.body.classList.toggle('theme-dark', isDark);
    window.localStorage.setItem(THEME_STORAGE_KEY, isDark ? 'dark' : 'light');
  }, [isDark]);

  useEffect(() => {
    if (viewMode === 'backend') {
      void admin.loadAdminData();
    }
  }, [viewMode]);

  return (
    <ConfigProvider {...(isDark ? carbonDarkTheme : carbonTheme)}>
      <div className={`map-console-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
        <AppSidebar
          viewMode={viewMode}
          sidebarCollapsed={sidebarCollapsed}
          chatHistory={[]}
          activeHistoryId={null}
          onToggleCollapse={() => setSidebarCollapsed((prev) => !prev)}
          onNewChat={() => void conversation.create('global')}
          onSelectHistory={() => {}}
          onSwitchView={(mode) => setViewMode(mode)}
        />

        <main className="map-main">
          <header className="main-header">
            <div>
              <h1>{viewMode === 'chat' ? '前台问答' : '后台管理'}</h1>
              <p>
                {viewMode === 'chat'
                  ? '提问后可在下方查看回答,运行过程由 canonical Run 事件驱动。'
                  : '后台功能与算法服务解耦，由业务后端独立承载管理流程。'}
              </p>
            </div>
            <div className="main-header-actions">
              <Tag>{isDark ? '深色' : '浅色'}</Tag>
              <Switch checked={isDark} onChange={setIsDark} />
              {viewMode === 'backend' ? (
                <Button onClick={() => void admin.loadAdminData()} loading={admin.adminLoading}>
                  刷新管理数据
                </Button>
              ) : null}
            </div>
          </header>

          {viewMode === 'chat' ? (
            <Suspense fallback={null}>
              <LazyConversationView controller={conversation} />
            </Suspense>
          ) : (
            <Suspense fallback={null}>
              <LazyAdminView api={admin} />
            </Suspense>
          )}
        </main>
      </div>
    </ConfigProvider>
  );
}
