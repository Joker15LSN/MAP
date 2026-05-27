import os

from .config_schema import LLMConfig


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


MAP_LLM_BASE_URL = os.getenv("MAP_LLM_BASE_URL", "https://api.deepseek.com")
MAP_LLM_API_KEY = os.getenv("MAP_LLM_API_KEY", "")
MAP_LLM_MODEL = os.getenv("MAP_LLM_MODEL", "deepseek-v4-flash")
MAP_LLM_TEMPERATURE = _env_float("MAP_LLM_TEMPERATURE", 0.3)
MAP_AGENT_LLM_TEMPERATURE = _env_float("MAP_AGENT_LLM_TEMPERATURE", 0.1)
MAP_SCENE_SELECTOR_LLM_TEMPERATURE = _env_float(
    "MAP_SCENE_SELECTOR_LLM_TEMPERATURE",
    0.0,
)
MAP_SUMMARIZATION_LLM_TEMPERATURE = _env_float(
    "MAP_SUMMARIZATION_LLM_TEMPERATURE",
    0.2,
)
MAP_REVIEWER_BASE_URL = os.getenv(
    "MAP_REVIEWER_BASE_URL",
    MAP_LLM_BASE_URL,
)
MAP_REVIEWER_API_KEY = os.getenv("MAP_REVIEWER_API_KEY", MAP_LLM_API_KEY)
MAP_REVIEWER_MODEL = os.getenv("MAP_REVIEWER_MODEL", MAP_LLM_MODEL)

# 40 qwen2.5 72B
# QWEN2_5_72B_40_CONFIG = LLMConfig(
#     base_url="http://10.16.11.40:11112/v1/", model="local", temperature=0.4
# )

# 40 qwen3 32B
# QWEN3_32B_40_CONFIG = LLMConfig(base_url="http://10.16.11.40:11111/v1/", model="local")

# 41 qwen2.5 72B
# QWEN2_5_72B_41_CONFIG = LLMConfig(
#     base_url="http://10.16.11.41:11112/v1/", model="local"
# )

# 24 qwen3 32B
QWEN3_32B_24_CONFIG = LLMConfig(
    base_url="http://10.16.11.24:11111/v1/",
    model="local",
    # chat_template_kwargs={"enable_thinking": False},
    temperature=0.1,
)
DEEPSEEKV3_LOCAL_CONFIG = LLMConfig(
    base_url="http://10.16.11.38:15773/v1/",
    model="/models/DeepSeek-V4-Flash",
    temperature=0.1,
)
# 41 gpt-oss 120B
# GPTOSS_120B_41_CONFIG = LLMConfig(
#     base_url="http://10.16.11.41:8766/v1/",
#     model="local"
# )

# apiyi deepseek v3
DEEPSEEKV3_APIYI_CONFIG = LLMConfig(
    base_url=os.getenv("MAP_DEEPSEEKV3_APIYI_BASE_URL", MAP_LLM_BASE_URL),
    api_key=os.getenv("MAP_DEEPSEEKV3_APIYI_API_KEY", MAP_LLM_API_KEY),
    model=os.getenv("MAP_DEEPSEEKV3_APIYI_MODEL", MAP_LLM_MODEL),
    temperature=0.0,
)

LITE_APIYI_CONFIG = LLMConfig(
    base_url=os.getenv("MAP_LITE_APIYI_BASE_URL", MAP_LLM_BASE_URL),
    api_key=os.getenv("MAP_LITE_APIYI_API_KEY", MAP_LLM_API_KEY),
    model=os.getenv("MAP_LITE_APIYI_MODEL", MAP_LLM_MODEL),
    temperature=0.0,
)

REVIEWER_LLM_CONFIG = LLMConfig(
    base_url=MAP_REVIEWER_BASE_URL,
    api_key=MAP_REVIEWER_API_KEY,
    model=MAP_REVIEWER_MODEL,
    temperature=0.0,
    max_tokens=2048,
)

THINKING_LLM_CONFIG = LLMConfig(
    base_url=os.getenv("MAP_THINKING_BASE_URL", MAP_LLM_BASE_URL),
    api_key=os.getenv("MAP_THINKING_API_KEY", MAP_LLM_API_KEY),
    model=os.getenv("MAP_THINKING_MODEL", MAP_LLM_MODEL),
    temperature=0.0,
)


QWEN3_NEXT_80B_CONFIG = LLMConfig(
    base_url=os.getenv("MAP_QWEN3_NEXT_BASE_URL", MAP_LLM_BASE_URL),
    model=os.getenv("MAP_QWEN3_NEXT_MODEL", MAP_LLM_MODEL),
    temperature=0.1,
    api_key=os.getenv("MAP_QWEN3_NEXT_API_KEY", MAP_LLM_API_KEY),
)

QWEN3_5_27B = LLMConfig(
    base_url="http://10.16.12.25:11112/v1/",
    model="qwen3.5-27B",
    temperature=0.1,
    chat_template_kwargs={"enable_thinking": False},
)

