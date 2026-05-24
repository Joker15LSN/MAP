import sys
from pathlib import Path

from loguru import logger

from ..config.base_config import LOG_DIR
from .global_context import agent_id_ctx, parent_id_ctx, request_id_ctx, session_id_ctx


def patch_record(record):
    """Inject request/session/agent context ids into each log record."""

    rid = request_id_ctx.get("-")
    sid = session_id_ctx.get("-")
    record["extra"]["request_id"] = rid
    record["extra"]["session_id"] = sid
    agent_id = agent_id_ctx.get("-")
    parent_id = parent_id_ctx.get("-")
    if agent_id not in ("-", "", None):
        record["extra"]["agent_id"] = agent_id
        record["extra"]["parent_id"] = parent_id


def init_logger(path: str | Path = LOG_DIR):

    logger.remove()

    logger.configure(patcher=patch_record)

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)

    # log display format: time | level | filename:line number | rid | message
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
        "rid=<magenta>{extra[request_id]}</magenta> | "
        "sid=<magenta>{extra[session_id]}</magenta> | "
        "<level>{message}</level>"
    )

    agent_log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
        "rid=<magenta>{extra[request_id]}</magenta> | "
        "sid=<magenta>{extra[session_id]}</magenta> | "
        "aid=<magenta>{extra[agent_id]}</magenta> | "
        "parid=<magenta>{extra[parent_id]}</magenta> | "
        "<level>{message}</level>"
    )

    def _has_agent(record):
        return "agent_id" in record["extra"] or "parent_id" in record["extra"]

    def _no_agent(record):
        return not _has_agent(record)

    logger.add(
        sys.stdout, format=log_format, level="DEBUG", enqueue=True, filter=_no_agent
    )
    logger.add(
        sys.stdout,
        format=agent_log_format,
        level="DEBUG",
        enqueue=True,
        colorize=True,
        filter=_has_agent,
    )

    # Keep file format aligned with console format and disable tag stripping.
    # file_format = (
    #     log_format.replace("<green>", "")
    #     .replace("</green>", "")
    #     .replace("<level>", "")
    #     .replace("</level>", "")
    #     .replace("<cyan>", "")
    #     .replace("</cyan>", "")
    #     .replace("<magenta>", "")
    #     .replace("</magenta>", "")
    # )
    # agent_file_format = (
    #     agent_log_format.replace("<green>", "")
    #     .replace("</green>", "")
    #     .replace("<level>", "")
    #     .replace("</level>", "")
    #     .replace("<cyan>", "")
    #     .replace("</cyan>", "")
    #     .replace("<magenta>", "")
    #     .replace("</magenta>", "")
    # )
    file_format = log_format
    agent_file_format = agent_log_format

    logger.add(
        path / "all.log",
        format=file_format,
        rotation="00:00",
        retention="14 days",
        level="DEBUG",
        enqueue=True,
        encoding="utf-8",
        filter=_no_agent,
    )
    logger.add(
        path / "all.log",
        format=agent_file_format,
        rotation="00:00",
        retention="14 days",
        level="DEBUG",
        enqueue=True,
        encoding="utf-8",
        filter=_has_agent,
    )

    logger.add(
        path / "error.log",
        rotation="00:00",
        retention="10 days",
        level="ERROR",
        enqueue=True,
        backtrace=True,
        diagnose=True,
        filter=_no_agent,
    )
    logger.add(
        path / "error.log",
        format=agent_file_format,
        rotation="00:00",
        retention="10 days",
        level="ERROR",
        enqueue=True,
        backtrace=True,
        diagnose=True,
        filter=_has_agent,
    )

    return logger
