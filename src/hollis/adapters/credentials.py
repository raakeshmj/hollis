"""The credential boundary for adapters (ADR-0009).

Credentials are read only here, only when asked for, and returned as `SecretStr` so they do not
appear in reprs, logs or tracebacks by accident. They are passed straight to provider clients and
never placed in run specs, model requests or events. A secrets manager can replace this module
later without touching the runtime.
"""

import os
from collections.abc import Mapping

from pydantic import SecretStr


class MissingCredential(LookupError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"credential {name} is not set; see .env.example")


def require_env_secret(name: str, environ: Mapping[str, str] | None = None) -> SecretStr:
    value = (os.environ if environ is None else environ).get(name, "")
    if not value.strip():
        raise MissingCredential(name)
    return SecretStr(value)