DS_V4_FLASH_LLM_CONFIG = LLMConfig(
    base_url=MAP_LLM_BASE_URL,
    model=MAP_LLM_MODEL,
    temperature=MAP_LLM_TEMPERATURE,
    api_key=MAP_LLM_API_KEY,
)

DS_V4_FLASH_AGENT_CONFIG = LLMConfig(
    base_url=MAP_LLM_BASE_URL,
    model=MAP_LLM_MODEL,
    temperature=MAP_AGENT_LLM_TEMPERATURE,
    api_key=MAP_LLM_API_KEY,
)

# 意图分类llm
# SCENE_SELECTION_LLM_CONFIG = QWEN3_NEXT_80B_CONFIG
SCENE_SELECTION_LLM_CONFIG = LLMConfig(
    base_url=MAP_LLM_BASE_URL,
    model=MAP_LLM_MODEL,
    temperature=MAP_SCENE_SELECTOR_LLM_TEMPERATURE,
    api_key=MAP_LLM_API_KEY,
)


# 总结 LLM
SUMMARIZATION_LLM_CONFIG = LLMConfig(
    base_url=MAP_LLM_BASE_URL,
    model=MAP_LLM_MODEL,
    temperature=MAP_SUMMARIZATION_LLM_TEMPERATURE,
    api_key=MAP_LLM_API_KEY,
)


# Nebula Graph 配置
# NEBULA_NAME_SPACE = "report_one_latest"

# NEBULA_CONFIG = {
#     "host": [],
#     "port": 9669,
#     "username": "root",
#     "password": "password",
#     "space_name": NEBULA_NAME_SPACE,
#     "timeout": 120000,
#     "max_connection_pool_size": 20,
#     "min_connection_pool_size": 2,
#     "idle_time": 60000,
#     "interval_check": 30000,
#     "db_thread_pool_size": 20,
# }

# Milvus 配置
# MILVUS_CONFIG = {
#     "uri": "http://10.16.11.41:19530",
#     "db_name": "report_database",
#     "timeout": 30,
#     "pool_size": 20,
# }

# PostgreSQL 配置（可通过环境变量覆盖，便于容器化部署）
DEFAULT_POSTGRES_DSN = "postgresql://map:map@127.0.0.1:15432/map"
POSTGRES_CONFIG = {
    "dsn": os.getenv("POSTGRES_DSN", DEFAULT_POSTGRES_DSN),
}

# MongoDB 配置（可通过环境变量覆盖，便于容器化部署）
DEFAULT_MONGODB_URI = "mongodb://map:map@127.0.0.1:27017/?authSource=admin"
DEFAULT_MONGODB_DATABASE = "map_db_dev"
MONGODB_CONFIG = {
    "uri": os.getenv("MONGODB_URI", DEFAULT_MONGODB_URI),
    "database": os.getenv("MONGODB_DATABASE", DEFAULT_MONGODB_DATABASE),
}

MONGODB_STATE_RECORD_COLLECTION = "agent_call_states"  # legacy, kept for reference

# Collections for the three-way event routing in MongoAgentStateHandler
MONGODB_AGENT_EXECUTIONS_COLLECTION = "agent_executions"
MONGODB_TOOL_CALL_COLLECTION = "tool_call_records"
MONGODB_REQUEST_COLLECTION = "request_records"

# Agents memory
MONGODB_AGENT_MEMORY_COLLECTION = "agent_session_memories"
AGENT_MEMORY_ENABLED_AGENT_CODES = {}
AGENT_MEMORY_DEFAULT_INTENTION_ID = "default"
AGENT_MEMORY_MAX_MESSAGES = 20
AGENT_MEMORY_LOOKUP_TIMEOUT_S = 1.0
AGENT_MEMORY_RECORD_TIMEOUT_S = 1.0

# 效率派CBB服务地址
EFFI_API = "http://10.54.56.113:10004/text-to-ngql/query"
AIM_GRAPH_SPACE = "efficiency_graph_sbx"

# 知识库
# MAP_KB_API_BASE_URL = "http://10.48.1.46:1103"
MAP_KB_API_BASE_URL = "http://10.40.0.77:1103"

# 公司知识库
CORP_KB_API_BASE_URL = "http://10.54.56.109:7888/local_doc_qa/aggr_retrieve"

# 图表绘制服务
# CHART_PLOTTING_API_URL = "http://10.10.80.97:8002/generate_chart"
CHART_PLOTTING_API_URL = "http://10.50.56.46:8002/generate_chart"

# 问数CBB
TEXT_TO_METRICS_API = "http://10.54.56.113:10006/text-to-metrics/query"

# 问表CBB
TEXT_TO_SQL_API = "http://10.54.56.113:10010/text-to-sql/query"

# ZHIWEN
ZHIWEN_API_URL = "http://10.40.0.77:7884/enterprise_kb/source_retrieve"

# 问数 milvus（问表也用这个）
METRIC_MILVUS_URI = "http://10.16.11.41:19537"  # prod

# WEB搜索
WEB_SEARCH_API = "http://10.50.56.46:8001/search"
