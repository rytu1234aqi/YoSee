# 有得看

有得看是一个本地内容聚合阅读器：浏览器客户端、SQLite 数据库和单进程后台抓取。

## 启动

在项目目录执行（Python 3.12+）：

```bash
uv sync --locked
./.venv/bin/python -m server.run
```

浏览器打开 `http://127.0.0.1:8765`。启动命令会依次升级数据库、加载
`configs/sources/` 中的配置，然后启动网页和自动抓取。首次运行时列表可能暂时为空，
抓取完成后页面会出现更新提示；点击提示或顶栏刷新按钮即可读取新内容。

默认数据库为 `data/gamedev.sqlite`，可通过 `DATABASE_URL` 指定其他 SQLite 文件。
`Ctrl+C` 关闭服务，也会取消正在进行的抓取。自动抓取只在服务运行期间生效；
重新启动时会根据数据库中的上次抓取时间继续安排。

```bash
# 只阅读本地内容，不启动自动抓取
./.venv/bin/python -m server.run --no-auto-fetch

# 自定义端口、检查周期或来源配置目录
./.venv/bin/python -m server.run --port 8766 --poll-seconds 30 --sources-dir configs/sources
```

## 阅读与更新

- 使用 All articles / Unread / Saved、来源和搜索组合筛选。
- 每页 50 篇，列表底部的 Load more 继续加载，直到显示已到末尾。
- 分页使用文章时间和 ID 游标，后台新增文章不会把已加载页面挤动。
- 三栏宽度可拖动中间的竖直分隔条调整；分隔条也支持左右方向键（按住 Shift 加速）、
  Home/End 或双击恢复默认。宽度保存在本机，刷新后保持。中等宽度窗口（561–1000px）
  仍保持三栏并排、只是默认列宽更小；只有 ≤560px 才切换为堆叠布局并隐藏分隔条。
  首页与静态资源一律返回 `Cache-Control: no-store`；此外首页会把 `web/` 下除首页模板
  外的静态文件内容指纹（sha256 前 12 位）注入 `app.css` / `app.js` 的 URL
  （`?v=<hash>`）。内嵌 WebView 可能在加上 `no-store` 之前就永久缓存了旧样式表，
  只有换成从未请求过的 URL 才会重新下载，否则新 HTML（含两条分隔条）会被旧的三列
  CSS 自动排版，把详情区挤到第二行。访问 `/?debug=1` 可在右下角显示视口宽度、
  实际网格列宽和三栏是否同排，并把同样的几何信息打到服务端访问日志
  （`GET /health?w=...&cols=...`），便于排查内嵌预览面板。
- 详情页支持已读/未读、保存/取消保存；修改立即反映在列表中，重启后保留。
- 顶栏刷新仅重新读取数据库。后台默认每 30 秒检查一次到期来源，实际抓取频率遵循
  每个 YAML 的 `fetch.interval_seconds`，不会每 30 秒请求所有网站。
- 一个来源失败不会阻断其他来源；连续失败会延长间隔，最多退避至一天
  （原本就超过一天的间隔保持不变）。左栏显示检查时间、抓取进度及失败来源数。
- 少数站点会把全部历史文章塞进一个 feed（例如 Hugging Face Blog 约 900 条）。
  这类来源可在 YAML 的 `fetch` 段设置 `max_items_per_run: 50`，每次只导入最新的
  前 N 条，避免首次抓取把多年的旧文章灌进列表；不设置则不限制。
- 更新提示不会自动替换正在阅读的文章。正文只展示允许的 HTML 元素，原站脚本不会执行。
- 顶栏下载按钮可将本地文章和阅读状态导出为 JSON，便于备份和迁移。
- 顶栏上传按钮可导入之前导出的 JSON；导入内容会进入离线来源，不会参与自动抓取。
- 顶栏备份按钮会保存来源配置、文章和阅读状态，适合完整迁移本地数据库。

立即手动抓取仍可使用：

```bash
./.venv/bin/python -m server.ingestion.cli fetch-all
```

调度器用于一个本地服务进程。手动抓取（添加来源、左栏刷新）与后台轮询共用同一把锁，
不会并发写入 SQLite；仍不要同时启动多个服务进程。当前没有账号或跨设备同步，
默认仅监听本机。

## 安全模型

- 服务启动时生成随机 token 并注入首页，所有 API 请求需携带 `X-Local-Token`；
  Host 非回环且无 token 的请求被拒绝，用于阻断跨站调用与 DNS rebinding。
- 来源 URL 拒绝回环、私网、链路本地与保留地址；重定向逐跳重新校验，响应体上限 8 MB。
- 外站 HTML 在入库前按标签/属性白名单净化，接口输出再次净化；所有响应附带 CSP。
- 导入内容按字段与长度严格校验，导入的来源强制离线，不会重新开始抓取。
- 删除来源时在同一事务内清除文章、抓取记录与阅读状态。

## 验证

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m mypy server
./.venv/bin/ruff check server tests desktop/scripts
node --test tests/web/*.test.mjs
```

Python 测试使用临时数据库和模拟 HTTP，包括启动初始化、来源调度、失败隔离、关闭取消、
抓取期间的并发写入，以及有新文章插入时的分页一致性；`tests/test_hardening.py` 覆盖
SSRF、本地 token、HTML 净化、导入去重与级联删除。`tests/test_reader.py` 依赖可选的
Tkinter，环境没有安装时会自动跳过。前端逻辑测试仅需要 Node.js，不需要 npm 依赖。
浏览器交互与外站当前可访问性需另行实测。

CI 在 `.github/workflows/ci.yml` 中执行同样四项检查，并额外校验版本一致性与
Android 源码拷贝是否与根目录同步。

## 主要入口

- `server/run.py`：初始化和启动命令。
- `server/ingestion/scheduler.py`：按来源间隔调度抓取，也是手动抓取的唯一入口。
- `server/security.py`：URL 白名单、HTML 净化与本地 Host 判定。
- `server/api.py`：`/feed/page`、文章详情和阅读状态、`/sync/status`，以及来源管理、
  发现、导入/导出/备份和本地请求守卫。
- `web/`：客户端界面、分页状态管理与正文渲染。

原有 `/feed?limit=50&offset=0` 接口保留兼容；客户端使用 `/feed/page` 返回的
`next_cursor` 请求后续页面。API 文档见本地 `/docs`。
