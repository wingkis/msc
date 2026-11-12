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
- **照片 RAG（msc-pic，2.4.0）** — 照片源卡片（本机目录 / SMB / WebDAV）渐进摄入：先占位、逐张出 512px 缩略图、多模态模型（`vision` 卡片）写中文描述、描述进 bge-m3 向量。照片页按月浏览缩略图墙，语义搜索命中缩略图，勾选看原图（流式透传，原图不入库）；回收站软删可恢复，「彻底删除」带确认才动磁盘原图。扫描/摄入随时可暂停、中断自动续传（游标 + 状态机队列），详见 `.env.example` 的 `MSC_PIC_*` 与 `MSC_VISION_*`。读不了的原图停在「失败」标签里，可以重试，也可以「忽略」——忽略只让 worker 不再尝试那张，原图与失败原因都留着，随时可撤销

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
| GET | `/memories` | 列表，支持 `category/priority/tag/branch/since/q/include_archived/include_invalidated/order/limit/offset`；响应头 `X-Contradicted-Hidden` 报被隐藏条数 |
| GET | `/memories/{slug}` | 单条；slug、精确名称或唯一片段都行 |
| GET | `/memories/preflight` | **复核 refs**（只报结论、绝不回写；`limit/budget_ms/category/priority/tag`） |
| POST | `/memories` | 新建（`related_slugs` 顺便建关联） |
| PUT | `/memories/{slug}` | upsert（`?upsert=false` 则不存在时 404） |
| DELETE | `/memories/{slug}` | 软删除；`?hard=true` 彻底删除 |
| POST | `/memories/{slug}/restore` | 撤销软删除 |
| POST | `/memories/batch` | 批量，逐条返回结果 |
| GET/POST | `/memories/{slug}/relate` | 查/建关联 |
| POST | `/memories/{slug}/invalidate` | **记下"复核推翻了我"**：`{"reason":"…","status":"contradicted｜stale","superseded_by":"另一条slug"}` |
| POST | `/memories/{slug}/verify` | 复核结论：`{"verdict":"ok｜stale｜contradicted","reason":"…"}`；`ok` 是唯一回到 active 的路 |
| GET | `/memories/{slug}/versions` | 编辑历史（旧版路径 `/memories/versions/{slug}` 仍可用） |
| POST | `/memories/versions/{slug}/restore/{n}` | 回到第 n 版 |
| GET | `/search?q=...` | 全文 + 过滤，带 `X-Total-Count`；`X-Search-Status`＝`ok｜no_match｜degraded｜error`（索引坏了与没命中不是一回事），`include_invalidated=true` 才召回被推翻的 |
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

## 记忆出处与自我修正

记忆是**历史观察**，不是已验证事实。这一节让"复核过、被推翻、已纠正"变成库里的一等事实，而不是一句注入时的免责声明。

**出处 refs** — 每条记忆可挂若干 `<kind>:<target>`，存的是"后来人能廉价复核的那部分"：

| 前缀 | 含义 | 谁能验 |
------|------|--------|
| `git:<sha>` | 某仓库的提交/对象 | 客户端（服务端探不了） |
| `file:<path>` | 绝对路径 | 服务端＋客户端（各自本机视角） |
| `url:<endpoint>` | 服务/接口 | 服务端＋客户端 |
| `mem:<slug>` | 另一条记忆还在 | 服务端 |
| `cmd:<line>` | 当时执行的命令 | **永不执行**，只作记录 |
| `host:<name>` | 结论出自哪台机器 | 客户端比对当前主机 |

前缀写错直接 422 并列出允许值——**一个验不了的出处比没有出处更糟**，因为它读起来像担保。

**三态 status** — `active｜stale｜contradicted`。`contradicted` 的记忆：

- 从 `GET /memories` 与 `/search` 默认结果里消失，但仍可按 slug 取，`?include_invalidated=true` 可召回；
- 隐藏条数写进响应头 `X-Contradicted-Hidden`，**货架变窄从不静默**；
- 永不删除——错的信念也是证据，`invalidated_at/reason` 记下是谁在什么时候把它推翻的；
- 指定 `superseded_by` 会建 `supersedes` 关系边（复用已有关系类型，不新增字段）。

**复位只有复核** — `POST /{slug}/verify {"verdict":"ok"}` 是唯一回到 `active` 的路；**改正文不会解除推翻**。轮次通道每轮 PUT 全文，若改写即可复位，那所有被推翻的信念下一轮就洗白了。恢复是"复核"这个动作的结果，不是编辑的结果。

**复核分两头** — `GET /memories/preflight` 探服务端够得着的 `url:/file:/mem:`，`git:/cmd:/host:` 老实返回 `unchecked`（只有写下它的机器知道答案）。它只报结论不回写：一个会改库的 GET，也就是一个会在两个 agent 判断不一致时污染库的 GET。客户端用 `cli/msc-verify.sh` 补齐另一半：

```bash
MSC_URL=http://192.168.29.50:8000 sh cli/msc-verify.sh --limit 20        # dry-run
MSC_URL=http://192.168.29.50:8000 sh cli/msc-verify.sh --yes <slug>      # 结论写回库
```

默认 dry-run。HTTP 语义也一并按"能不能算推翻"区分：连不上/404 才算 `missing`，405/501 会换 GET 再问一次，401/403/5xx 只算 `unchecked`——把"不接受这个动词"读成"端点没了"，探针就会自己造出过时来。

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

## 照片摄入调参

「设置 → 照片源 → 摄入调参」六个框（`PUT /api/v1/pic/settings`，存 DB，改完即生效，不用重建）。
括号里是默认值／本机当前值。

