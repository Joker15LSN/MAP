import { useEffect, useMemo, useRef } from 'react';
import { Button, Card, Input, Modal, Select, Switch, Tag } from '@agentscope-ai/design';
import type { RequestDetail } from 'map-tree-core';
import { RequestCallTree } from 'map-tree-core';
import type {
  ChatMessage,
  ChatMode,
  FlowRequestConfig,
  SourceReferenceItem,
  TracePanelMode,
} from '../../api/types';
import { CHAT_MODE_LABEL } from './constants';
import { cloneFlowRequestConfig } from './flowConfig';
import { computeFlowHitData } from './flowHit';
import MessageList from './components/MessageList';
import SourcePanel from './components/SourcePanel';
import FlowHitPanel from './components/FlowHitPanel';

export interface ChatViewProps {
  chatMode: ChatMode;
  onChatModeChange: (mode: ChatMode) => void;
  messages: ChatMessage[];
  isStreaming: boolean;
  inputValue: string;
  onInputChange: (value: string) => void;
  onSend: (query: string) => void;
  onStop: () => void;
  detail?: RequestDetail;
  tracePanelOpen: boolean;
  tracePanelMode: TracePanelMode;
  onTracePanelOpenChange: (open: boolean) => void;
  onTracePanelModeChange: (mode: TracePanelMode) => void;
  traceSourceItems: SourceReferenceItem[];
  latestAssistantContent: string;
  flowUseRuntimePolicy: boolean;
  onFlowUseRuntimePolicyChange: (checked: boolean) => void;
  flowRuntimeSnapshotUpdatedAt?: string;
  flowSessionOverride: FlowRequestConfig;
  onFlowSessionOverrideChange: (
    updater: FlowRequestConfig | ((current: FlowRequestConfig) => FlowRequestConfig),
  ) => void;
  flowOverrideOpen: boolean;
  onFlowOverrideOpenChange: (open: boolean) => void;
  onFlowOverrideReset: () => void;
  expandedInputOpen: boolean;
  expandedInputValue: string;
  onExpandedInputOpenChange: (open: boolean) => void;
  onExpandedInputValueChange: (value: string) => void;
}

