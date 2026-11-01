"""Principals, tokens and sessions (ADR-0028)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hollis.platform.auth import LOGIN_TOKEN_MINUTES, Auth
from hollis.platform.db import WorkspaceDb
from hollis.platform.errors import Conflict, NotFound, Unauthenticated
from hollis.platform.schemas import PrincipalInput, Role


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def auth(tmp_path: Path) -> Auth:
    return Auth(await WorkspaceDb.open(tmp_path / "workspace.db"), clock=Clock())


def _clock(auth: Auth) -> Clock:
    clock = auth._clock  # pyright: ignore[reportPrivateUsage]  # the injected test clock
    assert isinstance(clock, Clock)
    return clock


async def test_the_local_admin_exists_once(auth: Auth) -> None:
    first = await auth.ensure_admin()
    second = await auth.ensure_admin()
    assert first == second
    assert first.is_admin
    assert first.can_approve
    with pytest.raises(Conflict):
        await auth.create_principal(PrincipalInput(name="admin"))


async def test_tokens_are_stored_only_as_hashes(auth: Auth, tmp_path: Path) -> None:
    await auth.ensure_admin()
    token = await auth.create_token("admin", label="ci")

    assert token.token.startswith("hx_")
    assert (await auth.authenticate(token.token)).name == "admin"
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("workspace.db*"))
    assert token.token.encode() not in raw
    [listed] = await auth.tokens()
    assert listed.last_used_at is not None
    assert not hasattr(listed, "token")


async def test_revoked_expired_and_unknown_tokens_are_refused(auth: Auth) -> None:
    await auth.ensure_admin()
    revoked = await auth.create_token("admin")
    expiring = await auth.create_token("admin", expires_in_days=1)
    await auth.revoke(revoked.token_id)

    with pytest.raises(Unauthenticated):
        await auth.authenticate(revoked.token)
    _clock(auth).now += timedelta(days=2)
    with pytest.raises(Unauthenticated):
        await auth.authenticate(expiring.token)
    with pytest.raises(Unauthenticated):
        await auth.authenticate("hx_not-a-token")
    with pytest.raises(NotFound):
        await auth.create_token("nobody")


async def test_login_tokens_are_single_use_short_lived_and_only_open_sessions(auth: Auth) -> None:
    await auth.ensure_admin()
    login = await auth.create_token("admin", kind="login")
    assert login.token.startswith("hxl_")
    with pytest.raises(Unauthenticated):
        await auth.authenticate(login.token)  # not an API token

    session, _, principal = await auth.open_session(login.token, hours=1)
    assert principal.name == "admin"
    with pytest.raises(Unauthenticated):
        await auth.open_session(login.token, hours=1)  # used

    stale = await auth.create_token("admin", kind="login")
    _clock(auth).now += timedelta(minutes=LOGIN_TOKEN_MINUTES + 1)
    with pytest.raises(Unauthenticated):
        await auth.open_session(stale.token, hours=1)
    assert (await auth.session(session))[0].name == "admin"


async def test_sessions_expire_and_close(auth: Auth) -> None:
    await auth.ensure_admin()
    api = await auth.create_token("admin")
    session, expires, _ = await auth.open_session(api.token, hours=2)
    assert expires == _clock(auth).now + timedelta(hours=2)

    assert (await auth.session(session))[0].role is Role.ADMIN
    _clock(auth).now += timedelta(hours=3)
    with pytest.raises(Unauthenticated):
        await auth.session(session)

    _clock(auth).now -= timedelta(hours=3)
    await auth.close_session(session)
    with pytest.raises(Unauthenticated):
        await auth.session(session)


async def test_principal_grants_round_trip(auth: Auth) -> None:
    from hollis.runtime.knowledge import KnowledgeAccess
    from hollis.runtime.memory import MemoryAccess

    memory = MemoryAccess(read=("team:a",), write=("team:a",))
    knowledge = KnowledgeAccess(collections=("docs",), labels=("team:docs",))
    created = await auth.create_principal(
        PrincipalInput(name="olive", role=Role.VIEWER, memory=memory, knowledge=knowledge)
    )
    assert (created.memory, created.knowledge, created.can_approve) == (memory, knowledge, False)
    assert [p.name for p in await auth.principals()] == ["olive"]