| 参数 | 意思 | 调大的代价 |
|------|------|-----------|
| 每轮扫描秒数 `scan_seconds`（8／15） | 一次目录 walk 的时间预算；到点停在游标处，下轮从断点续走（1–300） | 那一轮占用更久，首航大目录入库更快 |
| 缩略图批量 `thumb_batch`（16／30） | 每轮为多少张占位图出 webp（1–200） | 纯 CPU＋磁盘活，瞬时占用高 |
| 重扫间隔（小时）`rescan_hours`（24／24） | 隔多久把整个源完整重走一遍，用来发现新增与已消失的原图（1–8760） | 太小＝反复走目录的网络/CPU 开销 |
| 缩略图尺寸 px `thumb_px`（本机 512） | webp 长边（64–2048）；改动会让全部照片重出图（`?v=` 变，缓存自动失效） | 大＝占盘、画廊慢；小＝看不清 |
| 全开时限（秒/轮）`burst_seconds`（300） | 按下「全开连续描述」后，每一轮让视觉模型连续跑多少秒（15–900）；跑满只歇 3 秒再续，直到队列空或关掉 | 视觉 GPU 被连续占用，同机聊天会慢 |
| 描述并发 `caption_workers`（1） | 一批里同时向视觉模型发几个请求（1–64，2.5.15 起）。只有服务端有多个槽位才更快；单槽上多余请求只是在那边排队，还会把每张的等待拉长 | 超时风险（卡片 `timeout_seconds`）与 GPU 争用 |

另外两个只能走 API：`caption_batch`（1–32，默认 1）＝不启用全开时每轮描述几张 —— 默认 1 是因为视觉卡通常和聊天共用一块 GPU；
`embed_batch`（1–64，默认 8）＝每轮索引多少条描述。两个开关：`caption_paused`（暂停图像描述）压过
`caption_burst`（全开连续描述）。`GET /api/v1/pic/status` 的 `settings` 会回读全部调参，UI 每 5 秒跟随。

## 视觉描述模型（vision 卡片）

「设置 → 视觉描述模型」就是描述照片那张卡：API 地址 / 模型名 / 密钥 / 最大输出 tokens / 超时 / 额外参数 JSON / 启用开关，
存 DB，保存即生效（`MSC_VISION_*` 只是首次启动的种子）。和其它卡同一个接口：`GET /api/v1/model?purpose=vision`、
`PUT /api/v1/model?purpose=vision`、`POST /api/v1/model/test?purpose=vision`。

- 必须是**能吃图片**的 OpenAI 兼容 `/v1/chat/completions`：llama.cpp 要带 `--mmproj`（只挂纯文本模型时文字测试会过、描述会全失败），
  所以这张卡的「测试」真的发一张 16×16 红图，并把 `/v1/models` 的模型名灌进「模型名」下拉——填错模型 id 是这里最常见的坑。
- 地址/模型名留空或未启用时：扫描与缩略图照常，`thumb_ready` 原地排队，不做无谓重试；卡片填好后自动补发积压。
- 状态可见处：卡片状态行（上次失败原因／就绪）、照片页状态条的 `视觉 <模型名>` 徽标（未填且队列非空时变成 `视觉模型未填 API`）。
- 节流与并发不在这张卡（在「照片源」卡片）；单张描述变慢先调 `max_tokens`（思考型模型会把预算吃在思考上）与 `timeout_seconds`，
  要关思考就在「额外请求参数 JSON」里填，例如 `{"reasoning": {"effort": "low"}}`。

## 常见故障

| 现象 | 处理 |
|------|------|
| UI 没变化 | 后端在镜像里：`make restart`；浏览器有 `?v=` 版本号，硬刷新即可 |
| 搜索没结果 | `msc sync reindex`（或 `POST /api/v1/sync/reindex`），再看 `/api/v1/health` 的 `search_index` |
| 磁盘上的文件没进库 | `msc sync status` 看 `only_in_files`，然后 `msc sync pull` |
| 端口不通 | `docker compose ps`、`docker compose logs msc-api` |
| 图谱页只显示列表 | vis-network 走 CDN，内网不通时自动退化为关联列表（功能不丢） |
| `.msc_index.json` 里有已删除条目 | `msc sync reindex`（同时重建 FTS 和文件索引） |
| 照片列表有个红色的「失败 N」 | 点那个数字（或「失败」标签）看每张的原因。读不了的就别反复试：勾选 →「忽略」，它退出计数、理由仍留着，之后能在「已忽略」里「重新排队」。瞬时故障（NAS 掉线、模型没起）不用管，自动退避重试，最长 5 分钟一轮 |
| 某张 `.heic` 报 `cannot identify image file` | 先看文件头：`ftypmp42` 是改名叫 `.heic` 的 mp4 视频（不是照片）；真 HEIC 则多半是当前 libheif 不支持的新派生属性（如 `could not parse item property 'rloc'`），升级 pillow-heif/libheif 后重试即可。库里其余 HEIC 能正常描述就说明解码器本身没问题 |

## 更新记录

### 2.6.3（main）：Web UI 里能推翻、能复核

出处与三态自 2.6.0 起只在 API 里活着：线上 **113 条记忆 contradicted = 0**，不是库全对，是没人能触发 —— UI 不显示 `refs`/`status`，没有"显示被推翻的"开关，也没有复核按钮。这一版把那扇门开在页面上。

- **详情面板的出处区块** — 列出全部 `refs`（`url:`/`mem:` 可点：前者开新标签，后者直接跳到那条记忆），`status` 不是 active 时顶部一条横幅写明原因、时间、上次复核时间。
- **复核出处** — 按条复核：`GET /memories/preflight` 新增 `slug=` 过滤（为一条记忆扫全库是错的工具），结论逐条显示「成立 / 已失效 / 查不了」。判定词用文字不用颜色：本 UI 只有灰阶，而"绿色"在这里不构成证据。
- **推翻 / 复核为成立** — 推翻是一张就地展开的表单（原因必填、可填取代它的那条 slug、`contradicted｜stale` 二选一）：把原因写进第二个弹层，等于赌用户不会关掉它。复位只有一个按钮，走 `POST /{slug}/verify {"verdict":"ok"}`。
- **列表侧** — 「显示被推翻」开关（`include_invalidated`）、卡片上的状态徽章与「出处 N」计数，被推翻的不再伪装成一条活事实。
- **编辑表单能挂出处** — 新增 refs 输入框；只在内容与打开时不同才发出去。PUT 不传 `refs` 的语义是"不动"，把空框读成"清空全部出处"会一保存就洗掉历史。
- 版本号统一到 2.6.3。

