"""Build, run and store memory digests with the model from the model card.

Deliberately boring where a small local model is involved:

* the prompt asks for Markdown sections, never JSON — 9B-class models mangle
  JSON and the digest is meant to be read by a human anyway;
* the input is trimmed to a character budget newest-first, because these
  models run on short context windows;
* the digest body always carries the source list, so the summary stays
  auditable even when the model leaves it out;
* sources are soft-deleted only when asked, and the digest keeps
  ``summarizes`` relations to them so the archive can be undone.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from msc.filesystem.write_through import delete_through, write_through
from msc.models import Memory, MemoryRelation, MemoryVersion, ModelConfig, SummaryRun, Tag
from msc.services import llm
from msc.text import clean_body, normalize_tags

SYSTEM_PROMPT = "你是一个严谨的中文记忆库整理助手。只输出要求的 Markdown，不要寒暄。"

PROMPT_TEMPLATE = """下面是 {count} 条记忆（{date_range}）。把它们合并成一条小结。

严格按这个格式输出，第一行必须是 TITLE: <不超过 20 字的标题>：

TITLE: ...

## 摘要
2-4 句话，说明这段时间发生了什么。

## 要点
- 3-8 条，每条一行，保留关键数字、路径、命令与结论。

## 待办与风险
- 没有就写：无

{instruction}
记忆内容：

{sources}
"""

# One memory's body is never allowed to crowd out the rest of the digest.
MAX_BODY_PER_MEMORY = 1200

# A search digest has a different job from the daily one, and a different
# failure mode. The daily digest narrates a period; here the user typed a query
# and wants it answered - a summary of the hits is not an answer. And because
# the query already picked the memories, the model is tempted to explain the
# topic from its own background knowledge instead of from what was retrieved.
SEARCH_PROMPT_TEMPLATE = """搜索查询：{query}

下面是这次搜索的 {count} 条命中记忆（{date_range}）。

第一行必须是 TITLE: <不超过 20 字的标题>，然后按下面的段落输出：

TITLE: ...

## 回答
{answer_ask}

## 依据
每行一条：结论 ← 出处 slug（直接记载 或 综合推断）

## 记忆里没有的
查询问到、但上面这些记忆没有覆盖的点

规则：下面三段的小标题原样保留；段落里没有内容就只写「无」；不要把本提示词里的
说明句当作答案抄进正文。{instruction}
记忆内容：

