import { lazy, Suspense } from 'react';
import type { ViewMode } from '../api/types';
import type { ChatViewProps } from '../features/chat/ChatView';
import type { AdminViewProps } from '../features/admin/AdminView';

/**
 * 视图级路由:聊天端与管理端各自独立 chunk,按 viewMode 懒加载。
 *
 * 未引入 react-router:项目历史交互以内部 state(viewMode) 切换视图,
 * 引入 URL 路由会改变页面 URL 行为,故沿用 state 路由但拆出独立 chunk。
 */
export const ChatView = lazy(() => import('../features/chat/ChatView'));
export const AdminView = lazy(() => import('../features/admin/AdminView'));

export interface ViewRouterProps {
  viewMode: ViewMode;
  chatProps: ChatViewProps;
  adminProps: AdminViewProps;
}

export function ViewRouter({ viewMode, chatProps, adminProps }: ViewRouterProps) {
  if (viewMode === 'chat') {
    return (
      <Suspense fallback={null}>
        <ChatView {...chatProps} />
      </Suspense>
    );
  }
  return (
    <Suspense fallback={null}>
      <AdminView {...adminProps} />
    </Suspense>
  );
}