### 2.6.2（main）：让写的 agent 自动知道要挂出处

- **help/llms.txt 教到能照抄** — 自 2.6.0 起 `CONVENTIONS` 里写了 `refs` 是什么，但 `POST /memories` 的示例 JSON 里没有那个字段：只 curl API 的 agent 读得懂概念，却不知道往哪个键写。现在 WRITE/EXAMPLES 两处都带上 `"refs":[...]` 的写法，并加一条命令式约定（"写记忆至少挂一个出处；PUT 传了是替换、不传是不动"）与一条复核指引（服务端只判得着 `url:/file:/mem:`，`git:/cmd:/host:` 的回 `unchecked`，那一半由客户端 `cli/msc-verify.sh` 补）。
- **`X-Provenance-Hint`** — `POST /memories` 与 `PUT /memories/{slug}`（含 upsert 新建那条路径）存下了一条没有出处的记忆时，响应头直接给出可照抄的取值形式。刻意是提示不是拒绝：很多记忆本来就没有可挂的东西，硬挡只会把 agent 推向编一个出处 —— 那比不挂更坏。
- 库侧另有一条 critical 规则记忆 `msc-refs-writing-rule`（`SessionStart` hook 会把 critical 注入每个 agent 眼前），把上面的口径讲成一句话，与 `记忆命名原则` 互为引用。

### 2.6.1（main）：把出处真的用起来

- **`cli/msc-refs-backfill.sh`** — 老记忆的出处一直写在正文里（首行的 host 头、正文中的提交号、路径、网址），可读，但没法复核：preflight 只认解析得出的把手。这个工具把当场确认得了的那部分抬进 `refs`，口径与探针一致 —— `host:` 取首行并去掉 `.local`（不去就永远比不中 `hostname -s`）；`git:` 必须 `git cat-file -e <sha>^{commit}` 真解析成提交，md5、日期这种长得像十六进制的自己就被刷掉；`file:` 必须存在且是项目/配置级路径（`/bin/sh`、`/dev/null` 几乎每条正文里都有，那不是出处）；`url:` 必须答话且不是 404（挂一个已经死掉的端点等于当场把这条记忆判死）；`localhost` 网址一律不挂 —— 那只对写它的那台机器成立，是 `host:` 的活。默认 dry-run，`--yes` 才写；已有 refs 的一条不碰（要覆盖加 `--force`）。
- **现网回填** — 78 条挂上 236 个出处，另外 31 条在这台机器上确认不了，就一条都不挂。回填只是元数据动作：PUT 顺手把 `updated_at` 推到当下，111 条集体"刚刚更新"会把注入侧的「最近更新」和按 `updated_at` 的折叠判定一起带歪，所以按写入前的快照把时间戳还原。
- **容器看不见的路径不再算「没了」** — 回填后 preflight 把 78 条里的 27 条判成 `contradicted`，理由清一色 `file:/home/astro/msc … path is gone`：路径是在主机上确认的，探测却跑在容器里，而源码树根本没挂载进这个容器。现在 `file:` 只有落在本服务真正挂载的目录（数据库 / 备份 / 两个记忆目录 / 照片根 / `MSC_REF_ROOTS`）才允许判 `missing`，其余一律 `unchecked`。判「没了」需要资格，正如判「端点消失」需要 404 而不是 405。`MSC_REF_ROOTS` 只填容器真挂载的前缀：把主机路径填进去，等于把这类误判重新请回来。

### 2.6.0（main）：记忆出处与自我修正

- **出处与状态** — `memories` 新增 `refs/status/invalidated_at/invalidated_reason/verified_at` 五列（`add_missing_columns` 自动补列；`ensure_provenance` 回填，因为 `ALTER TABLE ADD COLUMN` 在老库留下一片 NULL，而 NULL 在 SQL 里连 `!=` 都不满足，一条 `status != 'contradicted'` 能把升级库里的记忆**全部**变没）。`status` 刻意可空，与真实升级路径一致，读侧 NULL 安全。取代关系复用已有 `supersedes` 边，不新造字段。
- **推翻与复位** — `POST /{slug}/invalidate`、`POST /{slug}/verify`；推翻是幂等的（同一结论重复上报不刷新时间戳），默认从列表/搜索消失但 `X-Contradicted-Hidden` 报数，`verify ok` 是唯一复位路径并顺带清掉那条 `supersedes` 边（证据留在版本快照里）。
- **搜索不再说谎** — `_keyword_candidates` 以前用 `except Exception: candidates = []` 把 FTS 故障吞成"没找到"，于是**索引坏了与库里真没有**从外面完全同形（这正是 `search?q=codex` 返回 `hits: 0` 时被误读的东西）。现在拆成 `_keyword_candidates_diag` 报 `ok｜degraded｜error`，经 `X-Search-Status`/`X-Search-Error` 出给调用方；`api/rag.py` 用的旧签名保持原样。
- **复核通道** — `GET /memories/preflight`（只读、预算受控 `budget_ms`，超预算的 ref 记 `unchecked` 而不是猜）＋ `cli/msc-verify.sh`（POSIX sh，补客户端才验得了的 `git:/host:`；默认 dry-run，`--yes` 写回）。
- **自描述** — `/api/v1/help`、`/llms.txt` 与 `/meta` 的 `vocab` 同步补上 `status`/`ref_kind`，`/meta` 额外报 `contradicted` 计数：一个从不积累矛盾的库，不是恰好正确的库，是没人复核的库。
- 迁移在**线上库副本**（107 条记忆）上演练过：五列补齐、214 个值回填、默认过滤器一条都不藏。测试 192 个（2.5.16 的 174 + provenance 15 + 本轮 RAG 三条腿 3）（新增 `backend/tests/test_provenance.py` 15 项，含升级库 NULL 行、405 不算推翻、编辑不解推翻、preflight 只读、以及模块级自清理以免污染别人的计数断言）。
- **并入 main 时的接缝** — 与 2.5.16 撞在三处：README 更新记录；`memories.py`（pic 的 `DELETE /graph/auto-links` 与这里的 provenance 块落在同一位置，两块都保留）；`schemas`（`Dict` 与 `json`/`field_validator` 取并集）。`run_maintenance()` 现在同时跑 `ensure_provenance()` 与 `ensure_pic_defaults()`。
- **RAG 的三条腿一起堵上** — 已推翻的记忆向量还在、FTS 行还在，所以 `/rag/search` 照样把它排进结果，语音转写的「相似」候选会提议往它身上追加新事实，daily-pick 还会拿它取材。新增 `rag._contradicted_ids()`：一处定义（`status = 'contradicted'` 这个方向天然 NULL 安全 —— 老库的 NULL 行不匹配，不会被误藏）、三处调用；过滤在 RRF 融合之前做，掉一条不多占一个坑，响应多带一个 `contradicted_hidden`。`/rag/health-report` 与图谱刻意不过滤：那是库的体检表，不是往会话里递事实的地方。
- **`msc-verify.sh` 的 host: 分支补回一个制表符** — 这个脚本按「verdict + 制表符 + detail」解析自己的输出，而 host: 那条分支把转义写成了 `\written`（`\t` 少了 t）：跨主机的出处回来是一整坨，既不算 ok 也不算 unchecked，「几条属于别的机器」永远报 0。现在 `probe_one` 的分支由一条真跑脚本的断言钉住（`test_the_client_probe_answers_verdict_tab_detail`）。

