"""The documented launcher initializes a fresh database and serves the UI."""

import mimetypes
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.ingestion.sources import load_source_configs
from server.run import main


def test_launcher_initializes_sources_and_serves_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fresh.sqlite'}")
    # Windows registry associations can override Python's JavaScript MIME defaults.
    mimetypes.init()
    for extension in (".js", ".mjs", ".css"):
        monkeypatch.setitem(mimetypes.types_map, extension, "text/plain")
    calls = []

    def serve(app: FastAPI, **options: object) -> None:
        calls.append(options)
        with TestClient(app) as client:
            client.headers.update({"X-Local-Token": app.state.local_token})
            assert client.get("/").status_code == 200
            assert "load-more" in client.get("/").text
            for asset in ("app.js", "app.css", "feed-store.mjs", "article.mjs", "layout.mjs"):
                response = client.get(f"/static/{asset}")
                assert response.status_code == 200
                expected_type = "text/css" if asset.endswith(".css") else "text/javascript"
                assert response.headers["content-type"].split(";", 1)[0] == expected_type
            assert client.get("/static/missing.mjs").status_code == 404
            source_dir = Path(__file__).parent.parent / "configs" / "sources"
            assert len(client.get("/sources").json()) == len(load_source_configs(source_dir))
            assert client.get("/feed/page").json() == {"items": [], "next_cursor": None}
            assert client.get("/sync/status").json()["enabled"] is False

    monkeypatch.setattr(uvicorn, "run", serve)
    assert main(["--no-auto-fetch"]) == 0
    assert main(["--no-auto-fetch"]) == 0  # Initialization is idempotent.
    assert len(calls) == 2
    assert calls[0]["host"] == "127.0.0.1"


@pytest.mark.parametrize(
    "args",
    [
        ["--poll-seconds", "0"],
        ["--poll-seconds", "nan"],
        ["--port", "65536"],
    ],
)
def test_launcher_rejects_invalid_settings(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