/** 聊天主界面(前台问答)。状态与副作用由 App shell 注入,本组件仅负责渲染与交互。 */
export default function ChatView({
  chatMode,
  onChatModeChange,
  messages,
  isStreaming,
  inputValue,
  onInputChange,
  onSend,
  onStop,
  detail,
  tracePanelOpen,
  tracePanelMode,
  onTracePanelOpenChange,
  onTracePanelModeChange,
  traceSourceItems,
  latestAssistantContent,
  flowUseRuntimePolicy,
  onFlowUseRuntimePolicyChange,
  flowRuntimeSnapshotUpdatedAt,
  flowSessionOverride,
  onFlowSessionOverrideChange,
  flowOverrideOpen,
  onFlowOverrideOpenChange,
  onFlowOverrideReset,
  expandedInputOpen,
  expandedInputValue,
  onExpandedInputOpenChange,
  onExpandedInputValueChange,
}: ChatViewProps) {
  const flowHitData = useMemo(() => computeFlowHitData(detail), [detail]);
  const hasFlowHitData = Boolean(flowHitData);
  const chatMessageListRef = useRef<HTMLDivElement | null>(null);
  const tracePanelTitle =
    tracePanelMode === 'trace' ? '问答溯源' : tracePanelMode === 'source' ? '回答来源' : 'Flow 策略命中';

  useEffect(() => {
    const holder = chatMessageListRef.current;
    if (!holder) {
      return;
    }
    const behavior: ScrollBehavior = isStreaming ? 'auto' : 'smooth';
    const raf = window.requestAnimationFrame(() => {
      holder.scrollTo({
        top: holder.scrollHeight,
        behavior,
      });
    });
    return () => window.cancelAnimationFrame(raf);
  }, [messages, isStreaming, chatMode, tracePanelOpen]);

  return (
    <div className={`map-chat-layout ${tracePanelOpen ? 'trace-open' : ''}`}>
      <Card className="map-chat-main" title={chatMode === 'flow' ? '心流智能协作' : '全域智能协作'}>
        <div className="chat-main-header">
          <div className="mode-switch-group">
            <Button
              size="small"
              type={chatMode === 'global' ? 'primary' : 'default'}
              disabled={isStreaming}
              onClick={() => onChatModeChange('global')}
            >
              全域模式
            </Button>
            <Button
              size="small"
              type={chatMode === 'flow' ? 'primary' : 'default'}
              disabled={isStreaming}
              onClick={() => onChatModeChange('flow')}
            >
              心流模式
            </Button>
          </div>
          <Tag className="mode-tag">{CHAT_MODE_LABEL[chatMode]}</Tag>
          <Tag className={`stream-state-tag ${isStreaming ? 'running' : 'ready'}`}>
            {isStreaming ? '思考中...' : '就绪'}
          </Tag>
          {chatMode === 'flow' ? (
            <div className="flow-mode-runtime-bar">
              <Tag>{flowUseRuntimePolicy ? '策略来源：管理端默认' : '策略来源：会话覆写'}</Tag>
              <Tag>策略快照：{flowRuntimeSnapshotUpdatedAt ? flowRuntimeSnapshotUpdatedAt : '本地默认'}</Tag>
              <span className="flow-mode-runtime-switch-label">使用管理端默认</span>
              <Switch
                size="small"
                checked={flowUseRuntimePolicy}
                disabled={isStreaming}
                onChange={onFlowUseRuntimePolicyChange}
              />
              <Button size="small" disabled={isStreaming} onClick={() => onFlowOverrideOpenChange(!flowOverrideOpen)}>
                {flowOverrideOpen ? '收起覆写' : '会话覆写'}
              </Button>
            </div>
          ) : null}
        </div>
        {chatMode === 'flow' && flowOverrideOpen ? (
          <div className="flow-override-panel">
            <div className="flow-override-grid">
              <label className="switch-row">
                <span>Scenario Policy 启用</span>
                <Switch
                  checked={flowSessionOverride.scenario_policy.enabled}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  onChange={(checked) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      scenario_policy: { ...current.scenario_policy, enabled: checked },
                    }))
                  }
                />
              </label>
              <label>
                <span>Scenario 模式</span>
                <Select
                  value={flowSessionOverride.scenario_policy.mode}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  options={[
                    { label: 'auto', value: 'auto' },
                    { label: 'manual', value: 'manual' },
                  ]}
                  onChange={(value) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      scenario_policy: { ...current.scenario_policy, mode: value },
                    }))
                  }
                />
              </label>
              <label className="switch-row">
                <span>允许图修复</span>
                <Switch
                  checked={flowSessionOverride.scenario_policy.allow_graph_repair}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  onChange={(checked) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      scenario_policy: { ...current.scenario_policy, allow_graph_repair: checked },
                    }))
                  }
                />
              </label>
              <label>
                <span>最大修复轮次</span>
                <Input
                  value={String(flowSessionOverride.scenario_policy.max_graph_cycles)}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  onChange={(event) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      scenario_policy: {
                        ...current.scenario_policy,
                        max_graph_cycles: Number(event.target.value || 0),
                      },
                    }))
                  }
                />
              </label>
              <label className="switch-row">
                <span>Skill Policy 启用</span>
                <Switch
                  checked={flowSessionOverride.skill_policy.enabled}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  onChange={(checked) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      skill_policy: { ...current.skill_policy, enabled: checked },
                    }))
                  }
                />
              </label>
              <label>
                <span>挂载模式</span>
                <Select
                  value={flowSessionOverride.skill_policy.mount_mode}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  options={[{ label: 'agent_scoped', value: 'agent_scoped' }]}
                  onChange={(value) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      skill_policy: { ...current.skill_policy, mount_mode: value },
                    }))
                  }
                />
              </label>
              <label className="switch-row">
                <span>执行时二次鉴权</span>
                <Switch
                  checked={flowSessionOverride.skill_policy.runtime_auth_check}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  onChange={(checked) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      skill_policy: { ...current.skill_policy, runtime_auth_check: checked },
                    }))
                  }
                />
              </label>
              <label>
                <span>最大节点预算</span>
                <Input
                  value={String(flowSessionOverride.max_node_budget)}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  onChange={(event) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      max_node_budget: Number(event.target.value || 0),
                    }))
                  }
                />
              </label>
              <label className="switch-row">
                <span>编排失败回退全域链路</span>
                <Switch
                  checked={flowSessionOverride.fallback_to_global}
                  disabled={flowUseRuntimePolicy || isStreaming}
                  onChange={(checked) =>
                    onFlowSessionOverrideChange((current) => ({
                      ...current,
                      fallback_to_global: checked,
                    }))
                  }
                />
              </label>
            </div>
            <div className="flow-override-actions">
              <Button size="small" disabled={isStreaming} onClick={onFlowOverrideReset}>
                重置为管理端默认
              </Button>
              <Button size="small" type="primary" disabled={isStreaming} onClick={() => onFlowUseRuntimePolicyChange(false)}>
                使用会话覆写
              </Button>
            </div>
          </div>
        ) : null}
        <MessageList
          messages={messages}
          isStreaming={isStreaming}
          detail={detail}
          chatMode={chatMode}
          hasFlowHitData={hasFlowHitData}
          hasSourceItems={traceSourceItems.length > 0}
          onViewTrace={() => {
            onTracePanelModeChange('trace');
            onTracePanelOpenChange(true);
          }}
          onViewSource={() => {
            onTracePanelModeChange('source');
            onTracePanelOpenChange(true);
          }}
          onViewFlow={() => {
            onTracePanelModeChange('flow');
            onTracePanelOpenChange(true);
          }}
          onQuickSend={(query) => onSend(query)}
          listRef={chatMessageListRef}
        />
        <div className="chat-input-row">
          <Input.TextArea
            value={inputValue}
            placeholder="输入业务问题，Enter 发送，Shift+Enter 换行"
            autoSize={{ minRows: 2, maxRows: 6 }}
            onChange={(event) => onInputChange(event.target.value)}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                void onSend(inputValue);
              }
            }}
          />
          <div className="chat-input-actions">
            <Button
              onClick={() => {
                onExpandedInputValueChange(inputValue);
                onExpandedInputOpenChange(true);
              }}
            >
              扩展输入
            </Button>
            {isStreaming ? (
              <Button danger onClick={() => onStop()}>
                停止
              </Button>
            ) : null}
            <Button type="primary" loading={isStreaming} onClick={() => void onSend(inputValue)}>
              发送
            </Button>
          </div>
        </div>
      </Card>

      <Modal
        title="扩展输入"
        open={expandedInputOpen}
        width={820}
        onCancel={() => onExpandedInputOpenChange(false)}
        onOk={() => {
          onInputChange(expandedInputValue);
          onExpandedInputOpenChange(false);
          void onSend(expandedInputValue);
        }}
        okText="发送"
        cancelText="取消"
      >
        <Input.TextArea
          className="expanded-chat-input"
          value={expandedInputValue}
          autoSize={{ minRows: 12, maxRows: 18 }}
          placeholder="输入长文本。弹窗内 Enter 换行，Ctrl/Cmd+Enter 发送。"
          onChange={(event) => onExpandedInputValueChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              onInputChange(expandedInputValue);
              onExpandedInputOpenChange(false);
              void onSend(expandedInputValue);
            }
          }}
        />
      </Modal>

      {tracePanelOpen ? (
        <aside className="map-trace-sidebar">
          <Card
            className="map-chat-tree"
            title={tracePanelTitle}
            extra={
              <Button type="text" onClick={() => onTracePanelOpenChange(false)}>
                收起
              </Button>
            }
          >
            {tracePanelMode === 'trace' ? (
              <>
                <div className="tree-holder">
                  <RequestCallTree detail={detail} />
                </div>
                <div className="tree-footer">
                  输出摘要：{latestAssistantContent ? latestAssistantContent.slice(0, 120) : '-'}
                </div>
              </>
            ) : tracePanelMode === 'source' ? (
              <SourcePanel items={traceSourceItems} />
            ) : (
              <FlowHitPanel flowHitData={flowHitData} />
            )}
          </Card>
        </aside>
      ) : null}
    </div>
  );
}
