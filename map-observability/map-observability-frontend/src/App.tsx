import { useEffect, useMemo, useState } from 'react';
import dayjs from 'dayjs';
import {
  Alert,
  Button,
  Card,
  ConfigProvider,
  Drawer,
  Input,
  Select,
  Switch,
  Tabs,
  Tag,
  carbonDarkTheme,
  carbonTheme,
} from '@agentscope-ai/design';

import { analyticsApi } from './api/client';
import { FilterBar } from './components/FilterBar';
import { AgentsToolsPage } from './pages/AgentsToolsPage';
import { CorrelationPage } from './pages/CorrelationPage';
import { FridayPage } from './pages/FridayPage';
import { OverviewPage } from './pages/OverviewPage';
import { RequestsPage } from './pages/RequestsPage';
import { UsersPage } from './pages/UsersPage';
import { FilterState, FridayAction, FridayConfig } from './types';
import { inferMainFlowContainer, isKnownContainer } from './constants/containers';

type SectionKey = 'overview' | 'projects' | 'traces' | 'friday';
type ProjectSubView = 'requests' | 'users' | 'agents-tools';
type LanguageKey = 'en' | 'zh';
type IconKind =
  | 'overview'
  | 'projects'
  | 'traces'
  | 'friday'
  | 'settings'
  | 'collapse-left'
  | 'collapse-right';

const STORAGE_KEYS = {
  filters: 'map_filters',
  section: 'map_active_section',
  projectSubView: 'map_project_subview',
  theme: 'map_theme',
  language: 'map_language',
  sidebarCollapsed: 'map_sidebar_collapsed',
} as const;

const buildDefaultFilters = (): FilterState => {
  const end = dayjs();
  const start = end.subtract(24, 'hour');

  return {
    startTs: start.toISOString(),
    endTs: end.toISOString(),
    container: 'map_core-dev',
    granularity: 'hour',
    status: '',
    queryLike: '',
    staffCode: '',
    sessionId: '',
    requestId: '',
    agentCode: '',
    tool: '',
    logLevels: [],
  };
};

const parseStoredFilters = (): FilterState => {
  const defaults = buildDefaultFilters();
  if (typeof window === 'undefined') {
    return defaults;
  }

  const raw = window.localStorage.getItem(STORAGE_KEYS.filters);
  if (!raw) {
    return defaults;
  }

  try {
    const parsed = JSON.parse(raw) as Partial<FilterState>;
    const storedContainer = isKnownContainer(parsed.container) ? inferMainFlowContainer(parsed.container) : 'map_core-dev';
    const parsedStart = dayjs(parsed.startTs);
    const openingEnd = dayjs(defaults.endTs);
    const safeStartTs = parsedStart.isValid() && parsedStart.isBefore(openingEnd)
      ? parsedStart.toISOString()
      : defaults.startTs;
    return {
      ...defaults,
      ...parsed,
      startTs: safeStartTs,
      // Always align the end time with page-open time so the first query includes the latest data.
      endTs: defaults.endTs,
      container: storedContainer,
      granularity: parsed.granularity === 'day' ? 'day' : 'hour',
      logLevels: Array.isArray(parsed.logLevels) ? parsed.logLevels : [],
    };
  } catch {
    return defaults;
  }
};

const parseStoredSection = (): SectionKey => {
  if (typeof window === 'undefined') {
    return 'overview';
  }
  const raw = window.localStorage.getItem(STORAGE_KEYS.section);
  return raw === 'projects' || raw === 'traces' || raw === 'friday' ? raw : 'overview';
};

const parseStoredProjectSubView = (): ProjectSubView => {
  if (typeof window === 'undefined') {
    return 'requests';
  }
  const raw = window.localStorage.getItem(STORAGE_KEYS.projectSubView);
  return raw === 'users' || raw === 'agents-tools' ? raw : 'requests';
};

const parseStoredLanguage = (): LanguageKey => {
  if (typeof window === 'undefined') {
    return 'en';
  }
  return window.localStorage.getItem(STORAGE_KEYS.language) === 'zh' ? 'zh' : 'en';
};

const parseStoredSidebarCollapsed = (): boolean => {
  if (typeof window === 'undefined') {
    return false;
  }
  return window.localStorage.getItem(STORAGE_KEYS.sidebarCollapsed) === '1';
};

