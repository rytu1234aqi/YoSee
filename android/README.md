# 有得看 Android 端

Android 客户端由 Kotlin + WebView 壳和 Chaquopy 内嵌的 Python 后端组成，复用仓库根的
`server/`、`web/`、`configs/`、`alembic.ini`。`preBuild` 会自动把这些资源同步进
`app/src/main/python/`，无需手工执行；`scripts/sync_python.sh` 仍可用于手动同步。
页面、`/static/` 资源和 API 全部由应用内运行的
FastAPI 服务提供，WebView 直接加载它。数据库在应用私有目录
`filesDir/youdekan.sqlite`，格式与桌面端一致。

## 环境要求

- Android SDK：platform `android-36.1`、build-tools、platform-tools。
  本机没有 cmdline-tools 也可以构建，不需要 sdkmanager。
- JDK 17+：可直接用 Android Studio 自带的 JBR。
- Gradle：使用仓库自带的 wrapper（`./gradlew`），无需单独安装。
- Rust 工具链：仅重新交叉编译 pydantic-core wheel 时需要
  （rustup target `aarch64-linux-android`、`x86_64-linux-android`，外加 cargo-ndk）。

## 构建

先在 `android/` 下创建 `local.properties`（不入库，已在 `.gitignore` 中）：

```properties
sdk.dir=<Android SDK 路径>
```

然后：

```bash
cd android
JAVA_HOME=<JBR路径> ./gradlew assembleDebug
```

`preBuild` 依赖 `syncSharedPython` 任务，每次构建都会从仓库根重新复制
`server/`、`web/`、`configs/`、`alembic.ini` 并清理 `__pycache__`；干净 clone
也能直接构建。CI 中的一致性检查会在同步后做 `diff` 校验，防止拷贝漂移。

产物为 `app/build/outputs/apk/debug/app-debug.apk`。

`./gradlew assembleRelease` 生成未签名的 release APK；正式发布需自行生成
keystore 并配置签名。

## pydantic-core wheel

Chaquopy 官方仓库没有 pydantic-core 的 Android wheel，`pip-repo/` 里的两个
wheel（arm64-v8a、x86_64）是用 `scripts/build_pydantic_core.sh` 自行交叉编译的
（约 10 分钟，依赖 NDK + Rust + maturin；脚本顶部的常量可改 NDK 路径和
Chaquopy target 版本）。升级 pydantic / pydantic-core 版本时必须同步重编。

## 关键技术说明

- `app/build.gradle.kts` 的 chaquopy 块里 `pyc { src = false }` 和
  `extractPackages("server")`：alembic 用 importlib 按文件系统路径加载
  `migrations/versions/*.py`，默认的 .pyc 打包会把 .py 源文件从 APK 里剔除；
  而 server 包的数据文件（如 `script.py.mako`）只在包被导入时才解包，
  `extractPackages` 保证整个包在文件系统上可见。
- lxml 固定 5.3.0：Chaquopy 对 cp313 只提供这个版本，项目仅用它做
  BeautifulSoup 的解析后端。
- ABI 只出 arm64-v8a + x86_64（受 lxml wheel 限制），分别覆盖真机和 x64 模拟器。

## 验证状态

debug 包已在 x86_64 模拟器（API 36.1）上验证通过：alembic 迁移、FastAPI
全部 API、5 个内置来源抓取、WebView 渲染均正常。尚未做正式发布签名。
