import os
from pathlib import Path

import pytest
from hypothesis import settings

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
