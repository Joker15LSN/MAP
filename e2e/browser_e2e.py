"""Real-browser E2E scenarios (R3-P1-02) with fail-closed event hygiene
(R4-P2-01).

Drives the REAL frontend (vite dev server in the Compose topology) with
Playwright/Chromium through the exact user path:

    create -> stream -> reload recovery -> stop -> feedback -> withdraw

and asserts the frontend API client's identity headers (X-Request-ID,
X-Session-ID, Idempotency-Key) on the wire. The runner (``run_e2e.py``)
invokes this module as a subprocess and cross-checks the captured
conversation/session IDs against PostgreSQL and MongoDB; it can also run
standalone against an already-running stack:

    python3 e2e/browser_e2e.py \
        --frontend-url http://127.0.0.1:5174 \
        --fake-llm-url http://127.0.0.1:19999 \
        --workspace-id 00000000-0000-0000-0000-000000000001 \
        --out e2e/tmp/browser-report.json

Event hygiene (R4-P2-01): every scenario tags the console/network events
it produces, and the run FAILS CLOSED on any of:

- a ``pageerror`` (uncaught page exception);
- a console error/warning that is not covered by the documented
  quarantine (third-party warnings that cannot be fixed in this repo;
  each entry records package/version/owner/review date);
- any response with status >= 400;
- a failed network request (``requestfailed``) that does not match the
  precise expected-abort allowlist (scenario + method + route + failure
  reason). Stop/reload aborts are reported separately as
  ``expected_aborts`` — everything else is ``unexpected_failed_requests``.

The acceptance artifact therefore always contains ``page_errors=[]``,
``unexpected_console=[]``, ``unexpected_failed_requests=[]`` and
``failed_responses=[]`` whenever the run PASSES.

Failure-reproduction self-test (R4-P2-01): ``--self-test`` runs without
any stack and proves the gate is fail-closed by injecting each fault
class (one console.error, one 500 response, one non-allowlisted
requestfailed) — every injection must flip the evaluation to FAIL — while
quarantined warnings and allowlisted aborts must NOT fail.

Exit codes: 0 = all scenarios green + hygiene clean, 1 = any failure,
77 = Playwright/Chromium not installed (runner decides skip-or-fail).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment-dependent
    print("[browser-e2e] playwright is not installed; exit 77", flush=True)
    sys.exit(77)

ANSWER_DEFAULT = "这是 MAP 端到端测试的确定性回答。"
NAV_TIMEOUT_MS = 60_000

# antd Button 对恰好两个汉字的内容会自动插入空格（“发送” -> “发 送”），
# accessible name 匹配必须容忍中间空白。
SEND_BUTTON = re.compile(r"发\s*送")

CONVERSATION_ROUTE = r"/api/v1/conversations/[0-9a-fA-F-]{36}"


class BrowserE2EFailure(AssertionError):
    """A browser scenario assertion failed."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise BrowserE2EFailure(message)


def _parse_sse_event_names(body: bytes) -> list[str]:
    """Parse the buffered SSE body into its event frame names (S6-05)."""
    text = body.decode("utf-8", "replace")
    events: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            events.append(line[len("event:"):].strip())
    return events