{sources}
"""

# A 20B-class model echoes whatever sentence looks like filler ("关于这个主题，
# 这些记忆里说了什么"), so the ask differs per query shape and the anti-echo
# rule sits next to it.
SEARCH_ANSWER_QUESTION = ("先通读下面的记忆、把它们放在一起对照，再回答上面的查询："
                          "先给结论，再给支撑结论的具体事实（数字、路径、命令、版本、时间），"
                          "3-8 句。结论可以是跨多条记忆想出来的判断，不必是哪一条的原文。"
                          "若这些记忆与查询无关，就直说「这些记忆没有覆盖该查询」，不要勉强成文。")
SEARCH_ANSWER_KEYWORD = ("查询只是一个关键词：先通读下面的记忆再归纳，说明这些记忆关于该"
                         "关键词实际记录了什么，给出具体事实与跨条目的联系；若这些记忆与关键词"
                         "无关，就直说「没有相关记忆」。")

# Grounding without lobotomising it: the answer should be the result of reading
# the hits together, so cross-memory synthesis is asked for - and paid for with
# an explicit provenance label so a reader can tell a quote from a conclusion.
SEARCH_SYNTHESIS = (
    "可以把这些记忆放在一起观察、对照、归纳后再作答：互相印证的合起来说；同一件事"
    "有多条时以时间较新或版本号较大的为准，并写明取舍；跨条目的联系（前一条是原因、"
    "后一条是结果，或两处配置必须一起改）直接写出来。这样的结论标为「综合推断」并列出"
    "它由哪几条 slug 组成；逐条能在原文里找到的标为「直接记载」。"
)

SEARCH_GROUNDING = (
    "但依据只能是上面列出的记忆：综合推断也要说得出由哪几条记忆撑起；记忆里没有的"
    "日期、数字、路径和动机不要补全，因果关系只有记忆里写明时才能断言；不要用"
    "记忆之外的背景知识解释这个主题；记忆之间说法不一致时并列写出并指明冲突；"
    "查询里问到了但没有依据的部分，明确写进「记忆里没有的」。"
)


def search_is_question(query: str) -> bool:
    """Rough, and deliberately so: whether to demand an answer or a report.

    A keyword query has no proposition to confirm, so asking a 20B model to
    'answer' it is what produces confident filler about the topic."""
    q = (query or "").strip()
    if not q:
        return False
    return (q.endswith(("?", "？"))
            or q[:2] in ("如何", "怎么", "为何", "是否", "什么", "为什", "为什", "能否", "可以", "需要")
            or q[:3] in ("为什么", "是不是", "有没有", "能不能", "怎么样", "是什么")
            or any(w in q for w in ("如何", "怎么", "为什么", "哪里", "多少", "哪个")))

_TITLE_RE = re.compile(r"^\s*(?:#{1}\s+|(?:TITLE|标题)\s*[::]\s*)(.{2,120}?)\s*$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)
_AUTO_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# ── Time helpers ─────────────────────────────────────────────────────

def container_tz():
    return datetime.now().astimezone().tzinfo


def effective_tz(cfg: ModelConfig):
    """The card's timezone, falling back to the container's (UTC by default).

    A "daily" digest is meaningless if the day boundary is nobody's local
    midnight, so the card carries an explicit timezone.
    """
    name = (getattr(cfg, "auto_timezone", "") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, OSError):
            pass
    return container_tz()


def local_now(cfg: ModelConfig, now: datetime = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(effective_tz(cfg))


def auto_minutes(cfg: ModelConfig) -> int:
    """The card's HH:MM as minutes past midnight; 23:30 if unusable."""
    value = (getattr(cfg, "auto_time", "") or "").strip()
    match = _AUTO_TIME_RE.match(value)
    if not match:
        return 23 * 60 + 30
    return int(match.group(1)) * 60 + int(match.group(2))


def day_bounds(cfg: ModelConfig, day: Optional[date] = None) -> Tuple[str, str, str]:
    """(period, start_utc_iso, end_utc_iso) for one local calendar day."""
    tz = effective_tz(cfg)
    day = day or local_now(cfg).date()
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return (day.isoformat(),
            start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat())


def parse_ts(value, assume_tz=None) -> Optional[datetime]:
    """Parse the timestamps memories carry — ISO with or without an offset,
    space instead of ``T``, and hand-written frontmatter that lost the zone."""
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz or container_tz())
    return parsed


def done_periods(db: Session) -> List[str]:
    """Days the auto digest already attempted — failures included, so a dead
    model cannot make the worker retry once a minute forever."""
    return [row[0] for row in db.query(SummaryRun.period).filter(SummaryRun.kind == "auto").all()]


def due_period(cfg: ModelConfig, now: datetime, attempted: Sequence[str]) -> Optional[str]:
    """Which day to summarise now, or None.

    After the card's time it is today. Before it, only the previous day counts
    (a container that started late still gets its digest); anything older stays
    in the past and is a manual job.
    """
    if not cfg.auto_enabled:
        return None
    local = local_now(cfg, now)
    if local.hour * 60 + local.minute >= auto_minutes(cfg):
        period = local.date().isoformat()
    else:
        period = (local.date() - timedelta(days=1)).isoformat()
    return None if period in attempted else period


# ── Collecting the input ─────────────────────────────────────────────

