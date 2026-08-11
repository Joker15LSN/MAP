import { Card } from '@agentscope-ai/design';
import type { SourceReferenceItem } from '../../../api/types';

export interface SourcePanelProps {
  items: SourceReferenceItem[];
}

/** 回答来源面板:trace 侧栏中 "回答来源" 模式的内容 */
export default function SourcePanel({ items }: SourcePanelProps) {
  return (
    <div className="source-list">
      {items.length === 0 ? <div className="empty-hint">暂无可展示来源。</div> : null}
      {items.map((item) => (
        <Card key={item.id} size="small" className="source-card">
          <div className="source-card-title">{item.title}</div>
          <div className="source-card-meta">
            <span>{item.source}</span>
            <span>{item.date}</span>
          </div>
          <div className="source-card-summary">{item.summary}</div>
        </Card>
      ))}
    </div>
  );
}
