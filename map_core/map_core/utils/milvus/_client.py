from pymilvus import AsyncMilvusClient

_milvus_clients: dict[str, AsyncMilvusClient] = {}  # 按 db_name 缓存