### 2.5.16（msc-pic 分支）

- **失败照片第一次有出路** — 现场：16180 张照片里 9 张 `failed`，其中 6 张是手机全景图撞上 Pillow 默认的
  178,956,970 像素解压炸弹上限（实测 201/271 Mpx），3 张真读不了。worker 从不回捞 `failed`，
  所以这 9 张既修不了也关不掉，只能在状态条上顶着一个红色数字。
- **像素上限按格式放行** — 新增 `pic_index.open_image()`，照片管线里每一次 `Image.open()` 都过它。
  JPEG/MPO 的缩略图走 `draft()`（`thumbnail()` 先把 271 Mpx 全景图降到 1/8 DCT 尺度，解码只用约 13 MB 内存），
  上限放到 6 亿像素；PNG/TIFF/HEIF 没有这条路径、一次解码整张位图，保留 Pillow 原本的 1.79 亿。
  `Image.MAX_IMAGE_PIXELS` 必须先抬到最松一档，否则 Pillow 在我们看出格式之前就 raise。越界报的话是
  「图片过大：271 百万像素，PNG 上限 179 百万像素」——worker 的 except 会把它原样写进 `last_error` 给人看。
- **忽略（`pic_items.ignored`）** — 批量动作多一个 `ignore`：只接受 `state='failed'` 的行，`state` 和
  `last_error` 原样留着（点进去仍看得到为什么失败），但退出队列、退出相册、退出 `/pic/search`；
  `retry` 连 flag 一起清回来，完全可逆。做成旁挂标志而不是新增 state：`state` 上有 CHECK 约束，
  而且「忽略」本质是一个可撤销的用户意愿，不是流水线的第 6 个阶段。
  配套改动：`state_counts` / `source_counts` / `source_counts_all` / `retry_backlog` / 相册默认过滤 /
  `/pic/search` 关键词腿一律排除 ignored，`lifecycle_counts` 多返回 `ignored`，`/pic/items` 新增
  `view=failed` 与 `view=ignored`（前者还能重试，后者是反悔的地方）。
- **退避封顶 5 分钟** — `_backoff` 原来是 1→32 分钟翻倍：端点两分钟前就回来了，照片还要在 UI 里挂半小时才
  重试，看起来跟卡死没区别。现在 `BACKOFF_CAP_SECONDS = 300`；描述/索引成功时顺手清 `error_count`，
  一次硬重试成功之后从 1 分钟重新起步，而不是带着旧的连败次数直接落到上限。
- **队列里的等待要说出来** — `/pic/status` 新增 `waiting_retry` / `retry_in_seconds`（`retry_backlog()`）：
  停在退避里的行数与最近一次醒来的时间。没有这两个数，一条刚从模型故障里恢复的健康队列和一条真卡死的队列长得一模一样。
- **UI** — 照片页多「失败」「已忽略」两个标签；批量条在失败类视图里给「重新排队／忽略」，格子和详情弹窗给同款
  单张按钮（弹窗里原来只有一行灰字提示，现在能直接动手）；状态条上「失败 N」「已忽略 N」两个数字本身可点，
  直接进对应视图；「待描述」后面会跟一句「等模型 3 分钟」这种人话。
- **两个「隐形照片」的坑** — ① scan 发现同路径换了新字节会把行退回 `discovered`，但没清 `ignored`，于是这张图
  既不在相册（相册排除 ignored）也不在 `view=ignored`（那里要求 `state='failed'`），从所有视图里消失；
  ② `ALTER TABLE ADD COLUMN` 给老行留 NULL，而每个过滤条件写的都是 `ignored = 0` → 16182 行会集体隐身，
  所以 `ensure_pic_defaults()` 一并回填。两个都和当年 `missing` 那个坑同源：**新增列必须同时想清楚 NULL 落在哪一边**。