def memories_in_window(db: Session, cfg: ModelConfig, period: str) -> List[Memory]:
    """Live memories created or updated inside one local calendar day."""
    _, start_iso, end_iso = day_bounds(cfg, date.fromisoformat(period))
    start = parse_ts(start_iso)
    end = parse_ts(end_iso)
    tz = effective_tz(cfg)

    rows = (db.query(Memory).filter(Memory.archived == 0)
            .order_by(Memory.updated_at.desc()).limit(500).all())
    picked = []
    for memory in rows:
        created = parse_ts(memory.created_at, tz)
        updated = parse_ts(memory.updated_at, tz)
        if (created and start <= created < end) or (updated and start <= updated < end):
            picked.append(memory)
    picked.sort(key=lambda m: m.updated_at or m.created_at or "")
    return picked


def source_block(memories: Sequence[Memory], max_input_chars: int) -> Tuple[str, List[Memory], int]:
    """Fill the prompt budget newest-first, then emit the kept memories in
    chronological order. Returns (block, kept, dropped)."""
    budget = max(int(max_input_chars or 12000), 500)
    parts: List[str] = []
    kept: List[Memory] = []
    used = 0

    for memory in sorted(memories, key=lambda m: m.updated_at or m.created_at or "", reverse=True):
        body = clean_body(memory.body or "")[:MAX_BODY_PER_MEMORY]
        header = f"### {memory.name}（slug={memory.slug}, {memory.category}/{memory.priority}）"
        chunk = f"{header}\n{memory.description or ''}\n{body}\n"
        if kept and used + len(chunk) > budget:
            break
        parts.append(chunk)
        kept.append(memory)
        used += len(chunk)

    parts.reverse()
    kept.reverse()
    return "\n".join(parts), kept, len(memories) - len(kept)


def build_messages(cfg: ModelConfig, memories: Sequence[Memory], *, date_range: str = "",
                   instruction: str = "", query: str = "", template: str = "") -> List[dict]:
    """Prompt for one digest run.

    ``template`` is the scenario default (daily/selected vs search); a template
    saved on the card still wins, so the settings page keeps its override.
    ``query`` reaches the search template as the question to answer."""
    block, kept, _ = source_block(memories, cfg.max_input_chars)
    template = (getattr(cfg, "prompt_template", "") or "").strip() or (template or PROMPT_TEMPLATE)
    fields = {
        "count": len(kept),
        "date_range": date_range or "手动选择",
        "query": query or "",
        "answer_ask": (SEARCH_ANSWER_QUESTION if search_is_question(query)
                       else SEARCH_ANSWER_KEYWORD),
        "instruction": f"额外要求：{instruction}\n" if instruction else "",
        "sources": block,
    }
    try:
        user_prompt = template.format(**fields)
    except (KeyError, IndexError, ValueError):
        # A hand-written template with a typo still gets its memories.
        user_prompt = f"{template}\n\n{block}"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}]


# ── Parsing the answer ───────────────────────────────────────────────

def parse_output(raw: str) -> Tuple[str, str]:
    """Split the model's answer into (title, body).

    Only a level-1 heading counts as a title, so a model that starts straight
    at ``## 摘要`` does not produce a digest called "摘要".
    """
    text = (raw or "").strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)

    title = ""
    if lines:
        match = _TITLE_RE.match(lines[0])
        if match:
            title = match.group(1).strip().strip("#*`\"' ")
            lines.pop(0)
    return title, "\n".join(lines).strip()


def first_paragraph(markdown: str, limit: int = 200) -> str:
    """The description for the digest — the model's own summary line."""
    for line in (markdown or "").splitlines():
        candidate = line.strip().strip("#-*`> ").strip()
        if len(candidate) >= 4:
            return candidate[:limit]
    return ""


def digest_body(markdown: str, sources: Sequence[Memory], *, model: str, when_local: str,
                archive_sources: bool, note: str = "") -> str:
    """The generated markdown plus the part the model must not be trusted with."""
    lines = [markdown.rstrip(), "", "---",
             f"模型: {model} · 生成于 {when_local} · 来源 {len(sources)} 条记忆",
             "原始记忆已软删除（记忆页勾选“显示已归档”可恢复）" if archive_sources
             else "原始记忆保留未删除"]
    if note:
        lines.append(note)
    lines.append("")
    lines.append("## 来源记忆")
    lines.extend(f"- [{memory.id}] {memory.name} — {memory.slug}" for memory in sources)
    return clean_body("\n".join(lines))


