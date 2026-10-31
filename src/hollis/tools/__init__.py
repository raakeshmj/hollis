"""Tool definitions, registry, policy and integrations (ADR-0015).

from hollis.tools import ToolRegistry, tool, ToolEffect

registry = ToolRegistry()

@registry.add
@tool
async def get_time(timezone: str) -> str:
    \"\"\"Current time in an IANA timezone.\"\"\"
"""

from hollis.runtime.tools import (
    ApprovalDecision,
    ApprovalRequest,
    Decision,
    DefaultToolPolicy,
    Isolation,
    JsonPart,
    ReferencePart,
    TextPart,
    ToolArgumentError,
    ToolContext,
    ToolEffect,
    ToolError,
    ToolOutput,
    ToolTrust,
)
from hollis.tools.function import FunctionTool, tool
from hollis.tools.policy import (
    CallbackApprover,
    Rule,
    RulePolicy,
    allow,
    deny,
    describe,
    require_approval,
)
from hollis.tools.registry import DuplicateTool, ToolRegistry

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "CallbackApprover",
    "Decision",
    "DefaultToolPolicy",
    "DuplicateTool",
    "FunctionTool",
    "Isolation",
    "JsonPart",
    "ReferencePart",
    "Rule",
    "RulePolicy",
    "TextPart",
    "ToolArgumentError",
    "ToolContext",
    "ToolEffect",
    "ToolError",
    "ToolOutput",
    "ToolRegistry",
    "ToolTrust",
    "allow",
    "deny",
    "describe",
    "require_approval",
    "tool",
]
