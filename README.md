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
- **记忆小结（模型卡片）** — 在 Web UI 里填一个 OpenAI 兼容的本地/局域网模型，把多条记忆合并成一条小结记忆：手动勾选或每天定点自动当日小结；原记忆可选软删除（可一键恢复）
- **照片 RAG（msc-pic，2.4.0）** — 照片源卡片（本机目录 / SMB / WebDAV）渐进摄入：先占位、逐张出 512px 缩略图、多模态模型（`vision` 卡片）写中文描述、描述进 bge-m3 向量。照片页按月浏览缩略图墙，语义搜索命中缩略图，勾选看原图（流式透传，原图不入库）；回收站软删可恢复，「彻底删除」带确认才动磁盘原图。扫描/摄入随时可暂停、中断自动续传（游标 + 状态机队列），详见 `.env.example` 的 `MSC_PIC_*` 与 `MSC_VISION_*`

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
│   │   ├── workers/          # 同步 / 备份 / 当日小结线程
│   │   ├── services/         # 关键词连线 / 模型客户端 / 小结生成
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

## 记忆小结与模型卡片

**只在 Web UI 里操作**，属于后台管理功能：这些接口刻意不写进 `/api/v1/help` 与 `/llms.txt`，agent 不会看到它们。

1. 打开 设置 → 🤖 模型卡片，填 API 地址与模型名（任何 OpenAI 兼容端点：LM Studio、llama.cpp、ollama 的 OpenAI 接口、vLLM 都行），点「测试连接」。
   配置存在数据库里，改完立即生效，不用改 `.env` 也不用重启容器；`MSC_LLM_*` 只是首次启动的默认值。
2. 手动小结：记忆页点「☑ 多选」→ 勾选记忆 → 「🧠 小结所选」→ 可先「预览」（不写库），确认后「生成并写入」。
3. 自动当日小结：卡片里勾选「开启」并设时间（含时区，决定“当天”的边界）。到点后台把当天创建/更新的记忆合并成一条 `每日小结 YYYY-MM-DD`；
   容器错过了那个点，下次启动会补跑前一天，更早的不补。每天只尝试一次（失败也记账），失败原因显示在卡片上，可手动补。

写进去的小结就是一条普通记忆（标签 `summary`，自动小结再加 `daily`；分类/优先级取卡片默认值），可编辑、可删除、进搜索、进图谱：
它与被合并的记忆之间建 `summarizes` 关联，正文末尾固定附上来源清单与「原始记忆已软删除/保留未删除」的说明。

- **原记忆删不删由你选**：卡片里的「默认软删除」决定勾选框初值，弹窗里可临时改。软删除会保留行与编辑历史，
  弹窗/toast 上的「恢复原始记忆」按 `summarizes` 关联把原记忆全部拉回来（小结本身不动）。
- **在哪逐条恢复**：设置 → 🧹 维护 现在列出全部已归档记忆（点名称看正文，点「恢复」还原记忆 + markdown 文件）；
  记忆页勾上「显示已归档」也能逐条恢复/彻底删除。彻底删除只有「彻底清除所有已归档」和单条「彻底删除」两处。
- **模型失败不动数据**：模型没回答就什么都不写，只记一条失败日志；`dry_run` 预览同样零写入。
- **小模型的上下文很短**：送入模型的正文按「输入上限（字符）」裁剪，先丢最旧的，被跳过的条数会写在小结里；
  单条记忆正文最多取 1200 字符。
- **推理模型**：gpt-oss、Qwen3 这类会先把 token 花在思考上。若正文为空且被 token 截断，接口会直接说明「调大最大输出 tokens 或在额外参数里关闭思考」，
  而不是报一句空内容。「额外请求参数 JSON」原样合并进请求体，可填 `{"reasoning": {"effort": "low"}}` 之类。
- 记录在 `summary_runs` 表，界面 设置 → 最近小结 可见（`GET /api/v1/summarize/runs`）。

## 开发

```bash
make test                     # pytest backend/tests/
make lint                     # ruff --select F,E9（未用导入/变量、未定义名）
make dev                      # 装 pytest + 可编辑安装的 msc CLI
make health                   # 看容器自检
```

