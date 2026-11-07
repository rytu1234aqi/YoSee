#!/usr/bin/env bash
# 把仓库根的 Python 后端与前端资源同步进 Android 工程的 Chaquopy 源目录。
# 同步后 android/app/src/main/python/ 的布局与仓库根一致：
#   server/  web/  configs/  alembic.ini  （外加 android_entry.py 入口）
# 用法：bash android/scripts/sync_python.sh
set -euo pipefail

ANDROID_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$ANDROID_DIR/.." && pwd)"
DEST="$ANDROID_DIR/app/src/main/python"

copy_tree() {
    local name="$1"
    rm -rf "$DEST/$name"
    mkdir -p "$DEST"
    cp -r "$REPO_ROOT/$name" "$DEST/$name"
}

copy_tree server
copy_tree web
copy_tree configs
cp "$REPO_ROOT/alembic.ini" "$DEST/alembic.ini"

# 清理缓存与字节码
find "$DEST" -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -prune -exec rm -rf {} +
find "$DEST" -type f -name '*.pyc' -delete

echo "同步完成 -> $DEST"
ls "$DEST"
