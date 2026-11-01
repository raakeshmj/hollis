"""The Hollis platform: one application layer over the runtime for every interface (ADR-0026).

`Platform` (services) is the API the REST server, the SDK and the CLI share; `Workspace`
composes the stores, gateway, tools, memory and knowledge from `hollis.toml`.
"""

from hollis.platform.errors import PlatformError
from hollis.platform.services import Platform

__all__ = ["Platform", "PlatformError"]