- **线上实测** — 6 张超大 JPEG 重新排队后全部走通（最大 17068×15872＝271 Mpx，缩略图 200 `image/webp`，
  中文描述正常）：`embedded 16171 → 16177`。剩 3 张已忽略：`3743` 的文件头是 `ftypmp42`，是个改名叫 `.heic` 的
  mp4 视频；`4388`/`4441` 是真 HEIC，但 libheif 1.23.6 报 `could not parse item property 'rloc'`。
  库里 10169 张 HEIC 全部 `embedded`，所以不是解码器坏了。
- 测试 174 个（新增分格式像素上限、退避封顶、退避中的队列深度、忽略与找回、换文件解忽略五组断言）。

### 2.5.15（msc-pic 分支）

- **回响按钮补齐** — `renderMemoryCard` 从来没渲染过回响按钮（只有搜索结果卡片有），记忆页因此完全看不到入口；
  现在未归档卡片补上，与搜索卡片同款。`.echo-inline-btn` 原本 `opacity: 0`、只在 hover 时才出现（触屏根本没有
  hover），看起来就像没有这个按钮 —— 改成常显低对比 0.55、hover 提到 1。详情弹窗底部也加「回响」：弹窗里放不下
  1–2 分钟的模型调用，点了先关弹窗，再把 slug 交给洞察页的评注面板执行。归档记忆不给按钮
  （`_resolve_memory` 默认跳过归档行，给了也必然 404）。顺带：搜索卡片的 `btn-small` 是 CSS 里不存在的类 → `btn-sm`；
  `#detail-content .modal-footer` 允许换行，窄屏五个按钮不再挤出边界。
- **描述并发上限 8 → 64** — `caption_workers` 原本三处硬编码 `le=8`/`max=8`，多槽位的视觉服务用不满；现在 1–64 可调，
  设置页的说明文字同步。挪上限要连测试一起钉：原来测试钉着「填 9 该被拒」，改成拒 65 并接受 1 和 64，
  区间两头都锁住，下次再挪会立刻报错而不是悄悄漂回去。这两条没各自占版本号。

### 2.5.14（msc-pic 分支）

- **视觉描述模型也有卡片了** — 描述照片的 `vision` 卡后端一直有（`GET /api/v1/model/cards` 里就在，`PUT /api/v1/model?purpose=vision` 也能写），
  但设置页只渲染 chat/embed/rerank/whisper/echo 五张：想换"用哪个模型看图"只能手敲 curl 或改 `.env` 重建容器。现在设置页多一张
  **视觉描述模型（照片 caption）** 卡片，走同一套 fill/save/test 机器（`loadRagCards()` 的 purpose 列表补上 `vision`，
  每张卡的数字字段改成 `CARD_NUMBER_FIELDS` 按 purpose 映射，不再是 embed 的特例 if）：API 地址、模型名（带 `/v1/models` 下拉）、
  API Key（掩码／留空＝不改／勾选清除）、最大输出 tokens（128–16384）、超时（5–1800）、额外请求参数 JSON、启用、清除密钥。
  「测试（发一张红图）」沿用后端那个真发 16×16 PNG 的探针，通过后状态行显示模型答了什么与延迟，顺带把该服务的模型名灌进下拉
  （echo 卡同样受益：它的探针也是 chat 形的）。输入框的 `min`/`max` 与 schema 一致，越界先夹取而不是弹 422（2.5.12 那条教训）。
- **照片页会说出用哪个模型描述** — `/pic/status` 本来就带 `providers.vision`，现在照片页状态条多一个 `视觉 <模型名>` 徽标；
  地址/模型名没填而队列里还有 `thumb_ready` 时改成红色 `视觉模型未填 API — 设置 → 视觉描述模型`，不再让"队列只涨不动"看起来像坏了。
- **「照片源」卡片写明描述模型在哪改** — 两个开关那段 hint 上面补一行：模型 API 在下面的卡片里填，保存即生效。
- 测试 169 个（新增 `test_the_vision_model_has_a_card_on_the_settings_page`：卡片九个 id、保存/测试接线、两个数字框的 min/max、
  `CARD_PURPOSES` 含 vision、探针会灌模型名列表并回显回复、照片页两处徽标都在；`test_vision_card_writes_every_field_the_settings_page_sends`：
  卡片要写的七个字段全落库、裸地址自动补 `http://`、越界 422、`/pic/status` 的 `providers.vision.ready` 随卡片空/满翻转）。

### 2.5.13（msc-pic 分支）

- **照片页每张都有删除键** — 相册里想删一张之前只有「勾选 → 底部批量条」这条路，格子本身上没有删除。现在悬停（或选中）格子时，右下角出现回收站图标（和左上角的选择框同一套语法：都在照片上、都悬停才露出；触屏没有 hover，那里常显）。点一下 = `DELETE /api/v1/pic/item/{id}`：记录进回收站，**磁盘原图不动**。toast 带「撤销」直接恢复，之后也能从「回收站」标签整批恢复。回收站/缺失两个标签的格子保持原样（那里已有 恢复·彻底删除 / 保留·清除），语义搜索结果走同一条相册分支，也有删除键。
- **详情弹窗补回「移入回收站」** — 弹窗底栏在 查看原图 / 下载原图 / 关闭 之间多一个删除按钮（`#pic-detail-archive-btn`，同一个软删接口）。删掉当前这张后弹窗不会停在一张已不在墙上的照片上：它把这张从弹窗的走动列表里摘掉、直接走到相邻那张（`←`/`→` 照常），列表空了才关闭。回收站标签里打开的照片不显示这个按钮（已经在站里了）。
- **顺带：弹窗底栏可换行** — 四个按钮在手机宽度下会挤爆 `.modal-footer`（`.pic-modal` 是 `min(1080px, 94vw)`），加 `flex-wrap: wrap`。
- 测试 167 个（新增 `test_every_photo_can_be_trashed_from_the_page`：钉住格子上的 `.pic-del` 只在相册视图出现、弹窗按钮存在并已接线、单张删除走的是软删接口而不是 `picPurge`、彻底删除仍需输入 `DELETE`、`.pic-del` 的 hover 揭示与触屏常显都在 CSS 里）。