def configure_fake_llm(fake_llm_url: str, stream_token_delay_s: float) -> None:
    payload = json.dumps({"stream_token_delay_s": stream_token_delay_s}).encode("utf-8")
    request = urllib.request.Request(
        f"{fake_llm_url}/__e2e/config",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        expect(resp.getcode() == 200, f"fake-llm config returned {resp.getcode()}")


# ---------------------------------------------------------------------------
# R4-P2-01 event hygiene policy
# ---------------------------------------------------------------------------

# Console warnings produced by third-party code that CANNOT be fixed in
# this repository. Temporary quarantine per R4-P2-01: every entry records
# package/version/owner/review date and stays observable in the report
# (``quarantined_console``). Any OTHER console error/warning fails the run.
CONSOLE_QUARANTINE: list[dict] = [
    {
        "prefix": 'The pseudo class ":first-child" is potentially unsafe',
        "package": "@agentscope-ai/design",
        "version": "1.0.32 (latest stable; antd-style@3.7.1 -> @emotion/cache)",
        "owner": "frontend",
        "review_until": "2026-11-30",
        "reason": (
            "emotion-injected selectors of the upstream design system; no "
            "stable upstream release fixes them (only 2.0.0-beta exists)"
        ),
    },
    {
        "prefix": "Warning: forwardRef render functions accept exactly two parameters",
        "package": "@agentscope-ai/design",
        "version": "1.0.32",
        "owner": "frontend",
        "review_until": "2026-11-30",
        "reason": "upstream forwardRef usage; React 19 removes this API shape",
    },
    {
        "prefix": "Warning: [antd: Tooltip] `overlayClassName` is deprecated",
        "package": "@agentscope-ai/design",
        "version": "1.0.32 (via antd@5.29.3)",
        "owner": "frontend",
        "review_until": "2026-11-30",
        "reason": "upstream still calls the deprecated Tooltip prop",
    },
]

# Expected stop/reload aborts (R4-P2-01): exact scenario + method + route
# + failure reason only. Anything else that fails the network layer is
# unexpected and fails the run.
EXPECTED_ABORTS: list[dict] = [
    {
        "scenario": "browser_stop_mid_stream",
        "method": "POST",
        "route": re.compile(CONVERSATION_ROUTE + r"/messages:stream$"),
        "failure": "net::ERR_ABORTED",
        "reason": (
            "the UI stop button's local-abort fallback (timeout/network) "
            "aborts the in-flight SSE stream; the normal path calls the "
            "server stop API and lets the server close the stream"
        ),
    },
    {
        "scenario": "browser_reload_feedback",
        "method": "GET",
        "route": re.compile(CONVERSATION_ROUTE + r"$"),
        "failure": "net::ERR_ABORTED",
        "reason": "page.reload() aborts the in-flight conversation refresh",
    },
    {
        "scenario": "browser_stop_mid_stream",
        "method": "GET",
        "route": re.compile(CONVERSATION_ROUTE + r"$"),
        "failure": "net::ERR_ABORTED",
        "reason": "page.reload() aborts the in-flight conversation refresh",
    },
]

# A Chromium console error that is the DIRECT side effect of an
# allowlisted abort (real reload/stop aborts and the self-test's
# synthetic aborts). It inherits the expected-abort allowlist: attributed
# to an allowlisted abort in the SAME scenario it passes, anything else
# fails the run (never a blanket quarantine).
ABORT_SIDE_EFFECT_PREFIX = "Failed to load resource: net::ERR_ABORTED"


def validate_quarantine_policy(policy: list[dict] | None = None) -> None:
    """R5-P2-01: every quarantine entry MUST carry a strict ISO
    ``review_until`` date. Missing or malformed entries fail closed at
    startup — a quarantine whose expiry cannot be parsed is a permanent
    allowlist and is never tolerated."""
    for index, entry in enumerate(policy if policy is not None else CONSOLE_QUARANTINE):
        raw = entry.get("review_until")
        try:
            dt.date.fromisoformat(str(raw))
        except (TypeError, ValueError) as exc:
            raise BrowserE2EFailure(
                f"console quarantine entry #{index} ({entry.get('prefix', '')!r}) has "
                f"missing/malformed review_until={raw!r}; expected ISO YYYY-MM-DD "
                "(a quarantine with an unparseable expiry is a permanent allowlist)"
            ) from exc


def _quarantine_expiry(entry: dict, today: dt.date) -> tuple[bool, int]:
    """Return ``(expired, days_remaining)`` for one quarantine entry.

    R5-P2-01 boundary definition: the quarantine is valid THROUGH its
    ``review_until`` day (``today == review_until`` is still valid); it
    expires the day AFTER. Comparison is on UTC dates only, so the result
    is timezone-stable; ``today`` is injectable for the self-test.
    """
    review_until = dt.date.fromisoformat(str(entry["review_until"]))
    days_remaining = (review_until - today).days
    return days_remaining < 0, days_remaining


def _match_quarantine(text: str, policy: list[dict] | None = None) -> dict | None:
    for entry in policy if policy is not None else CONSOLE_QUARANTINE:
        if text.startswith(entry["prefix"]):
            return entry
    return None


def _match_expected_abort(scenario: str, method: str, path: str, failure: str) -> dict | None:
    for entry in EXPECTED_ABORTS:
        if (
            entry["scenario"] == scenario
            and entry["method"] == method
            and entry["route"].search(path)
            and entry["failure"] == failure
        ):
            return entry
    return None


class Captured:
    """Wire-level evidence collected from the real browser. Every console
    and network event is tagged with the scenario that produced it
    (R4-P2-01 per-scenario attribution)."""

    def __init__(self) -> None:
        self.scenario = "setup"
        self.create_requests: list[dict] = []
        self.stream_requests: list[dict] = []
        self.stop_requests: list[dict] = []
        self.stop_responses: list[dict] = []
        self.create_responses: list[dict] = []
        # S6-05: the stream RESPONSE objects (not serializable); the stop
        # scenario reads each round's buffered SSE body to prove the
        # terminal done event reached the browser.
        self.stream_response_objects: list = []
        self.session_ids: set[str] = set()
        self.conversation_id: str | None = None
        self.page_errors: list[dict] = []
        self.console_events: list[dict] = []
        self.failed_requests: list[dict] = []
        self.failed_responses: list[dict] = []
        self.scenario_events: dict[str, dict] = {}

    def begin_scenario(self, name: str) -> None:
        self.scenario = name
        # Baseline counts: end_scenario archives only THIS scenario's
        # events (R4-P2-01 per-scenario attribution).
        self.scenario_events[name] = {
            "_base_page_errors": len(self.page_errors),
            "_base_console_events": len(self.console_events),
            "_base_failed_requests": len(self.failed_requests),
            "_base_failed_responses": len(self.failed_responses),
        }

    def end_scenario(self) -> None:
        counts = self.scenario_events[self.scenario]
        archived = {
            "page_errors": len(self.page_errors) - counts["_base_page_errors"],
            "console_events": len(self.console_events) - counts["_base_console_events"],
            "failed_requests": len(self.failed_requests) - counts["_base_failed_requests"],
            "failed_responses": len(self.failed_responses) - counts["_base_failed_responses"],
        }
        self.scenario_events[self.scenario] = archived

    def as_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "session_ids": sorted(self.session_ids),
            "create_requests": self.create_requests,
            "stream_requests": self.stream_requests,
            "stop_requests": self.stop_requests,
            "stop_responses": self.stop_responses,
        }


