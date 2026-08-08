import { Button, Card, Drawer, Input, Select, Switch, Table, Tabs, Tag } from '@agentscope-ai/design';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AgentConfigTabKey } from '../../api/types';
import { MODEL_OPTIONS } from './constants';
import type { AdminApi } from './AdminApi';

export interface AgentEditorDrawerProps {
  api: AdminApi;
}

/** 业务智能体编辑抽屉(含基本配置/资源挂载/关联词库/提示词管理/测试)。 */
export default function AgentEditorDrawer({ api }: AgentEditorDrawerProps) {
  const editingAgent = api.editingAgent;

  return (
    <Drawer
      title={editingAgent ? `编辑业务智能体：${editingAgent.display_name}` : '编辑业务智能体'}
      open={api.editingAgentOpen}
      width={860}
      onClose={() => {
        api.setEditingAgentOpen(false);
        api.setEditingAgent(null);
        api.setAgentConfigTab('basic');
      }}
      extra={
        <Button type="primary" onClick={() => void api.saveEditingBusinessAgent()}>
          保存
        </Button>
      }
    >
      {editingAgent ? (
        <Tabs
          activeKey={api.agentConfigTab}
          onChange={(key) => api.setAgentConfigTab(key as AgentConfigTabKey)}
          items={[
            {
              key: 'basic',
              label: '基本配置',
              children: (
                <div className="form-grid">
                  <label>
                    <span>智能体名称</span>
                    <Input
                      value={editingAgent.display_name}
                      onChange={(event) => api.setEditingAgent({ ...editingAgent, display_name: event.target.value })}
                      placeholder="请输入智能体名称"
                    />
                  </label>
                  <label>
                    <span>智能体编号</span>
                    <Input
                      value={editingAgent.agent_code}
                      onChange={(event) => api.setEditingAgent({ ...editingAgent, agent_code: event.target.value })}
                    />
                  </label>
                  <label className="full-span">
                    <span>描述</span>
                    <Input.TextArea
                      autoSize={{ minRows: 3, maxRows: 6 }}
                      value={editingAgent.description}
                      onChange={(event) => api.setEditingAgent({ ...editingAgent, description: event.target.value })}
                      placeholder="请输入描述"
                    />
                  </label>
                  <label>
                    <span>场景</span>
                    <Input
                      value={editingAgent.scene_name}
                      onChange={(event) => api.setEditingAgent({ ...editingAgent, scene_name: event.target.value })}
                    />
                  </label>
                  <label>
                    <span>归属团队</span>
                    <Input
                      value={editingAgent.owner_team}
                      onChange={(event) => api.setEditingAgent({ ...editingAgent, owner_team: event.target.value })}
                    />
                  </label>
                  <label>
                    <span>模型</span>
                    <Select
                      value={editingAgent.model}
                      options={MODEL_OPTIONS}
                      onChange={(value) => api.setEditingAgent({ ...editingAgent, model: value })}
                    />
                  </label>
                  <label>
                    <span>数据范围</span>
                    <Input
                      value={editingAgent.data_scope}
                      onChange={(event) => api.setEditingAgent({ ...editingAgent, data_scope: event.target.value })}
                    />
                  </label>
                  <label>
                    <span>调度权重</span>
                    <Input
                      value={String(editingAgent.weight)}
                      onChange={(event) =>
                        api.setEditingAgent({ ...editingAgent, weight: Number(event.target.value || 0) })
                      }
                    />
                  </label>
                  <label>
                    <span>超时(s)</span>
                    <Input
                      value={String(editingAgent.timeout_s)}
                      onChange={(event) =>
                        api.setEditingAgent({ ...editingAgent, timeout_s: Number(event.target.value || 0) })
                      }
                    />
                  </label>
                  <label>
                    <span>重试次数</span>
                    <Input
                      value={String(editingAgent.retry_limit)}
                      onChange={(event) =>
                        api.setEditingAgent({ ...editingAgent, retry_limit: Number(event.target.value || 0) })
                      }
                    />
                  </label>
                  <label>
                    <span>并发上限</span>
                    <Input
                      value={String(editingAgent.parallel_limit)}
                      onChange={(event) =>
                        api.setEditingAgent({ ...editingAgent, parallel_limit: Number(event.target.value || 0) })
                      }
                    />
                  </label>
                  <label className="switch-row">
                    <span>启用状态</span>
                    <Switch
                      checked={editingAgent.enabled}
                      onChange={(checked) => api.setEditingAgent({ ...editingAgent, enabled: checked })}
                    />
                  </label>
                </div>
              ),
            },
            {
              key: 'resource',
              label: '资源挂载',
              children: (
                <div className="detail-layout">
                  <Card
                    size="small"
                    title="typed 资源挂载"
                    extra={
                      <Button
                        size="small"
                        onClick={() =>
                          api.setEditingAgent({
                            ...editingAgent,
                            resource_mounts: [
                              ...(editingAgent.resource_mounts || []),
                              {
                                mount_id: `mount-${Date.now()}`,
                                resource_type: 'builtin_tool',
                                resource_id: 'general_qa_agent',
                                resource_name: '通用问答',
                                source_name: '',
                                enabled: true,
                                include_all_tools: false,
                                mcp_tool_names: [],
                                builtin_tool_name: 'general_qa_agent',
                                created_at: new Date().toISOString(),
                                config: {},
                              },
                            ],
                          })
                        }
                      >
                        添加资源
                      </Button>
                    }
                  >
                    <Table
                      size="small"
                      pagination={false}
                      rowKey="mount_id"
                      dataSource={editingAgent.resource_mounts || []}
                      columns={[
                        {
                          title: '类型',
                          key: 'resource_type',
                          width: 150,
                          render: (_, row, index) => (
                            <Select
                              value={row.resource_type}
                              options={[
                                { label: 'MCP Server', value: 'mcp_server' },
                                { label: 'MCP Tool', value: 'mcp_tool' },
                                { label: 'Skill', value: 'skill' },
                                { label: '知识库', value: 'knowledge_base' },
                                { label: '数据模型', value: 'data_model' },
                                { label: '内置工具', value: 'builtin_tool' },
                              ]}
                              onChange={(value) => {
                                const next = [...(editingAgent.resource_mounts || [])];
                                next[index] = { ...row, resource_type: value };
                                api.setEditingAgent({ ...editingAgent, resource_mounts: next });
                              }}
                            />
                          ),
                        },
                        {
                          title: '资源',
                          key: 'resource',
                          render: (_, row, index) => {
                            const options =
                              row.resource_type === 'mcp_server' || row.resource_type === 'mcp_tool'
                                ? api.mcpServers.map((server) => ({ label: server.display_name, value: server.server_id }))
                                : row.resource_type === 'skill'
                                  ? api.uploadedSkills.map((skill) => ({
                                      label: skill.display_name,
                                      value: skill.skill_id,
                                    }))
                                  : [
                                      { label: 'general_qa_agent', value: 'general_qa_agent' },
                                      { label: 'search_mounted_kb_agent', value: 'search_mounted_kb_agent' },
                                      { label: 'ask_database_agent', value: 'ask_database_agent' },
                                    ];
                            return (
                              <Select
                                value={row.mcp_server_id || row.skill_id || row.builtin_tool_name || row.resource_id}
                                options={options}
                                onChange={(value) => {
                                  const next = [...(editingAgent.resource_mounts || [])];
                                  next[index] = {
                                    ...row,
                                    resource_id: value,
                                    resource_name: value,
                                    mcp_server_id: row.resource_type.startsWith('mcp') ? value : row.mcp_server_id,
                                    skill_id: row.resource_type === 'skill' ? value : row.skill_id,
                                    builtin_tool_name:
                                      row.resource_type === 'builtin_tool' ? value : row.builtin_tool_name,
                                  };
                                  api.setEditingAgent({ ...editingAgent, resource_mounts: next });
                                }}
                              />
                            );
                          },
                        },
                        {
                          title: 'MCP Tools',
                          key: 'mcp_tools',
                          render: (_, row, index) => {
                            const server = api.mcpServers.find((item) => item.server_id === row.mcp_server_id);
                            return (
                              <Select
                                mode="multiple"
                                disabled={!server || row.resource_type !== 'mcp_tool'}
                                value={row.mcp_tool_names || []}
                                options={(server?.tools || []).map((tool) => ({ label: tool.name, value: tool.name }))}
                                onChange={(value) => {
                                  const next = [...(editingAgent.resource_mounts || [])];
                                  next[index] = { ...row, mcp_tool_names: value, include_all_tools: false };
                                  api.setEditingAgent({ ...editingAgent, resource_mounts: next });
                                }}
                              />
                            );
                          },
                        },
                        {
                          title: '全部工具',
                          key: 'include_all_tools',
                          width: 100,
                          render: (_, row, index) => (
                            <Switch
                              checked={row.include_all_tools}
                              disabled={!row.resource_type.startsWith('mcp')}
                              onChange={(checked) => {
                                const next = [...(editingAgent.resource_mounts || [])];
                                next[index] = { ...row, include_all_tools: checked };
                                api.setEditingAgent({ ...editingAgent, resource_mounts: next });
                              }}
                            />
                          ),
                        },
                        {
                          title: '启用',
                          key: 'enabled',
                          width: 80,
                          render: (_, row, index) => (
                            <Switch
                              checked={row.enabled}
                              onChange={(checked) => {
                                const next = [...(editingAgent.resource_mounts || [])];
                                next[index] = { ...row, enabled: checked };
                                api.setEditingAgent({ ...editingAgent, resource_mounts: next });
                              }}
                            />
                          ),
                        },
                      ]}
                    />
                  </Card>
                  <Card
                    size="small"
                    title="挂载工具"
                    extra={
                      <Button
                        size="small"
                        onClick={() =>
                          api.setEditingAgent({
                            ...editingAgent,
                            tools: [...editingAgent.tools, `新工具${editingAgent.tools.length + 1}`],
                          })
                        }
                      >
                        添加工具
                      </Button>
                    }
                  >
                    <div className="chip-wrap">
                      {editingAgent.tools.map((tool) => (
                        <Tag key={tool}>{tool}</Tag>
                      ))}
                    </div>
                  </Card>
                  <Card
                    size="small"
                    title="资源列表"
                    extra={
                      <Button
                        size="small"
                        onClick={() =>
                          api.setEditingAgent({
                            ...editingAgent,
                            mounted_resources: [
                              ...editingAgent.mounted_resources,
                              {
                                resource_name: `新资源${editingAgent.mounted_resources.length + 1}`,
                                resource_type: '指标数据模型',
                                source_name: 'ESSENDATA',
                                permission_scope: '跟随智能体',
                                dimension_status: '同步成功',
                                created_at: new Date().toISOString(),
                                enabled: true,
                              },
                            ],
                          })
                        }
                      >
                        添加资源
                      </Button>
                    }
                  >
                    <Table
                      size="small"
                      pagination={false}
                      rowKey={(row) => `${row.resource_name}-${row.created_at || ''}`}
                      dataSource={editingAgent.mounted_resources}
                      columns={[
                        { title: '资源', dataIndex: 'resource_name', key: 'resource_name' },
                        { title: '类型', dataIndex: 'resource_type', key: 'resource_type' },
                        { title: '来源', dataIndex: 'source_name', key: 'source_name' },
                        { title: '权限', dataIndex: 'permission_scope', key: 'permission_scope' },
                        { title: '维度标注', dataIndex: 'dimension_status', key: 'dimension_status' },
                        {
                          title: '启用',
                          key: 'enabled',
                          render: (_, row, index) => (
                            <Switch
                              checked={row.enabled}
                              onChange={(checked) => {
                                const next = [...editingAgent.mounted_resources];
                                next[index] = { ...next[index], enabled: checked };
                                api.setEditingAgent({ ...editingAgent, mounted_resources: next });
                              }}
                            />
                          ),
                        },
                      ]}
                    />
                  </Card>
                </div>
              ),
            },
            {
              key: 'glossary',
              label: '关联词库',
              children: (
                <div className="detail-layout">
                  <Card
                    size="small"
                    title="已关联术语库"
                    extra={
                      <Button
                        size="small"
                        onClick={() =>
                          api.setEditingAgent({
                            ...editingAgent,
                            glossary_terms: [
                              ...editingAgent.glossary_terms,
                              `术语库${editingAgent.glossary_terms.length + 1}`,
                            ],
                          })
                        }
                      >
                        + 关联术语库
                      </Button>
                    }
                  >
                    {editingAgent.glossary_terms.length === 0 ? <div className="empty-hint">暂无关联的术语库</div> : null}
                    <div className="chip-wrap">
                      {editingAgent.glossary_terms.map((term, index) => (
                        <Tag key={`${term}-${index}`}>{term}</Tag>
                      ))}
                    </div>
                  </Card>
                </div>
              ),
            },
            {
              key: 'prompt',
              label: '提示词管理',
              children: (
                <div className="detail-layout">
                  <Card size="small" title="提示词版本配置">
                    <div className="form-grid">
                      <label>
                        <span>基座模型配置</span>
                        <Select
                          value={editingAgent.prompt_config.base_model}
                          options={MODEL_OPTIONS}
                          onChange={(value) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: { ...editingAgent.prompt_config, base_model: value },
                            })
                          }
                        />
                      </label>
                      <label>
                        <span>Temperature（0=严谨 ←→ 1=创意）</span>
                        <Input
                          value={String(editingAgent.prompt_config.temperature)}
                          onChange={(event) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: {
                                ...editingAgent.prompt_config,
                                temperature: Number(event.target.value || 0),
                              },
                            })
                          }
                        />
                      </label>
                      <label>
                        <span>Max Tokens（范围: 1024-8196）</span>
                        <Input
                          value={String(editingAgent.prompt_config.max_tokens)}
                          onChange={(event) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: {
                                ...editingAgent.prompt_config,
                                max_tokens: Number(event.target.value || 0),
                              },
                            })
                          }
                        />
                      </label>
                      <label className="full-span">
                        <span>工具调用提示词</span>
                        <Input.TextArea
                          autoSize={{ minRows: 3, maxRows: 8 }}
                          value={editingAgent.prompt_config.tool_call_prompt}
                          onChange={(event) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: {
                                ...editingAgent.prompt_config,
                                tool_call_prompt: event.target.value,
                              },
                            })
                          }
                        />
                      </label>
                      <label className="full-span">
                        <span>系统提示词（兼容字段）</span>
                        <Input.TextArea
                          autoSize={{ minRows: 3, maxRows: 8 }}
                          value={editingAgent.prompt_config.system_prompt}
                          onChange={(event) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: { ...editingAgent.prompt_config, system_prompt: event.target.value },
                            })
                          }
                        />
                      </label>
                      <label className="full-span">
                        <span>User Prompt</span>
                        <Input.TextArea
                          autoSize={{ minRows: 3, maxRows: 8 }}
                          value={editingAgent.prompt_config.user_prompt}
                          onChange={(event) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: { ...editingAgent.prompt_config, user_prompt: event.target.value },
                            })
                          }
                        />
                      </label>
                      <label className="full-span">
                        <span>总结提示词</span>
                        <Input.TextArea
                          autoSize={{ minRows: 3, maxRows: 8 }}
                          value={editingAgent.prompt_config.summary_prompt}
                          onChange={(event) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: { ...editingAgent.prompt_config, summary_prompt: event.target.value },
                            })
                          }
                        />
                      </label>
                      <label className="full-span">
                        <span>版本说明</span>
                        <Input
                          value={editingAgent.prompt_config.version_note}
                          onChange={(event) =>
                            api.setEditingAgent({
                              ...editingAgent,
                              prompt_config: { ...editingAgent.prompt_config, version_note: event.target.value },
                            })
                          }
                        />
                      </label>
                    </div>
                  </Card>
                  <Card
                    size="small"
                    title="工具内部提示词"
                    extra={
                      <Button
                        size="small"
                        onClick={() =>
                          api.setEditingAgent({
                            ...editingAgent,
                            prompt_config: {
                              ...editingAgent.prompt_config,
                              tool_internal_prompts: [
                                ...editingAgent.prompt_config.tool_internal_prompts,
                                { tool_name: 'general_qa_agent', prompt: '', enabled: true },
                              ],
                            },
                          })
                        }
                      >
                        新增
                      </Button>
                    }
                  >
                    <Table
                      size="small"
                      pagination={false}
                      rowKey={(row, index) => `${row.tool_name}-${index}`}
                      dataSource={editingAgent.prompt_config.tool_internal_prompts}
                      columns={[
                        {
                          title: '工具',
                          key: 'tool_name',
                          width: 160,
                          render: (_, row, index) => (
                            <Input
                              value={row.tool_name}
                              onChange={(event) => {
                                const next = [...editingAgent.prompt_config.tool_internal_prompts];
                                next[index] = { ...row, tool_name: event.target.value };
                                api.setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, tool_internal_prompts: next },
                                });
                              }}
                            />
                          ),
                        },
                        {
                          title: '拆分/并发执行提示词',
                          key: 'prompt',
                          render: (_, row, index) => (
                            <Input.TextArea
                              autoSize={{ minRows: 2, maxRows: 5 }}
                              value={row.prompt}
                              onChange={(event) => {
                                const next = [...editingAgent.prompt_config.tool_internal_prompts];
                                next[index] = { ...row, prompt: event.target.value };
                                api.setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, tool_internal_prompts: next },
                                });
                              }}
                            />
                          ),
                        },
                        {
                          title: '启用',
                          key: 'enabled',
                          width: 80,
                          render: (_, row, index) => (
                            <Switch
                              checked={row.enabled}
                              onChange={(checked) => {
                                const next = [...editingAgent.prompt_config.tool_internal_prompts];
                                next[index] = { ...row, enabled: checked };
                                api.setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, tool_internal_prompts: next },
                                });
                              }}
                            />
                          ),
                        },
                      ]}
                    />
                  </Card>
                  <Card size="small" title="工具提示词">
                    <Table
                      size="small"
                      pagination={false}
                      rowKey="tool_name"
                      dataSource={editingAgent.prompt_config.tool_prompts}
                      columns={[
                        { title: '工具', dataIndex: 'tool_name', key: 'tool_name', width: 120 },
                        {
                          title: 'System Prompt',
                          key: 'system_prompt',
                          render: (_, row, index) => (
                            <Input.TextArea
                              autoSize={{ minRows: 2, maxRows: 4 }}
                              value={row.system_prompt}
                              onChange={(event) => {
                                const next = [...editingAgent.prompt_config.tool_prompts];
                                next[index] = { ...next[index], system_prompt: event.target.value };
                                api.setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, tool_prompts: next },
                                });
                              }}
                            />
                          ),
                        },
                        {
                          title: 'User Prompt',
                          key: 'user_prompt',
                          render: (_, row, index) => (
                            <Input.TextArea
                              autoSize={{ minRows: 2, maxRows: 4 }}
                              value={row.user_prompt}
                              onChange={(event) => {
                                const next = [...editingAgent.prompt_config.tool_prompts];
                                next[index] = { ...next[index], user_prompt: event.target.value };
                                api.setEditingAgent({
                                  ...editingAgent,
                                  prompt_config: { ...editingAgent.prompt_config, tool_prompts: next },
                                });
                              }}
                            />
                          ),
                        },
                      ]}
                    />
                  </Card>
                  <Card size="small" title="历史版本">
                    <Table
                      size="small"
                      pagination={false}
                      rowKey="version"
                      dataSource={editingAgent.prompt_config.history_versions}
                      columns={[
                        { title: '版本号', dataIndex: 'version', key: 'version' },
                        { title: '更新时间', dataIndex: 'updated_at', key: 'updated_at' },
                        { title: '操作人', dataIndex: 'operator', key: 'operator' },
                        { title: '模型', dataIndex: 'model', key: 'model' },
                        { title: 'Temperature', dataIndex: 'temperature', key: 'temperature' },
                        { title: 'Max Tokens', dataIndex: 'max_tokens', key: 'max_tokens' },
                        { title: '版本说明', dataIndex: 'version_note', key: 'version_note' },
                      ]}
                    />
                  </Card>
                </div>
              ),
            },
            {
              key: 'test',
              label: '测试',
              children: (
                <div className="detail-layout">
                  <Card size="small" title="测试">
                    <div className="summary-row">
                      <Tag>当前状态：{editingAgent.test_config.publish_status || '未发布'}</Tag>
                      <Tag>最后保存：{editingAgent.test_config.last_saved_at || '-'}</Tag>
                    </div>
                    <div className="agent-test-chat">
                      <div className="agent-test-messages">
                        {api.agentTestMessages.length === 0 ? (
                          <div className="empty-hint">只测试当前业务智能体，不影响已发布配置。</div>
                        ) : null}
                        {api.agentTestMessages.map((item, index) => (
                          <div key={`${item.role}-${index}`} className={`agent-test-message ${item.role}`}>
                            <Tag>{item.role === 'user' ? '用户' : '智能体'}</Tag>
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.content}</ReactMarkdown>
                          </div>
                        ))}
                      </div>
                      <Input.TextArea
                        value={api.agentTestInput}
                        autoSize={{ minRows: 3, maxRows: 6 }}
                        placeholder="向当前业务智能体提问"
                        onChange={(event) => api.setAgentTestInput(event.target.value)}
                        onPressEnter={(event) => {
                          if (!event.shiftKey) {
                            event.preventDefault();
                            void api.runAgentTest();
                          }
                        }}
                      />
                      <div className="chat-input-actions">
                        <Button onClick={() => api.setAgentTestMessages([])}>清空</Button>
                        <Button type="primary" loading={api.agentTestLoading} onClick={() => void api.runAgentTest()}>
                          发送测试
                        </Button>
                      </div>
                    </div>
                  </Card>
                </div>
              ),
            },
          ]}
        />
      ) : null}
    </Drawer>
  );
}
