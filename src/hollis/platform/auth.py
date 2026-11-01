"""Principals, API tokens and browser sessions (ADR-0028).

- A *principal* is a named identity with a role (admin, operator, viewer), an approve permission,
  and memory and knowledge grants. Admins have all grants.
- *API tokens* (`hx_…`, 256 bits of randomness) authenticate SDK, CLI and HTTP clients. Only a
  SHA-256 hash is stored; the token is shown once. Tokens can expire and be revoked.
- *Login tokens* are single-use and expire after ten minutes. `hollis serve` mints one for the
  local admin and prints a link; the web app exchanges it for a *session* (an HttpOnly cookie
  whose value is also stored only as a hash).

There is no password database and no external identity provider in M6: principals and tokens
are managed by an admin through the CLI, API or web app.
"""

import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hollis.platform.db import WorkspaceDb, parse_ts, ts
from hollis.platform.errors import Conflict, InvalidRequest, NotFound, Unauthenticated
from hollis.platform.schemas import (
    CreatedToken,
    PrincipalInput,
    PrincipalView,
    Role,
    TokenView,
)
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.memory import MemoryAccess

LOGIN_TOKEN_MINUTES = 10


def _utc_now() -> datetime:
    return datetime.now(UTC)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role
    can_approve: bool
    memory: MemoryAccess | None
    knowledge: KnowledgeAccess | None
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    def view(self) -> PrincipalView:
        return PrincipalView(
            name=self.name,
            role=self.role,
            can_approve=self.can_approve,
            memory=self.memory,
            knowledge=self.knowledge,
            created_at=self.created_at,
        )


