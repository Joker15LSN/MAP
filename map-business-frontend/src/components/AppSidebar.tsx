import { Button } from '@agentscope-ai/design';
import type { ChatHistoryItem, ViewMode } from '../api/types';

export interface AppSidebarProps {
  viewMode: ViewMode;
  sidebarCollapsed: boolean;
  chatHistory: ChatHistoryItem[];
  activeHistoryId: string | null;
  onToggleCollapse: () => void;
  onNewChat: () => void;
  onSelectHistory: (item: ChatHistoryItem) => void;
  onSwitchView: (mode: ViewMode) => void;
}

/** 左侧导航栏:品牌、折叠、历史记录、前台/后台切换。 */
export default function AppSidebar({
  viewMode,
  sidebarCollapsed,
  chatHistory,
  activeHistoryId,
  onToggleCollapse,
  onNewChat,
  onSelectHistory,
  onSwitchView,
}: AppSidebarProps) {
  return (
    <aside className={`map-sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}>
      <div className={`map-brand ${sidebarCollapsed ? 'collapsed' : ''}`}>
        <div className="brand-mark">MAP</div>
        <div className={`brand-meta ${sidebarCollapsed ? 'collapsed' : ''}`}>
          <div className="brand-title">MAP Console</div>
          <div className="brand-subtitle">Multi Agent Path</div>
        </div>
      </div>

      <Button
        className={`map-sidebar-rail-toggle ${sidebarCollapsed ? 'collapsed' : ''}`}
        type="text"
        onClick={onToggleCollapse}
      >
        {sidebarCollapsed ? '›' : '‹'}
      </Button>

      {viewMode === 'chat' && sidebarCollapsed ? (
        <Button className="collapsed-new-chat" type="primary" onClick={onNewChat}>
          +
        </Button>
      ) : null}

      {viewMode === 'chat' ? (
        <div className="chat-history-panel">
          <div className="history-head">
            <span className={`history-title ${sidebarCollapsed ? 'collapsed' : ''}`}>历史记录</span>
            <Button type="text" size="small" className={`new-chat-btn ${sidebarCollapsed ? 'collapsed' : ''}`} onClick={onNewChat}>
              + 新对话
            </Button>
          </div>
          <div className="history-list">
            {chatHistory.map((item) => (
              <button
                key={item.id}
                className={`history-item ${activeHistoryId === item.id ? 'active' : ''}`}
                onClick={() => onSelectHistory(item)}
                title={item.question}
              >
                <span>{item.question}</span>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className={`sidebar-placeholder ${sidebarCollapsed ? 'collapsed' : ''}`}>后台配置导航</div>
      )}

      <div className="sidebar-switcher">
        <Button type={viewMode === 'chat' ? 'primary' : 'default'} block onClick={() => onSwitchView('chat')}>
          {sidebarCollapsed ? '前台' : '前台问答'}
        </Button>
        <Button type={viewMode === 'backend' ? 'primary' : 'default'} block onClick={() => onSwitchView('backend')}>
          {sidebarCollapsed ? '后台' : '后台管理'}
        </Button>
      </div>
    </aside>
  );
}
