from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from hollis.platform.auth import Principal
from hollis.platform.config import initialize
from hollis.platform.services import Platform


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