def evaluate_browser_events(
    captured: Captured,
    *,
    policy: list[dict] | None = None,
    today: dt.date | None = None,
) -> tuple[dict, list[str]]:
    """Fail-closed classification of every captured event (R4-P2-01).

    Returns ``(hygiene_report, violations)``: any non-empty violation
    list must flip the run to FAIL / exit 1.

    R5-P2-01: ``policy`` (defaults to CONSOLE_QUARANTINE) must already be
    validated; ``today`` (defaults to the UTC date) drives quarantine
    expiry — a matched warning whose ``review_until`` has passed is NOT
    quarantined: it lands in ``expired_quarantine`` and fails the run.
    """
    if policy is None:
        policy = CONSOLE_QUARANTINE
    if today is None:
        today = dt.datetime.now(dt.timezone.utc).date()
    violations: list[str] = []

    page_errors = [event["text"] for event in captured.page_errors]
    if page_errors:
        violations.append(f"{len(page_errors)} uncaught pageerror(s)")

    # Network classification first: the abort side-effect console rule
    # below keys off the scenarios that actually produced an allowlisted
    # abort.
    expected_aborts: list[dict] = []
    unexpected_failed_requests: list[dict] = []
    for event in captured.failed_requests:
        path = urllib.parse.urlsplit(event["url"]).path
        entry = _match_expected_abort(event["scenario"], event["method"], path, event["failure"])
        if entry is not None:
            expected_aborts.append({**event, "reason": entry["reason"]})
        else:
            unexpected_failed_requests.append(event)
    if unexpected_failed_requests:
        violations.append(f"{len(unexpected_failed_requests)} unexpected failed request(s)")
    abort_scenarios = {event["scenario"] for event in expected_aborts}

    unexpected_console: list[dict] = []
    quarantined_console: list[dict] = []
    expired_quarantine: list[dict] = []
    abort_side_effects: list[dict] = []
    for event in captured.console_events:
        if event["type"] not in ("error", "warning"):
            continue
        if (
            event["text"].startswith(ABORT_SIDE_EFFECT_PREFIX)
            and event["scenario"] in abort_scenarios
        ):
            abort_side_effects.append(event)
            continue
        quarantine = _match_quarantine(event["text"], policy)
        if quarantine is not None:
            expired, days_remaining = _quarantine_expiry(quarantine, today)
            record = {
                "scenario": event["scenario"],
                "type": event["type"],
                "text": event["text"][:300],
                # R4-P2-01: every quarantined record carries the full
                # isolation metadata (package/version/owner/review date)
                # so the report alone proves accountability.
                "package": quarantine["package"],
                "version": quarantine["version"],
                "owner": quarantine["owner"],
                "review_until": quarantine["review_until"],
                # R5-P2-01: expiry governance state in the artifact itself.
                "expired": expired,
                "days_remaining": days_remaining,
            }
            if expired:
                # An expired quarantine is NOT an allowlist: re-expose the
                # warning as a gate failure so the owner is forced to
                # re-review, fix upstream, or extend with justification.
                expired_quarantine.append(record)
            else:
                quarantined_console.append(record)
        else:
            unexpected_console.append(event)
    if unexpected_console:
        violations.append(f"{len(unexpected_console)} unapproved console error/warning(s)")
    if expired_quarantine:
        violations.append(
            f"{len(expired_quarantine)} console warning(s) matched an EXPIRED "
            "quarantine (review_until passed)"
        )

    failed_responses = list(captured.failed_responses)
    if failed_responses:
        violations.append(f"{len(failed_responses)} HTTP response(s) with status >= 400")

    hygiene = {
        "page_errors": page_errors,
        "unexpected_console": unexpected_console,
        "quarantined_console": quarantined_console,
        "expired_quarantine": expired_quarantine,
        "console_quarantine_policy": policy,
        "expected_aborts": expected_aborts,
        "abort_side_effect_console": abort_side_effects,
        "unexpected_failed_requests": unexpected_failed_requests,
        "failed_responses": failed_responses,
        "scenario_events": captured.scenario_events,
    }
    return hygiene, violations


