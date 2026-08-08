import { Button, Card, Input, Select, Switch, Table, Tabs, Tag } from '@agentscope-ai/design';
import type { ColumnsType } from 'antd/es/table';
import type {
  AdminPageKey,
  BusinessAgentConfig,
  ModelTabKey,
} from '../../api/types';
import { parseListInput, stringifyListInput } from '../../lib/utils';
import { createEmptyBusinessAgent, normalizeBusinessAgent } from './businessAgent';
import {
  MODEL_OPTIONS,
  MODEL_TAB_MAP,
  ROUTE_STRATEGY_OPTIONS,
  STREAM_VERSION_OPTIONS,
} from './constants';
import type { AdminApi } from './AdminApi';

const renderModelCenterPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="模型管理"
    extra={
      <div className="backend-toolbar">
        <Button onClick={() => api.addModelRecord()}>添加模型</Button>
        <Button type="primary" onClick={() => void api.saveModelCenter()}>
          保存模型配置
        </Button>
        <Input
          value={api.modelSearch}
          placeholder="搜索模型名称..."
          onChange={(event) => api.setModelSearch(event.target.value)}
          style={{ width: 220 }}
        />
      </div>
    }
  >
    <Tabs
      activeKey={api.modelTab}
      onChange={(key) => api.setModelTab(key as ModelTabKey)}
      items={(Object.keys(MODEL_TAB_MAP) as ModelTabKey[]).map((key) => ({
        key,
        label: MODEL_TAB_MAP[key],
        children: (
          <Table
            pagination={false}
            rowKey={(row) => `${key}_${row.model_name}`}
            dataSource={api.filteredModels}
            columns={[
              {
                title: '模型名称',
                dataIndex: 'model_name',
                key: 'model_name',
                render: (_, row) => (
                  <Input
                    value={row.model_name}
                    onChange={(event) => api.updateModelRecord(row, { model_name: event.target.value })}
                  />
                ),
              },
              {
                title: '模型类型',
                dataIndex: 'model_type',
                key: 'model_type',
                render: (_, row) => (
                  <Select
                    value={row.model_type}
                    options={[
                      { label: '远程', value: '远程' },
                      { label: '本地', value: '本地' },
                    ]}
                    onChange={(value) => api.updateModelRecord(row, { model_type: value })}
                  />
                ),
              },
              {
                title: '模型地址',
                dataIndex: 'model_url',
                key: 'model_url',
                render: (_, row) => (
                  <Input
                    value={row.model_url}
                    onChange={(event) => api.updateModelRecord(row, { model_url: event.target.value })}
                  />
                ),
              },
              {
                title: '默认模型',
                key: 'is_default',
                render: (_, row) => (
                  <Switch checked={row.is_default} onChange={(checked) => api.setDefaultModel(row, checked)} />
                ),
              },
              {
                title: '接口类型',
                dataIndex: 'api_type',
                key: 'api_type',
                render: (_, row) => (
                  <Input
                    value={row.api_type}
                    onChange={(event) => api.updateModelRecord(row, { api_type: event.target.value })}
                  />
                ),
              },
              {
                title: '操作',
                key: 'action',
                render: (_, row) => (
                  <div className="table-actions-inline">
                    <Button type="link" danger onClick={() => api.removeModelRecord(row)}>
                      删除
                    </Button>
                  </div>
                ),
              },
            ]}
          />
        ),
      }))}
    />
  </Card>
);

const renderBasicSettingsPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="基础设置"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection('/api/admin/basic-settings', api.basicSettings, '基础设置已保存', '基础设置保存失败')
        }
      >
        保存配置
      </Button>
    }
  >
    <Table
      rowKey="setting_code"
      pagination={false}
      dataSource={api.basicSettings}
      columns={[
        { title: '配置项', dataIndex: 'setting_name', key: 'setting_name' },
        { title: '分类', dataIndex: 'category', key: 'category' },
        {
          title: '配置值',
          key: 'setting_value',
          render: (_, row, index) => (
            <Input
              disabled={!row.editable}
              value={row.setting_value}
              onChange={(event) => {
                const next = [...api.basicSettings];
                next[index] = { ...next[index], setting_value: event.target.value };
                api.setBasicSettings(next);
              }}
            />
          ),
        },
        { title: '说明', dataIndex: 'description', key: 'description' },
      ]}
    />
  </Card>
);

const renderAddressConfigPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="地址配置"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection('/api/admin/address-configs', api.addressConfigs, '地址配置已保存', '地址配置保存失败')
        }
      >
        保存地址
      </Button>
    }
  >
    <Table
      rowKey="address_code"
      pagination={false}
      dataSource={api.addressConfigs}
      columns={[
        { title: '地址编码', dataIndex: 'address_code', key: 'address_code' },
        { title: '地址名称', dataIndex: 'address_name', key: 'address_name' },
        { title: 'Base URL', dataIndex: 'base_url', key: 'base_url' },
        { title: '超时(s)', dataIndex: 'timeout_s', key: 'timeout_s' },
        {
          title: '状态',
          key: 'enabled',
          render: (_, row, index) => (
            <Switch
              checked={row.enabled}
              onChange={(checked) => {
                const next = [...api.addressConfigs];
                next[index] = { ...next[index], enabled: checked };
                api.setAddressConfigs(next);
              }}
            />
          ),
        },
        { title: '备注', dataIndex: 'remarks', key: 'remarks' },
      ]}
    />
  </Card>
);

const renderDataAccessPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="数据接入"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection('/api/admin/data-connectors', api.dataAccessItems, '数据接入配置已保存', '数据接入配置保存失败')
        }
      >
        保存接入
      </Button>
    }
  >
    <Table
      rowKey="source_name"
      pagination={false}
      dataSource={api.dataAccessItems}
      columns={[
        { title: '数据源', dataIndex: 'source_name', key: 'source_name' },
        { title: '类型', dataIndex: 'source_type', key: 'source_type' },
        { title: '鉴权', dataIndex: 'auth_mode', key: 'auth_mode' },
        { title: 'Endpoint', dataIndex: 'endpoint', key: 'endpoint' },
        { title: '库名', dataIndex: 'database_name', key: 'database_name' },
        { title: '负责人', dataIndex: 'owner', key: 'owner' },
        { title: '最近同步', dataIndex: 'last_sync', key: 'last_sync' },
        {
          title: '启用',
          key: 'enabled',
          render: (_, row, index) => (
            <Switch
              checked={row.enabled}
              onChange={(checked) => {
                const next = [...api.dataAccessItems];
                next[index] = { ...next[index], enabled: checked };
                api.setDataAccessItems(next);
              }}
            />
          ),
        },
      ]}
    />
  </Card>
);

const renderDataAssetsPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="数据管理"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection('/api/admin/data-assets', api.dataAssets, '数据资产配置已保存', '数据资产配置保存失败')
        }
      >
        保存资产
      </Button>
    }
  >
    <Table
      rowKey="asset_code"
      pagination={false}
      dataSource={api.dataAssets}
      columns={[
        { title: '资产编码', dataIndex: 'asset_code', key: 'asset_code' },
        { title: '资产名称', dataIndex: 'asset_name', key: 'asset_name' },
        { title: '类型', dataIndex: 'asset_type', key: 'asset_type' },
        { title: '来源', dataIndex: 'source_name', key: 'source_name' },
        { title: '行数', dataIndex: 'row_count', key: 'row_count' },
        { title: '刷新周期', dataIndex: 'refresh_cycle', key: 'refresh_cycle' },
        {
          title: '启用',
          key: 'enabled',
          render: (_, row, index) => (
            <Switch
              checked={row.enabled}
              onChange={(checked) => {
                const next = [...api.dataAssets];
                next[index] = { ...next[index], enabled: checked };
                api.setDataAssets(next);
              }}
            />
          ),
        },
        { title: '更新时间', dataIndex: 'last_updated', key: 'last_updated' },
      ]}
    />
  </Card>
);

const renderMcpServerPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="MCP Server"
    extra={
      <div className="backend-toolbar">
        <Button
          onClick={() =>
            api.setMcpServers([
              {
                server_id: `mcp-${Date.now()}`,
                display_name: '新 MCP Server',
                transport: 'stdio',
                enabled: true,
                command: '',
                args: [],
                url: '',
                headers: {},
                env_refs: {},
                timeout_s: 30,
                tools: [],
                status: 'unknown',
                remarks: '',
              },
              ...api.mcpServers,
            ])
          }
        >
          新增
        </Button>
        <Button type="primary" onClick={() => void api.saveMcpServers()}>
          保存
        </Button>
      </div>
    }
  >
    <Table
      className="wide-config-table"
      rowKey="server_id"
      pagination={false}
      scroll={{ x: 1280 }}
      dataSource={api.mcpServers}
      columns={[
        { title: 'Server ID', dataIndex: 'server_id', key: 'server_id', width: 180 },
        {
          title: '名称',
          key: 'display_name',
          width: 180,
          render: (_, row, index) => (
            <Input
              value={row.display_name}
              onChange={(event) => {
                const next = [...api.mcpServers];
                next[index] = { ...row, display_name: event.target.value };
                api.setMcpServers(next);
              }}
            />
          ),
        },
        {
          title: 'Transport',
          key: 'transport',
          width: 150,
          render: (_, row, index) => (
            <Select
              value={row.transport}
              options={[
                { label: 'stdio', value: 'stdio' },
                { label: 'sse', value: 'sse' },
                { label: 'streamable_http', value: 'streamable_http' },
              ]}
              onChange={(value) => {
                const next = [...api.mcpServers];
                next[index] = { ...row, transport: value };
                api.setMcpServers(next);
              }}
            />
          ),
        },
        {
          title: '命令 / URL',
          key: 'endpoint',
          width: 260,
          render: (_, row, index) => (
            <Input
              value={row.transport === 'stdio' ? row.command : row.url}
              onChange={(event) => {
                const next = [...api.mcpServers];
                next[index] =
                  row.transport === 'stdio'
                    ? { ...row, command: event.target.value }
                    : { ...row, url: event.target.value };
                api.setMcpServers(next);
              }}
            />
          ),
        },
        {
          title: 'Args',
          key: 'args',
          width: 220,
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.args)}
              onChange={(event) => {
                const next = [...api.mcpServers];
                next[index] = { ...row, args: parseListInput(event.target.value) };
                api.setMcpServers(next);
              }}
            />
          ),
        },
        {
          title: 'Tools',
          key: 'tools',
          width: 260,
          render: (_, row) => (
            <div className="chip-wrap">
              {row.tools.map((tool) => (
                <Tag key={tool.name}>{tool.name}</Tag>
              ))}
            </div>
          ),
        },
        { title: '状态', dataIndex: 'status', key: 'status', width: 160 },
        {
          title: '启用',
          key: 'enabled',
          width: 80,
          render: (_, row, index) => (
            <Switch
              checked={row.enabled}
              onChange={(checked) => {
                const next = [...api.mcpServers];
                next[index] = { ...row, enabled: checked };
                api.setMcpServers(next);
              }}
            />
          ),
        },
      ]}
    />
  </Card>
);

const renderSkillsPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="Skills"
    extra={
      <div className="backend-toolbar">
        <Button
          onClick={() =>
            api.setUploadedSkills([
              {
                skill_id: `skill-${Date.now()}`,
                name: 'new_skill',
                display_name: '新 Skill',
                version: '1.0.0',
                description: '',
                content: '# 新 Skill\n\n请在这里填写 skill 指令。',
                metadata: {},
                mount_agents: [],
                status: 'active',
                source: 'manual_upload',
                uploaded_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
              },
              ...api.uploadedSkills,
            ])
          }
        >
          新增
        </Button>
        <Button type="primary" onClick={() => void api.saveUploadedSkills()}>
          保存
        </Button>
      </div>
    }
  >
    <Table
      className="wide-config-table"
      rowKey="skill_id"
      pagination={false}
      scroll={{ x: 1280 }}
      dataSource={api.uploadedSkills}
      columns={[
        { title: 'Skill ID', dataIndex: 'skill_id', key: 'skill_id', width: 180 },
        {
          title: '展示名称',
          key: 'display_name',
          width: 180,
          render: (_, row, index) => (
            <Input
              value={row.display_name}
              onChange={(event) => {
                const next = [...api.uploadedSkills];
                next[index] = { ...row, display_name: event.target.value };
                api.setUploadedSkills(next);
              }}
            />
          ),
        },
        {
          title: '挂载 Agent',
          key: 'mount_agents',
          width: 220,
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.mount_agents)}
              onChange={(event) => {
                const next = [...api.uploadedSkills];
                next[index] = { ...row, mount_agents: parseListInput(event.target.value) };
                api.setUploadedSkills(next);
              }}
            />
          ),
        },
        {
          title: 'Skill 指令',
          key: 'content',
          render: (_, row, index) => (
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 6 }}
              value={row.content}
              onChange={(event) => {
                const next = [...api.uploadedSkills];
                next[index] = { ...row, content: event.target.value, updated_at: new Date().toISOString() };
                api.setUploadedSkills(next);
              }}
            />
          ),
        },
        {
          title: '状态',
          key: 'status',
          width: 100,
          render: (_, row, index) => (
            <Switch
              checked={row.status === 'active'}
              onChange={(checked) => {
                const next = [...api.uploadedSkills];
                next[index] = { ...row, status: checked ? 'active' : 'inactive' };
                api.setUploadedSkills(next);
              }}
            />
          ),
        },
      ]}
    />
  </Card>
);

const renderMasterAgentPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="Master 智能体"
    extra={
      <Button type="primary" onClick={() => void api.saveMasterConfig()}>
        保存配置
      </Button>
    }
  >
    {api.masterConfig ? (
      <div className="form-grid">
        <label>
          <span>显示名称</span>
          <Input
            value={api.masterConfig.display_name}
            onChange={(event) => api.setMasterConfig({ ...api.masterConfig!, display_name: event.target.value })}
          />
        </label>
        <label>
          <span>模型</span>
          <Select
            value={api.masterConfig.model}
            options={MODEL_OPTIONS}
            onChange={(value) => api.setMasterConfig({ ...api.masterConfig!, model: value })}
          />
        </label>
        <label>
          <span>路由模型</span>
          <Select
            value={api.masterConfig.route_model || api.masterConfig.scene_selector_model}
            options={MODEL_OPTIONS}
            onChange={(value) =>
              api.setMasterConfig({ ...api.masterConfig!, route_model: value, scene_selector_model: value })
            }
          />
        </label>
        <label>
          <span>总结模型</span>
          <Select
            value={api.masterConfig.summary_model || api.masterConfig.model}
            options={MODEL_OPTIONS}
            onChange={(value) => api.setMasterConfig({ ...api.masterConfig!, summary_model: value })}
          />
        </label>
        <label>
          <span>路由策略</span>
          <Select
            value={api.masterConfig.route_strategy}
            options={ROUTE_STRATEGY_OPTIONS}
            onChange={(value) => api.setMasterConfig({ ...api.masterConfig!, route_strategy: value })}
          />
        </label>
        <label>
          <span>Temperature</span>
          <Input
            value={String(api.masterConfig.temperature)}
            onChange={(event) =>
              api.setMasterConfig({ ...api.masterConfig!, temperature: Number(event.target.value || 0) })
            }
          />
        </label>
        <label>
          <span>Max Tokens</span>
          <Input
            value={String(api.masterConfig.max_tokens)}
            onChange={(event) =>
              api.setMasterConfig({ ...api.masterConfig!, max_tokens: Number(event.target.value || 0) })
            }
          />
        </label>
        <label>
          <span>Stream 版本</span>
          <Select
            value={api.masterConfig.stream_version}
            options={STREAM_VERSION_OPTIONS}
            onChange={(value) => api.setMasterConfig({ ...api.masterConfig!, stream_version: value })}
          />
        </label>
        <label>
          <span>超时(s)</span>
          <Input
            value={String(api.masterConfig.timeout_s)}
            onChange={(event) =>
              api.setMasterConfig({ ...api.masterConfig!, timeout_s: Number(event.target.value || 0) })
            }
          />
        </label>
        <label className="full-span">
          <span>总结策略</span>
          <Input
            value={api.masterConfig.summarize_style}
            onChange={(event) => api.setMasterConfig({ ...api.masterConfig!, summarize_style: event.target.value })}
          />
        </label>
        <label className="full-span">
          <span>场景路由提示词</span>
          <Input.TextArea
            autoSize={{ minRows: 4, maxRows: 10 }}
            value={api.masterConfig.route_prompt}
            onChange={(event) => api.setMasterConfig({ ...api.masterConfig!, route_prompt: event.target.value })}
          />
        </label>
        <label className="full-span">
          <span>总结提示词</span>
          <Input.TextArea
            autoSize={{ minRows: 4, maxRows: 10 }}
            value={api.masterConfig.summary_prompt}
            onChange={(event) => api.setMasterConfig({ ...api.masterConfig!, summary_prompt: event.target.value })}
          />
        </label>
        <label className="full-span">
          <span>执行策略（每行一条）</span>
          <Input.TextArea
            autoSize={{ minRows: 4, maxRows: 8 }}
            value={api.masterConfig.policies.join('\n')}
            onChange={(event) =>
              api.setMasterConfig({
                ...api.masterConfig!,
                policies: event.target.value
                  .split('\n')
                  .map((item) => item.trim())
                  .filter(Boolean),
              })
            }
          />
        </label>
        <div className="full-span detail-layout">
          <Card
            size="small"
            title={`提示词版本：${api.masterConfig.current_version || 'draft'}`}
            extra={
              <div className="backend-toolbar">
                <Button onClick={() => void api.diffMasterPrompt(api.masterConfig?.current_version)}>查看 Diff</Button>
                <Button type="primary" onClick={() => void api.publishMasterPrompt()}>
                  发布提示词
                </Button>
              </div>
            }
          >
            {api.masterDiff ? (
              <pre className="raw-json master-diff-view">{api.masterDiff}</pre>
            ) : (
              <div className="empty-hint">发布前可查看当前草稿与历史版本差异。</div>
            )}
          </Card>
          <Table
            size="small"
            rowKey="version"
            pagination={false}
            dataSource={api.masterVersions}
            columns={[
              { title: '版本', dataIndex: 'version', key: 'version', width: 100 },
              { title: '操作人', dataIndex: 'operator', key: 'operator', width: 100 },
              { title: '说明', dataIndex: 'note', key: 'note' },
              { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 180 },
              {
                title: '操作',
                key: 'actions',
                width: 220,
                render: (_, row) => (
                  <div className="backend-toolbar">
                    <Button size="small" onClick={() => void api.diffMasterPrompt(row.version)}>
                      Diff 当前
                    </Button>
                    <Button size="small" onClick={() => void api.rollbackMasterPrompt(row.version)}>
                      切换
                    </Button>
                  </div>
                ),
              },
            ]}
          />
        </div>
      </div>
    ) : null}
  </Card>
);

const renderBusinessAgentPage = (api: AdminApi) => {
  const businessColumns: ColumnsType<BusinessAgentConfig> = [
    {
      title: '智能体名称',
      dataIndex: 'display_name',
      key: 'display_name',
      width: 160,
    },
    { title: '编码', dataIndex: 'agent_code', key: 'agent_code', width: 140 },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      render: (value) => <span className="text-ellipsis-cell">{value || '-'}</span>,
    },
    { title: '模型', dataIndex: 'model', key: 'model', width: 160 },
    {
      title: '状态',
      key: 'enabled',
      width: 90,
      render: (_, row) => <Tag color={row.enabled ? 'green' : 'red'}>{row.enabled ? '启用' : '停用'}</Tag>,
    },
    {
      title: '最后发布时间',
      key: 'last_updated',
      width: 170,
      render: (_, row) => row.last_updated || '-',
    },
    {
      title: '操作',
      key: 'action',
      width: 88,
      render: (_, row) => (
        <Button
          type="link"
          onClick={() => {
            api.setEditingAgent(normalizeBusinessAgent({ ...row }));
            api.setEditingAgentOpen(true);
            api.setAgentConfigTab('basic');
          }}
        >
          配置
        </Button>
      ),
    },
  ];

  return (
    <Card
      loading={api.adminLoading}
      title="业务智能体"
      extra={
        <div className="backend-toolbar">
          <Button
            type="primary"
            onClick={() => {
              api.setEditingAgent(createEmptyBusinessAgent());
              api.setEditingAgentOpen(true);
              api.setAgentConfigTab('basic');
            }}
          >
            新增业务智能体
          </Button>
          <Button onClick={() => void api.loadAdminData()}>刷新</Button>
        </div>
      }
    >
      <Table rowKey="agent_code" dataSource={api.businessAgents} columns={businessColumns} pagination={false} />
    </Card>
  );
};

const renderFlowPolicyPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="心流策略"
    extra={
      <Button type="primary" onClick={() => void api.saveFlowPolicy()}>
        保存策略
      </Button>
    }
  >
    {api.flowPolicy ? (
      <div className="form-grid">
        <label className="switch-row">
          <span>Scenario Policy 启用</span>
          <Switch
            checked={api.flowPolicy.scenario_policy.enabled}
            onChange={(checked) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                scenario_policy: { ...api.flowPolicy!.scenario_policy, enabled: checked },
              })
            }
          />
        </label>
        <label>
          <span>Scenario 模式</span>
          <Select
            value={api.flowPolicy.scenario_policy.mode}
            options={[
              { label: 'auto', value: 'auto' },
              { label: 'manual', value: 'manual' },
            ]}
            onChange={(value) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                scenario_policy: { ...api.flowPolicy!.scenario_policy, mode: value },
              })
            }
          />
        </label>
        <label className="switch-row">
          <span>允许图修复</span>
          <Switch
            checked={api.flowPolicy.scenario_policy.allow_graph_repair}
            onChange={(checked) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                scenario_policy: { ...api.flowPolicy!.scenario_policy, allow_graph_repair: checked },
              })
            }
          />
        </label>
        <label>
          <span>最大修复轮次</span>
          <Input
            value={String(api.flowPolicy.scenario_policy.max_graph_cycles)}
            onChange={(event) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                scenario_policy: {
                  ...api.flowPolicy!.scenario_policy,
                  max_graph_cycles: Number(event.target.value || 0),
                },
              })
            }
          />
        </label>
        <label className="full-span">
          <span>允许场景（逗号或换行分隔，留空表示自动匹配）</span>
          <Input.TextArea
            autoSize={{ minRows: 2, maxRows: 6 }}
            value={stringifyListInput(api.flowPolicy.scenario_policy.allowed_scenarios || [])}
            onChange={(event) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                scenario_policy: {
                  ...api.flowPolicy!.scenario_policy,
                  allowed_scenarios: parseListInput(event.target.value),
                },
              })
            }
          />
        </label>
        <label className="switch-row">
          <span>Skill Policy 启用</span>
          <Switch
            checked={api.flowPolicy.skill_policy.enabled}
            onChange={(checked) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                skill_policy: { ...api.flowPolicy!.skill_policy, enabled: checked },
              })
            }
          />
        </label>
        <label>
          <span>挂载模式</span>
          <Select
            value={api.flowPolicy.skill_policy.mount_mode}
            options={[{ label: 'agent_scoped', value: 'agent_scoped' }]}
            onChange={(value) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                skill_policy: { ...api.flowPolicy!.skill_policy, mount_mode: value },
              })
            }
          />
        </label>
        <label className="switch-row">
          <span>执行时二次鉴权</span>
          <Switch
            checked={api.flowPolicy.skill_policy.runtime_auth_check}
            onChange={(checked) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                skill_policy: { ...api.flowPolicy!.skill_policy, runtime_auth_check: checked },
              })
            }
          />
        </label>
        <label>
          <span>最大节点预算</span>
          <Input
            value={String(api.flowPolicy.max_node_budget)}
            onChange={(event) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                max_node_budget: Number(event.target.value || 0),
              })
            }
          />
        </label>
        <label className="switch-row">
          <span>编排失败回退全域链路</span>
          <Switch
            checked={api.flowPolicy.fallback_to_global}
            onChange={(checked) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                fallback_to_global: checked,
              })
            }
          />
        </label>
        <label className="full-span">
          <span>备注</span>
          <Input.TextArea
            autoSize={{ minRows: 2, maxRows: 6 }}
            value={api.flowPolicy.notes}
            onChange={(event) =>
              api.setFlowPolicy({
                ...api.flowPolicy!,
                notes: event.target.value,
              })
            }
          />
        </label>
      </div>
    ) : null}
  </Card>
);

const renderScenarioHubPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="ScenarioHub 场景包"
    extra={
      <Button type="primary" onClick={() => void api.saveScenarioPacks()}>
        保存场景包
      </Button>
    }
  >
    <Table
      rowKey="scenario_id"
      pagination={false}
      dataSource={api.scenarioPacks}
      columns={[
        { title: 'Scenario ID', dataIndex: 'scenario_id', key: 'scenario_id', width: 220 },
        {
          title: '场景名称',
          key: 'display_name',
          width: 150,
          render: (_, row, index) => (
            <Input
              value={row.display_name}
              onChange={(event) => {
                const next = [...api.scenarioPacks];
                next[index] = { ...next[index], display_name: event.target.value };
                api.setScenarioPacks(next);
              }}
            />
          ),
        },
        {
          title: '业务域',
          key: 'domain',
          width: 180,
          render: (_, row, index) => (
            <Input
              value={row.domain}
              onChange={(event) => {
                const next = [...api.scenarioPacks];
                next[index] = { ...next[index], domain: event.target.value };
                api.setScenarioPacks(next);
              }}
            />
          ),
        },
        {
          title: '触发意图',
          key: 'trigger_intents',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.trigger_intents)}
              onChange={(event) => {
                const next = [...api.scenarioPacks];
                next[index] = { ...next[index], trigger_intents: parseListInput(event.target.value) };
                api.setScenarioPacks(next);
              }}
            />
          ),
        },
        {
          title: 'Required Agents',
          key: 'required_agents',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.required_agents)}
              onChange={(event) => {
                const next = [...api.scenarioPacks];
                next[index] = { ...next[index], required_agents: parseListInput(event.target.value) };
                api.setScenarioPacks(next);
              }}
            />
          ),
        },
        {
          title: 'Auth Scopes',
          key: 'auth_scopes',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.auth_scopes)}
              onChange={(event) => {
                const next = [...api.scenarioPacks];
                next[index] = { ...next[index], auth_scopes: parseListInput(event.target.value) };
                api.setScenarioPacks(next);
              }}
            />
          ),
        },
        {
          title: '状态',
          key: 'status',
          width: 80,
          render: (_, row, index) => (
            <Switch
              checked={row.status === 'active'}
              onChange={(checked) => {
                const next = [...api.scenarioPacks];
                next[index] = { ...next[index], status: checked ? 'active' : 'inactive' };
                api.setScenarioPacks(next);
              }}
            />
          ),
        },
      ]}
    />
  </Card>
);

const renderSkillHubPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="SkillHub 能力挂载"
    extra={
      <Button type="primary" onClick={() => void api.saveFlowSkillDescriptors()}>
        保存能力
      </Button>
    }
  >
    <Table
      className="wide-config-table"
      rowKey="skill_id"
      pagination={false}
      scroll={{ x: 1280 }}
      dataSource={api.flowSkillDescriptors}
      columns={[
        { title: 'Skill ID', dataIndex: 'skill_id', key: 'skill_id', width: 190 },
        {
          title: '展示名称',
          key: 'display_name',
          width: 150,
          render: (_, row, index) => (
            <Input
              value={row.display_name}
              onChange={(event) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], display_name: event.target.value };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
        {
          title: 'Tool Name',
          key: 'tool_name',
          width: 180,
          render: (_, row, index) => (
            <Input
              value={row.tool_name}
              onChange={(event) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], tool_name: event.target.value };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
        {
          title: '挂载 Agent',
          key: 'mount_agents',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.mount_agents)}
              onChange={(event) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], mount_agents: parseListInput(event.target.value) };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
        {
          title: '所需 Scope',
          key: 'required_scopes',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.required_scopes)}
              onChange={(event) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], required_scopes: parseListInput(event.target.value) };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
        {
          title: '允许用户',
          key: 'allowed_users',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.allowed_users || ['*'])}
              onChange={(event) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], allowed_users: parseListInput(event.target.value) };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
        {
          title: '允许租户',
          key: 'allowed_tenants',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.allowed_tenants || ['*'])}
              onChange={(event) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], allowed_tenants: parseListInput(event.target.value) };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
        {
          title: '允许场景',
          key: 'allowed_scenarios',
          render: (_, row, index) => (
            <Input
              value={stringifyListInput(row.allowed_scenarios || [])}
              onChange={(event) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], allowed_scenarios: parseListInput(event.target.value) };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
        {
          title: '状态',
          key: 'status',
          width: 80,
          render: (_, row, index) => (
            <Switch
              checked={row.status === 'active'}
              onChange={(checked) => {
                const next = [...api.flowSkillDescriptors];
                next[index] = { ...next[index], status: checked ? 'active' : 'inactive' };
                api.setFlowSkillDescriptors(next);
              }}
            />
          ),
        },
      ]}
    />
  </Card>
);

const renderSessionPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="会话管理"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection('/api/admin/session-policies', api.sessionPolicies, '会话策略已保存', '会话策略保存失败')
        }
      >
        保存会话策略
      </Button>
    }
  >
    <Table
      rowKey="policy_code"
      pagination={false}
      dataSource={api.sessionPolicies}
      columns={[
        { title: '策略编码', dataIndex: 'policy_code', key: 'policy_code' },
        { title: '策略名称', dataIndex: 'policy_name', key: 'policy_name' },
        { title: '保留天数', dataIndex: 'retention_days', key: 'retention_days' },
        { title: '限流(QPM)', dataIndex: 'rate_limit_qpm', key: 'rate_limit_qpm' },
        { title: '状态', dataIndex: 'status', key: 'status' },
        { title: '更新人', dataIndex: 'updated_by', key: 'updated_by' },
        { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
      ]}
    />
  </Card>
);

const renderDashboardPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="数据看板"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection('/api/admin/dashboard-cards', api.dashboardCards, '看板配置已保存', '看板配置保存失败')
        }
      >
        保存看板
      </Button>
    }
  >
    <Table
      rowKey="card_code"
      pagination={false}
      dataSource={api.dashboardCards}
      columns={[
        { title: '卡片编码', dataIndex: 'card_code', key: 'card_code' },
        { title: '卡片名称', dataIndex: 'card_name', key: 'card_name' },
        { title: '指标表达式', dataIndex: 'metric_expr', key: 'metric_expr' },
        { title: '刷新间隔(s)', dataIndex: 'refresh_interval_s', key: 'refresh_interval_s' },
        {
          title: '启用',
          key: 'enabled',
          render: (_, row, index) => (
            <Switch
              checked={row.enabled}
              onChange={(checked) => {
                const next = [...api.dashboardCards];
                next[index] = { ...next[index], enabled: checked };
                api.setDashboardCards(next);
              }}
            />
          ),
        },
      ]}
    />
  </Card>
);

const renderSecurityPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="安全管理"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection('/api/admin/security-policies', api.securityPolicies, '安全策略已保存', '安全策略保存失败')
        }
      >
        保存安全策略
      </Button>
    }
  >
    <Table
      rowKey="rule_code"
      pagination={false}
      dataSource={api.securityPolicies}
      columns={[
        { title: '规则编码', dataIndex: 'rule_code', key: 'rule_code' },
        { title: '规则名称', dataIndex: 'rule_name', key: 'rule_name' },
        { title: '级别', dataIndex: 'severity', key: 'severity' },
        { title: '策略', dataIndex: 'strategy', key: 'strategy' },
        {
          title: '启用',
          key: 'enabled',
          render: (_, row, index) => (
            <Switch
              checked={row.enabled}
              onChange={(checked) => {
                const next = [...api.securityPolicies];
                next[index] = { ...next[index], enabled: checked };
                api.setSecurityPolicies(next);
              }}
            />
          ),
        },
        { title: '更新时间', dataIndex: 'last_updated', key: 'last_updated' },
      ]}
    />
  </Card>
);

const renderGlossaryPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="词库管理"
    extra={
      <Button
        type="primary"
        onClick={() => void api.saveSection('/api/admin/glossary-terms', api.glossaryTerms, '词库已保存', '词库保存失败')}
      >
        保存词库
      </Button>
    }
  >
    <Table
      rowKey="term"
      pagination={false}
      dataSource={api.glossaryTerms}
      columns={[
        { title: '词条', dataIndex: 'term', key: 'term' },
        { title: '分类', dataIndex: 'category', key: 'category' },
        { title: '定义', dataIndex: 'definition', key: 'definition' },
        { title: '同义词', key: 'synonyms', render: (_, row) => row.synonyms.join(' / ') },
        { title: '状态', dataIndex: 'status', key: 'status' },
        { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
      ]}
    />
  </Card>
);

const renderHomeRecommendationPage = (api: AdminApi) => (
  <Card
    loading={api.adminLoading}
    title="首页推荐"
    extra={
      <Button
        type="primary"
        onClick={() =>
          void api.saveSection(
            '/api/admin/homepage-recommendations',
            api.homepageRecommendations,
            '首页推荐已保存',
            '首页推荐保存失败',
          )
        }
      >
        保存推荐
      </Button>
    }
  >
    <Table
      rowKey="recommendation_id"
      pagination={false}
      dataSource={api.homepageRecommendations}
      columns={[
        { title: '推荐ID', dataIndex: 'recommendation_id', key: 'recommendation_id' },
        { title: '标题', dataIndex: 'title', key: 'title' },
        { title: '目标场景', dataIndex: 'target_scene', key: 'target_scene' },
        { title: '优先级', dataIndex: 'priority', key: 'priority' },
        {
          title: '启用',
          key: 'enabled',
          render: (_, row, index) => (
            <Switch
              checked={row.enabled}
              onChange={(checked) => {
                const next = [...api.homepageRecommendations];
                next[index] = { ...next[index], enabled: checked };
                api.setHomepageRecommendations(next);
              }}
            />
          ),
        },
        { title: '操作人', dataIndex: 'operator', key: 'operator' },
        { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
      ]}
    />
  </Card>
);

const renderPermissionPage = (api: AdminApi) => (
  <div className="backend-grid-2">
    <Card
      title="权限策略"
      loading={api.adminLoading}
      extra={
        <Button type="primary" onClick={() => void api.savePermissionRules()}>
          保存权限
        </Button>
      }
    >
      <Table
        rowKey="role"
        pagination={false}
        dataSource={api.permissionRules}
        columns={[
          { title: '角色', dataIndex: 'role', key: 'role' },
          { title: '可用智能体', key: 'allowed_agents', render: (_, row) => row.allowed_agents.join(' / ') },
          { title: '可用操作', key: 'allowed_operations', render: (_, row) => row.allowed_operations.join(' / ') },
          { title: '部门范围', key: 'department_codes', render: (_, row) => row.department_codes.join(' / ') || '-' },
          { title: '指定人员', key: 'staff_codes', render: (_, row) => row.staff_codes.join(' / ') || '-' },
          {
            title: '启用',
            key: 'active',
            render: (_, row, index) => (
              <Switch
                checked={row.active}
                onChange={(checked) => {
                  const next = [...api.permissionRules];
                  next[index] = { ...next[index], active: checked };
                  api.setPermissionRules(next);
                }}
              />
            ),
          },
        ]}
      />
    </Card>

    <Card
      title="知识库与 Skill"
      loading={api.adminLoading}
      extra={
        <Button type="primary" onClick={() => void api.saveSkillPolicies()}>
          保存 Skill
        </Button>
      }
    >
      <Tabs
        items={[
          {
            key: 'kb',
            label: '知识库',
            children: (
              <Table
                rowKey="kb_code"
                pagination={false}
                dataSource={api.knowledgeBindings}
                columns={[
                  { title: '团队', dataIndex: 'team', key: 'team' },
                  { title: '知识库', dataIndex: 'kb_name', key: 'kb_name' },
                  { title: '编码', dataIndex: 'kb_code', key: 'kb_code' },
                  { title: '类型', dataIndex: 'kb_type', key: 'kb_type' },
                  { title: 'Embedding', dataIndex: 'embedding_model', key: 'embedding_model' },
                  { title: '更新策略', dataIndex: 'update_mode', key: 'update_mode' },
                  { title: '可访问角色', key: 'readable_roles', render: (_, row) => row.readable_roles.join(' / ') },
                ]}
              />
            ),
          },
          {
            key: 'skill',
            label: 'Skill',
            children: (
              <Table
                rowKey="skill_code"
                pagination={false}
                dataSource={api.skillPolicies}
                columns={[
                  { title: 'Skill', dataIndex: 'skill_name', key: 'skill_name' },
                  { title: '编码', dataIndex: 'skill_code', key: 'skill_code' },
                  { title: '类型', dataIndex: 'skill_type', key: 'skill_type' },
                  { title: '来源', dataIndex: 'source', key: 'source' },
                  { title: '最大调用', dataIndex: 'max_calls', key: 'max_calls' },
                  { title: '超时(s)', dataIndex: 'timeout_s', key: 'timeout_s' },
                  { title: '可见角色', key: 'visible_roles', render: (_, row) => row.visible_roles.join(' / ') },
                  {
                    title: '状态',
                    key: 'enabled',
                    render: (_, row, index) => (
                      <Switch
                        checked={row.enabled}
                        onChange={(checked) => {
                          const next = [...api.skillPolicies];
                          next[index] = { ...next[index], enabled: checked };
                          api.setSkillPolicies(next);
                        }}
                      />
                    ),
                  },
                ]}
              />
            ),
          },
        ]}
      />
    </Card>
  </div>
);

const renderUserRolePage = (api: AdminApi) => (
  <Card title="角色与用户" loading={api.adminLoading}>
    <Tabs
      items={[
        {
          key: 'role',
          label: '角色策略',
          children: (
            <>
              <div className="inline-right-btn">
                <Button type="primary" onClick={() => void api.saveRolePolicies()}>
                  保存角色策略
                </Button>
              </div>
              <Table
                rowKey="role_code"
                pagination={false}
                dataSource={api.rolePolicies}
                columns={[
                  { title: '角色编码', dataIndex: 'role_code', key: 'role_code' },
                  { title: '角色名称', dataIndex: 'role_name', key: 'role_name' },
                  { title: '权限', key: 'permissions', render: (_, row) => row.permissions.join(' / ') },
                  { title: '数据范围', dataIndex: 'data_scope', key: 'data_scope' },
                  {
                    title: '启用',
                    key: 'enabled',
                    render: (_, row, index) => (
                      <Switch
                        checked={row.enabled}
                        onChange={(checked) => {
                          const next = [...api.rolePolicies];
                          next[index] = { ...next[index], enabled: checked };
                          api.setRolePolicies(next);
                        }}
                      />
                    ),
                  },
                ]}
              />
            </>
          ),
        },
        {
          key: 'user',
          label: '用户管理',
          children: (
            <>
              <div className="inline-right-btn">
                <Button type="primary" onClick={() => void api.saveUserAccounts()}>
                  保存用户配置
                </Button>
              </div>
              <Table
                rowKey="staff_code"
                pagination={false}
                dataSource={api.userAccounts}
                columns={[
                  { title: '工号', dataIndex: 'staff_code', key: 'staff_code' },
                  { title: '姓名', dataIndex: 'user_name', key: 'user_name' },
                  { title: '部门', dataIndex: 'department', key: 'department' },
                  { title: '角色', key: 'roles', render: (_, row) => row.roles.join(' / ') },
                  { title: '状态', dataIndex: 'status', key: 'status' },
                  { title: '最近登录', dataIndex: 'last_login', key: 'last_login' },
                ]}
              />
            </>
          ),
        },
      ]}
    />
  </Card>
);

const renderReleasePanel = (api: AdminApi) => (
  <Card title="发布记录" loading={api.adminLoading}>
    <div className="release-row">
      <Input
        value={api.releaseNote}
        placeholder="输入发布说明，例如：更新经营分析智能体工具权限"
        onChange={(event) => api.setReleaseNote(event.target.value)}
      />
      <Input
        value={api.releaseVersion}
        placeholder="版本号，如 v1.3.0"
        onChange={(event) => api.setReleaseVersion(event.target.value)}
      />
      <Select
        value={api.releaseRiskLevel}
        options={[
          { label: 'low', value: 'low' },
          { label: 'medium', value: 'medium' },
          { label: 'high', value: 'high' },
        ]}
        onChange={(value) => api.setReleaseRiskLevel(value)}
      />
      <Button type="primary" onClick={() => void api.publishConfigSnapshot()}>
        新增发布记录
      </Button>
    </div>
    <Table
      rowKey="id"
      dataSource={api.releaseHistory}
      pagination={false}
      columns={[
        { title: 'ID', dataIndex: 'id', key: 'id' },
        { title: '版本', dataIndex: 'version', key: 'version' },
        { title: '操作人', dataIndex: 'operator', key: 'operator' },
        { title: '说明', dataIndex: 'note', key: 'note' },
        { title: '影响智能体', key: 'affected_agents', render: (_, row) => row.affected_agents.join(' / ') },
        { title: '风险等级', dataIndex: 'risk_level', key: 'risk_level' },
        { title: '时间', dataIndex: 'created_at', key: 'created_at' },
      ]}
    />
  </Card>
);

/** 根据当前管理页 key 渲染对应页面 */
export const renderAdminPage = (adminPage: AdminPageKey, api: AdminApi) => {
  switch (adminPage) {
    case 'model-center':
      return renderModelCenterPage(api);
    case 'basic-settings':
      return renderBasicSettingsPage(api);
    case 'address-config':
      return renderAddressConfigPage(api);
    case 'data-access':
      return renderDataAccessPage(api);
    case 'data-assets':
      return renderDataAssetsPage(api);
    case 'mcp-server':
      return renderMcpServerPage(api);
    case 'skills':
      return renderSkillsPage(api);
    case 'master-agent':
      return renderMasterAgentPage(api);
    case 'business-agent':
      return renderBusinessAgentPage(api);
    case 'flow-policy':
      return renderFlowPolicyPage(api);
    case 'scenario-hub':
      return renderScenarioHubPage(api);
    case 'skill-hub':
      return renderSkillHubPage(api);
    case 'session-management':
      return renderSessionPage(api);
    case 'dashboard':
      return renderDashboardPage(api);
    case 'security':
      return renderSecurityPage(api);
    case 'glossary':
      return renderGlossaryPage(api);
    case 'home-recommendation':
      return renderHomeRecommendationPage(api);
    case 'permission':
      return renderPermissionPage(api);
    case 'user-role':
      return renderUserRolePage(api);
    default:
      return renderModelCenterPage(api);
  }
};

export { renderReleasePanel };