def unique_slug(db: Session, base: str) -> str:
    """A slug nobody holds — including the soft-deleted ones."""
    candidate, suffix = base, 1
    while db.query(Memory).filter(Memory.slug == candidate).first():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def default_names(cfg: ModelConfig, kind: str, period: str) -> Tuple[str, str]:
    """(name, slug base) for a digest the user did not name."""
    local = local_now(cfg)
    if kind == "auto":
        day = period or local.date().isoformat()
        return f"每日小结 {day}", f"daily-summary-{day.replace('-', '')}"
    return (f"记忆小结 {local.strftime('%Y-%m-%d %H:%M')}",
            f"summary-{local.strftime('%Y%m%d-%H%M')}")


# ── The run ──────────────────────────────────────────────────────────

def _record(db: Session, *, kind: str, period: str, model: str, source_count: int,
            archived_count: int, ok: bool, error: str, duration_ms: int,
            slug: Optional[str], actor: str) -> dict:
    row = SummaryRun(
        kind=kind, period=period or "", slug=slug, model=model or "",
        source_count=source_count, archived_count=archived_count,
        ok=1 if ok else 0, error=(error or None), duration_ms=duration_ms,
        created_by=actor, created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(row)
    db.commit()
    return {
        "id": row.id, "kind": row.kind, "period": row.period, "slug": row.slug,
        "model": row.model, "source_count": row.source_count,
        "archived_count": row.archived_count, "ok": bool(row.ok), "error": row.error,
        "duration_ms": row.duration_ms, "created_at": row.created_at,
        "created_by": row.created_by,
    }


def run_summary(db: Session, cfg: ModelConfig, memories: Sequence[Memory], *, actor: str,
                kind: str = "manual", period: str = "", name: str = "", category: str = "",
                priority: str = "", tags: Optional[List[str]] = None,
                archive_sources: Optional[bool] = None, instruction: str = "",
                date_range: str = "", note: str = "", query: str = "",
                template: str = "", dry_run: bool = False) -> Dict:
    """Summarise ``memories`` into one new memory.

    ``note`` is a line the body carries that the model did not write - which
    search produced this digest, for instance. Nothing is written before the
    model has answered, so a dead endpoint leaves
    the memory set exactly as it was. ``dry_run`` stops there on purpose.
    """
    if not memories:
        if dry_run:
            return {"dry_run": True, "created": None, "sources": [], "body": "",
                    "model": cfg.model or "", "dropped": 0, "message": "范围内没有记忆"}
        return {"created": None, "sources": [], "archived": [], "dropped": 0, "body": "",
                "model": cfg.model or "", "message": "范围内没有记忆",
                "run": _record(db, kind=kind, period=period, model=cfg.model, source_count=0,
                               archived_count=0, ok=True, error="范围内没有记忆", duration_ms=0,
                               slug=None, actor=actor)}

    _, kept, dropped = source_block(memories, cfg.max_input_chars)
    messages = build_messages(cfg, kept, date_range=date_range, instruction=instruction,
                              query=query, template=template)

    try:
        answer = llm.chat(cfg, messages)
    except llm.LLMError as exc:
        if not dry_run:
            _record(db, kind=kind, period=period, model=cfg.model, source_count=len(kept),
                    archived_count=0, ok=False, error=str(exc), duration_ms=0, slug=None, actor=actor)
            cfg.last_error = str(exc)[:500]
            cfg.updated_at = datetime.now(timezone.utc).isoformat()
            db.commit()
        raise

    title, markdown = parse_output(answer["text"])
    auto_name, slug_base = default_names(cfg, kind, period)
    digest_name = (name or "").strip() or title or auto_name
    digest_category = category or cfg.default_category or "project"
    digest_priority = priority or cfg.default_priority or "medium"
    archive = bool(cfg.archive_sources) if archive_sources is None else bool(archive_sources)
    when_local = local_now(cfg).strftime("%Y-%m-%d %H:%M %Z")
    if dropped:
        note = (note + " " if note else "") + f"输入超出长度上限，有 {dropped} 条较旧的记忆未送入模型。"
    if answer.get("reasoning_only"):
        note = (note + " " if note else "") + "模型只输出了思考内容，已直接采用。"

    body = digest_body(markdown, kept, model=answer["model"], when_local=when_local,
                       archive_sources=archive, note=note)

    result = {
        "created": None, "sources": [{"id": m.id, "slug": m.slug, "name": m.name} for m in kept],
        "dropped": dropped, "model": answer["model"], "latency_ms": answer["latency_ms"],
        "usage": answer["usage"], "body": body, "name": digest_name,
        "archived": [],
    }
    if dry_run:
        result["dry_run"] = True
        return result

    now = datetime.now(timezone.utc).isoformat()
    slug = unique_slug(db, slug_base)
    memory = Memory(slug=slug, name=digest_name, description=first_paragraph(markdown),
                    body=body, priority=digest_priority, category=digest_category,
                    lifespan="permanent", branch="", created_at=now, updated_at=now,
                    source="msc")
    db.add(memory)
    db.flush()

    for tag_name in normalize_tags(tags or (["summary", "daily"] if kind == "auto" else ["summary"])):
        tag = db.query(Tag).filter(Tag.name == tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            db.add(tag)
            db.flush()
        memory.tags.append(tag)

    db.add(MemoryVersion(
        memory_id=memory.id, version_num=1, name=memory.name, description=memory.description,
        body=memory.body, priority=memory.priority, category=memory.category,
        tags_snapshot="[]", change_summary=f"由 {len(kept)} 条记忆生成（{answer['model']}）",
        created_at=now, created_by=actor,
    ))

    # 'summarizes' edges: the graph shows the digest next to what it absorbed,
    # and restoring the sources later only has to follow them.
    for source in kept:
        db.add(MemoryRelation(memory_id_a=memory.id, memory_id_b=source.id,
                              relation_type="summarizes", created_at=now))
        db.add(MemoryRelation(memory_id_a=source.id, memory_id_b=memory.id,
                              relation_type="summarizes", created_at=now))
    db.commit()
    write_through(db, memory)

    archived: List[str] = []
    if archive:
        for source in kept:
            source.archived = 1
            source.updated_at = now
            archived.append(source.slug)
        db.commit()
        for source_slug in archived:
            delete_through(source_slug)

    cfg.last_ok_at = now
    cfg.last_error = None
    cfg.updated_at = now
    db.commit()

    result["created"] = {"id": memory.id, "slug": memory.slug, "name": memory.name,
                        "category": memory.category, "priority": memory.priority,
                        "created_at": memory.created_at}
    result["archived"] = archived
    result["run"] = _record(db, kind=kind, period=period, model=answer["model"],
                            source_count=len(kept), archived_count=len(archived), ok=True,
                            error="", duration_ms=answer["latency_ms"], slug=slug, actor=actor)
    return result


def restore_sources(db: Session, digest: Memory) -> List[str]:
    """Undo the archiving half of a digest by following its 'summarizes' edges."""
    restored: List[str] = []
    relations = db.query(MemoryRelation).filter(
        MemoryRelation.memory_id_a == digest.id,
        MemoryRelation.relation_type == "summarizes",
    ).all()
    for relation in relations:
        source = db.query(Memory).filter(Memory.id == relation.memory_id_b).first()
        if source and source.archived:
            source.archived = 0
            source.updated_at = datetime.now(timezone.utc).isoformat()
            restored.append(source.slug)
    db.commit()
    for slug in restored:
        memory = db.query(Memory).filter(Memory.slug == slug).first()
        if memory:
            write_through(db, memory)
    return restored
