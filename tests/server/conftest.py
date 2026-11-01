from collections.abc import AsyncIterator

import httpx2
import pytest

from hollis.platform.auth import Principal
from hollis.platform.schemas import TokenInput
from hollis.platform.services import Platform
from hollis.server import create_app

BASE = "http://127.0.0.1:7878"


@pytest.fixture
async def anonymous(platform: Platform) -> AsyncIterator[httpx2.AsyncClient]:
    """An HTTP client for the app, in process (no socket), without credentials."""
    transport = httpx2.ASGITransport(app=create_app(platform=platform))
    async with httpx2.AsyncClient(transport=transport, base_url=BASE) as client:
        yield client


@pytest.fixture
async def client(
    anonymous: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> httpx2.AsyncClient:
    """The same client, authenticated as the local admin with an API token."""
    token = await platform.create_token(admin, TokenInput(principal="admin", label="tests"))
    anonymous.headers["authorization"] = f"Bearer {token.token}"
    return anonymous