const SidebarIcon = ({ kind }: { kind: IconKind }) => {
  const commonProps = {
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  };

  if (kind === 'overview') {
    return (
      <svg aria-hidden className="sidebar-icon-svg" {...commonProps}>
        <path d="M4 11.5 12 5l8 6.5" />
        <path d="M6.5 10.5V19h11v-8.5" />
      </svg>
    );
  }
  if (kind === 'projects') {
    return (
      <svg aria-hidden className="sidebar-icon-svg" {...commonProps}>
        <path d="M3.5 7.5h6l1.8 2h8.2v9.5a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2z" />
      </svg>
    );
  }
  if (kind === 'traces') {
    return (
      <svg aria-hidden className="sidebar-icon-svg" {...commonProps}>
        <circle cx="5.5" cy="6.5" r="1.5" />
        <circle cx="18.5" cy="6.5" r="1.5" />
        <circle cx="12" cy="17.5" r="1.5" />
        <path d="M7 7.3 10.7 15.8M17 7.3 13.3 15.8" />
      </svg>
    );
  }
  if (kind === 'friday') {
    return (
      <svg aria-hidden className="sidebar-icon-svg" {...commonProps}>
        <path d="M12 3.5v4M12 16.5v4M4.5 12h4M15.5 12h4M6.6 6.6l2.8 2.8M14.6 14.6l2.8 2.8M17.4 6.6l-2.8 2.8M9.4 14.6l-2.8 2.8" />
      </svg>
    );
  }
  if (kind === 'settings') {
    return (
      <svg aria-hidden className="sidebar-icon-svg" {...commonProps}>
        <path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z" />
        <path d="m19.2 13.2.5-1.2-.5-1.2-1.8-.5a5.9 5.9 0 0 0-.6-1.4l.9-1.7-.8-.8-1.7.9c-.4-.2-.9-.4-1.4-.6l-.5-1.8-1.2-.5-1.2.5-.5 1.8c-.5.2-1 .4-1.4.6l-1.7-.9-.8.8.9 1.7c-.3.4-.5.9-.6 1.4l-1.8.5-.5 1.2.5 1.2 1.8.5c.1.5.3 1 .6 1.4l-.9 1.7.8.8 1.7-.9c.4.3.9.5 1.4.6l.5 1.8 1.2.5 1.2-.5.5-1.8c.5-.1 1-.3 1.4-.6l1.7.9.8-.8-.9-1.7c.3-.4.5-.9.6-1.4z" />
      </svg>
    );
  }
  if (kind === 'collapse-right') {
    return (
      <svg aria-hidden className="sidebar-icon-svg" {...commonProps}>
        <path d="m10 7 5 5-5 5" />
      </svg>
    );
  }
  return (
    <svg aria-hidden className="sidebar-icon-svg" {...commonProps}>
      <path d="m14 7-5 5 5 5" />
    </svg>
  );
};

const navIcons: Record<SectionKey, IconKind> = {
  overview: 'overview',
  projects: 'projects',
  traces: 'traces',
  friday: 'friday',
};

const navLabels: Record<LanguageKey, Record<SectionKey, string>> = {
  en: {
    overview: '总览',
    projects: '项目',
    traces: '链路追踪',
    friday: 'Friday',
  },
  zh: {
    overview: '总览',
    projects: '项目',
    traces: '链路追踪',
    friday: 'Friday',
  },
};

const sectionTitles: Record<LanguageKey, Record<SectionKey, string>> = {
  en: {
    overview: '总览',
    projects: '项目',
    traces: '链路追踪',
    friday: 'Friday',
  },
  zh: {
    overview: '总览',
    projects: '项目',
    traces: '链路追踪',
    friday: 'Friday',
  },
};

const sectionDescriptions: Record<LanguageKey, Record<SectionKey, string>> = {
  en: {
    overview: '全局 KPI 总览，聚焦请求量、耗时、Token 与失败情况。',
    projects: '请求工作台：检索、详情下钻、用户与 Agent/工具分析。',
    traces: '基于 Loki + Mongo 的 RID/SID/AID/PARID 关联定位。',
    friday: 'Friday 对话诊断助手：自动联查请求、链路与错误证据。',
  },
  zh: {
    overview: '全局 KPI 总览，聚焦请求量、耗时、Token 与失败情况。',
    projects: '请求工作台：检索、详情下钻、用户与 Agent/工具分析。',
    traces: '基于 Loki + Mongo 的 RID/SID/AID/PARID 关联定位。',
    friday: 'Friday 对话诊断助手：自动联查请求、链路与错误证据。',
  },
};

