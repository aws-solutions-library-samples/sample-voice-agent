"""Shared test configuration and fixtures for voice-agent tests.

Registers custom pytest markers and provides skip logic for tests
that require container-only dependencies (pipecat, aiohttp, structlog, aioboto3).
"""

import sys
import types

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "container: marks tests that require container-only dependencies (pipecat, aiohttp, etc.)",
    )


# ─── Phase 7A stub: let agent_config tests collect without full pipecat ────
#
# Problem: `app.services.__init__.py` eagerly imports
# `DeepgramSageMakerTTSService`, which drags in pipecat-ai's sagemaker
# extras, which require aioboto3 and Python 3.12+. Heavy to install
# locally just to run one unit-test file.
#
# Solution: if the chain fails to import at collection time, replace
# the offending module with a stub so `from app.services.agent_config
# import ...` in tests works. Production Docker skips tests/ entirely,
# so this has zero prod impact.
#
# This is NOT a test mock — it's purely to prevent collection-time
# ImportError. Tests that exercise real pipecat code should still run
# in the container image where the full dep graph is present.

def _ensure_stub_module(module_name: str):
    if module_name in sys.modules:
        return
    sys.modules[module_name] = types.ModuleType(module_name)


try:
    import pipecat  # noqa: F401
except ImportError:
    _ensure_stub_module("pipecat")
    _ensure_stub_module("pipecat.services")
    _ensure_stub_module("pipecat.services.aws")

try:
    import aioboto3  # noqa: F401
except ImportError:
    _ensure_stub_module("aioboto3")

try:
    from app.services.deepgram_sagemaker_tts import DeepgramSageMakerTTSService  # noqa: F401
except Exception:
    stub_mod = types.ModuleType("app.services.deepgram_sagemaker_tts")

    class _StubDeepgramSageMakerTTSService:
        pass

    stub_mod.DeepgramSageMakerTTSService = _StubDeepgramSageMakerTTSService
    sys.modules["app.services.deepgram_sagemaker_tts"] = stub_mod