### 2.5.12（msc-pic 分支）

- **修：改完调参后设置页"跳歪"** — 保存成功时状态行写的是 `已保存（{…10 个键…}）`，约 200 字的 JSON 塞进 `.sync-controls` 那条 flex 行里，把整行的几何改掉、下面的卡片整体往下顶；六个调参字段用的是 `.form-row` 默认两列轨道。现在状态行自己占一行（`flex: 1 1 100%; min-width: 0; overflow-wrap: anywhere`），措辞改成人话（`已保存：扫描 15 秒/轮 · 缩略图 30 张/轮 · … · 全开 300 秒/轮 · 描述按节奏走`），并把保存后的**服务端实际值**回写进输入框；调参行改 `.form-row.pic-tuning`：≥1000px 三列、600–1000 两列、≤600 单列，轨道一律 `minmax(0, 1fr)`；`.settings-grid` 也补上 `minmax(0, 1fr)` —— 裸 `1fr` 的最小值来自内容，一个长 token 就能把卡片连同页面撑宽（2.5.7 记忆页那个 bug 的同一类）。
- **修：422 只回显 `[object Object]`** — FastAPI 的校验错误 `detail` 是**对象数组**，`new Error(data.detail)` 把它变成 `[object Object]`，所以「描述并发填 16」看起来像页面坏了。新增 `apiErrorText()`：数组拼成 `caption_workers: Input should be less than or equal to 8`，`apiFetch` / `/search` / `apiUpload` 三处统一走它。同时 `savePicSettings()` 按输入框自带的 `min`/`max` 先夹取（越界照样保存并说明"已夹到允许范围"），`重扫间隔` 补 `max=8760` 与后端一致。
- **六个调参的说明写进 UI** — 摄入调参行下面多一段 hint，README「照片摄入调参」同步。
- 测试 166 个（新增 `test_pic_tuning_row_cannot_widen_the_settings_page`：钉住 `.settings-grid` 与调参轨道都是 `minmax(0, 1fr)`、状态行 `min-width:0`＋`flex:1 1 100%`、六个输入框都带 `min`/`max`、保存不再 `JSON.stringify` 回显、`apiErrorText` 在三处生效）。

### 2.5.11（msc-pic 分支）

- **「全开连续描述」按钮（能力全开）** — 描述这一级默认是 `caption_batch=1`：每 15 秒一轮、每轮只描述一张，实测 ~25 秒/张，14131 张待描述要排将近十天。现在「设置 → 照片源」在「暂停图像描述」旁边多一个 **全开连续描述**：`caption_burst=1` 把节奏拆掉——`_captions` 在 `burst_seconds`（默认 300 秒，15–900）的时间预算里连续拉队列直到跑完或时限到，`_embeds` 按同一预算跟着走（不留 captioned 积压），worker 轮间歇从 15 秒降到 `BURST_GAP=3` 秒。
- **并发描述数 `caption_workers`（1–8，默认 1）** — 时限再长也只是"连续"，单张延迟才是主成本。>1 时一批里同时向视觉卡发 N 个请求：`_caption_call` 只读快照值＋自己建 httpx client，不碰 session/ORM，写回和 commit 仍在主线程逐张做。视觉服务只有一个槽位时加并发不会更快（只是在那边排队），默认因此留 1。burst 下 `batch = max(caption_batch, caption_workers)`，否则多出来的线程只会空等。
- **两个开关的关系** — 「暂停」优先于「全开」：paused 时 `_captions` 直接返回，burst 不会绕过它；UI 里按「全开」会顺手把 `caption_paused` 归零（否则原地排队，按钮看起来像坏的）。相册状态行显示 `描述全开 ×N` 或 `描述已暂停`，二者互斥；设置页与相册页谁改都跟随。
- 测试 165 个（新增 `test_caption_burst_drains_the_queue_in_one_pass`：节流时一轮一张、全开时一轮跑穿队列、paused 仍然压过 burst、四个越界值都 422；`test_caption_workers_run_several_vision_calls_at_once`：并发峰值 ≥2、3×0.3 秒的串行假设不成立、每张各自写回）。

### 2.5.10（msc-pic 分支）

- **相册一次载入 2000 张** — 之前每页写死 200（`PIC_PAGE`），1.6 万张要滚 80 次；后端 `/pic/items?limit=` 又卡在 `le=1000`。现在筛选带里多一个「每页 200/500/2000/5000 张」下拉，默认 2000，选择记在 `localStorage`（`msc-pic-page-size`），后端上限提到 5000。
- **代价其实很小** — 网格是懒加载（缩略图滚动到才取，`rootMargin: 200px`），所以加大页长只多花列表 JSON 与 DOM 节点：实测 1000 张 = 770 KB / 90 ms，2000 张约 1.5 MB；副本徽标的 `content_hash` 探测按 400 个一批分块，不会撞 SQLite 变量上限。缩略图流量仍只按可见区域发生。
- **顺带修：轮询会把已翻页的尾部丢掉** — 5 秒轮询只取第一页，而 `picSyncGrid()` 之前要求 `tiles.length === items.length`，一旦用户翻到第二页就把整个网格重建成第一页（图片重新下载、滚动位置跳回顶部）。现在只对齐"返回区间"的前缀，尾部保持不动；只有第一页里的 id 真的错位时才整页重建。
- 测试 163 个（新增 `test_gallery_page_size_goes_up_to_5000`：200/2000/5000 都被接受并回显 `limit`，5001 与 0 是 422 而不是静默夹取）。

### 2.5.7（msc-pic 分支）

