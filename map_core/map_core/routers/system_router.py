"""Health check and basic information routes"""

import asyncio
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter

system_router = APIRouter()
_STARTED_AT = datetime.now(timezone.utc)
_START_MONOTONIC = time.monotonic()
_ENV = os.environ.get("ENV", "dev")

try:
    _GIT_SHA = (
        subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        )
        .decode()
        .strip()
    )
except Exception:
    _GIT_SHA = "unknown"


def _get_process_rss_bytes() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # Format: VmRSS:\t  12345 kB
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except Exception:
        return None
    return None


@system_router.get("/")
async def root():
    return {
        "message": "Welcome to the MAP 2.0",
    }


@system_router.get("/status")
async def status_check():
    uptime_seconds = time.monotonic() - _START_MONOTONIC
    coroutine_count = len(asyncio.all_tasks())
    thread_count = threading.active_count()
    process_pid = os.getpid()
    process_memory_rss_bytes = _get_process_rss_bytes()

    return {
        "name": "MAP 2.0 Service",
        "env": _ENV,
        "version": _GIT_SHA,
        "started_at": _STARTED_AT.isoformat(),
        "uptime_seconds": round(uptime_seconds, 3),
        "thread_count": thread_count,
        "coroutine_count": coroutine_count,
        "process_pid": process_pid,
        "process_memory_rss_mb": (
            round(process_memory_rss_bytes / (1024 * 1024), 3)
            if process_memory_rss_bytes is not None
            else None
        ),
    }


@system_router.get("/health")
async def health_check():
    return {
        "status": "ok",
    }
