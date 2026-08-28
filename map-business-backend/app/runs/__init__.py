"""Canonical Run module (ADR-0002 / Step 2).

Public surface:
- BFF: :class:`app.runs.application.RunApplication`
- Worker: :class:`app.runs.attempt.RunWorker`

Everything else in this package is an internal seam or an adapter.
"""

from __future__ import annotations

from .application import RunApplication
from .attempt import RunWorker
from .core_transport import CoreRunStream, HttpCoreRunStream, InMemoryCoreRunStream
from .domain import RunCommand
from .memory_store import InMemoryRunStore
from .pg_store import PgRunStore
from .sandbox_effects import (
    EffectView,
    build_create_key,
    build_execute_key,
    effect_executing,
    effect_failed,
    effect_planned,
    effect_reconciling,
    effect_succeeded,
    effect_uncertain,
    project_effects,
    request_digest,
)
from .sandbox_remote import (
    HttpSandboxRemote,
    InMemorySandboxRemote,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxIdentity,
    SandboxReference,
    SandboxRemote,
)
from .store import RunStore

__all__ = [
    "CoreRunStream",
    "EffectView",
    "HttpCoreRunStream",
    "HttpSandboxRemote",
    "InMemoryCoreRunStream",
    "InMemoryRunStore",
    "InMemorySandboxRemote",
    "PgRunStore",
    "RunApplication",
    "RunCommand",
    "RunStore",
    "RunWorker",
    "SandboxExecutionRequest",
    "SandboxExecutionResult",
    "SandboxIdentity",
    "SandboxReference",
    "SandboxRemote",
    "build_create_key",
    "build_execute_key",
    "effect_executing",
    "effect_failed",
    "effect_planned",
    "effect_reconciling",
    "effect_succeeded",
    "effect_uncertain",
    "project_effects",
    "request_digest",
]
