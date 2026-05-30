from __future__ import annotations

import logging

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import OperationFailure

from app.core.database import MongoCollections

logger = logging.getLogger(__name__)

def _is_auth_error(exc: OperationFailure) -> bool:
    message = str(exc).lower()
    return exc.code == 13 or "unauthorized" in message or "requires authentication" in message


def ensure_indexes(database: Database, collections: MongoCollections, ignore_auth_error: bool = True) -> bool:
    request = database[collections.request_records]
    agent = database[collections.agent_executions]
    tool = database[collections.tool_call_records]
    llm = database[collections.llm_call_records]
    reports = database[collections.friday_reports]

    try:
        request.create_index([("request_id", ASCENDING)], background=True)
        request.create_index([("state_id", ASCENDING)], background=True)
        request.create_index([("session_id", ASCENDING)], background=True)
        request.create_index([("staff_code", ASCENDING)], background=True)
        request.create_index([("status", ASCENDING)], background=True)
        request.create_index([("start_ts", DESCENDING)], background=True)

        agent.create_index([("request_id", ASCENDING)], background=True)
        agent.create_index([("state_id", ASCENDING)], background=True)
        agent.create_index([("session_id", ASCENDING)], background=True)
        agent.create_index([("staff_code", ASCENDING)], background=True)
        agent.create_index([("agent_code", ASCENDING)], background=True)
        agent.create_index([("payload.agent_id", ASCENDING)], background=True)
        agent.create_index([("ts", DESCENDING)], background=True)
        agent.create_index([("stage", ASCENDING)], background=True)

        tool.create_index([("request_id", ASCENDING)], background=True)
        tool.create_index([("state_id", ASCENDING)], background=True)
        tool.create_index([("session_id", ASCENDING)], background=True)
        tool.create_index([("agent_code", ASCENDING)], background=True)
        tool.create_index([("agent_id", ASCENDING)], background=True)
        tool.create_index([("tool", ASCENDING)], background=True)
        tool.create_index([("ts", DESCENDING)], background=True)
        tool.create_index([("status", ASCENDING)], background=True)

        llm.create_index([("request_id", ASCENDING)], background=True)
        llm.create_index([("state_id", ASCENDING)], background=True)
        llm.create_index([("agent_code", ASCENDING)], background=True)
        llm.create_index([("phase", ASCENDING)], background=True)
        llm.create_index([("start_ts", DESCENDING)], background=True)
        llm.create_index([("status", ASCENDING)], background=True)

        reports.create_index([("report_id", ASCENDING)], unique=True, background=True)
        reports.create_index([("schedule_key", ASCENDING)], background=True)
        reports.create_index([("created_at", DESCENDING)], background=True)
        logger.info("MongoDB indexes ensured successfully.")
        return True
    except OperationFailure as exc:
        if ignore_auth_error and _is_auth_error(exc):
            logger.warning(
                "Skipping MongoDB index creation because credentials do not have createIndexes privilege. "
                "Set INDEX_ENSURE_MODE=skip to silence this warning."
            )
            return False
        raise
