"""Print the REST API's OpenAPI document: `python -m hollis.server.openapi > web/openapi.json`.

The web application's TypeScript types are generated from it (`npm run api` in web/), and a test
fails when the committed copy no longer matches the server.
"""

import json
from pathlib import Path
from typing import Any

from hollis.server.app import create_app


def spec() -> dict[str, Any]:
    return create_app(root=Path(".")).openapi()  # routes only: nothing is opened


def dumps() -> str:
    return json.dumps(spec(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    print(dumps(), end="")
