from contextlib import contextmanager
from contextvars import ContextVar

from loguru import logger

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="unknown")
session_id_ctx: ContextVar[str] = ContextVar("session_id", default="-")
agent_id_ctx: ContextVar[str] = ContextVar("agent_id", default="-")
parent_id_ctx: ContextVar[str] = ContextVar("parent_id", default="-")


def current_agent_id() -> str:
	return agent_id_ctx.get("-")


def current_session_id() -> str:
	return session_id_ctx.get("-")


@contextmanager
def agent_log_context(agent_id: str, parent_id: str | None = None):
	parent = parent_id or current_agent_id()
	token_agent = agent_id_ctx.set(agent_id)
	token_parent = parent_id_ctx.set(parent if parent and parent != agent_id else "-")
	try:
		yield
	finally:
		try:
			agent_id_ctx.reset(token_agent)
		except ValueError:
			logger.warning(
				"Skip agent_id context reset due to context mismatch during generator finalization"
			)
		try:
			parent_id_ctx.reset(token_parent)
		except ValueError:
			logger.warning(
				"Skip parent_id context reset due to context mismatch during generator finalization"
			)
