import { Alert, Button, Card } from '@agentscope-ai/design';
import type { AdminPageKey } from '../../api/types';
import { ADMIN_PAGE_LABEL } from './constants';
import { renderAdminPage, renderReleasePanel } from './pages';
import AgentEditorDrawer from './AgentEditorDrawer';
import type { AdminApi } from './AdminApi';

export interface AdminViewProps {
  api: AdminApi;
}

/** 管理端(后台管理)主视图。状态由 App shell 注入,本组件只负责渲染与导航。 */
export default function AdminView({ api }: AdminViewProps) {
  const summaryCards = [
    { key: 'model', title: '模型总数', value: api.adminSummary?.model_count ?? '-' },
    { key: 'agent', title: '业务智能体', value: api.adminSummary?.business_agent_count ?? '-' },
    { key: 'perm', title: '权限策略', value: api.adminSummary?.permission_rule_count ?? '-' },
    { key: 'user', title: '启用用户', value: api.adminSummary?.user_enabled_count ?? '-' },
  ];

  const navGroups: Array<{ title: string; items: Array<{ key: AdminPageKey; label: string }> }> = [
    {
      title: '模型管理',
      items: [
        { key: 'model-center', label: '模型管理' },
        { key: 'basic-settings', label: '基础设置' },
        { key: 'address-config', label: '地址配置' },
      ],
    },
    {
      title: '数据连接器',
      items: [
        { key: 'data-access', label: '数据接入' },
        { key: 'data-assets', label: '数据管理' },
      ],
    },
    {
      title: '智能体配置',
      items: [
        { key: 'master-agent', label: 'Master智能体' },
        { key: 'business-agent', label: '业务智能体' },
        { key: 'mcp-server', label: 'MCP Server' },
        { key: 'skills', label: 'Skills' },
        { key: 'flow-policy', label: '心流策略' },
        { key: 'scenario-hub', label: 'ScenarioHub' },
        { key: 'skill-hub', label: 'SkillHub' },
      ],
    },
    {
      title: '运营管理中心',
      items: [
        { key: 'session-management', label: '会话管理' },
        { key: 'dashboard', label: '数据看板' },
        { key: 'security', label: '安全管理' },
        { key: 'glossary', label: '词库管理' },
        { key: 'home-recommendation', label: '首页推荐' },
      ],
    },
    {
      title: '用户与权限',
      items: [
        { key: 'permission', label: '权限策略' },
        { key: 'user-role', label: '角色与用户' },
      ],
    },
  ];

  return (
    <div className="backend-wrapper">
      {api.adminError ? <Alert type="error" message={api.adminError} showIcon style={{ marginBottom: 12 }} /> : null}
      {api.saveStatus ? <Alert type="info" message={api.saveStatus} showIcon style={{ marginBottom: 12 }} /> : null}

      <div className="backend-overview-grid">
        {summaryCards.map((item) => (
          <Card key={item.key} className="metric-card" loading={api.adminLoading}>
            <div className="metric-title">{item.title}</div>
            <div className="metric-value">{item.value}</div>
          </Card>
        ))}
      </div>

      <div className="backend-layout">
        <aside className="backend-menu">
          {navGroups.map((group) => (
            <div key={group.title} className="backend-nav-group">
              <div className="backend-nav-title">{group.title}</div>
              <div className="backend-nav-list">
                {group.items.map((item) => (
                  <button
                    key={item.key}
                    className={`backend-nav-item ${api.adminPage === item.key ? 'active' : ''}`}
                    onClick={() => api.setAdminPage(item.key)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </aside>

        <section className="backend-content">
          <div className="backend-content-header">
            <div>
              <h2>{ADMIN_PAGE_LABEL[api.adminPage]}</h2>
              <p>对齐线上后台管理结构，支持模型、智能体、权限和运营配置。</p>
            </div>
            <Button onClick={() => void api.loadAdminData()} loading={api.adminLoading}>
              刷新当前数据
            </Button>
          </div>
          {renderAdminPage(api.adminPage, api)}
          {renderReleasePanel(api)}
        </section>
      </div>

      <AgentEditorDrawer api={api} />
    </div>
  );
}