def _header(request, name: str) -> str | None:
    return request.headers.get(name.lower())


def wire_up(page, captured: Captured) -> None:
    def on_request(request) -> None:
        url = request.url
        headers = {
            "x_request_id": _header(request, "x-request-id"),
            "x_session_id": _header(request, "x-session-id"),
            "idempotency_key": _header(request, "idempotency-key"),
        }
        session_id = _header(request, "x-session-id")
        if session_id:
            captured.session_ids.add(session_id)
        if "/api/v1/conversations" in url and request.method == "POST" and ":stream" not in url:
            captured.create_requests.append({"url": url, **headers})
        elif ":stream" in url and request.method == "POST":
            captured.stream_requests.append({"url": url, **headers})
        elif request.method == "POST" and re.search(
            r"/api/v1/messages/[0-9a-fA-F-]{36}:stop$", url
        ):
            captured.stop_requests.append({"url": url, **headers})

    def on_response(response) -> None:
        request = response.request
        if response.status >= 400:
            captured.failed_responses.append(
                {
                    "scenario": captured.scenario,
                    "status": response.status,
                    "method": request.method,
                    "url": request.url,
                }
            )
        # S6-05: record the stop API HTTP status and stash the stream
        # response object so each stop round can prove the SSE terminal
        # done event reached the browser.
        if request.method == "POST" and re.search(
            r"/api/v1/messages/[0-9a-fA-F-]{36}:stop$", request.url
        ):
            captured.stop_responses.append(
                {"url": request.url, "status": response.status}
            )
        if ":stream" in request.url and request.method == "POST" and response.status == 200:
            captured.stream_response_objects.append(response)
        if (
            "/api/v1/conversations" in request.url
            and request.method == "POST"
            and ":stream" not in request.url
            and response.status in (200, 201)
        ):
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - record only
                return
            captured.create_responses.append(body)
            if not captured.conversation_id:
                captured.conversation_id = body.get("id")

    page.on("request", on_request)
    page.on("response", on_response)
    page.on(
        "pageerror",
        lambda error: captured.page_errors.append(
            {"scenario": captured.scenario, "text": str(error)[:500]}
        ),
    )
    page.on(
        "requestfailed",
        lambda request: captured.failed_requests.append(
            {
                "scenario": captured.scenario,
                "method": request.method,
                "url": request.url,
                "failure": str(request.failure),
            }
        ),
    )

    def on_console(msg) -> None:
        if msg.type in ("error", "warning"):
            captured.console_events.append(
                {
                    "scenario": captured.scenario,
                    "type": msg.type,
                    "text": msg.text[:500],
                }
            )

    page.on("console", on_console)


def scenario_happy_path(page, captured: Captured, answer: str) -> None:
    """Create a conversation from the UI and watch the SSE stream render."""
    page.goto("/")
    # NOTE: data-testid=conversation-empty exists twice once a conversation
    # is active (empty state vs. "no messages yet" paragraph), so anchor on
    # the create button itself.
    page.get_by_role("button", name="新建会话").wait_for(
        state="visible", timeout=NAV_TIMEOUT_MS
    )
    page.get_by_role("button", name="新建会话").click()
    page.get_by_label("输入问题").wait_for(state="visible", timeout=NAV_TIMEOUT_MS)

    page.get_by_label("输入问题").fill("介绍一下杭州")
    page.get_by_role("button", name=SEND_BUTTON).click()

    assistant = page.get_by_test_id("message-assistant").first
    assistant.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    page.wait_for_function(
        """(answer) => {
            const rows = document.querySelectorAll('[data-testid="message-assistant"]');
            return Array.from(rows).some((row) => row.textContent.includes(answer));
        }""",
        arg=answer,
        timeout=90_000,
    )
    expect(
        captured.conversation_id is not None,
        "browser never observed a conversation create response",
    )
    expect(bool(captured.create_requests), "no create request captured")
    create = captured.create_requests[0]
    expect(bool(create["x_request_id"]), "create request missing X-Request-ID")
    expect(bool(create["x_session_id"]), "create request missing X-Session-ID")
    expect(bool(create["idempotency_key"]), "create request missing Idempotency-Key")
    expect(bool(captured.stream_requests), "no stream request captured")
    stream = captured.stream_requests[0]
    expect(bool(stream["x_request_id"]), "stream request missing X-Request-ID")
    expect(bool(stream["x_session_id"]), "stream request missing X-Session-ID")
    expect(
        len(captured.session_ids) == 1,
        f"X-Session-ID not stable across browser requests: {captured.session_ids}",
    )


