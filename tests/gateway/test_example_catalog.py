from pathlib import Path

from hollis.gateway import Catalog


def test_shipped_example_catalog_is_valid() -> None:
    catalog = Catalog.from_toml(Path(__file__).parents[2] / "hollis.example.toml")
    assert [d.id for d in catalog.deployments] == ["local-qwen", "corp-llama", "claude"]
