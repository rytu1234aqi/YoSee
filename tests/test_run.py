"""The documented launcher initializes a fresh database and serves the UI."""

from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.run import main


def test_launcher_initializes_sources_and_serves_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fresh.sqlite'}")
    calls = []

    def serve(app: FastAPI, **options: object) -> None:
        calls.append(options)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            assert "load-more" in client.get("/").text
            for asset in ("app.js", "app.css", "feed-store.mjs", "article.mjs"):
                assert client.get(f"/static/{asset}").status_code == 200
            assert len(client.get("/sources").json()) == 5
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