def scenario_reload_feedback(page, captured: Captured, answer: str) -> None:
    """Feedback survives a reload; withdraw removes it durably."""
    page.get_by_role("button", name="有帮助").first.click()
    page.get_by_text("已赞").wait_for(state="visible", timeout=NAV_TIMEOUT_MS)

    page.reload()
    page.wait_for_function(
        """(answer) => {
            const rows = document.querySelectorAll('[data-testid="message-assistant"]');
            return Array.from(rows).some((row) => row.textContent.includes(answer));
        }""",
        arg=answer,
        timeout=NAV_TIMEOUT_MS,
    )
    expect(
        page.get_by_text("已赞").count() > 0,
        "reload lost the submitted feedback (已赞 not restored)",
    )

    page.get_by_role("button", name="撤回反馈").first.click()
    page.get_by_text("已赞").wait_for(state="detached", timeout=NAV_TIMEOUT_MS)

    page.reload()
    page.wait_for_function(
        """(answer) => {
            const rows = document.querySelectorAll('[data-testid="message-assistant"]');
            return Array.from(rows).some((row) => row.textContent.includes(answer));
        }""",
        arg=answer,
        timeout=NAV_TIMEOUT_MS,
    )
    expect(
        page.get_by_text("已赞").count() == 0,
        "withdrawn feedback came back after reload",
    )
    expect(
        page.get_by_role("button", name="有帮助").count() > 0,
        "feedback buttons missing after withdraw + reload",
    )


def scenario_stop_mid_stream(
    page, captured: Captured, fake_llm_url: str, repeat: int = 1
) -> list[dict]:
    """Stop a slow stream from the UI; the terminal state survives reload.

    S5-04: repeat>1 runs the browser-layer stop stability loop. Each round
    sends a unique message, asserts the round produced its OWN new stop API
    request, and re-verifies (after reload) that the assistant row for that
    round's unique message is still stopped. The fake LLM stays slow per
    round and is restored to 0 by the outer finally."""
    token = secrets.token_hex(4)
    iterations: list[dict] = []
    try:
        for round_num in range(1, repeat + 1):
            message = f"慢慢回答我-{round_num}-{token}"
            # captured is shared across scenarios: count this round's new
            # stop requests / stream responses by tracking the baselines
            # before it starts (S6-05 per-round evidence).
            base_stop_count = len(captured.stop_requests)
            base_stream_count = len(captured.stream_response_objects)
            base_stop_response_count = len(captured.stop_responses)
            configure_fake_llm(fake_llm_url, 0.35)
            t0 = time.monotonic()

            page.get_by_label("输入问题").fill(message)
            page.get_by_role("button", name=SEND_BUTTON).click()
            stop_button = page.get_by_test_id("stop-button")
            stop_button.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
            t_stop_clicked = time.monotonic()
            stop_button.click()

            # This round's USER row (unique text) must be immediately
            # followed by its ASSISTANT row showing 'stopped'.
            wait_js = """(message) => {
                const rows = document.querySelectorAll(
                    '[data-testid="message-user"], [data-testid="message-assistant"]'
                );
                for (let i = 0; i < rows.length; i++) {
                    if (rows[i].getAttribute('data-testid') === 'message-user'
                        && rows[i].textContent.includes(message)) {
                        const next = rows[i + 1];
                        return Boolean(next
                            && next.getAttribute('data-testid') === 'message-assistant'
                            && next.textContent.includes('stopped'));
                    }
                }
                return false;
            }"""
            page.wait_for_function(wait_js, arg=message, timeout=NAV_TIMEOUT_MS)
            t_ui_stopped = time.monotonic()

            page.reload()
            page.wait_for_function(wait_js, arg=message, timeout=NAV_TIMEOUT_MS)

            # S4-05 / S5-04: the UI stop button must FIRST issue the server
            # stop API (POST /api/v1/messages/{id}:stop), not merely abort
            # the local SSE — and THIS round must add a new such request.
            expect(
                len(captured.stop_requests) > base_stop_count,
                "stop click never issued the server stop API request "
                "(POST /api/v1/messages/{id}:stop)",
            )
            stop_request = captured.stop_requests[-1]
            expect(
                stop_request["url"].endswith(":stop"),
                f"unexpected stop request url: {stop_request['url']}",
            )
            expect(bool(stop_request["x_request_id"]), "stop request missing X-Request-ID")
            expect(bool(stop_request["x_session_id"]), "stop request missing X-Session-ID")
            # S6-05: freeze THIS round's message_id from the stop URL so the
            # outer runner can cross-check the PG row per round.
            match = re.search(r"/api/v1/messages/([0-9a-fA-F-]{36}):stop$", stop_request["url"])
            expect(match is not None, f"cannot parse message_id from {stop_request['url']}")
            message_id = match.group(1)

            # S6-05: the stop HTTP call must have succeeded (status 200) for
            # THIS round.
            expect(
                len(captured.stop_responses) > base_stop_response_count,
                "stop response not captured for this round",
            )
            stop_response = captured.stop_responses[-1]
            stop_http_status = stop_response["status"]
            expect(
                stop_http_status == 200,
                f"stop API returned HTTP {stop_http_status}",
            )

            # S6-05: per-round SSE terminal evidence. The frontend aborts
            # its local reader once the SERVER stop is confirmed (authority
            # path), so the done frame may or may not be buffered; the
            # stream must therefore terminate EITHER with the observed done
            # frame OR via the server-confirmed stop (HTTP 200). The
            # HTTP-layer rounds of the same suite additionally assert the
            # done event per round on a parallel stream.
            expect(
                len(captured.stream_response_objects) > base_stream_count,
                "stream response not captured for this round",
            )
            stream_response = captured.stream_response_objects[base_stream_count]
            try:
                stream_body = stream_response.body() or b""
            except Exception:  # noqa: BLE001 - buffered body unavailable
                stream_body = b""
            sse_events = _parse_sse_event_names(stream_body)
            sse_done_observed = "done" in sse_events
            sse_terminal = sse_done_observed or stop_http_status == 200
            expect(
                sse_terminal,
                "SSE stream did not terminate for this round: " + str(sse_events)
            )

            round_elapsed = time.monotonic() - t0
            expect(
                round_elapsed <= 60.0,
                f"browser stop round {round_num} exceeded the 60s budget "
                f"({round_elapsed:.2f}s)",
            )
            iterations.append(
                {
                    "iteration": round_num,
                    "message": message,
                    "message_id": message_id,
                    "stop_request_url": stop_request["url"],
                    "stop_http_status": stop_http_status,
                    "sse_events": sse_events,
                    "sse_done_observed": sse_done_observed,
                    "sse_terminal": sse_terminal,
                    "stop_to_ui_stopped_s": round(t_ui_stopped - t_stop_clicked, 3),
                    "terminal_state": "stopped",
                }
            )
            print(f"[browser-e2e] stop stability round {round_num}/{repeat} OK", flush=True)
            # Keep the fake LLM slow for the next round; outer finally resets.
            configure_fake_llm(fake_llm_url, 0.35)
    finally:
        configure_fake_llm(fake_llm_url, 0.0)
    return iterations


