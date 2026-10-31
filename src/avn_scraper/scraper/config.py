"""Loading of settings.yaml, sources.yaml and keywords.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _project_root() -> Path:
    """$AVN_HOME, else the current directory if it has config/, else the source checkout."""
    if os.environ.get("AVN_HOME"):
        return Path(os.environ["AVN_HOME"]).resolve()
    if (Path.cwd() / "config" / "settings.yaml").exists():
        return Path.cwd()
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _project_root()
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass
class Source:
    id: str
    name: str
    language: str
    type: str
    homepage: str
    district: str | None = None
    feeds: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    auto_sections: bool = True
    enabled: bool = True


@dataclass
class Config:
    settings: dict[str, Any]
    sources: list[Source]
    keywords: dict[str, Any]
    config_dir: Path

    @property
    def data_dir(self) -> Path:
        path = Path(self.settings.get("data_dir", "data"))
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def timezone(self) -> str:
        return self.settings.get("timezone", "Asia/Dhaka")

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(config_dir: Path | None = None) -> Config:
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    settings = _read_yaml(config_dir / "settings.yaml")
    raw_sources = _read_yaml(config_dir / "sources.yaml").get("sources", [])
    keywords = _read_yaml(config_dir / "keywords.yaml")

    sources = [Source(**entry) for entry in raw_sources]
    ids = [s.id for s in sources]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate source ids in sources.yaml: {sorted(duplicates)}")
    return Config(settings=settings, sources=sources, keywords=keywords, config_dir=config_dir)
