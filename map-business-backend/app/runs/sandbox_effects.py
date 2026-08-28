"""Sandbox Invocation effect rules and projection (Step 3 / PR-E).

The Run module is the single durable owner of a sandbox invocation. Core is
stateless and only executes the remote OpenSandbox call; the RunWorker turns
that call into ``effect.*`` events here, in order:

    effect.planned -> effect.executing -> effect.succeeded
                                       -> effect.failed
                                       -> effect.uncertain

``project_effects`` folds those events by ``effect_id`` into an
:class:`EffectView` for replay. It validates every transition against the
frozen effect state machine, so a terminal effect can never be overwritten
by a late event.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..runtime.event_envelope import EventEnvelope
from ..runtime.state_machine import EffectState, validate_transition
from .domain import (
    AttemptInput,
    CoreEvent,
    CoreItem,
    CoreOutcome,
    RunAttemptHandler,
    RunEventDraft,
)
from .sandbox_remote import (
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxIdentity,
    SandboxRemote,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

# event type -> effect state. ``effect.reconciled`` is a frozen event type
# but has no state-machine state (uncertain -> reconciling -> terminal), so
# projection deliberately ignores it.
_EVENT_STATE_TARGET: dict[str, str] = {
    "effect.planned": EffectState.PLANNED,
    "effect.executing": EffectState.EXECUTING,
    "effect.succeeded": EffectState.SUCCEEDED,
    "effect.failed": EffectState.FAILED,
    "effect.uncertain": EffectState.UNCERTAIN,
    "effect.reconciling": EffectState.RECONCILING,
    "effect.cancelled": EffectState.CANCELLED,
}


@dataclass(frozen=True)
class EffectView:
    """Replay projection of one sandbox effect (never a row or a write)."""

    effect_id: str
    invocation_id: str
    status: str
    sandbox_id: str | None = None
    create_key: str | None = None
    execute_key: str | None = None
    request_digest: str | None = None
    command: str | None = None
    limits: dict[str, int] | None = None
    output: str | None = None
    error_code: str | None = None
    reason: str | None = None


def request_digest(*, command: str, limits: dict[str, int]) -> str:
    """Stable digest of the execution request (single key rule)."""
    payload = {"command": command, "limits": limits}
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_create_key(
    *, workspace_id: str, invocation_id: str, request_digest: str
) -> str:
    return f"create:{workspace_id}:{invocation_id}:{request_digest}"


def build_execute_key(
    *, workspace_id: str, invocation_id: str, request_digest: str
) -> str:
    return f"execute:{workspace_id}:{invocation_id}:{request_digest}"


def _common_data(
    *,
    effect_id: str,
    invocation_id: str,
    status: str,
    command: str,
    limits: dict[str, int],
    request_digest: str,
    create_key: str,
    execute_key: str,
    sandbox_id: str | None = None,
    output: str | None = None,
    error_code: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "effect_id": effect_id,
        "invocation_id": invocation_id,
        "status": status,
        "command": command,
        "limits": dict(limits),
        "request_digest": request_digest,
        "create_key": create_key,
        "execute_key": execute_key,
    }
    if sandbox_id is not None:
        data["sandbox_id"] = sandbox_id
    if output is not None:
        data["output"] = output
    if error_code is not None:
        data["error_code"] = error_code
    if reason is not None:
        data["reason"] = reason
    return data


def effect_planned(
    *,
    effect_id: str,
    invocation_id: str,
    command: str,
    limits: dict[str, int],
    request_digest: str,
    create_key: str,
    execute_key: str,
) -> RunEventDraft:
    return RunEventDraft(
        type="effect.planned",
        data=_common_data(
            effect_id=effect_id,
            invocation_id=invocation_id,
            status=EffectState.PLANNED,
            command=command,
            limits=limits,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
        ),
    )


def effect_executing(
    *,
    effect_id: str,
    invocation_id: str,
    command: str,
    limits: dict[str, int],
    request_digest: str,
    create_key: str,
    execute_key: str,
) -> RunEventDraft:
    return RunEventDraft(
        type="effect.executing",
        data=_common_data(
            effect_id=effect_id,
            invocation_id=invocation_id,
            status=EffectState.EXECUTING,
            command=command,
            limits=limits,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
        ),
    )


def effect_succeeded(
    *,
    effect_id: str,
    invocation_id: str,
    command: str,
    limits: dict[str, int],
    request_digest: str,
    create_key: str,
    execute_key: str,
    sandbox_id: str,
    output: str,
) -> RunEventDraft:
    return RunEventDraft(
        type="effect.succeeded",
        data=_common_data(
            effect_id=effect_id,
            invocation_id=invocation_id,
            status=EffectState.SUCCEEDED,
            command=command,
            limits=limits,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
            sandbox_id=sandbox_id,
            output=output,
        ),
    )


def effect_failed(
    *,
    effect_id: str,
    invocation_id: str,
    command: str,
    limits: dict[str, int],
    request_digest: str,
    create_key: str,
    execute_key: str,
    error_code: str,
    reason: str,
    sandbox_id: str | None = None,
) -> RunEventDraft:
    return RunEventDraft(
        type="effect.failed",
        data=_common_data(
            effect_id=effect_id,
            invocation_id=invocation_id,
            status=EffectState.FAILED,
            command=command,
            limits=limits,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
            sandbox_id=sandbox_id,
            error_code=error_code,
            reason=reason,
        ),
    )


def effect_uncertain(
    *,
    effect_id: str,
    invocation_id: str,
    command: str,
    limits: dict[str, int],
    request_digest: str,
    create_key: str,
    execute_key: str,
    error_code: str,
    reason: str,
    sandbox_id: str | None = None,
) -> RunEventDraft:
    return RunEventDraft(
        type="effect.uncertain",
        data=_common_data(
            effect_id=effect_id,
            invocation_id=invocation_id,
            status=EffectState.UNCERTAIN,
            command=command,
            limits=limits,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
            sandbox_id=sandbox_id,
            error_code=error_code,
            reason=reason,
        ),
    )


def effect_reconciling(
    *,
    effect_id: str,
    invocation_id: str,
    command: str,
    limits: dict[str, int],
    request_digest: str,
    create_key: str,
    execute_key: str,
    reason: str,
    sandbox_id: str | None = None,
) -> RunEventDraft:
    return RunEventDraft(
        type="effect.reconciling",
        data=_common_data(
            effect_id=effect_id,
            invocation_id=invocation_id,
            status=EffectState.RECONCILING,
            command=command,
            limits=limits,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
            sandbox_id=sandbox_id,
            reason=reason,
        ),
    )


def _limits_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("limits")
    if not isinstance(raw, dict):
        return {}
    return {str(key): int(value) for key, value in raw.items() if isinstance(value, int)}


def _validated_identity_field(value: str, field: str) -> str:
    if not value or not _ID_RE.fullmatch(value):
        raise ValueError(
            f"sandbox invocation {field} must match the shared ID contract "
            f"([A-Za-z0-9._:-]{{1,128}}), got {value!r}"
        )
    return value


def sandbox_invocation_handler(
    remote: SandboxRemote | None,
) -> RunAttemptHandler:
    """Built-in handler for ``RunCommand.kind == "sandbox_invocation"``.

    The handler owns ONLY the effect event order around the remote seam; it
    never touches PG, leases, commits or headers. An ``unknown`` remote
    result becomes ``effect.uncertain`` and a failed CoreOutcome - never a
    fake success.
    """

    async def _drive(attempt: AttemptInput) -> AsyncIterator[CoreItem]:
        if remote is None:
            yield CoreOutcome(
                status="failed",
                error_code="SANDBOX_REMOTE_NOT_CONFIGURED",
                error_message=(
                    "sandbox_invocation command requires a SandboxRemote "
                    "adapter (core sandbox path is not configured)"
                ),
            )
            return

        payload = dict(attempt.command.payload)
        command = str(payload.get("command") or "").strip()
        if not command:
            yield CoreOutcome(
                status="failed",
                error_code="OPENSANDBOX_INVALID_ARGS",
                error_message="sandbox_invocation command must be non-empty",
            )
            return

        run_id = str(attempt.run_id)
        step_id = str(payload.get("step_id") or "").strip() or f"step-{run_id}"
        invocation_id = (
            str(payload.get("invocation_id") or "").strip()
            or f"inv-{run_id}-{attempt.attempt}"
        )
        attempt_id = (
            str(payload.get("attempt_id") or "").strip() or f"att-{attempt.attempt}"
        )
        client_request_id = (
            str(payload.get("client_request_id") or "").strip()
            or f"req-{run_id}-{attempt.attempt}"
        )
        try:
            _validated_identity_field(step_id, "step_id")
            _validated_identity_field(invocation_id, "invocation_id")
            _validated_identity_field(attempt_id, "attempt_id")
            _validated_identity_field(client_request_id, "client_request_id")
        except ValueError as exc:
            yield CoreOutcome(
                status="failed",
                error_code="OPENSANDBOX_IDENTITY_INCOMPLETE",
                error_message=str(exc),
            )
            return

        limits = _limits_from_payload(payload)
        digest = request_digest(command=command, limits=limits)
        create_key = build_create_key(
            workspace_id=str(attempt.workspace_id),
            invocation_id=invocation_id,
            request_digest=digest,
        )
        execute_key = build_execute_key(
            workspace_id=str(attempt.workspace_id),
            invocation_id=invocation_id,
            request_digest=digest,
        )
        effect_id = str(payload.get("effect_id") or "").strip() or (
            f"effect-{run_id}-{attempt.attempt}"
        )

        planned = effect_planned(
            effect_id=effect_id,
            invocation_id=invocation_id,
            command=command,
            limits=limits,
            request_digest=digest,
            create_key=create_key,
            execute_key=execute_key,
        )
        executing = effect_executing(
            effect_id=effect_id,
            invocation_id=invocation_id,
            command=command,
            limits=limits,
            request_digest=digest,
            create_key=create_key,
            execute_key=execute_key,
        )
        yield CoreEvent(type=planned.type, data=planned.data or {})
        yield CoreEvent(type=executing.type, data=executing.data or {})

        request = SandboxExecutionRequest(
            identity=SandboxIdentity(
                workspace_id=str(attempt.workspace_id),
                run_id=run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                invocation_id=invocation_id,
                client_request_id=client_request_id,
            ),
            command=command,
            limits=limits,
            create_key=create_key,
            execute_key=execute_key,
            resume_sandbox_id=payload.get("resume_sandbox_id"),
            resume_phase=payload.get("resume_phase"),
        )
        result: SandboxExecutionResult = await remote.execute(request)

        if result.status == "succeeded":
            succeeded = effect_succeeded(
                effect_id=effect_id,
                invocation_id=invocation_id,
                command=command,
                limits=limits,
                request_digest=digest,
                create_key=create_key,
                execute_key=execute_key,
                sandbox_id=result.sandbox_id or "",
                output=result.output or "",
            )
            yield CoreEvent(type=succeeded.type, data=succeeded.data or {})
            yield CoreOutcome(status="completed")
            return

        if result.status == "unknown":
            uncertain = effect_uncertain(
                effect_id=effect_id,
                invocation_id=invocation_id,
                command=command,
                limits=limits,
                request_digest=digest,
                create_key=create_key,
                execute_key=execute_key,
                sandbox_id=result.sandbox_id,
                error_code=result.error_code or "OPENSANDBOX_UNKNOWN_OUTCOME",
                reason=result.error_message or "sandbox outcome unknown",
            )
            yield CoreEvent(type=uncertain.type, data=uncertain.data or {})
            yield CoreOutcome(
                status="failed",
                error_code=result.error_code or "OPENSANDBOX_UNKNOWN_OUTCOME",
                error_message=result.error_message or "sandbox outcome unknown",
            )
            return

        failed = effect_failed(
            effect_id=effect_id,
            invocation_id=invocation_id,
            command=command,
            limits=limits,
            request_digest=digest,
            create_key=create_key,
            execute_key=execute_key,
            sandbox_id=result.sandbox_id,
            error_code=result.error_code or "OPENSANDBOX_FAILED",
            reason=result.error_message or "sandbox execution failed",
        )
        yield CoreEvent(type=failed.type, data=failed.data or {})
        yield CoreOutcome(
            status="failed",
            error_code=result.error_code or "OPENSANDBOX_FAILED",
            error_message=result.error_message or "sandbox execution failed",
        )

    return _drive


@dataclass
class _Fold:
    status: str
    invocation_id: str
    sandbox_id: str | None = None
    create_key: str | None = None
    execute_key: str | None = None
    request_digest: str | None = None
    command: str | None = None
    limits: dict[str, int] | None = None
    output: str | None = None
    error_code: str | None = None
    reason: str | None = None


def _optional_str(data: Any, field: str) -> str | None:
    value = data.get(field)
    return value if isinstance(value, str) else None


def _optional_int_dict(data: Any, field: str) -> dict[str, int] | None:
    value = data.get(field)
    if not isinstance(value, Mapping):
        return None
    return {str(k): int(v) for k, v in value.items() if isinstance(v, int)}


def _require_effect_id(data: Any) -> str:
    value = data.get("effect_id")
    if not isinstance(value, str) or not value:
        raise ValueError("effect.* event data must carry a non-empty effect_id")
    return value


def project_effects(events: Iterable[EventEnvelope]) -> dict[str, EffectView]:
    """Fold ``effect.*`` events into one :class:`EffectView` per effect.

    Terminal effects reject any later event through the frozen effect state
    machine (``validate_transition`` raises exactly like the write path).
    A partial replay may legitimately start after ``effect.planned``; the
    first observed event initializes the fold, and every subsequent event
    is transition-validated against the frozen table.
    """
    folds: dict[str, _Fold] = {}
    for event in events:
        if not event.type.startswith("effect."):
            continue
        target = _EVENT_STATE_TARGET.get(event.type)
        if target is None:
            # ``effect.reconciled`` has no state-machine state; skip it.
            continue
        effect_id = _require_effect_id(event.data)
        fold = folds.get(effect_id)
        if fold is None:
            fold = _Fold(
                status=target,
                invocation_id=_optional_str(event.data, "invocation_id") or "",
            )
            folds[effect_id] = fold
        else:
            validate_transition("effect", fold.status, target)
            fold.status = target

        fold.sandbox_id = _optional_str(event.data, "sandbox_id") or fold.sandbox_id
        fold.create_key = _optional_str(event.data, "create_key") or fold.create_key
        fold.execute_key = _optional_str(event.data, "execute_key") or fold.execute_key
        fold.request_digest = (
            _optional_str(event.data, "request_digest") or fold.request_digest
        )
        fold.command = _optional_str(event.data, "command") or fold.command
        fold.limits = _optional_int_dict(event.data, "limits") or fold.limits
        fold.output = _optional_str(event.data, "output") or fold.output
        fold.error_code = _optional_str(event.data, "error_code") or fold.error_code
        fold.reason = _optional_str(event.data, "reason") or fold.reason

    return {
        effect_id: EffectView(
            effect_id=effect_id,
            invocation_id=fold.invocation_id,
            status=fold.status,
            sandbox_id=fold.sandbox_id,
            create_key=fold.create_key,
            execute_key=fold.execute_key,
            request_digest=fold.request_digest,
            command=fold.command,
            limits=fold.limits,
            output=fold.output,
            error_code=fold.error_code,
            reason=fold.reason,
        )
        for effect_id, fold in folds.items()
    }


__all__ = [
    "EffectView",
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
    "sandbox_invocation_handler",
]
