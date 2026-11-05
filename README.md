# MSC — Memory Sync Client

个人记忆服务：Docker 部署，REST API + Web UI + CLI，SQLite + Markdown 双写。
默认**局域网免登录**，任何本机/局域网 agent 直接调用，不需要 token。

## 功能

- **记忆管理** — 增删改查，分类/标签/优先级/分支过滤，软删除可撤销
- **全文搜索** — SQLite FTS5（自动触发器维护），任意字符都不会破坏查询，另有 LIKE 兜底
- **版本历史** — 每次编辑自动存版本，可查看/回滚（回滚本身也进历史）
- **记忆图谱** — Vis.js 动态图，节点按分类着色
- **双向同步** — API 写入即时落盘；磁盘上手改 markdown 由后台 worker 拉回库（`MSC_SYNC_INTERVAL_SECONDS`）
- **自描述 API** — `GET /api/v1/help`、`/llms.txt`、`/meta`，agent 读完就会用
- **自愈迁移** — 启动时补列、清洗标签、注册分支、清理正文里混入的 frontmatter、修复搜索索引
- **备份** — 定时（24h）+ 手动，tar.gz，自动保留最近 `MSC_BACKUP_KEEP` 份
- **CLI** — `msc`，走 HTTP，可与服务器不同机

## 快速开始

```bash
cd /home/astro/msc
./setup.sh                # 生成 .env + 迁移 + 起容器 + 装 msc CLI
```

或手动：

```bash
cp .env.example .env
docker compose up -d
pip install --user --break-system-packages -e cli/    # 装 msc 命令
msc status
```

改过后端代码后必须重建镜像（代码在镜像里，不是挂载的）：

```bash
make restart              # = docker compose build && docker compose up -d
```

## 访问

| 地址 | 说明 |
|------|------|
| http://localhost:8000 | Web UI |
| http://localhost:8000/api/v1 | REST API |
| http://localhost:8000/api/v1/help | 给 agent 看的纯文本用法 |
| http://localhost:8000/llms.txt | 同上（agent 惯例入口） |
| http://localhost:8000/docs | Swagger |

## API 速查

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/memories` | 列表，支持 `category/priority/tag/branch/since/q/include_archived/order/limit/offset` |
| GET | `/memories/{slug}` | 单条；slug、精确名称或唯一片段都行 |
| POST | `/memories` | 新建（`related_slugs` 顺便建关联） |
| PUT | `/memories/{slug}` | upsert（`?upsert=false` 则不存在时 404） |
| DELETE | `/memories/{slug}` | 软删除；`?hard=true` 彻底删除 |
| POST | `/memories/{slug}/restore` | 撤销软删除 |
| POST | `/memories/batch` | 批量，逐条返回结果 |
| GET/POST | `/memories/{slug}/relate` | 查/建关联 |
| GET | `/memories/{slug}/versions` | 编辑历史（旧版路径 `/memories/versions/{slug}` 仍可用） |
| POST | `/memories/versions/{slug}/restore/{n}` | 回到第 n 版 |
| GET | `/search?q=...` | 全文 + 过滤，带 `X-Total-Count` |
| GET | `/search/recommend/{slug}` | 相关推荐 |
| GET | `/memories/graph` | 图谱数据 |
| GET | `/meta` `/tags` `/branches` `/stats` | 词表、标签、分支、计数 |
| GET | `/sync/status` | 库与磁盘的差异、索引状态、上次同步 |
| POST | `/sync/push` `/sync/pull` `/sync/reindex` | 推 / 拉 / 重建索引 |
| GET | `/sync/logs` `/sync/conflicts` | 同步日志与冲突 |
| POST/GET | `/backup/create` `/backup/list` | 备份 |
| GET/DELETE | `/backup/backups/{file}/download` `/backup/backups/{file}` | 下载 / 删除备份 |
| GET | `/health` `/config` | 自检（含版本、数据库、索引）/ 客户端配置 |

写请求建议带 `X-Agent: <你的名字>`，会记进版本历史；`/api/v1/health` 的访问日志也会记。

## CLI

```bash
msc status                    # 服务器 + 同步状态一屏
msc list --tag codex --limit 20
msc get <slug> [--raw]        # --raw 只输出正文，方便管道
msc add --name "标题" --body "内容" --tags a,b [--body-file f.md | stdin]
msc edit <slug> --priority high
msc delete <slug>             # 软删除，msc restore <slug> 撤销
msc search "关键词"
msc versions <slug> / msc revert <slug> <n>
msc relate <a> <b> / msc related <slug>
msc tags / msc branches
msc sync push|pull|status|reindex|logs
msc backup create|list|download|delete
msc api-help                  # 服务器自己的用法说明
```

配置优先级：命令行 > 环境变量 `MSC_BASE_URL` / `MSC_API_KEY` / `MSC_AGENT` > `~/.config/msc/credentials`（`msc init` 生成）。
默认指向 `http://localhost:8000`，局域网模式无需任何凭据。

> 注意：以前 `~/.local/bin/msc` 是老的扁平文件工具（`~/.local/msc/msc.py`，读写 `~/.msc/memory`）。
> 现在它改名成 `msc-files`，`msc` 是本项目的 API 客户端。两者共用同一个 memory 目录，
> 老工具写入的文件会被后台 sync 自动收进数据库。

## 访问模式

默认 **局域网免登录**（`MSC_AUTH_ENABLED=false`）：Web UI 与全部 API 直接可用。

