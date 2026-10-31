"""Redaction of secrets from text the runtime persists or logs (ADR-0009).

Two mechanisms with different guarantees:
- Known values: exact secret strings the composition root registers (for example the configured
  API key). Reliable for those exact values, regardless of where they appear.
- Patterns: a few common credential shapes. Best effort only; they cannot recognise arbitrary
  secrets, and they are applied only to diagnostics (failure messages, logs), never to model
  content, to avoid mangling legitimate text.

The runtime never redacts run input or instructions: recovery must resend the identical request
under the same operation id. Inputs containing a known secret are rejected instead.
"""

import re
from collections.abc import Iterable

from pydantic import SecretStr

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 8
"""Shorter values would redact ordinary words; register them and you get a ValueError."""

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"),
    re.compile(r"(?i)\b(x-api-key|api[_-]?key|authorization)(\s*[:=]\s*)[^\s,;\"']+"),
)


class Redactor:
    def __init__(self, secrets: Iterable[str | SecretStr] = ()) -> None:
        values: set[str] = set()
        for secret in secrets:
            value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
            if len(value) < MIN_SECRET_LENGTH:
                raise ValueError(f"secrets must be at least {MIN_SECRET_LENGTH} characters")
            values.add(value)
        # Longest first, so a secret containing another is replaced whole.
        self._secrets = tuple(sorted(values, key=len, reverse=True))

    def contains_secret(self, text: str) -> bool:
        """Whether `text` contains a registered secret value (patterns are not consulted)."""
        return any(secret in text for secret in self._secrets)

    def redact_known(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def redact(self, text: str) -> str:
        """Redact registered secrets and common credential patterns. For diagnostics only."""
        text = self.redact_known(text)
        for pattern in _PATTERNS:
            text = pattern.sub(_replacement, text)
        return text


def _replacement(match: re.Match[str]) -> str:
    # Keep a matched header or key name so the diagnostic stays readable.
    if match.re.groups >= 2:
        return f"{match.group(1)}{match.group(2)}{REDACTED}"
    return REDACTED
