import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import SourcePanel from './SourcePanel';

describe('SourcePanel 回答来源面板', () => {
  const items = [
    { id: 'src-1', source: '知识库检索', title: '合规政策库返回结果', summary: '根据企业合规政策...', date: '2026-01-01' },
    { id: 'src-2', source: '通用助手', title: '通用助手返回结果', summary: '通用回答摘要', date: '2026-01-02' },
  ];

  it('渲染来源条目标题、来源与日期', () => {
    render(<SourcePanel items={items} />);
    expect(screen.getByText('合规政策库返回结果')).toBeTruthy();
    expect(screen.getByText('知识库检索')).toBeTruthy();
    expect(screen.getByText('2026-01-01')).toBeTruthy();
    expect(screen.getAllByText(/返回结果/)).toHaveLength(2);
  });

  it('渲染来源摘要内容', () => {
    render(<SourcePanel items={items} />);
    expect(screen.getByText('根据企业合规政策...')).toBeTruthy();
    expect(screen.getByText('通用回答摘要')).toBeTruthy();
  });

  it('items 为空时展示空态提示', () => {
    render(<SourcePanel items={[]} />);
    expect(screen.getByText('暂无可展示来源。')).toBeTruthy();
  });
});