```bash
curl http://dgx:8000/api/v1/memories
curl -X PUT http://dgx:8000/api/v1/memories/my-key \
     -H "Content-Type: application/json" -H "X-Agent: my-agent" \
     -d '{"name":"My key","body":"...","category":"reference","tags":["a"]}'
```

`GET /api/v1/config` → `{"auth_enabled": false, "mode": "lan"}`。

无登录不等于无痕迹：每个 `/api/` 请求都会写访问日志（方法、路径、状态、耗时、`X-Agent`、来源 IP），
容器日志按 10MB × 5 轮转。紧急止血用 `MSC_READ_ONLY=true`（所有写操作返回 423）。

### 需要鉴权时

```bash
# .env
MSC_AUTH_ENABLED=true
MSC_SECRET_KEY=<随机串>
```

`docker compose up -d` 后调用一次 `POST /api/v1/setup` 生成管理员 API Key。API Key 前缀路由权限：

| 前缀 | 权限 |
|------|------|
| `999` | 全量记忆读写 |
| `001` | work 分支读写 |
| `003` | config + reference 分类只读 |

## 数据与文件

```
/home/astro/msc/
├── docker-compose.yml        # 编排（卷：data / backups / memory 目录）
├── backend/
│   ├── msc/                  # FastAPI 后端
│   │   ├── api/              # 路由
│   │   ├── filesystem/       # markdown 读写 + 索引
│   │   ├── workers/          # 同步 / 备份线程
│   │   ├── migrations.py     # 启动自愈（补列、清洗标签、注册分支、修索引）
│   │   └── fts.py            # FTS5 触发器与查询转义
│   ├── ui/                   # 无构建步骤的原生 Web UI
│   └── tests/                # pytest 接口测试
├── cli/msc_cli/              # CLI
├── migrations/               # 一次性历史迁移
├── data/msc.db               # SQLite
└── backups/                  # tar.gz 归档
```

记忆同时存在于数据库和 `~/.msc/memory/<slug>.md`（frontmatter + 正文），两边都能直接编辑。

## 开发

```bash
make test                     # pytest backend/tests/
make lint                     # ruff --select F,E9（未用导入/变量、未定义名）
make dev                      # 装 pytest + 可编辑安装的 msc CLI
make health                   # 看容器自检
```

启动时会跑一次维护并在有变化时打印，例如：

```
Startup maintenance: {'columns_added': ['sync_conflicts.status'], 'tags_normalized': 25,
 'branches_registered': ['main'], 'bodies_cleaned': ['test-memory'],
 'search_index': {'total': 38, 'missing': 0, 'slugs': []}}
```

只动活记忆，软删除的不会被“复活”成文件；正文清洗同时写回磁盘（文件 + `.msc_index.json` + 同步哈希）。

## 常见故障

| 现象 | 处理 |
|------|------|
| UI 没变化 | 后端在镜像里：`make restart`；浏览器有 `?v=` 版本号，硬刷新即可 |
| 搜索没结果 | `msc sync reindex`（或 `POST /api/v1/sync/reindex`），再看 `/api/v1/health` 的 `search_index` |
| 磁盘上的文件没进库 | `msc sync status` 看 `only_in_files`，然后 `msc sync pull` |
| 端口不通 | `docker compose ps`、`docker compose logs msc-api` |
| 图谱页只显示列表 | vis-network 走 CDN，内网不通时自动退化为关联列表（功能不丢） |
| `.msc_index.json` 里有已删除条目 | `msc sync reindex`（同时重建 FTS 和文件索引） |

## 更新记录

### 2.1.1

- **正文里的 frontmatter 被清掉了** — 老导入器把整个 markdown 文件（含元数据块）当正文写进库，读取侧原来只认 `---` 完整围栏，漏了“开栏已被解析吃掉”的形状（`key: value` 行 + 单个 `---`）。现在 `strip_stray_frontmatter` 两种都认，启动维护新增 `bodies_cleaned`，一次修好 数据库 + 文件 + `.msc_index.json` + 同步哈希。只处理活记忆，软删除的不会被复活成文件。
- **删除不再留陈索引** — 新增 `drop_index_entry` / `delete_through`，删记忆时文件和 `.msc_index.json` 条目一起走；已有的脏条目用 `msc sync reindex` 清。
- **备份列表按时间倒序** — 文件名里是随机 id，按名排序会把顺序打乱。
- **push/pull 不再反复改写** — 写入路径统一走 `write_through()`（文件 + 索引 + `last_sync_hash`），紧跟着的 pull 认出“没变化”。
- **图谱页离线可用** — vis-network CDN 不可达时退化为关联列表，不再是一片空白。
- **`/ui/` 直接出页面**；静态资源 URL 带 `?v=` 版本号。
- 备份自动保留最近 `MSC_BACKUP_KEEP` 份（默认 14）；`/ui` 静态挂载、CORS 解析、`msc` 版本与后端单一来源。
- 新增 `make lint`（`ruff --select F,E9`）；`setup.sh` 会顺手装 `msc` CLI；新增 `.gitignore`（`data/`、`backups/`、`.env` 不入库）。
- 测试 50 个（新增 `backend/tests/test_text.py`，以及正文修复 / 索引摘除 / 归档不复活回归）。

### 2.1.0

- FTS5 全文搜索（触发器维护 + 查询消毒 + LIKE 兜底）、启动自愈迁移（补列 / 清洗标签 / 注册分支 / 修复索引）、软删除 + 撤销、版本历史与回滚、记忆图谱、备份、`/meta` `/help` `/llms.txt` 自描述接口，Web UI 与 `msc` CLI 全面重写。
