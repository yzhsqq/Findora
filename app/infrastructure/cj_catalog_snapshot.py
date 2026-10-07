"""Resolve versioned read-only publications without replacing an open file."""
from pathlib import Path


def resolve_catalog_snapshot(path: Path) -> Path:
    versions = list(path.parent.glob(f"{path.stem}.snapshot.*{path.suffix}"))
    return max(versions, key=lambda item: item.name) if versions else path
