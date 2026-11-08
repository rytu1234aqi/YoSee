"""Verify the real Windows shell's storage and automatic migration in an isolated copy."""
import ctypes
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import urllib.request
from contextlib import closing
from ctypes import wintypes
from pathlib import Path

root = Path(__file__).resolve().parents[2]
release = root / 'desktop/src-tauri/target/release'
executable = (release / 'youdekan.exe').read_bytes()
pe_offset = struct.unpack_from('<I', executable, 0x3c)[0]
assert struct.unpack_from('<H', executable, pe_offset + 24 + 68)[0] == 2
print('PASS: Windows main executable uses the GUI subsystem (no console window)')
install = Path(tempfile.mkdtemp(prefix='安装目录检查-', dir=root / 'desktop/build'))
shutil.copy2(release / 'youdekan.exe', install / 'youdekan.exe')
shutil.copytree(release / 'backend', install / 'backend')
legacy = Path(os.environ['APPDATA']) / 'com.youdekan.reader/youdekan.sqlite'
legacy_before = legacy.read_bytes()
with closing(sqlite3.connect(legacy.resolve().as_uri() + '?mode=ro', uri=True)) as db:
    expected = db.execute('SELECT count(*) FROM content_items').fetchone()[0]
assert expected > 0

user32 = ctypes.WinDLL('user32', use_last_error=True)
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

def app_windows(pid, close=False):
    @callback_type
    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            caption = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, caption, 256)
            if caption.value != '有得看':
                return True
            if close:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            else:
                user32.ShowWindow(hwnd, 0)
        return True
    user32.EnumWindows(callback, 0)

def launch():
    log = install / 'backend.log'
    offset = log.stat().st_size if log.exists() else 0
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    env = os.environ.copy()
    env['WEBVIEW2_USER_DATA_FOLDER'] = str(install / 'webview-profile')
    process = subprocess.Popen([str(install / 'youdekan.exe')], cwd=root,
                               env=env, startupinfo=startup,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            app_windows(process.pid)
            assert process.poll() is None, 'Desktop app exited early'
            if log.exists():
                lines = log.read_bytes()[offset:].decode('utf-8', errors='replace').splitlines()
                ready = [
                    line.partition('=')[2]
                    for line in lines
                    if line.startswith('YOUDEKAN_READY=')
                ]
                if ready:
                    try:
                        with urllib.request.urlopen(ready[-1] + '/health', timeout=2) as response:
                            assert json.load(response) == {'status': 'ok'}
                        return process, ready[-1]
                    except OSError:
                        pass
            time.sleep(0.1)
        detail = log.read_text(errors='replace') if log.exists() else 'No install-directory log'
        raise AssertionError(detail)
    except BaseException:
        app_windows(process.pid, close=True)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise

def stop(process):
    time.sleep(2)
    app_windows(process.pid, close=True)
    try:
        assert process.wait(timeout=15) == 0
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        print('Desktop test process stopped after close-message timeout')

process, url = launch()
try:
    database = install / 'youdekan.sqlite'
    assert database.is_file()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('SELECT count(*) FROM content_items').fetchone()[0] >= expected
    with urllib.request.urlopen(url + '/feed/page', timeout=5) as response:
        assert len(json.load(response)['items']) > 0
    for module in ('app.js', 'feed-store.mjs', 'article.mjs', 'layout.mjs'):
        with urllib.request.urlopen(url + '/static/' + module, timeout=5) as response:
            assert 'javascript' in response.headers['content-type']
    print(
        'PASS: actual Windows shell stores database and log beside executable; '
        f'migrated {expected} articles'
    )
finally:
    stop(process)

with closing(sqlite3.connect(database)) as db:
    db.execute('CREATE TABLE install_storage_check (value TEXT)')
    db.execute("INSERT INTO install_storage_check VALUES ('keep existing database')")
    db.commit()
process, url = launch()
try:
    with closing(sqlite3.connect(database)) as db:
        value = db.execute('SELECT value FROM install_storage_check').fetchone()
        assert value == ('keep existing database',)
    assert legacy.read_bytes() == legacy_before
    print('PASS: restart retains install-directory database; original AppData database unchanged')
finally:
    stop(process)
print('PASS: both desktop launches verified')
print(f'Validated directory: {install}')
