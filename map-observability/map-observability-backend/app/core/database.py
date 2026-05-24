from __future__ import annotations

from dataclasses import dataclass

from pymongo import MongoClient
from pymongo.database import Database


@dataclass
class MongoCollections:
    request_records: str = "request_records"
    agent_executions: str = "agent_executions"
    tool_call_records: str = "tool_call_records"


def create_client(mongo_uri: str) -> MongoClient:
    if not mongo_uri:
        raise RuntimeError("MONGO_URI is required for backend startup")
    return MongoClient(mongo_uri, tz_aware=True)


def get_database(client: MongoClient, db_name: str) -> Database:
    return client[db_name]
