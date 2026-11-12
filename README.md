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

### msc-me 分支（基于 2.3.1）：记忆出处与自我修正

- **出处与状态** — `memories` 新增 `refs/status/invalidated_at/invalidated_reason/verified_at` 五列（`add_missing_columns` 自动补列；`ensure_provenance` 回填，因为 `ALTER TABLE ADD COLUMN` 在老库留下一片 NULL，而 NULL 在 SQL 里连 `!=` 都不满足，一条 `status != 'contradicted'` 能把升级库里的记忆**全部**变没）。`status` 刻意可空，与真实升级路径一致，读侧 NULL 安全。取代关系复用已有 `supersedes` 边，不新造字段。
- **推翻与复位** — `POST /{slug}/invalidate`、`POST /{slug}/verify`；推翻是幂等的（同一结论重复上报不刷新时间戳），默认从列表/搜索消失但 `X-Contradicted-Hidden` 报数，`verify ok` 是唯一复位路径并顺带清掉那条 `supersedes` 边（证据留在版本快照里）。
- **搜索不再说谎** — `_keyword_candidates` 以前用 `except Exception: candidates = []` 把 FTS 故障吞成"没找到"，于是**索引坏了与库里真没有**从外面完全同形（这正是 `search?q=codex` 返回 `hits: 0` 时被误读的东西）。现在拆成 `_keyword_candidates_diag` 报 `ok｜degraded｜error`，经 `X-Search-Status`/`X-Search-Error` 出给调用方；`api/rag.py` 用的旧签名保持原样。
- **复核通道** — `GET /memories/preflight`（只读、预算受控 `budget_ms`，超预算的 ref 记 `unchecked` 而不是猜）＋ `cli/msc-verify.sh`（POSIX sh，补客户端才验得了的 `git:/host:`；默认 dry-run，`--yes` 写回）。
- **自描述** — `/api/v1/help`、`/llms.txt` 与 `/meta` 的 `vocab` 同步补上 `status`/`ref_kind`，`/meta` 额外报 `contradicted` 计数：一个从不积累矛盾的库，不是恰好正确的库，是没人复核的库。
- 迁移在**线上库副本**（107 条记忆）上演练过：五列补齐、214 个值回填、默认过滤器一条都不藏。测试 114 个（新增 `backend/tests/test_provenance.py` 15 项，含升级库 NULL 行、405 不算推翻、编辑不解推翻、preflight 只读、以及模块级自清理以免污染别人的计数断言）。

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
