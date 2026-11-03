"""Specialized agents (M8, ADR-0033 to ADR-0035): capabilities, templates, output contracts and
Agent Skills. A specialist is an ordinary agent assembled from these; nothing here runs at run
time except contract checks and the skill reader, which the platform offers as tools."""

from hollis.specialists.catalog import (
    TEMPLATE_FIELDS,
    Catalog,
    Check,
    Draft,
    Entry,
    ModelFacts,
    TemplateError,
    WorkspaceFacts,
    materialize,
    model_checks,
    newer,
    parameter_values,
    workspace_checks,
)
from hollis.specialists.contracts import (
    CONTRACTS,
    OUTPUT_KIND,
    Contract,
    RunFacts,
    submit_tool_name,
)
from hollis.specialists.model import Capability, Requirements, Template
from hollis.specialists.skills import Skill, load_skills, offer, skill_tools

__all__ = [
    "CONTRACTS",
    "OUTPUT_KIND",
    "TEMPLATE_FIELDS",
    "Capability",
    "Catalog",
    "Check",
    "Contract",
    "Draft",
    "Entry",
    "ModelFacts",
    "Requirements",
    "RunFacts",
    "Skill",
    "Template",
    "TemplateError",
    "WorkspaceFacts",
    "load_skills",
    "materialize",
    "model_checks",
    "newer",
    "offer",
    "parameter_values",
    "skill_tools",
    "submit_tool_name",
    "workspace_checks",
]