- **修：记忆页横向溢出，「多选」「小结所选」看不见** — 卡片摘要 `.memory-desc` 之前是 `white-space: nowrap`（内容取 body 前 200 字），它的 min-content 宽度会顺着 `.memory-content` → `.memory-card` → `.memories-main` 传给 `.memories-layout { grid-template-columns: 200px 1fr }` 里的 `1fr`（`1fr` 的最小值是 `auto`＝内容的 min-content），于是整页比窗口还宽，页头右侧的两个按钮落在视口外，只能横向拖动才看到。图谱/仪表盘没事，是因为它们的轨道写的是 `minmax(0, 1fr)`。
- **两处一起修** — 摘要改成两行截断（`-webkit-line-clamp: 2` + `overflow-wrap: anywhere`，窄卡片里反而比一行 nowrap 看得多）；卡片网格轨道一律 `minmax(0, 1fr)`，长 token 只会撑破卡片内部而不会撑破页面。
- **一行三个卡片 + 筛选移到上面** — `#memories-list` 加 `.memory-grid`：`repeat(3, minmax(0, 1fr))`，≤1000px 两列、≤600px 单列；空状态 `grid-column: 1 / -1`。原来的 sticky 左侧筛选栏（占掉 200px 宽度，还把列表整体往下推）换成列表上方的一条筛选带：分类/优先级复选框横排、分支下拉、显示已归档、清除筛选靠右。`#filter-categories` / `#filter-priorities` / `#filter-branch` / `#filter-archived` / `#select-mode-btn` 等 id 一个没动，`filterMemories()` 与 `clearFilters()` 照旧。搜索页与仪表盘的 `.memory-list` 仍是单列，不受影响。
- 测试 158 个（新增 `test_memory_cards_fit_three_across_without_widening_the_page`：钉住"筛选在列表之上"、`.memory-desc` 不得再有 `nowrap`、轨道必须是 `minmax(0, 1fr)`、以及 app.js 依赖的那批 id 仍在）。

### 2.5.6（msc-pic 分支）

- **修：取消勾选后图谱退化成列表** — `graphRelationsParam()` 给查询串用的是数组，`graphNoteText()` 却按 Set 调 `wanted.has(type)`，抛出后被 `loadGraph()` 的 catch 接住，于是把画好的图换成「图形库未能加载（wanted.has is not a function）」的列表。现在只有 `graphRelationHidden(type)` 这一个谓词读 Set，勾选框与说明文字都走它。
- **顺带收窄失败面** — `loadGraph()` 拆开两段：抓取失败才退化列表（vis CDN 不可达等），已经画出来的图不会被连线说明的报错换掉；装饰性代码（勾选框、`#graph-note`）的异常只记 console。
- 测试 157 个（新增 `test_graph_relation_filter_reads_one_kind_of_collection`：`graphNoteText` 里不得出现 `graphRelationsParam`、必须走 `graphRelationHidden`，并钉住"抓取失败才 fallback"的分层）。

### 2.5.5（msc-pic 分支）

- **按关系类型过滤图谱连线** — 度数上限只裁掉最忙的那些边，噪声本身还在。`GET /api/v1/memories/graph` 新增 `?relations=a,b`（逗号白名单，空＝全部；未知类型只返回空边集，不报 422），响应新增 `edges_by_type`（每种类型的边数）与 `relations`（本次生效的白名单）。图谱页据此渲染一排勾选框：自动关联 / 摘要 / 关联 / 依赖，带条数，默认全勾选，取消即重载。`#graph-note` 会写「显示 X / Y 条关联（按类型隐藏 N 条，每节点 ≤6，省略 M 条）」。过滤只裁边，节点永远不丢。
- **一键清除自动关联** — 新增 `DELETE /api/v1/memories/graph/auto-links`：只删 `relation_type='keyword'` 的行（手写关联一律不动），返回 `rows_deleted` / `edges_deleted`。这不是破坏性操作——自动关联是当前语料的纯函数，再点一次「自动关联」即按现记忆重建。图谱控制条加「清除自动关联」按钮，二次确认并说明可重建。根因即此：现网 2341 条 `keyword` 边里绝大多数是共同词噪声，源头清掉比每次裁边省事。
- `/help` 同步这两处（`?relations=`、`DELETE /memories/graph/auto-links`）。
- 测试 156 个（新增：`?relations=` 隐藏自动边但节点数不变且 `edges_by_type` 仍给出被隐藏的条数、未知类型返回空边不报错、清除只删 keyword 行并保留手工 `related` 且幂等再删返回 0；界面侧 `test_graph_view_can_filter_edges_by_relation_type` 钉住勾选框容器、清除按钮、`params.relations` 与 DELETE 路由）。

### 2.5.4（msc-pic 分支）

- **图谱不再疯转** — 现网 84 节点 / 2363 条边（其中 2341 条是自动关联产生的 `keyword` 边），最大度数 81：枢纽节点让 vis-network 的物理模拟永不收敛，整张图一直在转。`GET /api/v1/memories/graph` 新增 `max_degree`（默认 0＝不限制，agent 契约不变）：按类型权重再时间裁剪 `depends_on > summarizes > relates_to/related > keyword`，同权重取最新，且只有两端都还有余量才保留该边；响应新增 `edges_total` / `edges_dropped` / `max_degree`，度数上限只裁边、从不删节点。
- **图谱页控件** — 控制条新增「每节点 ≤3/4/6/8/12 关联」下拉（默认 ≤6，另有"不限制（会很挤）"）与「重新布局」按钮；右侧 `#graph-note` 实时显示「显示 X / Y 条关联（每节点 ≤N，省略 M 条）」或布局状态。
- **布局跑完就冻结** — 原来 `stabilization: {iterations: 100}` 此后再没管过 physics，图会永远微微转动。现在 300 轮 stabilization 结束即 `physics.enabled=false` 并自动 `fit()`，节点仍可单独拖动，「重新布局」用 `network.stabilize()` 再排一次；斥力/阻尼调稳（gravitationalConstant -1200、damping 0.5、avoidOverlap 0.35、springLength 90、minVelocity 1.2）。
- 测试 152 个（新增：无 `max_degree` 时不裁任何边、`max_degree=2` 时每个节点度数 ≤2 且节点数不变、`max_degree=1` 时手工 `related` 边胜过自动 `keyword` 边；界面侧 `test_graph_view_can_cap_edges_per_node` 钉住控件与"稳定后关闭 physics"。另修一处潜在陷阱：`test_selected_digest_creates_memory_and_archives_sources` 的计数算术假设 `/memories` 不分页，而它默认只返回 50 条，已改为显式 `limit=500`）。

