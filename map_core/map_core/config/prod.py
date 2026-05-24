NUM_WORK = 5

MONGODB_CONFIG = {
    "uri": "mongodb://root:48f#7fQuk6!@10.50.56.29:27017,10.50.56.33:27017,10.50.56.34:27017/admin?replicaSet=rs0",
    "database": "map_db_prod",
}

# Agents memory
MONGODB_AGENT_MEMORY_COLLECTION = "agent_session_memories_prod"
# AGENT_MEMORY_ENABLED_AGENT_CODES = {"Customer_Assistant", "Ecosystem_Partner"}
AGENT_MEMORY_ENABLED_AGENT_CODES = {}
AGENT_MEMORY_DEFAULT_INTENTION_ID = "default"


# 问数CBB
TEXT_TO_METRICS_API = "http://10.54.56.109:10005/text-to-metrics/query"

# 效率派CBB服务地址
EFFI_API = "http://10.54.56.109:10003/text-to-ngql/query"
AIM_GRAPH_SPACE = "efficiency_graph_sbx"

# 问表CBB
TEXT_TO_SQL_API = "http://10.54.56.109:10007/text-to-sql/query"

# 问数 milvus
METRIC_MILVUS_URI = "http://10.50.56.154:19530"  # prod

# ----------------------- enyu -----------------------
# ZHIWEN
ZHIWEN_API_URL = "http://10.54.56.109:7884/enterprise_kb/source_retrieve"

MAP_KB_API_BASE_URL = "http://10.54.56.109:1103"

# ----------------------- chenyu -----------------------
CHART_PLOTTING_API_URL = "http://10.54.56.109:8002/generate_chart"

WEB_SEARCH_API = "http://10.54.56.109:8005/search"