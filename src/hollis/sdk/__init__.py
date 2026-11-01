"""The Hollis Python SDK: one typed API for an embedded workspace or a Hollis server.

from hollis import Hollis, AsyncHollis

with Hollis.open() as hollis:                       # embedded: the workspace here
    ...
with Hollis.connect("http://127.0.0.1:7878", token) as hollis:   # a server
    ...
"""

from hollis.platform.errors import (
    Conflict,
    Forbidden,
    InvalidRequest,
    NotFound,
    PlatformError,
    Unauthenticated,
    Unavailable,
)
from hollis.platform.schemas import Plan, PlanStep
from hollis.sdk.client import AsyncHollis
from hollis.sdk.client import RunHandle as AsyncRunHandle
from hollis.sdk.sync import Hollis, RunHandle

__all__ = [
    "AsyncHollis",
    "AsyncRunHandle",
    "Conflict",
    "Forbidden",
    "Hollis",
    "InvalidRequest",
    "NotFound",
    "Plan",
    "PlanStep",
    "PlatformError",
    "RunHandle",
    "Unauthenticated",
    "Unavailable",
]