### 2.5.3（msc-pic 分支）

- **「依据」格式钉死** — 真机跑出来写成 `直接记载 ← slug`（标签在前，读不出结论是什么）。模板补一行结构示例 `照抄这个结构，把内容换成你自己的：某条结论 ← slug-甲, slug-乙（综合推断）`，小模型照着填即可，测试同时守住这两行。

### 2.5.2（msc-pic 分支）

- **允许"看完再想"的答复** — 上一版只准逐条复述命中，答案既干又短。现在搜索小结明确邀请模型把命中放在一起观察、对照、归纳：互相印证的合起来说；同一件事多条时以较新/版本号较大为准并写明取舍；跨条目的联系（前一条是原因后一条是结果、两处配置要一起改）直接写出（`SEARCH_SYNTHESIS`）。
- **代价是可追溯** — 每条结论在「依据」里标 `← slugA, slugB（直接记载 或 综合推断）`；紧约束改成"综合推断也要说得出由哪几条记忆撑起"，仍然禁止记忆之外的背景知识、禁止补全记忆里没有的日期/数字/路径、因果只在记忆里写明时才能断言（`SEARCH_GROUNDING`）。
- 测试 148 个（`test_search_digest_allows_thought_out_but_labels_it`；顺手修掉两处会串味的卡片测试——整场共用一条卡片行，自定义 `prompt_template` 与 `archive_sources` 用必须在 finally 里还原）。

### 2.5.1（msc-pic 分支）

- **搜索并总结改成"回答查询"** — 2.5.0 直接套用了每日小结的模板（摘要/要点/待办），结果只是把命中复述一遍，没有回答输入框里那句话。现在搜索这条路径走专用模板 `SEARCH_PROMPT_TEMPLATE`：查询原样写进提示词第一行（`搜索查询：{query}`），要求"用这些记忆回答查询，而不是把它们复述一遍"，输出改成 `## 回答`（先结论再具体事实）/ `## 依据`（每条结论 ← 出处 slug）/ `## 记忆里没有的`（查询问到但记忆没覆盖的部分）；查询只是关键词时也给了明确退路（回答"这些记忆里关于这个主题说了什么"）。
- **按查询形态提问** — 试机发现 20B 级模型会把提示词里的说明句原样抄进「回答」（"关于这个主题，这些记忆里说了什么。"）。现在 `search_is_question()` 先判断查询形态：问句要"先给结论，再给支撑结论的具体事实"，并给了合法的退出「这些记忆没有覆盖该查询」；关键词只要"说明这些记忆实际记录了什么"。模板里另加一条"不要把本提示词里的说明句当作答案抄进正文；段落没有内容就只写「无」"。
- **`build_messages` 支持场景模板** — 新增 `query`/`template` 参数：卡片上自定义的 `prompt_template` 仍然优先（可在自己的模板里用 `{query}`），没自定义时才用场景默认（每日/所选＝`PROMPT_TEMPLATE`，搜索＝`SEARCH_PROMPT_TEMPLATE`）。
- 测试 146 个（新增 `test_search_digest_asks_a_question_not_a_period`、`test_a_card_template_still_overrides_the_search_shape`，并改写原紧约束测试断言查询与三段回答骨架都在提示词里）。

### 2.5.0（msc-pic 分支）

- **搜索并总结** — 搜索页「搜索」旁边新增「搜索并总结」：把当前这一页搜索结果（最多 `/search` 的 20 条命中）交给记忆小结的 chat 模型写一篇小结。新端点 `POST /api/v1/summarize/search`（与其余 Web UI 端点一样不进 `/help`/`llms.txt`），先预览（`dry_run`）再点「存为记忆」才落库。
- **只依据记忆事实** — 该端点在提示里固定附加 `SEARCH_GROUNDING`：每条要点都要能在某条记忆原文里找到出处，记忆里没有的日期/数字/路径/因果/动机不得补全或推测，不得用记忆之外的背景知识解释，记忆互相冲突要并列指出，没依据就在「待办与风险」写「记忆里没有」；框架行也从"时间段"改成"搜索「q」的 N 条命中"。约束走 `{instruction}` 槽，自定义 `prompt_template` 的卡片仍然生效。
- **可审计、不伤人** — 生成的小结带 `summary`+`search` 标签，正文尾部列出取材记忆与"由搜索「q」的命中生成"；这条路径强制不归档来源（卡片上的 `archive_sources` 只影响每日小结），模型失败时 502 且记忆库一条不动，超出输入上限会注明"未送入 N 条"。
- 测试 144 个（新增 `test_search_digest_*` 3 项：无结果 422/不存在 404、提示词里含查询框架与命中原文与紧约束、卡片开归档也不归档且标签与来源列表落库；`test_search_page_offers_a_digest_of_the_hits` 守住按钮/面板/API 方法三件套）。

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
- **照片页 UI** — 相册/缺失/回收站改成三段标签条；状态行改彩色胶囊（含「正在扫描」呼吸点）；4:3 等比格子 + 骨架微光占位 + 缩略图淡入；悬停出选择框、选中描边；批量操作条改悬浮胶囊不再顶动格子；空视图按当前视图给出原因；月分组头 sticky；详情弹窗左右分栏（大图 + 描述/标签胶囊/字段表），`←`/`→`（键盘符号，非界面配色）在屏内照片间走动、`Esc` 关闭、原图↔缩略图原位切换、新增下载原图。
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
