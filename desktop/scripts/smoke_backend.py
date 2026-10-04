"""Exercise the bundled executable, from an unrelated working directory, without Python on PATH."""

import argparse
import json
import os
import queue
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("binary", type=Path)
parser.add_argument("--database", type=Path, required=True)
args = parser.parse_args()
binary = args.binary.resolve()
original = args.database.resolve()
with sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as database:
    expected = database.execute("SELECT count(*) FROM content_items").fetchone()[0]
assert expected > 0, "The regression fixture must contain real articles"

with tempfile.TemporaryDirectory(prefix="youdekan-smoke-") as directory:
    root = Path(directory)
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    with (root / "backend.log").open("w+") as log:
        process = subprocess.Popen([
            str(binary), "--data-dir", str(root / "资料库"),
            "--import-database", str(original), "--no-auto-fetch", "--watch-parent",
        ], cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, text=True)
        messages = queue.Queue()

        def read_ready():
            for line in process.stdout:
                if line.startswith("YOUDEKAN_READY="):
                    messages.put(line.strip().split("=", 1)[1])

        threading.Thread(target=read_ready, daemon=True).start()
        try:
            url = messages.get(timeout=30)
            def get(path):
                with urllib.request.urlopen(url + path, timeout=5) as response:
                    return response.headers, response.read().decode()

            for attempt in range(50):
                try:
                    assert json.loads(get("/health")[1]) == {"status": "ok"}
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Health endpoint never became available")
            html = get("/")[1]
            assert "有得看" in html
            for path in re.findall(r'(?:src|href)="(/static/[^\"]+)"', html):
                headers, content = get(path)
                assert "text/html" not in headers["Content-Type"], (path, headers)
                assert content.strip()
            for path in ("/static/feed-store.mjs", "/static/article.mjs"):
                assert "javascript" in get(path)[0]["Content-Type"]
            assert len(json.loads(get("/sources")[1])) > 0
            items = json.loads(get("/feed/page")[1])["items"]
            assert len(items) > 0
            article = json.loads(get("/content/" + items[0]["id"])[1])
            assert article["title"] == items[0]["title"]
            assert len(json.loads(get("/export/content")[1])) == expected
            assert json.loads(get("/sync/status")[1])["enabled"] is False
            print(f"PASS: frozen runtime, migrations, UI assets, sources, {expected} articles and detail API")
            process.stdin.close()
            assert process.wait(timeout=10) == 0
            print("PASS: parent exit shuts down backend cleanly")
        except BaseException:
            log.flush()
            log.seek(0)
            print(log.read())
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