interface ProjectsPageProps {
  filters: FilterState;
  refreshToken: number;
  isDark: boolean;
  subView: ProjectSubView;
  onSubViewChange: (next: ProjectSubView) => void;
  openRequestSignal?: { requestId: string; nonce: number } | null;
  onRequestSignalConsumed?: () => void;
}

const ProjectsPage = ({
  filters,
  refreshToken,
  isDark,
  subView,
  onSubViewChange,
  openRequestSignal,
  onRequestSignalConsumed,
}: ProjectsPageProps) => {
  const items = useMemo(
    () => [
      {
        key: 'requests',
        label: '请求检索',
        children: (
          <RequestsPage
            filters={filters}
            refreshToken={refreshToken}
            openRequestSignal={openRequestSignal}
            onRequestSignalConsumed={onRequestSignalConsumed}
          />
        ),
      },
      {
        key: 'users',
        label: '用户分析',
        children: <UsersPage filters={filters} refreshToken={refreshToken} isDark={isDark} />,
      },
      {
        key: 'agents-tools',
        label: 'Agent 与工具',
        children: <AgentsToolsPage filters={filters} refreshToken={refreshToken} isDark={isDark} />,
      },
    ],
    [filters, isDark, onRequestSignalConsumed, openRequestSignal, refreshToken],
  );

  return (
    <Card className="projects-card" variant="borderless">
      <Tabs
        activeKey={subView}
        onChange={(key) => onSubViewChange(key as ProjectSubView)}
        items={items}
      />
    </Card>
  );
};