UI 是静态文件（无构建步骤），但也打在镜像里：改完 `make restart`，并把 `index.html` 里四个资源的
`?v=` 后缀一起改（当前形如 `2.2.0-1`），否则浏览器还会用旧的 CSS/JS 缓存。

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

### 2.4.9（msc-pic 分支）

- **裸链接不再发紫** — 小结运行记录里的 slug、近重复对、搜索结果这类没有 class 的 `<a>` 从来没被 CSS 覆盖过，于是直接吃到浏览器的蓝/紫默认色。现在 `app.css` 给 `a`/`a:visited` 定死中性灰 + 发丝下划线（悬停只是下划线加深），带 class 的链接（导航、按钮、logo）仍走自己的样式。`test_bare_links_stay_greyscale` 守住这条底线。

### 2.4.8（msc-pic 分支）

- **全站一个字号** — 原来 10/11/12/13/14/15/16/18/24/28px 十档收敛成 `--fs: 13px` 一档（`--fs-micro` 只给页首 wordmark，正好是小字号的一半）；层级改由字重、大小写与发丝边框承担，`font-size` 在 html/js 里也不再内联出现。图谱画布的节点/边标签同样统一到这一个尺寸（`GRAPH_FONT_SIZE`）。
- **只留黑白灰** — 所有色值改成严格 R=G=B 的中性灰（页面 #0b0b0b → 文字 #f4f4f4），靛蓝主色/绿/琥珀/红/紫与五档分类色、优先级色全部换成灰阶梯度：健康=安静灰，故障=最亮灰并配边框；删除类按钮不再是红的，改成描边、悬停反白；匹配类型（语义/关键词/双命中）改用边框样式区分，热力图改成灰度强度梯度。字体本身不变（Inter/系统等宽栈照旧）。
- **闸门** — `test_ui_ships_only_black_white_and_grey` 扫 css/js/html 里每个 `#hex`/`rgb(a)()` 字面量并要求三通道相等；`test_one_font_size_for_the_whole_ui` 要求 app.css 里 `font-size` 只出现 `var(--fs)`/`var(--fs-micro)`，且 html/js 不含任何 `font-size`。

### 2.4.7（msc-pic 分支）

- **Web UI 去表情** — 导航/按钮/标题里的 emoji 全部清掉，换成一套线性描边图标（`index.html` 顶部内联 `<symbol>` sprite，`<svg class="icon"><use href="#i-name"/></svg>`，`stroke: currentColor`，随文字颜色走，与 192.168.29.50:8080 那套控制台的做法一致）。新增图标只在 sprite 里加一个 `<symbol>`。
- **状态改成色块文字** — 探针结果与自检里的 ✅/❌/⚠️/✓ 改成 `.status-ok` / `.status-warn` / `.status-bad`（绿/琥珀/红），空状态用 `#i-alert` / `#i-inbox` / `#i-link` 线性图标，待处理缩略图的 ⏳ 改成 CSS 旋转弧 `.pic-spin`（`prefers-reduced-motion` 下静止）。
- **同步指示灯修复** — 原来 `updateSyncStatus` 写的是 `sync-syncing`/`sync-error`，CSS 里却是 `.syncing`/`.error`：同步失败时灯一直是绿的。改 `.is-ready`/`.is-syncing`/`.is-error`，灯也从文本 `●` 换成真正的 CSS 圆点（同步中呼吸），并把三对类名写进测试防止再错位。
- **回归闸门** — `test_no_emoji_anywhere_in_the_ui` 扫 html/js/css（emoji、杂项符号、几何图形、变体选择符四段码位；`←` `→` 这类排版箭头放行），`test_every_icon_reference_resolves_to_a_symbol` 保证每个 `<use href="#…">` 都有对应 `<symbol>`（漏定义只会渲染成空白方块，正是过去 id 打错那类静默故障）。

### 2.4.6（msc-pic 分支）

