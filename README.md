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
- 详情页支持已读/未读、保存/取消保存；修改立即反映在列表中，重启后保留。
- 顶栏刷新仅重新读取数据库。后台默认每 30 秒检查一次到期来源，实际抓取频率遵循
  每个 YAML 的 `fetch.interval_seconds`，不会每 30 秒请求所有网站。
- 一个来源失败不会阻断其他来源；连续失败会延长间隔，最多退避至一天
  （原本就超过一天的间隔保持不变）。左栏显示检查时间、抓取进度及失败来源数。
- 更新提示不会自动替换正在阅读的文章。正文只展示允许的 HTML 元素，原站脚本不会执行。
- 顶栏下载按钮可将本地文章和阅读状态导出为 JSON，便于备份和迁移。
- 顶栏上传按钮可导入之前导出的 JSON；导入内容会进入离线来源，不会参与自动抓取。
- 顶栏备份按钮会保存来源配置、文章和阅读状态，适合完整迁移本地数据库。

立即手动抓取仍可使用：

```bash
./.venv/bin/python -m server.ingestion.cli fetch-all
```

调度器用于一个本地服务进程；不要同时启动多个自动抓取进程或并行运行手动抓取。
当前没有账号或跨设备同步，默认仅监听本机。

## 验证

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m mypy server
./.venv/bin/ruff check server tests
node --test tests/web/*.test.mjs
```

Python 测试使用临时数据库和模拟 HTTP，包括启动初始化、来源调度、失败隔离、关闭取消、
抓取期间的并发写入，以及有新文章插入时的分页一致性。前端逻辑测试仅需要 Node.js，
不需要安装 npm 依赖。浏览器交互与外站当前可访问性需另行实测。

## 主要入口

- `server/run.py`：初始化和启动命令。
- `server/ingestion/scheduler.py`：按来源间隔调度抓取。
- `server/api.py`：`/feed/page`、文章详情和阅读状态、`/sync/status`。
- `web/`：客户端界面、分页状态管理与正文渲染。

原有 `/feed?limit=50&offset=0` 接口保留兼容；客户端使用 `/feed/page` 返回的
`next_cursor` 请求后续页面。API 文档见本地 `/docs`。