class Auth:
    def __init__(self, db: WorkspaceDb, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._db = db
        self._clock = clock

    # --- principals -----------------------------------------------------------------------------

    async def ensure_admin(self, name: str = "admin") -> Principal:
        existing = await self.principal(name)
        if existing is not None:
            return existing
        return await self.create_principal(
            PrincipalInput(name=name, role=Role.ADMIN, can_approve=True)
        )

    async def principal(self, name: str) -> Principal | None:
        rows = await self._db.query(
            "SELECT name, role, data, created_at FROM principals WHERE name = ?", (name,)
        )
        return _principal(rows[0]) if rows else None

    async def principals(self) -> list[Principal]:
        rows = await self._db.query(
            "SELECT name, role, data, created_at FROM principals ORDER BY name"
        )
        return [_principal(r) for r in rows]

    async def create_principal(self, spec: PrincipalInput) -> Principal:
        data = json.dumps(
            {
                "can_approve": spec.can_approve or spec.role is Role.ADMIN,
                "memory": spec.memory.model_dump(mode="json") if spec.memory else None,
                "knowledge": spec.knowledge.model_dump(mode="json") if spec.knowledge else None,
            }
        )
        if await self.principal(spec.name) is not None:
            raise Conflict(f"principal {spec.name!r} already exists")
        await self._db.execute(
            "INSERT INTO principals (name, role, data, created_at) VALUES (?, ?, ?, ?)",
            (spec.name, spec.role.value, data, ts(self._clock())),
        )
        principal = await self.principal(spec.name)
        assert principal is not None  # noqa: S101  # just inserted
        return principal

    # --- tokens ---------------------------------------------------------------------------------

    async def create_token(
        self,
        principal: str,
        *,
        label: str = "",
        expires_in_days: float | None = None,
        kind: str = "api",
    ) -> CreatedToken:
        if await self.principal(principal) is None:
            raise NotFound(f"no principal {principal!r}")
        if kind not in ("api", "login"):
            raise InvalidRequest("token kind must be api or login")
        secret = ("hx_" if kind == "api" else "hxl_") + secrets.token_urlsafe(32)
        token_id = "tok_" + secrets.token_hex(6)
        now = self._clock()
        expires = (
            now + timedelta(minutes=LOGIN_TOKEN_MINUTES)
            if kind == "login"
            else (now + timedelta(days=expires_in_days) if expires_in_days else None)
        )
        await self._db.execute(
            "INSERT INTO tokens (token_id, principal, hash, kind, label, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_id, principal, hash_secret(secret), kind, label, ts(now), ts(expires)),
        )
        return CreatedToken(
            token_id=token_id,
            principal=principal,
            label=label,
            created_at=now,
            expires_at=expires,
            token=secret,
        )

    async def tokens(self, principal: str | None = None) -> list[TokenView]:
        rows = await self._db.query(
            "SELECT token_id, principal, label, created_at, last_used_at, expires_at FROM tokens "
            "WHERE kind = 'api' AND revoked_at IS NULL AND (? IS NULL OR principal = ?) "
            "ORDER BY created_at",
            (principal, principal),
        )
        return [
            TokenView(
                token_id=tid,
                principal=p,
                label=label,
                created_at=parse_ts(created) or self._clock(),
                last_used_at=parse_ts(used),
                expires_at=parse_ts(expires),
            )
            for tid, p, label, created, used, expires in rows
        ]

    async def revoke(self, token_id: str) -> bool:
        changed = await self._db.execute(
            "UPDATE tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
            (ts(self._clock()), token_id),
        )
        return changed > 0

    async def authenticate(self, secret: str, *, kinds: tuple[str, ...] = ("api",)) -> Principal:
        rows = await self._db.query(
            "SELECT token_id, principal, kind, expires_at, revoked_at FROM tokens WHERE hash = ?",
            (hash_secret(secret),),
        )
        if not rows:
            raise Unauthenticated("invalid token")
        token_id, name, kind, expires, revoked = rows[0]
        expiry = parse_ts(expires)
        if (
            revoked is not None
            or kind not in kinds
            or (expiry is not None and expiry <= self._clock())
        ):
            raise Unauthenticated("token expired, revoked or not valid here")
        if kind == "login":  # single use
            await self._db.execute(
                "UPDATE tokens SET revoked_at = ? WHERE token_id = ?", (ts(self._clock()), token_id)
            )
        else:
            await self._db.execute(
                "UPDATE tokens SET last_used_at = ? WHERE token_id = ?",
                (ts(self._clock()), token_id),
            )
        principal = await self.principal(name)
        if principal is None:
            raise Unauthenticated("token principal no longer exists")
        return principal

    # --- sessions -------------------------------------------------------------------------------

    async def open_session(self, secret: str, *, hours: float) -> tuple[str, datetime, Principal]:
        """Exchange an API or login token for a browser session."""
        principal = await self.authenticate(secret, kinds=("api", "login"))
        session = secrets.token_urlsafe(32)
        expires = self._clock() + timedelta(hours=hours)
        await self._db.execute(
            "INSERT INTO sessions (hash, principal, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (hash_secret(session), principal.name, ts(self._clock()), ts(expires)),
        )
        return session, expires, principal

    async def session(self, session: str) -> tuple[Principal, datetime]:
        rows = await self._db.query(
            "SELECT principal, expires_at FROM sessions WHERE hash = ?", (hash_secret(session),)
        )
        if not rows:
            raise Unauthenticated("no session")
        name, expires = rows[0]
        expiry = parse_ts(expires)
        if expiry is None or expiry <= self._clock():
            raise Unauthenticated("session expired")
        principal = await self.principal(name)
        if principal is None:
            raise Unauthenticated("session principal no longer exists")
        return principal, expiry

    async def close_session(self, session: str) -> None:
        await self._db.execute("DELETE FROM sessions WHERE hash = ?", (hash_secret(session),))


def _principal(row: tuple[str, str, str, str]) -> Principal:
    name, role, data, created = row
    details = json.loads(data)
    return Principal(
        name=name,
        role=Role(role),
        can_approve=bool(details.get("can_approve")),
        memory=MemoryAccess.model_validate(details["memory"]) if details.get("memory") else None,
        knowledge=KnowledgeAccess.model_validate(details["knowledge"])
        if details.get("knowledge")
        else None,
        created_at=parse_ts(created) or datetime.now(UTC),
    )