const App = () => {
  const [filters, setFilters] = useState<FilterState>(parseStoredFilters);
  const [activeSection, setActiveSection] = useState<SectionKey>(parseStoredSection);
  const [projectSubView, setProjectSubView] = useState<ProjectSubView>(parseStoredProjectSubView);
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(parseStoredSidebarCollapsed);
  const [refreshToken, setRefreshToken] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [clearMessage, setClearMessage] = useState('');
  const [fridayConfig, setFridayConfig] = useState<FridayConfig>();
  const [fridayBaseUrl, setFridayBaseUrl] = useState('');
  const [fridayModel, setFridayModel] = useState('');
  const [fridayLoading, setFridayLoading] = useState(false);
  const [fridaySaving, setFridaySaving] = useState(false);
  const [fridayMessage, setFridayMessage] = useState('');
  const [openRequestSignal, setOpenRequestSignal] = useState<{ requestId: string; nonce: number } | null>(null);
  const [language, setLanguage] = useState<LanguageKey>(parseStoredLanguage);
  const [isDark, setIsDark] = useState<boolean>(() => {
    if (typeof window === 'undefined') {
      return false;
    }
    const stored = window.localStorage.getItem(STORAGE_KEYS.theme);
    if (stored === 'dark') {
      return true;
    }
    if (stored === 'light') {
      return false;
    }
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  });

  useEffect(() => {
    document.body.classList.toggle('theme-dark', isDark);
    window.localStorage.setItem(STORAGE_KEYS.theme, isDark ? 'dark' : 'light');
  }, [isDark]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEYS.filters, JSON.stringify(filters));
  }, [filters]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEYS.section, activeSection);
  }, [activeSection]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEYS.projectSubView, projectSubView);
  }, [projectSubView]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEYS.language, language);
  }, [language]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEYS.sidebarCollapsed, sidebarCollapsed ? '1' : '0');
  }, [sidebarCollapsed]);

  const sparkTheme = useMemo(() => (isDark ? carbonDarkTheme : carbonTheme), [isDark]);

  const onRefresh = () => {
    setActiveSection('projects');
    setProjectSubView('requests');
    setRefreshToken((prev) => prev + 1);
  };

  const loadFridayConfig = async () => {
    setFridayLoading(true);
    setFridayMessage('');
    try {
      const payload = await analyticsApi.getFridayConfig();
      setFridayConfig(payload);
      setFridayBaseUrl(payload.base_url || '');
      setFridayModel(payload.model || '');
    } catch (error) {
      setFridayMessage(String((error as Error)?.message || error));
    } finally {
      setFridayLoading(false);
    }
  };

  const saveFridayConfig = async () => {
    setFridaySaving(true);
    setFridayMessage('');
    try {
      const payload = await analyticsApi.updateFridayConfig(fridayBaseUrl, fridayModel);
      setFridayConfig(payload);
      setFridayBaseUrl(payload.base_url || '');
      setFridayModel(payload.model || '');
      setFridayMessage(payload.restart_required ? '配置已保存，请手动重启 backend 后生效。' : '配置已保存。');
    } catch (error) {
      setFridayMessage(String((error as Error)?.message || error));
    } finally {
      setFridaySaving(false);
    }
  };

  const clearLocalCache = () => {
    window.localStorage.removeItem(STORAGE_KEYS.filters);
    window.localStorage.removeItem(STORAGE_KEYS.section);
    window.localStorage.removeItem(STORAGE_KEYS.projectSubView);
    window.localStorage.removeItem(STORAGE_KEYS.sidebarCollapsed);
    window.localStorage.removeItem('map_friday_conversations');
    window.localStorage.removeItem('map_friday_active_conversation');
    setFilters(buildDefaultFilters());
    setActiveSection('overview');
    setProjectSubView('requests');
    setSidebarCollapsed(false);
    setClearMessage('已清理本地缓存并恢复默认筛选。');
  };

  const handleFridayAction = (action: FridayAction) => {
    const targetContainer = isKnownContainer(action.container) ? action.container : undefined;
    const targetRequestId = String(action.request_id || '').trim();

    if (action.type === 'open_request_detail' && targetRequestId) {
      setFilters((prev) => ({
        ...prev,
        requestId: targetRequestId,
        ...(targetContainer ? { container: targetContainer } : {}),
      }));
      setActiveSection('projects');
      setProjectSubView('requests');
      setOpenRequestSignal({ requestId: targetRequestId, nonce: Date.now() });
      setRefreshToken((prev) => prev + 1);
      return;
    }

    if (action.type === 'open_traces') {
      setFilters((prev) => ({
        ...prev,
        requestId: targetRequestId || prev.requestId,
        ...(targetContainer ? { container: targetContainer } : {}),
      }));
      setActiveSection('traces');
      setRefreshToken((prev) => prev + 1);
    }
  };

  useEffect(() => {
    if (!settingsOpen) {
      return;
    }
    loadFridayConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsOpen]);

  const currentTitle = sectionTitles[language][activeSection];
  const currentDescription = sectionDescriptions[language][activeSection];

  const showFilterBar = activeSection === 'overview' || activeSection === 'projects' || activeSection === 'traces';

  return (
    <ConfigProvider {...sparkTheme}>
      <div className={`studio-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
        <aside className={`studio-sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}>
          <div className={`studio-brand ${sidebarCollapsed ? 'collapsed' : ''}`}>
            <div className="studio-brand-mark">MAP</div>
            <div className={`studio-brand-meta ${sidebarCollapsed ? 'collapsed' : ''}`}>
              <div className="studio-brand-title">MAP 2.0</div>
              <div className="studio-brand-subtitle">Agent 观测系统</div>
            </div>
          </div>
          <nav className="studio-nav">
            {(['overview', 'projects', 'traces', 'friday'] as SectionKey[]).map((item) => (
              <Button
                key={item}
                className={`studio-nav-item ${activeSection === item ? 'active' : ''}`}
                type="text"
                icon={<SidebarIcon kind={navIcons[item]} />}
                title={navLabels[language][item]}
                aria-label={navLabels[language][item]}
                onClick={() => {
                  setActiveSection(item);
                  setClearMessage('');
                }}
              >
                <span className={`studio-nav-label ${sidebarCollapsed ? 'collapsed' : ''}`}>
                  {navLabels[language][item]}
                </span>
              </Button>
            ))}
          </nav>
          <div className="studio-sidebar-footer">
            <Button
              className="studio-nav-item"
              type="text"
              icon={<SidebarIcon kind="settings" />}
              title="设置"
              aria-label="设置"
              onClick={() => {
                setSettingsOpen(true);
                setClearMessage('');
              }}
            >
              <span className={`studio-nav-label ${sidebarCollapsed ? 'collapsed' : ''}`}>设置</span>
            </Button>
          </div>
          <Button
            className={`studio-sidebar-rail-toggle ${sidebarCollapsed ? 'collapsed' : ''}`}
            type="text"
            icon={<SidebarIcon kind={sidebarCollapsed ? 'collapse-right' : 'collapse-left'} />}
            onClick={() => setSidebarCollapsed((prev) => !prev)}
            title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
            aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
          />
        </aside>

        <main className="studio-main">
          <header className="studio-header">
            <div>
              <h1>{currentTitle}</h1>
              <p>{currentDescription}</p>
            </div>
            <div className="studio-header-actions">
              <Tag>{isDark ? '暗色' : '亮色'}</Tag>
              <Switch checked={isDark} onChange={setIsDark} />
            </div>
          </header>

          {showFilterBar ? (
            <FilterBar
              filters={filters}
              onChange={(patch) => setFilters((prev) => ({ ...prev, ...patch }))}
              onRefresh={onRefresh}
            />
          ) : null}

          <section className="studio-content">
            {activeSection === 'overview' ? (
              <OverviewPage filters={filters} refreshToken={refreshToken} isDark={isDark} />
            ) : null}
            {activeSection === 'projects' ? (
              <ProjectsPage
                filters={filters}
                refreshToken={refreshToken}
                isDark={isDark}
                subView={projectSubView}
                onSubViewChange={setProjectSubView}
                openRequestSignal={openRequestSignal}
                onRequestSignalConsumed={() => setOpenRequestSignal(null)}
              />
            ) : null}
            {activeSection === 'traces' ? (
              <CorrelationPage filters={filters} refreshToken={refreshToken} />
            ) : null}
            {activeSection === 'friday' ? (
              <FridayPage filters={filters} onRunAction={handleFridayAction} />
            ) : null}
          </section>
        </main>
      </div>

      <Drawer title="设置" open={settingsOpen} onClose={() => setSettingsOpen(false)} width={520}>
        <div className="detail-layout">
          <Card title="主题">
            <div className="summary-row">
              <Tag>{isDark ? '暗色' : '亮色'}</Tag>
              <Switch checked={isDark} onChange={setIsDark} />
            </div>
          </Card>

          <Card title="语言">
            <Select
              value={language}
              options={[
                { label: 'English', value: 'en' },
                { label: '中文', value: 'zh' },
              ]}
              onChange={(value: LanguageKey) => setLanguage(value)}
            />
          </Card>

          <Card
            title="Friday 模型配置"
            loading={fridayLoading}
            extra={<Tag>{fridayConfig?.configured ? '已配置' : '未配置'}</Tag>}
          >
            <div className="detail-layout">
              <div>
                <div className="time-align-label">base_url</div>
                <Input
                  value={fridayBaseUrl}
                  onChange={(event) => setFridayBaseUrl(event.target.value)}
                  placeholder="例如 http://127.0.0.1:11434/v1"
                />
              </div>
              <div>
                <div className="time-align-label">model</div>
                <Input
                  value={fridayModel}
                  onChange={(event) => setFridayModel(event.target.value)}
                  placeholder="例如 qwen2.5:latest"
                />
              </div>
              <div className="summary-row">
                <Button
                  className="action-btn-primary"
                  type="primary"
                  loading={fridaySaving}
                  onClick={saveFridayConfig}
                >
                  保存 Friday 配置
                </Button>
                <Button onClick={loadFridayConfig}>重新读取</Button>
              </div>
              {fridayConfig?.restart_required ? (
                <Alert
                  type="warning"
                  showIcon
                  message="配置文件已更新，请手动重启 backend 后生效。"
                />
              ) : null}
              {fridayMessage ? (
                <Alert
                  type={fridayMessage.includes('已保存') ? 'success' : 'error'}
                  showIcon
                  message={fridayMessage}
                />
              ) : null}
              <div className="time-cell-sub">
                当前生效模型: {fridayConfig?.active_model || '-'} | 当前生效地址: {fridayConfig?.active_base_url || '-'}
              </div>
            </div>
          </Card>

          <Card title="数据管理">
            <Button onClick={clearLocalCache}>清理本地缓存</Button>
            {clearMessage ? <div className="table-gap-top">{clearMessage}</div> : null}
            <div className="table-gap-top time-cell-sub">
              仅清理本地 localStorage（筛选条件、页面状态），不会删除服务端数据。
            </div>
          </Card>
        </div>
      </Drawer>
    </ConfigProvider>
  );
};

export default App;