- **照片页翻页修复** — `picLoadMore` 自写下起就没人调用，画廊实际只能看到前 200 张（2.4.6 起 `#pic-sentinel` 挂 IntersectionObserver 滚动续页，并显示「已显示全部 N 张」/载入失败提示）。
- **摄入队列索引** — 启动自检新增 `ensure_pic_indexes()`：`(state, archived, missing, id)` 与 `(source_id, state, archived, missing)` 两条，把 worker 每 tick 的三段队列查询从全表扫描变成索引 seek（`EXPLAIN QUERY PLAN` 有测试守着）。
- **接口查询合并** — `/pic/status` 与 `/pic/sources` 的每源计数改为一条 `GROUP BY source_id, state`；调参一次读表；缺失/回收站计数合并成一条；`/pic/items` 的副本分组只查本页 hash（原来每次画廊请求都对全表 GROUP BY，而 UI 每 5 秒就在轮询）；彻底删除预取行与源（原来每张照片 2 次点查）。
- **目录下拉免重扫** — `/pic/folders` 要遍历该源全部 `rel_path` 才能拼出目录树；前端按「源 + 最近一次完成扫描 + 行数」做指纹缓存，切筛选/切视图不再重复拉，只有该源真的变了才重取。
- **scan 少一半语句** — 首轮摄入的新文件不再跟一条 touch-UPDATE（INSERT 里显式写 `missing=0/last_seen_at`，绕过 ORM 默认值的坑由 `ensure_pic_defaults` 同源说明）；缩略图尺寸从每张一次查表提到批首。
- **照片页 UI** — 相册/缺失/回收站改成三段标签条；状态行改彩色胶囊（含「正在扫描」呼吸点）；4:3 等比格子 + 骨架微光占位 + 缩略图淡入；悬停出选择框、选中描边；批量操作条改悬浮胶囊不再顶动格子；空视图按当前视图给出原因；月分组头 sticky；详情弹窗左右分栏（大图 + 描述/标签胶囊/字段表），`←`/`→` 在屏内照片间走动、`Esc` 关闭、原图↔缩略图原位切换、新增下载原图。
- **照片源卡片** — 设置页每个源一张卡（来源类型徽标 + 地址 + 状态/队列胶囊 + 错误行），不再是一行流水文本。

### 2.4.0（msc-pic 分支）

- **照片源卡片** — Web UI 添加/编辑/暂停/删除照片源：本机目录（白名单 `MSC_PIC_ROOTS` 内，浏览点选免手敲）、SMB（smbprotocol，NTLM/访客）、WebDAV（httpx PROPFIND）。凭证掩码与模型卡同策略。
- **渐进摄入 worker** — `pic_worker` 15s tick：scan（时间预算 + 游标续传）→ 缩略图（webp 512px，逐张落库）→ 中文描述（`vision` 卡默认批量 1，GPU 与 chat 共享）→ 描述嵌入（复用 embed 卡）。可打断可续传：kill 容器不丢进度；瞬时故障 retry_at 指数退避，坏文件标 failed 不堵队。相同 content_hash 的连拍/副本直接继承描述（连拍只花一次模型调用），画廊带 ⧉ 副本角标。
- **照片页** — 月分组缩略图墙（占位格 → 缩略图 → 描述逐态可见，5s 轮询），语义搜索（描述向量 + 关键词 RRF，与记忆语料完全隔离），详情弹窗（描述/EXIF/来源/查看原图流式透传），回收站（软删/恢复/彻底删除需输入 DELETE + `confirm=true` 双闸，只此一处会删磁盘原图）。
- **新模型卡 purpose** — `vision`：探针真发一张 16×16 PNG，拒图的文本模型当场暴露而不是 caption 时才失败。
- **运维** — `MSC_PIC_ENABLED=false` 关照片全链路；`/health` 带 `photos`/`photos_pending`；启动自检修剪坏/孤儿照片向量并清扫孤儿缩略图；docker-compose 需挂载照片根（`/home/astro/photos`，:rw 仅彻底删除功能需要）。

### 2.3.1（rag 分支）

- **回响评注独立化** — 新增 `echo` purpose 模型卡（设置页第四张 provider 卡）：评注可指定与 chat 不同的模型 API，未配置时自动回退 chat 卡，`POST /api/v1/model/test?purpose=echo` 走 chat 式探针。
- **评注存 md** — `POST /rag/echo` 新增 `save`（默认关，Web UI 默认开）与 `save_path`（base 内相对文件名，默认 `{slug}-回响`；绝对路径/`..`/slug 冲突 422）。保存 base 默认＝记忆目录，下个同步周期评注被 sync 吸收成普通记忆（category reference、tag echo）；`MSC_ECHO_DIR` 可改 base。`_save_echo` 复用 `md_writer` 格式。
- **搜索页回响按钮** — 每条搜索结果悬停出现 🪞 回响按钮，一键跳洞察页评注面板并生成（对齐 n1 sidecar 的交互）。

