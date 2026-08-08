import type { ContainerKey } from '../../constants/containers';
import type { LogLevel, RequestDetail } from '../../types';

export type ToolCallRow = Record<string, unknown>;
export type GenericRecord = Record<string, unknown>;

export interface SubQuestionResultBlock {
  key: string;
  question: string;
  status: string;
  queryRequest: string;
  summary: string;
  rows: GenericRecord[];
}

export interface RequestDetailDrawerProps {
  open: boolean;
  loading: boolean;
  detail?: RequestDetail;
  errorMessage?: string;
  activeContainer: ContainerKey;
  activeLevels: LogLevel[];
  onClose: () => void;
}

export const PAGE_SIZE = 10;
export const DETAIL_PANEL_KEYS = ['timeline', 'llm', 'tools', 'scene'] as const;