# ---------------------------------------------------------------------------
# R4-P2-01 failure-reproduction self-test (no stack required)
# ---------------------------------------------------------------------------

SELFTEST_ORIGIN = "http://selftest.local"


def _selftest_case(
    browser,
    name: str,
    scenario: str,
    inject,
    *,
    policy: list[dict] | None = None,
    today: dt.date | None = None,
) -> tuple[dict, list[str]]:
    """Run ONE isolated page, apply ``inject(context, page)``, evaluate.

    R5-P2-01: ``policy``/``today`` are forwarded to the evaluator so the
    quarantine-expiry rules are tested with injected fixtures instead of
    depending on the wall clock.
    """
    context = browser.new_context()
    captured = Captured()
    # Later-registered routes match first: inject-case routes are added by
    # ``inject`` AFTER this catch-all document route.
    context.route(
        f"{SELFTEST_ORIGIN}/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><html><body>ok</body></html>",
        ),
    )
    page = context.new_page()
    wire_up(page, captured)
    captured.begin_scenario(scenario)
    page.goto(f"{SELFTEST_ORIGIN}/")
    inject(context, page)
    page.wait_for_timeout(300)  # flush async console/network events
    hygiene, violations = evaluate_browser_events(captured, policy=policy, today=today)
    context.close()
    return hygiene, violations


