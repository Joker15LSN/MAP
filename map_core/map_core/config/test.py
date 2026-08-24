"""
ubddev 201
"""

import os

NUM_WORK = 2


# P0-SEC-01: no hardcoded credentials in the repository. The URI (including
# its password) MUST be injected via environment; an unset value fails closed.
MONGODB_CONFIG = {
    "uri": os.getenv("MONGODB_URI", ""),
    "database": "map_db_dev",
}

# Agents memory
MONGODB_AGENT_MEMORY_COLLECTION = "agent_session_memories_test"
AGENT_MEMORY_ENABLED_AGENT_CODES = {"Operations", "Ecosystem_Partner"}
AGENT_MEMORY_DEFAULT_INTENTION_ID = "default"

MAP_KB_API_BASE_URL = "http://10.48.2.201:1103"

# 问数CBB
TEXT_TO_METRICS_API = "http://10.48.2.201:10006/text-to-metrics/query"

# 效率派CBB服务地址
EFFI_API = "http://10.48.2.201:10004/text-to-ngql/query"
AIM_GRAPH_SPACE = "efficiency_graph_sbx"

# 问表CBB
TEXT_TO_SQL_API = "http://10.48.2.201:10010/text-to-sql/query"

# 问数CBB
TEXT_TO_METRICS_API = "http://10.48.2.201:10006/text-to-metrics/query"

# ZHIWEN
ZHIWEN_API_URL = "http://10.48.2.201:7884/enterprise_kb/source_retrieve"

# 问数 milvus
METRIC_MILVUS_URI = "http://10.16.11.41:19530"
