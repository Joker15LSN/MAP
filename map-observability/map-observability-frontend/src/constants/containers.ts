export const MAIN_FLOW_CONTAINERS = ['map_core-dev', 'map_core-test', 'map_core-preprod'] as const;
export type MainFlowContainerKey = (typeof MAIN_FLOW_CONTAINERS)[number];

const ENV_MAIN_FLOW_CONTAINER_MAP = {
  dev: 'map_core-dev',
  test: 'map_core-test',
  preprod: 'map_core-preprod',
} as const;

type ContainerEnv = keyof typeof ENV_MAIN_FLOW_CONTAINER_MAP;

const MAIN_FLOW_CONTAINER_ENV_MAP: Record<MainFlowContainerKey, ContainerEnv> = {
  'map_core-dev': 'dev',
  'map_core-test': 'test',
  'map_core-preprod': 'preprod',
};

export const CBB_CONTAINER_TOOL_MAP = {
  'cbb-text-to-metrics-dev': 'wenshu_agent',
  'cbb-text-to-metrics-test': 'wenshu_agent',
  'cbb-text-to-metrics-preprod': 'wenshu_agent',
  'cbb-text-to-sql-dev': 'ask_database_agent',
  'cbb-text-to-sql-test': 'ask_database_agent',
  'cbb-text-to-sql-preprod': 'ask_database_agent',
  'cbb-text-to-ngql-dev': 'efficiency_pi_agent',
  'cbb-text-to-ngql-test': 'efficiency_pi_agent',
  'cbb-text-to-ngql-preprod': 'efficiency_pi_agent',
  'cbb-kb-analyze-dev': 'search_mounted_kb_agent',
  'cbb-kb-analyze-test': 'search_mounted_kb_agent',
  'cbb-kb-analyze-preprod': 'search_mounted_kb_agent',
  'cbb-kb-analyze-ubddev201': 'search_mounted_kb_agent',
} as const;

export const ALL_CONTAINERS = [
  ...MAIN_FLOW_CONTAINERS,
  ...Object.keys(CBB_CONTAINER_TOOL_MAP),
] as const;

export type ContainerKey = (typeof ALL_CONTAINERS)[number];

const SPECIAL_CONTAINER_ENV_MAP: Partial<Record<ContainerKey, ContainerEnv>> = {
  'cbb-kb-analyze-ubddev201': 'preprod',
};

export const MAIN_FLOW_CONTAINER_OPTIONS: Array<{ label: string; value: MainFlowContainerKey }> = [
  { label: 'map_core-dev', value: 'map_core-dev' },
  { label: 'map_core-test', value: 'map_core-test' },
  { label: 'map_core-preprod', value: 'map_core-preprod' },
];

export const CONTAINER_OPTIONS: Array<{ label: string; value: ContainerKey }> = [
  ...MAIN_FLOW_CONTAINER_OPTIONS,
  { label: 'cbb-text-to-metrics-dev', value: 'cbb-text-to-metrics-dev' },
  { label: 'cbb-text-to-metrics-test', value: 'cbb-text-to-metrics-test' },
  { label: 'cbb-text-to-metrics-preprod', value: 'cbb-text-to-metrics-preprod' },
  { label: 'cbb-text-to-sql-dev', value: 'cbb-text-to-sql-dev' },
  { label: 'cbb-text-to-sql-test', value: 'cbb-text-to-sql-test' },
  { label: 'cbb-text-to-sql-preprod', value: 'cbb-text-to-sql-preprod' },
  { label: 'cbb-text-to-ngql-dev', value: 'cbb-text-to-ngql-dev' },
  { label: 'cbb-text-to-ngql-test', value: 'cbb-text-to-ngql-test' },
  { label: 'cbb-text-to-ngql-preprod', value: 'cbb-text-to-ngql-preprod' },
  { label: 'cbb-kb-analyze-dev', value: 'cbb-kb-analyze-dev' },
  { label: 'cbb-kb-analyze-test', value: 'cbb-kb-analyze-test' },
  { label: 'cbb-kb-analyze-preprod', value: 'cbb-kb-analyze-preprod' },
  { label: 'cbb-kb-analyze-ubddev201', value: 'cbb-kb-analyze-ubddev201' },
];

const inferEnvByContainer = (container?: string): ContainerEnv | undefined => {
  if (!container) {
    return undefined;
  }

  const special = SPECIAL_CONTAINER_ENV_MAP[container as ContainerKey];
  if (special) {
    return special;
  }

  if (Object.prototype.hasOwnProperty.call(MAIN_FLOW_CONTAINER_ENV_MAP, container)) {
    return MAIN_FLOW_CONTAINER_ENV_MAP[container as (typeof MAIN_FLOW_CONTAINERS)[number]];
  }
  if (container.endsWith('-preprod')) {
    return 'preprod';
  }
  if (container.endsWith('-test')) {
    return 'test';
  }
  if (container.endsWith('-dev')) {
    return 'dev';
  }
  return undefined;
};

const TOOL_TO_CBB_CONTAINER: Record<string, Partial<Record<ContainerEnv, ContainerKey>>> = {};
for (const [container, tool] of Object.entries(CBB_CONTAINER_TOOL_MAP)) {
  const envKey = inferEnvByContainer(container);
  if (!envKey) {
    continue;
  }
  const next = TOOL_TO_CBB_CONTAINER[tool] || {};
  next[envKey] = container as ContainerKey;
  TOOL_TO_CBB_CONTAINER[tool] = next;
}

export const isKnownContainer = (container?: string): container is ContainerKey => {
  if (!container) {
    return false;
  }
  return (ALL_CONTAINERS as readonly string[]).includes(container);
};

export const isCbbContainer = (container?: string): boolean => {
  if (!container) {
    return false;
  }
  return Object.prototype.hasOwnProperty.call(CBB_CONTAINER_TOOL_MAP, container);
};

export const getForcedToolByContainer = (container?: string): string | undefined => {
  if (!container) {
    return undefined;
  }
  return (CBB_CONTAINER_TOOL_MAP as Record<string, string>)[container];
};

export const inferMainFlowContainer = (container?: string): MainFlowContainerKey => {
  const env = inferEnvByContainer(container);
  if (env) {
    return ENV_MAIN_FLOW_CONTAINER_MAP[env];
  }
  return ENV_MAIN_FLOW_CONTAINER_MAP.dev;
};

export const inferCbbContainerByTool = (tool?: string, baseContainer?: string): ContainerKey | undefined => {
  const normalizedTool = String(tool || '').trim();
  if (!normalizedTool) {
    return undefined;
  }

  const pair = TOOL_TO_CBB_CONTAINER[normalizedTool];
  if (!pair) {
    return undefined;
  }

  const env = MAIN_FLOW_CONTAINER_ENV_MAP[inferMainFlowContainer(baseContainer)];
  return pair[env] || pair.dev || pair.test || pair.preprod;
};