def run_self_test() -> int:
    """Prove the hygiene gate is fail-closed (R4-P2-01 acceptance):
    each injected fault class ALONE must fail the evaluation, while
    quarantined warnings and allowlisted aborts must pass.

    R5-P2-01 additions: the production quarantine policy must parse
    (malformed ``review_until`` fails closed), and quarantine expiry is
    proven with injected fixtures — an EXPIRED quarantine must fail, the
    ``today == review_until`` boundary must still pass (documented rule),
    a future quarantine must pass.
    """
    print("[browser-e2e] self-test: failure reproduction", flush=True)
    problems: list[str] = []

    # R5-P2-01: startup policy validation — missing/malformed review_until
    # is a permanent allowlist and must fail closed before anything runs.
    try:
        validate_quarantine_policy()
    except BrowserE2EFailure as exc:
        print(f"[browser-e2e] SELF-TEST FAILED: {exc}", flush=True)
        return 1
    for bad_field, bad_value in (("missing", None), ("malformed", "not-a-date")):
        bad_policy = [{**CONSOLE_QUARANTINE[0], "review_until": bad_value}]
        if bad_value is None:
            bad_policy[0].pop("review_until")
        try:
            validate_quarantine_policy(bad_policy)
        except BrowserE2EFailure:
            pass
        else:
            problems.append(f"{bad_field} review_until did NOT fail validation")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # 0. clean page: nothing may fail.
        hygiene, violations = _selftest_case(
            browser, "clean", "selftest_clean", lambda c, p: None
        )
        if violations:
            problems.append(f"clean page unexpectedly failed: {violations}")

        # 1. one injected console.error must fail.
        hygiene, violations = _selftest_case(
            browser,
            "inject_console_error",
            "selftest_console",
            lambda c, p: p.evaluate("console.error('e2e-injected console error')"),
        )
        if not hygiene["unexpected_console"] or not violations:
            problems.append("injected console.error did NOT fail the gate")

        # 2. one 500 response must fail.
        def inject_500(context, page):
            context.route(
                "**/injected-500", lambda route: route.fulfill(status=500, body="boom")
            )
            page.evaluate("fetch('/injected-500').then((r) => r.status).catch(() => -1)")

        hygiene, violations = _selftest_case(browser, "inject_500", "selftest_500", inject_500)
        if not hygiene["failed_responses"] or not violations:
            problems.append("injected 500 response did NOT fail the gate")

        # 3. one non-allowlisted requestfailed must fail.
        def inject_abort(context, page):
            context.route("**/injected-abort", lambda route: route.abort())
            page.evaluate("fetch('/injected-abort').catch(() => 'aborted')")

        hygiene, violations = _selftest_case(
            browser, "inject_abort", "selftest_abort", inject_abort
        )
        if not hygiene["unexpected_failed_requests"] or not violations:
            problems.append("injected non-allowlisted requestfailed did NOT fail the gate")

        # 4. a quarantined warning must NOT fail (and stays observable).
        quarantine_text = CONSOLE_QUARANTINE[2]["prefix"] + " (self-test)"

        def inject_quarantined(context, page):
            page.evaluate(f"console.warn({json.dumps(quarantine_text)})")

        hygiene, violations = _selftest_case(
            browser, "quarantined_warning", "selftest_quarantine", inject_quarantined
        )
        if violations or not hygiene["quarantined_console"]:
            problems.append(f"quarantined warning mishandled: violations={violations}")
        # R4-P2-01: every quarantined record must carry the FULL isolation
        # metadata in the report itself (package/version/owner/review date).
        for record in hygiene["quarantined_console"]:
            if not all(record.get(k) for k in ("package", "version", "owner", "review_until")):
                problems.append(f"quarantined record missing isolation metadata: {record}")

        # 5. an allowlisted stop/reload abort must NOT fail.
        abort_path = "/api/v1/conversations/69f62ebb-1ce6-4ffb-9713-6391762a777f/messages:stream"

        def inject_expected_abort(context, page):
            # abort("aborted") yields net::ERR_ABORTED exactly like a real
            # reload/stop abort (the default abort() yields ERR_FAILED).
            context.route("**/messages:stream", lambda route: route.abort("aborted"))
            page.evaluate(
                f"fetch('{abort_path}', {{method: 'POST'}}).catch(() => 'aborted')"
            )

        hygiene, violations = _selftest_case(
            browser, "expected_abort", "browser_stop_mid_stream", inject_expected_abort
        )
        if violations or not hygiene["expected_aborts"]:
            problems.append(f"allowlisted abort mishandled: violations={violations}")

        # ------------------------------------------------------------------
        # R5-P2-01 quarantine-expiry cases (injected fixtures: the rules
        # are tested against a fixed ``today``, never the wall clock).
        # ------------------------------------------------------------------
        expiry_today = dt.date(2026, 8, 11)

        def _expiry_policy(review_until: str) -> list[dict]:
            policy = [
                {
                    **CONSOLE_QUARANTINE[0],
                    "prefix": "e2e-expiry-fixture",
                    "review_until": review_until,
                }
            ]
            validate_quarantine_policy(policy)
            return policy

        def inject_expiry_warning(context, page):
            page.evaluate("console.warn('e2e-expiry-fixture warning')")

        # 6. an EXPIRED quarantine must FAIL (no permanent allowlist).
        hygiene, violations = _selftest_case(
            browser,
            "expired_quarantine",
            "selftest_expiry",
            inject_expiry_warning,
            policy=_expiry_policy("2000-01-01"),
            today=expiry_today,
        )
        if not hygiene["expired_quarantine"] or not violations:
            problems.append("expired quarantine did NOT fail the gate")
        if hygiene["quarantined_console"]:
            problems.append("expired quarantine must not be counted as quarantined")

        # 7. the documented boundary: today == review_until is still valid.
        hygiene, violations = _selftest_case(
            browser,
            "expiry_boundary_today",
            "selftest_expiry",
            inject_expiry_warning,
            policy=_expiry_policy(expiry_today.isoformat()),
            today=expiry_today,
        )
        if violations or not hygiene["quarantined_console"]:
            problems.append(
                f"boundary (today == review_until) mishandled: violations={violations}"
            )
        elif hygiene["quarantined_console"][0]["days_remaining"] != 0:
            problems.append("boundary record must report days_remaining=0")

        # 8. a future quarantine passes and reports remaining days.
        hygiene, violations = _selftest_case(
            browser,
            "future_quarantine",
            "selftest_expiry",
            inject_expiry_warning,
            policy=_expiry_policy((expiry_today + dt.timedelta(days=30)).isoformat()),
            today=expiry_today,
        )
        if violations or not hygiene["quarantined_console"]:
            problems.append(f"future quarantine mishandled: violations={violations}")
        elif hygiene["quarantined_console"][0]["days_remaining"] != 30:
            problems.append("future record must report days_remaining=30")

        browser.close()

    if problems:
        for problem in problems:
            print(f"[browser-e2e] SELF-TEST FAILED: {problem}", flush=True)
        return 1
    print("[browser-e2e] self-test OK: gate fails closed on every injected fault", flush=True)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="MAP browser E2E scenarios")
    parser.add_argument("--frontend-url")
    parser.add_argument("--fake-llm-url")
    parser.add_argument("--workspace-id")
    parser.add_argument("--answer", default=ANSWER_DEFAULT)
    parser.add_argument("--out")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="R4-P2-01 failure reproduction: verify the hygiene gate is "
        "fail-closed without needing a running stack",
    )
    parser.add_argument(
        "--repeat-stop",
        type=int,
        default=1,
        help="S5-04: number of browser-layer stop scenario rounds (default 1)",
    )
    args = parser.parse_args()

    if args.repeat_stop < 1:
        parser.error(f"--repeat-stop must be >= 1 (got {args.repeat_stop})")

    if args.self_test:
        return run_self_test()

    if not (args.frontend_url and args.fake_llm_url and args.workspace_id and args.out):
        parser.error("--frontend-url, --fake-llm-url, --workspace-id and --out are required")

    # R5-P2-01: fail closed BEFORE launching the browser if any quarantine
    # entry has a missing/malformed review_until (permanent-allowlist risk).
    try:
        validate_quarantine_policy()
    except BrowserE2EFailure as exc:
        print(f"[browser-e2e] FAILED: {exc}", flush=True)
        return 1

    captured = Captured()
    scenarios: dict[str, str] = {}
    stop_repeat: list[dict] = []
    failure: str | None = None
    hygiene: dict = {}
    diagnostics: list[str] = []
    page = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(base_url=args.frontend_url)
            context.set_default_timeout(NAV_TIMEOUT_MS)
            page = context.new_page()
            wire_up(page, captured)

            captured.begin_scenario("browser_happy_path")
            scenario_happy_path(page, captured, args.answer)
            captured.end_scenario()
            scenarios["browser_happy_path"] = "PASS"
            print("[browser-e2e] happy path OK", flush=True)

            captured.begin_scenario("browser_reload_feedback")
            scenario_reload_feedback(page, captured, args.answer)
            captured.end_scenario()
            scenarios["browser_reload_feedback"] = "PASS"
            print("[browser-e2e] reload + feedback + withdraw OK", flush=True)

            captured.begin_scenario("browser_stop_mid_stream")
            stop_repeat = scenario_stop_mid_stream(
                page, captured, args.fake_llm_url, repeat=args.repeat_stop
            )
            captured.end_scenario()
            scenarios["browser_stop_mid_stream"] = "PASS"
            print("[browser-e2e] stop mid-stream + reload OK", flush=True)

            # R4-P2-01: fail closed on any unexpected page/console/network
            # event — the scenarios passing is necessary but NOT sufficient.
            hygiene, violations = evaluate_browser_events(captured)
            if violations:
                failure = "event hygiene violations: " + "; ".join(violations)

            context.close()
            browser.close()
    except BrowserE2EFailure as exc:
        failure = str(exc)
    except Exception as exc:  # noqa: BLE001 - report anything
        failure = f"unexpected browser error: {exc!r}"
    finally:
        # Failure diagnostics: screenshot + rendered body text (kept OUT of
        # page_errors so the hygiene fields stay acceptance-grade).
        if failure is not None and page is not None:
            try:
                shot = str(Path(args.out).with_suffix(".png"))
                page.screenshot(path=shot, full_page=True)
                diagnostics.append(f"screenshot: {shot}")
                diagnostics.append(f"body_text: {page.inner_text('body')[:2000]}")
            except Exception as diag_exc:  # noqa: BLE001 - best effort
                diagnostics.append(f"diagnostics failed: {diag_exc!r}")

    if not hygiene:
        hygiene, _ = evaluate_browser_events(captured)

    report = {
        "result": "PASS" if failure is None else "FAIL",
        "scenarios": scenarios,
        "workspace_id": args.workspace_id,
        "stop_repeat": stop_repeat,
        **captured.as_dict(),
        **hygiene,
    }
    if diagnostics:
        report["diagnostics"] = diagnostics
    if failure:
        report["failure"] = failure
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    if failure:
        print(f"[browser-e2e] FAILED: {failure}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
