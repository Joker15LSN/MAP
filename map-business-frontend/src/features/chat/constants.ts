import type { ChatMode } from '../../api/types';

export const QUICK_QUESTIONS = [
  '介绍一下中国杭州',
  '杭州有哪些代表性产业？',
  '杭州有哪些值得去的景点？',
  '杭州的历史文化特点是什么？',
  '杭州适合几月份旅游？',
  '请用 5 点总结杭州这座城市。',
];

export const CHAT_MODE_LABEL: Record<ChatMode, string> = {
  global: '全域模式',
  flow: '心流模式',
};
