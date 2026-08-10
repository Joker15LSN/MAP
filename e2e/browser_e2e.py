"""Real-browser E2E scenarios (R3-P1-02).

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

Exit codes: 0 = all scenarios green, 1 = assertion failure,
77 = Playwright/Chromium not installed (runner decides skip-or-fail).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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


class BrowserE2EFailure(AssertionError):
    """A browser scenario assertion failed."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise BrowserE2EFailure(message)


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


class Captured:
    """Wire-level evidence collected from the real browser."""

    def __init__(self) -> None:
        self.create_requests: list[dict] = []
        self.stream_requests: list[dict] = []
        self.create_responses: list[dict] = []
        self.session_ids: set[str] = set()
        self.conversation_id: str | None = None
        self.page_errors: list[str] = []
        self.console_errors: list[str] = []
        self.failed_requests: list[str] = []
        self.failed_responses: list[str] = []

    def as_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "session_ids": sorted(self.session_ids),
            "create_requests": self.create_requests,
            "stream_requests": self.stream_requests,
            "page_errors": self.page_errors,
            "console_errors": self.console_errors,
            "failed_requests": self.failed_requests,
            "failed_responses": self.failed_responses,
        }


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

    def on_response(response) -> None:
        request = response.request
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
    page.on("pageerror", lambda error: captured.page_errors.append(str(error)))
    page.on(
        "requestfailed",
        lambda request: captured.failed_requests.append(
            f"{request.method} {request.url} ({request.failure})"
        ),
    )

    def on_console(msg) -> None:
        if msg.type in ("error", "warning"):
            captured.console_errors.append(f"[{msg.type}] {msg.text[:500]}")

    page.on("console", on_console)

    def on_bad_response(response) -> None:
        if response.status >= 400:
            captured.failed_responses.append(
                f"{response.status} {response.request.method} {response.url}"
            )

    page.on("response", on_bad_response)


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


def scenario_stop_mid_stream(page, captured: Captured, fake_llm_url: str) -> None:
    """Stop a slow stream from the UI; the terminal state survives reload."""
    configure_fake_llm(fake_llm_url, 0.35)
    try:
        page.get_by_label("输入问题").fill("慢慢回答我")
        page.get_by_role("button", name=SEND_BUTTON).click()
        stop_button = page.get_by_test_id("stop-button")
        stop_button.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
        stop_button.click()
        page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('[data-testid="message-assistant"]');
                const last = rows[rows.length - 1];
                return Boolean(last && last.textContent.includes('stopped'));
            }""",
            timeout=NAV_TIMEOUT_MS,
        )

        page.reload()
        page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('[data-testid="message-assistant"]');
                return Array.from(rows).some((row) => row.textContent.includes('stopped'));
            }""",
            timeout=NAV_TIMEOUT_MS,
        )
    finally:
        configure_fake_llm(fake_llm_url, 0.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="MAP browser E2E scenarios")
    parser.add_argument("--frontend-url", required=True)
    parser.add_argument("--fake-llm-url", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--answer", default=ANSWER_DEFAULT)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    captured = Captured()
    scenarios: dict[str, str] = {}
    failure: str | None = None
    page = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(base_url=args.frontend_url)
            context.set_default_timeout(NAV_TIMEOUT_MS)
            page = context.new_page()
            wire_up(page, captured)

            scenario_happy_path(page, captured, args.answer)
            scenarios["browser_happy_path"] = "PASS"
            print("[browser-e2e] happy path OK", flush=True)

            scenario_reload_feedback(page, captured, args.answer)
            scenarios["browser_reload_feedback"] = "PASS"
            print("[browser-e2e] reload + feedback + withdraw OK", flush=True)

            scenario_stop_mid_stream(page, captured, args.fake_llm_url)
            scenarios["browser_stop_mid_stream"] = "PASS"
            print("[browser-e2e] stop mid-stream + reload OK", flush=True)

            context.close()
            browser.close()
    except BrowserE2EFailure as exc:
        failure = str(exc)
    except Exception as exc:  # noqa: BLE001 - report anything
        failure = f"unexpected browser error: {exc!r}"
    finally:
        # Failure diagnostics: screenshot + rendered body text.
        if failure is not None and page is not None:
            try:
                shot = str(Path(args.out).with_suffix(".png"))
                page.screenshot(path=shot, full_page=True)
                captured.page_errors.append(f"screenshot: {shot}")
                captured.page_errors.append(
                    f"body_text: {page.inner_text('body')[:2000]}"
                )
            except Exception as diag_exc:  # noqa: BLE001 - best effort
                captured.page_errors.append(f"diagnostics failed: {diag_exc!r}")

    report = {
        "result": "PASS" if failure is None else "FAIL",
        "scenarios": scenarios,
        "workspace_id": args.workspace_id,
        **captured.as_dict(),
    }
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
