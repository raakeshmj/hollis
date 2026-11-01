"""Rule-based tool policy and approvers (ADR-0015).

Rules are evaluated in order; the first matching rule decides. Matching uses only facts the
runtime controls: the registered tool's identity, declared effect and trust, the execution
environment, and optionally a deterministic predicate over the validated arguments (for resource
scoping). Model output and tool descriptions never influence the decision.
"""

import fnmatch
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, cast

from hollis.runtime.tools import (
    ApprovalDecision,
    ApprovalRequest,
    Decision,
    DefaultToolPolicy,
    PolicyDecision,
    PolicyRequest,
    ToolEffect,
    ToolPolicy,
    ToolTrust,
)

type Predicate = Callable[[PolicyRequest], bool]
type Verdict = Literal[Decision.ALLOW, Decision.REQUIRE_APPROVAL, Decision.DENY]


@dataclass(frozen=True)
class Rule:
    decision: Decision
    tools: str = "*"
    """Glob over tool names, e.g. "fs_*"."""
    effects: frozenset[ToolEffect] | None = None
    trust: frozenset[ToolTrust] | None = None
    environments: frozenset[str] | None = None
    when: Predicate | None = None
    """Extra deterministic condition, e.g. on a path argument."""
    delegated: bool | None = None
    """True: only runs another run delegated; False: only runs nobody delegated."""
    name: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.decision is Decision.INVALID:
            raise ValueError("a policy rule cannot decide INVALID")

    def matches(self, request: PolicyRequest) -> bool:
        tool = request.tool
        return (
            fnmatch.fnmatchcase(tool.name, self.tools)
            and (self.effects is None or tool.effect in self.effects)
            and (self.trust is None or tool.trust in self.trust)
            and (self.environments is None or request.environment in self.environments)
            and (self.delegated is None or (request.depth > 0) == self.delegated)
            and (self.when is None or self.when(request))
        )


@dataclass(frozen=True)
class RulePolicy:
    rules: tuple[Rule, ...] = ()
    fallback: ToolPolicy = field(default_factory=DefaultToolPolicy)

    @classmethod
    def of(cls, *rules: Rule, fallback: ToolPolicy | None = None) -> "RulePolicy":
        return cls(rules=rules, fallback=fallback or DefaultToolPolicy())

    def decide(self, request: PolicyRequest) -> PolicyDecision:
        for index, rule in enumerate(self.rules):
            if rule.matches(request):
                verdict = cast(Verdict, rule.decision)  # INVALID is rejected in Rule
                return PolicyDecision(
                    decision=verdict,
                    reason=rule.reason or f"rule {rule.name or index} matched",
                    rule=rule.name or f"rule-{index}",
                )
        return self.fallback.decide(request)


def _rule(
    decision: Decision,
    tools: str,
    effects: Iterable[ToolEffect] | None,
    trust: Iterable[ToolTrust] | None,
    environments: Iterable[str] | None,
    when: Predicate | None,
    name: str,
    reason: str,
    delegated: bool | None = None,
) -> Rule:
    return Rule(
        decision=decision,
        tools=tools,
        effects=frozenset(effects) if effects is not None else None,
        trust=frozenset(trust) if trust is not None else None,
        environments=frozenset(environments) if environments is not None else None,
        when=when,
        delegated=delegated,
        name=name,
        reason=reason,
    )


def allow(
    tools: str = "*",
    *,
    effects: Iterable[ToolEffect] | None = None,
    trust: Iterable[ToolTrust] | None = None,
    environments: Iterable[str] | None = None,
    when: Predicate | None = None,
    delegated: bool | None = None,
    name: str = "",
    reason: str = "",
) -> Rule:
    return _rule(Decision.ALLOW, tools, effects, trust, environments, when, name, reason, delegated)


def deny(
    tools: str = "*",
    *,
    effects: Iterable[ToolEffect] | None = None,
    trust: Iterable[ToolTrust] | None = None,
    environments: Iterable[str] | None = None,
    when: Predicate | None = None,
    delegated: bool | None = None,
    name: str = "",
    reason: str = "",
) -> Rule:
    return _rule(Decision.DENY, tools, effects, trust, environments, when, name, reason, delegated)


def require_approval(
    tools: str = "*",
    *,
    effects: Iterable[ToolEffect] | None = None,
    trust: Iterable[ToolTrust] | None = None,
    environments: Iterable[str] | None = None,
    when: Predicate | None = None,
    delegated: bool | None = None,
    name: str = "",
    reason: str = "",
) -> Rule:
    return _rule(
        Decision.REQUIRE_APPROVAL,
        tools,
        effects,
        trust,
        environments,
        when,
        name,
        reason,
        delegated,
    )


class CallbackApprover:
    """Adapts a function (sync or async) to the Approver protocol. Return None to defer."""

    def __init__(
        self,
        fn: Callable[
            [ApprovalRequest], Awaitable[ApprovalDecision | None] | ApprovalDecision | None
        ],
    ) -> None:
        self._fn = fn

    async def review(self, request: ApprovalRequest) -> ApprovalDecision | None:
        result = self._fn(request)
        if inspect.isawaitable(result):
            return await result
        return result


def describe(request: ApprovalRequest) -> str:
    """A human-readable summary for approval prompts: what will run, with which arguments."""
    lines = [
        f"Tool:      {request.tool.name} (version {request.tool.version})",
        f"Effect:    {request.tool.effect.value}; trust: {request.tool.trust.value}; "
        f"idempotent: {request.tool.idempotent}",
        f"Why:       {request.reason}",
        f"Expires:   {request.expires_at.isoformat()}",
        "Arguments:",
        *(f"  {k} = {v!r}" for k, v in sorted(request.arguments.items())),
    ]
    return "\n".join(lines)
