"""YAML source files become Source rows without touching fetch history."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from server.database.base import Base
from server.database.session import create_db_engine, create_session_factory
from server.ingestion.cli import main
from server.ingestion.sources import (
    SourceConfig,
    SourceConfigError,
    load_source_config,
    load_source_configs,
    source_from_config,
    upsert_sources,
)
from server.models import Source
from sqlalchemy import func, select
from sqlalchemy.orm import Session

_SAMPLE = """
id: redblobgames
name: Red Blob Games
provider:
  type: rss
config:
  feed_url: https://www.redblobgames.com/atom.xml
category: programming
enabled: true
fetch:
  interval_seconds: 21600
  min_request_interval: 1.5
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_load_source_config_maps_the_example_shape(tmp_path: Path) -> None:
    config = load_source_config(_write(tmp_path, "redblob.yaml", _SAMPLE))

    assert config.id == "redblobgames"
    assert config.slug is None
    assert config.provider_type == "rss"
    assert config.config == {"feed_url": "https://www.redblobgames.com/atom.xml"}
    assert config.category == "programming"
    assert config.enabled is True
    assert config.fetch.interval_seconds == 21600
    assert config.fetch.min_request_interval == 1.5
    assert config.fetch.max_requests_per_run is None


def test_directory_loads_yaml_and_yml_in_name_order(tmp_path: Path) -> None:
    beta = _SAMPLE.replace("redblobgames", "beta").replace("Red Blob Games", "Beta")
    alpha = _SAMPLE.replace("redblobgames", "alpha").replace("Red Blob Games", "Alpha")
    _write(tmp_path, "b.yml", beta)
    _write(tmp_path, "a.yaml", alpha)
    _write(tmp_path, "notes.txt", "not a source")

    loaded = load_source_configs(tmp_path)

    assert [config.id for config in loaded] == ["alpha", "beta"]


def test_invalid_files_name_the_path(tmp_path: Path) -> None:
    broken = _write(tmp_path, "bad.yaml", "id: only-an-id\n")
    empty = _write(tmp_path, "empty.yaml", "[]\n")
    missing_type = _write(tmp_path, "type.yaml", _SAMPLE.replace("type: rss", "kind: rss"))

    with pytest.raises(SourceConfigError) as invalid:
        load_source_config(broken)
    with pytest.raises(SourceConfigError) as not_mapping:
        load_source_config(empty)
    with pytest.raises(SourceConfigError) as no_type:
        load_source_config(missing_type)

    assert invalid.value.path == broken
    assert not_mapping.value.path == empty
    assert no_type.value.path == missing_type


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "one.yaml", _SAMPLE)
    _write(tmp_path, "two.yaml", _SAMPLE)

    with pytest.raises(SourceConfigError, match="duplicate source id"):
        load_source_configs(tmp_path)


def test_duplicate_slugs_are_rejected(tmp_path: Path) -> None:
    other = _SAMPLE.replace("id: redblobgames", "id: other\nslug: redblobgames")
    _write(tmp_path, "one.yaml", _SAMPLE)
    _write(tmp_path, "two.yaml", other)

    with pytest.raises(SourceConfigError, match="duplicate source slug"):
        load_source_configs(tmp_path)


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="does not exist"):
        load_source_configs(tmp_path / "missing")


def test_upsert_inserts_then_refreshes_without_clearing_history(
    session: Session,
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "redblob.yaml", _SAMPLE)
    created = upsert_sources(session, [load_source_config(path)])
    created[0].etag = '"cached"'
    created[0].consecutive_failures = 3
    created[0].last_error = "timeout"
    created[0].last_success_at = datetime(2026, 1, 1, tzinfo=UTC)

    revised = _SAMPLE.replace("Red Blob Games", "Red Blob").replace("21600", "7200")
    revised += "  max_requests_per_run: 2\n"
    refreshed = upsert_sources(
        session,
        [load_source_config(_write(tmp_path, "redblob.yaml", revised))],
    )
    stored = session.get(Source, "redblobgames")

    assert refreshed[0] is stored
    assert stored is not None
    assert stored.name == "Red Blob"
    assert stored.slug == "redblobgames"
    assert stored.provider_type == "rss"
    assert stored.default_category_id == "programming"
    assert stored.fetch_interval_seconds == 7200
    assert stored.config_json == {
        "feed_url": "https://www.redblobgames.com/atom.xml",
        "min_request_interval": 1.5,
        "max_requests_per_run": 2,
    }
    assert stored.etag == '"cached"'
    assert stored.consecutive_failures == 3
    assert stored.last_error == "timeout"
    assert stored.last_success_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert session.scalar(select(func.count()).select_from(Source)) == 1


def test_explicit_slug_is_kept() -> None:
    config = SourceConfig.model_validate(
        {
            "id": "redblobgames",
            "name": "Red Blob Games",
            "slug": "red-blob",
            "provider": {"type": " rss "},
            "config": {},
            "category": "  ",
            "fetch": {"interval_seconds": 60},
        }
    )
    source = source_from_config(config)

    assert source.slug == "red-blob"
    assert source.provider_type == "rss"
    assert source.default_category_id is None
    assert source.config_json == {}
    assert source.enabled is True


def test_cli_load_sources_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "gamedev.sqlite"
    sources = tmp_path / "sources"
    sources.mkdir()
    _write(sources, "redblob.yaml", _SAMPLE)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    engine = create_db_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()

    exit_code = main(["load-sources", "--dir", str(sources)])
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert printed == "loaded 1 sources\n"
    check = create_db_engine(f"sqlite:///{database}")
    try:
        with create_session_factory(check)() as session:
            stored = session.get(Source, "redblobgames")
            assert stored is not None
            assert stored.provider_type == "rss"
            assert stored.config_json["feed_url"] == "https://www.redblobgames.com/atom.xml"
    finally:
        check.dispose()


def test_cli_load_sources_reports_a_bad_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "gamedev.sqlite"
    sources = tmp_path / "sources"
    sources.mkdir()
    _write(sources, "bad.yaml", "id: only-an-id\n")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    engine = create_db_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()

    exit_code = main(["load-sources", "--dir", str(sources)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed:" in captured.err
    assert captured.out == ""
