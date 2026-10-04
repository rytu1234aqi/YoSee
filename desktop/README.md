# 有得看桌面端

macOS 客户端由 Tauri 2 和随包分发的 Python 服务组成。安装后不需要 Python、开发目录或另开终端。页面、`/static/` 资源和 API 全部由同一个内置服务提供；窗口等待健康检查通过才打开阅读器。

## 开发

在项目根目录安装构建依赖：

```bash
uv sync --locked --group desktop
```

再运行：

```bash
cd desktop
npm install
npm run tauri dev
```

## 构建

```bash
cd desktop
npm run tauri build
```

构建钩子先用 PyInstaller 收集运行时、依赖、迁移脚本、来源配置及前端，再生成 `.app` 和 `.dmg`。不包含个人数据库。macOS Apple Silicon 产物在 `src-tauri/target/release/bundle/`。

## 数据与诊断

数据保存在 `~/Library/Application Support/com.youdekan.reader/youdekan.sqlite`；日志为同目录的 `backend.log`。应用升级不会覆盖数据库。内置服务仅监听 `127.0.0.1`，自动选择空闲端口，退出应用时一同停止。启动失败时显示日志位置。

首次从网页版本迁移，可在应用未启动、目标数据库不存在时执行：

```bash
"src-tauri/target/release/bundle/macos/有得看.app/Contents/Resources/backend/youdekan-backend" \
  --data-dir "$HOME/Library/Application Support/com.youdekan.reader" \
  --import-database ../data/gamedev.sqlite --prepare-only
```

迁移使用 SQLite 在线备份接口，包含 WAL 中已提交的数据，保留原数据库，已有目标数据库不会被覆盖。

从项目根目录验证成品内置服务（临时目录、限制 PATH，验证前端资源、文章读取和退出清理）：

```bash
./.venv/bin/python desktop/scripts/smoke_backend.py \
  "desktop/src-tauri/target/release/bundle/macos/有得看.app/Contents/Resources/backend/youdekan-backend" \
  --database data/gamedev.sqlite
```

## 平台状态

- macOS：Apple Silicon 本地测试包；尚未进行 Apple Developer 签名及公证。
- Windows：尚未构建验证，需在 Windows 配置解释器路径、打包目标并重新生成对应平台运行时。
- Android：尚未生成 APK。Tauri Android 还需要 Android SDK、NDK、`adb` 和 Gradle 环境；当前项目的 Python 后端也需要迁移为移动端可运行的服务或连接到局域网服务器。
