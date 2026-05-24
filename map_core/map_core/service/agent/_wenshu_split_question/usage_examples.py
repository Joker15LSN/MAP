

# write a usage example for milvus client

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[4].absolute()))

from map_core.config.common import (
    DEEPSEEKV3_LOCAL_CONFIG,
    QWEN3_NEXT_80B_CONFIG,
)
from map_core.database.milvus import MilvusClient
from map_core.utils.llm_engine import LLMEngine
from map_core.utils.model_factory import aembed_text

METRIC_UBDDEV_MILVUS_URI = "http://10.16.11.41:19539"
MILVUS_DB_NAME = "dataorigin_6102261701897472"

async def milvus_client_usage():
    milvus_client = MilvusClient(
        uri=METRIC_UBDDEV_MILVUS_URI,
        user="root",
        password="password",
        db_name=MILVUS_DB_NAME,
    )
    await milvus_client.connect()
    _aclient = milvus_client._client
    assert _aclient is not None
    collections = await _aclient.list_collections()
    print(collections)
    
    await milvus_client.close()

async def achat_usage():
    llm_engine = LLMEngine(config=DEEPSEEKV3_LOCAL_CONFIG)
    prompt = "compute 2*2"
    response = await llm_engine.achat(
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    print(type(response), response)
    
async def get_embedding():
    text = "test"
    embedding = await aembed_text(text)
    print(type(embedding), embedding)


if __name__ == "__main__":
    import asyncio

    asyncio.run(milvus_client_usage())
    asyncio.run(achat_usage())
    asyncio.run(get_embedding())