### 2.3.0（rag 分支）

- **混合搜索** — `GET /api/v1/search?mode=keyword|semantic|hybrid`（默认 keyword，行为与 2.2.0 完全一致）。语义路由是 embeddings 余弦，hybrid 用 Reciprocal Rank Fusion 融合两路，结果带 `match` 徽标（关键词/语义/两者）。未配置 embed provider 时一切自动退回关键词，embed 挂了也在 `X-Semantic-Error` 头里说明原因。
- **多模型卡** — `model_configs` 新增 `purpose`（chat/embed/rerank/whisper），每类一张卡，`GET /api/v1/model/cards`、`PUT /api/v1/model?purpose=embed`、测试探针按 purpose 分派（embed 探针自动回报维度并存 `probed_dim`）。老库自动补列、把现有卡回填成 chat，并加 purpose 唯一索引。
- **向量索引** — 新表 `embeddings`（按 (memory_id, model) 存 float32，换模型写新行、切回零成本）、`rag_dirty` 队列（API 写/sync 拉/小结写入统一经 `write_through` 打点）、`rag_cache`、`rag_settings`。`rag_worker` 每 15s 消费一批 16 条，provider 离线保留队列。检索为 numpy 全扫描（10k×1024 维 ≈0.05s），`prune_broken_embeddings` 启动时清畸形行。
- **语音记忆页** — 浏览器 PCM → 16-bit WAV → whisper 转写 + cos>0.6 查重，一键新建或追加到相似记忆；http 访问自动降级为文件上传。
- **洞察页** — 记忆健康（cos>0.92 近重复对、>90 天 temporary、归档统计，只出建议）、今日奇遇（700-900 字长文，按日缓存）、记忆评注（只读不回写）、创建热力图。
- **运维** — `MSC_RAG_ENABLED=false` 一键回到 2.2.0 行为；`POST /rag/reindex`、`DELETE /rag/vectors?model=旧模型`、`POST /rag/import`（sidecar index.db 向量 base64 直灌免重嵌）；备份恢复新增可选 `embeddings` 表（旧备份缺表也能恢复）；`/health` 带向量与队列计数；依赖新增 `numpy>=1.26`。llama.cpp 嵌入需 `-ub 4096`。
- 测试 94 个（新增 `backend/tests/test_rag.py` 20 项：卡片隔离、队列/worker/断连重试、语义召回与降级、kill switch、导入/清理/调参、语音、洞察；迁移测试补 purpose 回填与畸形向量清理）。

### 2.2.0

- **模型卡片** — 设置页新增 🤖 卡片：API 地址 / 模型名 / 密钥 / 温度 / 最大输出 / 超时 / 输入上限 / 额外参数 JSON / 自定义提示词，全部存数据库，保存即生效；密钥只回显掩码，`api_key` 省略＝保留、传空＝清除。
- **记忆小结** — 记忆页多选 → 生成小结（支持预览），或每天定点自动当日小结（带时区、可补跑前一天）。小结是一条普通记忆，带 `summary`/`daily` 标签与 `summarizes` 关联。
- **原记忆可选软删除** — 弹窗一个勾选框；toast 上「恢复原始记忆」按关联撤销软删除。
- **失败不留痕** — 模型报错时不写任何记忆，失败记进 `summary_runs` 并在卡片上显示；每天只尝试一次自动小结，避免反复轰炸离线模型。
- 新表 `model_configs` / `summary_runs`（`create_all` + 启动补列自动建）；`memory_relations` 的 CHECK 增加 `summarizes`（沿用原地重建表的自愈路径）。
- 版本号统一到 2.2.0（后端 / CLI / 两个 `pyproject.toml` / UI 资源 `?v=`），此前 CLI 与 pyproject 还停在 2.1.1。
- 测试 73 个（新增 `backend/tests/test_summarize.py`：卡片掩码与校验、dry-run 零写入、模型失败回滚、时区窗口、输入裁剪、自动排期）。

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

## License

MIT — 见 [LICENSE](LICENSE)。可自由使用、修改、分发、商用，保留原版权声明即可。
