import { Alert, Tag } from '@agentscope-ai/design';
import { toPrettyJson } from '../../../lib/utils';
import type { FlowHitData } from '../flowHit';

export interface FlowHitPanelProps {
  flowHitData: FlowHitData | null;
}

/** Flow 策略命中面板:trace 侧栏中 "Flow 策略命中" 模式的内容 */
export default function FlowHitPanel({ flowHitData }: FlowHitPanelProps) {
  return (
    <div className="flow-hit-panel">
      {!flowHitData ? <div className="empty-hint">暂无策略命中信息。</div> : null}
      {flowHitData ? (
        <>
          <div className="flow-hit-kpis">
            <Tag>命中场景: {flowHitData.matchedScenarios.length}</Tag>
            <Tag>节点结果: {flowHitData.nodeResults.length}</Tag>
            <Tag>鉴权记录: {flowHitData.skillAuthorization.length}</Tag>
            <Tag>修复事件: {flowHitData.repairEvents.length}</Tag>
          </div>
          {flowHitData.fallbackReason ? (
            <Alert
              type="warning"
              message={`回退原因：${flowHitData.fallbackReason}`}
              className="flow-hit-alert"
            />
          ) : null}
          <div className="flow-hit-block">
            <div className="flow-hit-title">运行时配置快照</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowSnapshot)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">本次生效策略</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowConfig)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">策略命中信息</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowPolicyHit)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">命中场景</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.matchedScenarios)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">Skill 鉴权结果</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.skillAuthorization)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">执行节点结果</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.nodeResults)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">步骤判定</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.stepVerdicts)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">修复轨迹</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.repairEvents)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">构建执行图</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowGraph)}</pre>
          </div>
          <div className="flow-hit-block">
            <div className="flow-hit-title">Done 流程元数据</div>
            <pre className="flow-hit-json">{toPrettyJson(flowHitData.flowDoneMeta)}</pre>
          </div>
        </>
      ) : null}
    </div>
  );
}
