"""Load source definitions from ``configs/sources/*.yaml``.

YAML is the source of truth for identity and fetch settings. Runtime fields
such as the last error, cache validators, and failure count are left alone.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from server.ingestion.errors import ProviderError
from server.models.source import Source

DEFAULT_SOURCES_DIR = Path("configs/sources")


class FetchConfig(BaseModel):
    """How often one source may be fetched, and how hard."""

    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(gt=0)
    min_request_interval: float = Field(default=0, ge=0)
    max_requests_per_run: int | None = Field(default=None, ge=0)


class SourceConfig(BaseModel):
    """One file in ``configs/sources``. ``id`` becomes ``Source.id``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    slug: str | None = None
    provider: dict[str, str]
    config: dict[str, Any] = Field(default_factory=dict)
    category: str | None = None
    homepage_url: str | None = None
    icon_url: str | None = None
    enabled: bool = True
    fetch: FetchConfig

    @field_validator("id", "name")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("slug", "category", "homepage_url", "icon_url")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @property
    def provider_type(self) -> str:
        return _provider_type(self)


class SourceConfigError(ProviderError):
    """A source file is missing, unreadable, or does not match the schema."""

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message, source_id=path.name if path is not None else "")
        self.path = path


def load_source_config(path: Path) -> SourceConfig:
    """Parse one YAML file. The file name does not decide the source id."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceConfigError(f"could not read {path}: {exc}", path=path) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SourceConfigError(f"could not parse {path}: {exc}", path=path) from exc
    if not isinstance(raw, dict):
        raise SourceConfigError(f"{path} must contain a mapping", path=path)
    try:
        config = SourceConfig.model_validate(raw)
        _provider_type(config)
    except (ValidationError, ValueError) as exc:
        raise SourceConfigError(f"{path} is not a valid source: {exc}", path=path) from exc
    return config


def load_source_configs(directory: Path = DEFAULT_SOURCES_DIR) -> list[SourceConfig]:
    """Load every ``*.yaml`` and ``*.yml`` file. Order is by file name."""

    if not directory.is_dir():
        raise SourceConfigError(f"source directory {directory} does not exist", path=directory)
    paths = sorted(
        path for path in directory.iterdir() if path.suffix in {".yaml", ".yml"} and path.is_file()
    )
    configs = [load_source_config(path) for path in paths]
    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    for config in configs:
        if config.id in seen_ids:
            raise SourceConfigError(f"duplicate source id {config.id}", path=directory)
        slug = config.slug or config.id
        if slug in seen_slugs:
            raise SourceConfigError(f"duplicate source slug {slug}", path=directory)
        seen_ids.add(config.id)
        seen_slugs.add(slug)
    return configs


def apply_source_config(source: Source, config: SourceConfig) -> Source:
    """Copy config fields onto ``source``. Fetch history is not touched."""

    source.id = config.id
    source.name = config.name
    source.slug = config.slug or config.id
    source.provider_type = _provider_type(config)
    source.homepage_url = config.homepage_url
    source.icon_url = config.icon_url
    source.enabled = config.enabled
    source.default_category_id = config.category
    source.config_json = _config_json(config)
    source.fetch_interval_seconds = config.fetch.interval_seconds
    return source


def source_from_config(config: SourceConfig) -> Source:
    """Build a new source. Runtime columns stay at their defaults."""

    return apply_source_config(Source(id=config.id), config)


def upsert_sources(session: Session, configs: list[SourceConfig]) -> list[Source]:
    """Insert missing sources and refresh existing ones. The caller commits."""

    rows: list[Source] = []
    for config in configs:
        row = session.get(Source, config.id)
        if row is None:
            row = source_from_config(config)
            session.add(row)
        else:
            apply_source_config(row, config)
        rows.append(row)
    session.flush()
    return rows


def sync_sources(session: Session, directory: Path = DEFAULT_SOURCES_DIR) -> list[Source]:
    """Load a directory and upsert every source. The caller commits."""

    return upsert_sources(session, load_source_configs(directory))


def _provider_type(config: SourceConfig) -> str:
    raw = config.provider.get("type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise ValueError("provider.type is required")


def _config_json(config: SourceConfig) -> dict[str, Any]:
    stored = dict(config.config)
    if config.fetch.min_request_interval:
        stored["min_request_interval"] = config.fetch.min_request_interval
    if config.fetch.max_requests_per_run is not None:
        stored["max_requests_per_run"] = config.fetch.max_requests_per_run
    return stored
