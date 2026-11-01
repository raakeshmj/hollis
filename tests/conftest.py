import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from hypothesis import settings

from hollis.platform.auth import Principal
from hollis.platform.config import initialize
from hollis.platform.services import Platform
from hollis.runtime.store import RunStore
from tests.support.stores import STORE_KINDS, make_store

# CI runs derandomized so failures reproduce; locally Hypothesis explores new examples.
settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
settings.register_profile("dev", deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(params=STORE_KINDS)
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> RunStore:
    """Every RunStore adapter; tests using this fixture run once per adapter."""
    return await make_store(request.param, tmp_path)


@pytest.fixture
async def platform(tmp_path: Path) -> AsyncIterator[Platform]:
    """A fresh workspace (hollis init defaults: demo model, writable file tools, approvals)."""
    initialize(tmp_path, "test")
    opened = await Platform.open(tmp_path)
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
async def admin(platform: Platform) -> Principal:
    return await platform.local_admin()
