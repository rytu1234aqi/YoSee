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
npm install          # 首次需要，安装 Tauri CLI
npm run tauri build
```

构建钩子先用 PyInstaller 收集运行时、依赖、迁移脚本、来源配置及前端，再生成安装包，
不包含个人数据库。产物都在 `src-tauri/target/release/bundle/`。

- macOS：`.app` 和 `.dmg`。
- Windows：读取 `src-tauri/tauri.windows.conf.json`，生成 NSIS 安装包
  `bundle/nsis/有得看_<版本>_x64-setup.exe`（当前用户安装，不需要管理员权限）。

Windows 构建前置条件：Rust 的 `x86_64-pc-windows-msvc` 工具链、Visual Studio 的
“使用 C++ 的桌面开发”工作负载（含 MSVC 与 Windows SDK）、Node.js，以及项目根目录的
`uv sync --locked --group desktop`。图标集中已包含 Windows 所需的 `icon.ico`；如需替换，
先替换 `src-tauri/icons/icon.png`，再执行 `npm run tauri icon src-tauri/icons/icon.png`。

## 数据与诊断

- Windows：数据库为安装目录中的 `youdekan.sqlite`，与 `youdekan.exe` 同级；日志为同目录的
  `backend.log`。安装目录需要当前用户有写入权限。升级后首次启动时，如果安装目录尚无数据库，
  会自动从旧位置 `%APPDATA%\com.youdekan.reader\youdekan.sqlite` 迁移，保留旧文件。
- macOS：数据库为 `~/Library/Application Support/com.youdekan.reader/youdekan.sqlite`，
  日志为同目录的 `backend.log`。

应用升级不会覆盖数据库。内置服务仅监听 `127.0.0.1`，自动选择空闲端口，退出应用时一同停止。启动失败时显示日志位置。

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
- Windows：x64 NSIS 安装包已在本机构建并通过内置服务冒烟测试；尚未做代码签名，
  安装时会出现 SmartScreen 提示。安装包默认在缺少 WebView2 时联网下载引导程序；
  需要离线安装可把 `tauri.windows.conf.json` 的 `webviewInstallMode` 改为
  `embedBootstrapper` 或 `offlineInstaller`。
- Android：Kotlin + WebView 壳内嵌 Chaquopy Python 后端（详见 `../android/README.md`）；
  debug 包已在模拟器验证；尚未做正式发布签名。
