"""Credentials never reach stored data: tokens and sessions are kept only as hashes, and nothing
the API returns or records contains them."""

from pathlib import Path

import httpx2

from hollis.platform.auth import Principal
from hollis.platform.schemas import TokenInput
from hollis.platform.services import Platform
from hollis.server.app import allowed_hosts
from tests.server.conftest import BASE


async def test_tokens_and_sessions_never_appear_in_stored_data(
    anonymous: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await platform.create_starter(admin)
    api_token = (await platform.create_token(admin, TokenInput(principal="admin"))).token
    login = await platform.login_token()
    signed_in = await anonymous.post("/v1/session", json={"token": login})
    session = signed_in.cookies.get("hollis_session")
    assert session
    headers = {"authorization": f"Bearer {api_token}"}
    run = await anonymous.post("/v1/runs", json={"input": "list files"}, headers=headers)
    await anonymous.get(
        f"/v1/runs/{run.json()['run_id']}/summary", params={"wait_s": 10}, headers=headers
    )
    responses = [
        (await anonymous.get(path, headers=headers)).text
        for path in ("/v1/tokens", "/v1/audit", "/v1/settings", f"/v1/runs/{run.json()['run_id']}")
    ]

    stored = b"".join(path.read_bytes() for path in Path(platform.ws.root, ".hollis").glob("*.db*"))
    for secret in (api_token, login, session):
        assert secret.encode() not in stored
        assert not any(secret in body for body in responses)


def test_the_server_accepts_only_expected_host_names() -> None:
    assert allowed_hosts("127.0.0.1", ()) == frozenset({"127.0.0.1", "localhost", "::1"})
    assert "hollis.internal" in (allowed_hosts("0.0.0.0", ("hollis.internal",)) or set())  # noqa: S104  # a bind address under test
    assert "0.0.0.0" not in (allowed_hosts("0.0.0.0", ()) or set())  # noqa: S104  # a bind address under test
    assert allowed_hosts("127.0.0.1", ("*",)) is None
    assert BASE.startswith("http://127.0.0.1")
