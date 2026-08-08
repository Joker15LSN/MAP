from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from otel_env import OTEL_ENV_VARS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Hermetic suite guard. app.main is imported at collection time and its
# module-level configure_bff_telemetry() reads OTel env vars, so a developer
# shell or CI runner that exports MAP_OTEL_ENABLED=true / OTEL_SDK_DISABLED=true
# / MAP_OTEL_EXCLUDED_PATHS=/api/chat would silently change what the suite
# exercises. Drop every var BEFORE collection; the autouse fixture below then
# keeps each test deterministic and restores the host env afterwards.
for _var in OTEL_ENV_VARS:
    os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def _hermetic_otel_env(monkeypatch):
    """Remove host OTel vars for every test; monkeypatch restores them after.

    Note: we delete (never set) OTEL_SDK_DISABLED — setting it to "true"
    would also silence the explicit instrumentation that test_bff_spans
    installs (SDK kill-switch semantics) and break those tests.
    """
    for var in OTEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
