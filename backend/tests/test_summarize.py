"""The model card and memory digests.

The model itself is always faked here: these tests care about what MSC writes
around the model call — masking the key, refusing to touch a single memory
when the model fails, and undoing an archive.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from msc.services import llm
from msc.services.summarizer import (
    day_bounds, due_period, parse_output, source_block,
)

ANSWER = """```markdown
TITLE: 沙箱与备份日总结

## 摘要
今天主要在处理沙箱与备份。

## 要点
- 修好了 disk I/O error

## 待办与风险
- 无
```"""


def _fake_chat(monkeypatch, text=ANSWER, model="fake/model"):
    calls = []

    def fake_chat(cfg, messages, max_tokens=None):
        calls.append({"cfg": cfg, "messages": messages, "max_tokens": max_tokens})
        return {"text": text, "model": model, "finish_reason": "stop",
                "usage": {"total_tokens": 42}, "latency_ms": 12, "reasoning_only": False}

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def _make(client, name, body="内容"):
    created = client.post("/api/v1/memories", json={
        "name": name, "body": body, "category": "project", "priority": "medium"})
    assert created.status_code == 201, created.text
    return created.json()["slug"]


def _card(client, **patch):
    response = client.put("/api/v1/model", json=patch)
    assert response.status_code == 200, response.text
    return response.json()


# ── Model card ───────────────────────────────────────────────────────

def test_card_starts_unconfigured_and_is_editable(client):
    card = client.get("/api/v1/model").json()
    assert card["base_url"] == "" and card["ready"] is False

    saved = _card(client, base_url="192.168.29.100:1234", model="openai/gpt-oss-20b",
                  temperature=0.1, max_tokens=900, auto_time="22:00")
    assert saved["base_url"] == "http://192.168.29.100:1234"  # bare host gets a scheme
    assert saved["endpoint"] == "http://192.168.29.100:1234/v1/chat/completions"
    assert saved["ready"] is True and saved["auto_time"] == "22:00"


def test_card_never_returns_the_key_but_keeps_it(client):
    saved = _card(client, api_key="sk-local-abcdef123456")
    assert saved["api_key_set"] is True
    assert saved["api_key_masked"].startswith("sk-l") and "abcdef" not in str(saved.values())

    kept = _card(client, model="openai/gpt-oss-20b")
    assert kept["api_key_set"] is True  # omitted field keeps the stored key

    cleared = _card(client, api_key="")
    assert cleared["api_key_set"] is False


def test_card_rejects_values_the_worker_could_not_use(client):
    assert client.put("/api/v1/model", json={"auto_time": "25:99"}).status_code == 422
    assert client.put("/api/v1/model", json={"default_category": "daily"}).status_code == 422
    assert client.put("/api/v1/model", json={"model": "x", "base_url": ""}).status_code == 422


def test_test_endpoint_reports_an_unreachable_model(client, monkeypatch):
    def boom(cfg, messages, max_tokens=None):
        raise llm.LLMError("连接失败: all attempts failed")

    def no_models(cfg):
        raise llm.LLMError("no /v1/models here")

    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.setattr(llm, "list_models", no_models)
    _card(client, base_url="http://192.168.29.100:1234", model="openai/gpt-oss-20b")

    body = client.post("/api/v1/model/test").json()
    assert body["ok"] is False and "all attempts failed" in body["error"]
    assert "no /v1/models here" in body["models_error"]

    card = client.get("/api/v1/model").json()
    assert "all attempts failed" in (card["last_error"] or "")


# ── Digests ──────────────────────────────────────────────────────────

def test_selected_digest_creates_memory_and_archives_sources(client, monkeypatch):
    calls = _fake_chat(monkeypatch)
    _card(client, base_url="http://192.168.29.100:1234", model="openai/gpt-oss-20b",
          archive_sources=False)

    slugs = [_make(client, "Digest Source Alpha"), _make(client, "Digest Source Beta")]
    before = client.get("/api/v1/memories").json()

    result = client.post("/api/v1/summarize/selected", json={
        "slugs": slugs, "archive_sources": True, "name": "手动小结 Alpha Beta"}).json()

    assert result["created"]["name"] == "手动小结 Alpha Beta"
    assert result["created"]["slug"].startswith("summary-")
    assert sorted(result["archived"]) == sorted(slugs)
    assert len(calls) == 1

    digest = client.get(f"/api/v1/memories/{result['created']['slug']}").json()
    assert "沙箱与备份日总结" not in digest["body"]  # TITLE line became the name
    for slug in slugs:
        assert slug in digest["body"]
    assert {t["name"] for t in digest["tags"]} == {"summary"}
    assert digest["category"] == "project"

    for slug in slugs:
        assert client.get(f"/api/v1/memories/{slug}").status_code == 404

    after = client.get("/api/v1/memories").json()
    assert len(after) == len(before) - len(slugs) + 1

    # The archive is undoable: the digest itself stays put.
    undone = client.post(f"/api/v1/summarize/{result['created']['slug']}/restore-sources").json()
    assert sorted(undone["restored"]) == sorted(slugs)
    for slug in slugs:
        assert client.get(f"/api/v1/memories/{slug}").status_code == 200

    # The 'summarizes' edges are what made that undo possible. GET /relate hides
    # archived memories, so they only show up again once they are restored.
    related = client.get(f"/api/v1/memories/{result['created']['slug']}/relate").json()
    assert {r["slug"] for r in related} == set(slugs)
    assert {r["relation_type"] for r in related} == {"summarizes"}


def test_digest_name_falls_back_to_the_models_title(client, monkeypatch):
    _fake_chat(monkeypatch)
    slug = _make(client, "Digest Title Fallback")
    result = client.post("/api/v1/summarize/selected", json={"slugs": [slug]}).json()
    assert result["created"]["name"] == "沙箱与备份日总结"


def test_dry_run_previews_without_writing(client, monkeypatch):
    _fake_chat(monkeypatch)
    slug = _make(client, "Digest Dry Run Subject")

    before = client.get("/api/v1/memories").json()
    preview = client.post("/api/v1/summarize/selected",
                          json={"slugs": [slug], "dry_run": True}).json()

    assert preview["dry_run"] is True and preview["created"] is None
    assert "## 要点" in preview["body"] and slug in preview["body"]
    assert client.get("/api/v1/memories").json() == before


def test_failed_model_call_changes_nothing_but_is_logged(client, monkeypatch):
    def boom(cfg, messages, max_tokens=None):
        raise llm.LLMError("HTTP 500: insufficient system resources")

    monkeypatch.setattr(llm, "chat", boom)
    _card(client, base_url="http://192.168.29.100:1234", model="openai/gpt-oss-20b",
          archive_sources=True)

    slugs = [_make(client, "Digest Failure Source One"), _make(client, "Digest Failure Source Two")]
    response = client.post("/api/v1/summarize/selected", json={"slugs": slugs})
    assert response.status_code == 502
    assert "insufficient system resources" in response.json()["detail"]

    for slug in slugs:
        assert client.get(f"/api/v1/memories/{slug}").status_code == 200

    runs = client.get("/api/v1/summarize/runs").json()
    failed = [run for run in runs if run["error"] and "insufficient" in run["error"]]
    assert failed and failed[0]["ok"] is False and failed[0]["slug"] is None


def test_daily_digest_uses_the_cards_timezone(client, monkeypatch):
    _fake_chat(monkeypatch)
    card = _card(client, base_url="http://192.168.29.100:1234", model="openai/gpt-oss-20b",
                 auto_timezone="Asia/Shanghai")
    assert card["auto_timezone"] == "Asia/Shanghai"

    slug = _make(client, "Digest Today Subject")
    window = client.get("/api/v1/summarize/window").json()
    assert slug in [item["slug"] for item in window["memories"]]

    result = client.post("/api/v1/summarize/daily", json={"archive_sources": False}).json()
    assert result["created"]["slug"].startswith("summary-")
    assert window["period"] in result["run"]["period"]


def test_empty_day_writes_nothing_but_says_so(client, monkeypatch):
    calls = _fake_chat(monkeypatch)
    _card(client, base_url="http://192.168.29.100:1234", model="openai/gpt-oss-20b")

    before = client.get("/api/v1/memories").json()
    result = client.post("/api/v1/summarize/daily",
                         json={"date": "2000-01-01", "dry_run": True}).json()

    assert result["created"] is None and result["sources"] == [] and result["dropped"] == 0
    assert "没有记忆" in result["message"]
    assert calls == []  # nothing to summarise, so the model is not called at all
    assert client.get("/api/v1/memories").json() == before


# ── Pure helpers ─────────────────────────────────────────────────────

def test_chat_endpoint_accepts_any_shape_a_user_might_type():
    assert llm.chat_endpoint("192.168.29.100:1234") == "http://192.168.29.100:1234/v1/chat/completions"
    assert llm.chat_endpoint("http://host:1234/v1") == "http://host:1234/v1/chat/completions"
    assert llm.chat_endpoint("http://host/v1/chat/completions") == "http://host/v1/chat/completions"
    assert llm.models_endpoint("http://host:1234") == "http://host:1234/v1/models"


def test_parse_output_takes_a_title_but_not_a_section_heading():
    title, body = parse_output(ANSWER)
    assert title == "沙箱与备份日总结" and body.startswith("## 摘要")

    title, body = parse_output("## 摘要\n今天没什么事")
    assert title == "" and body.startswith("## 摘要")

    title, _ = parse_output("# 一级标题\n正文")
    assert title == "一级标题"


def test_input_budget_drops_the_oldest_memories_first(client):
    old = SimpleNamespace(name="old", slug="old", category="project", priority="medium",
                          description="", body="x" * 500, created_at="2026-01-01T00:00:00+00:00",
                          updated_at="2026-01-01T00:00:00+00:00")
    new = SimpleNamespace(name="new", slug="new", category="project", priority="medium",
                          description="", body="y" * 500, created_at="2026-11-01T00:00:00+00:00",
                          updated_at="2026-11-01T00:00:00+00:00")

    block, kept, dropped = source_block([old, new], 700)
    assert [memory.slug for memory in kept] == ["new"] and dropped == 1
    assert "new" in block and "old" not in block


def test_auto_schedule_is_due_once_per_local_day():
    cfg = SimpleNamespace(auto_enabled=1, auto_time="23:30", auto_timezone="Asia/Shanghai")

    # 23:00 in Shanghai is still before the card's time: yesterday may be caught up.
    assert due_period(cfg, datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc), set()) == "2026-11-07"
    assert due_period(cfg, datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc), {"2026-11-07"}) is None
    # 23:31 Shanghai -> today.
    assert due_period(cfg, datetime(2026, 11, 8, 15, 31, tzinfo=timezone.utc), set()) == "2026-11-08"
    assert due_period(SimpleNamespace(auto_enabled=0, auto_time="23:30", auto_timezone=""), 
                      datetime(2026, 11, 8, 15, 31, tzinfo=timezone.utc), set()) is None

    period, start, end = day_bounds(cfg, None)
    assert period and start < end


# ── Digest of a search result set ────────────────────────────────────

def test_search_digest_needs_something_to_summarise(client):
    assert client.post("/api/v1/summarize/search", json={"query": "备份"}).status_code == 422
    made = _make(client, "Search Digest Vanished")
    gone = client.post("/api/v1/summarize/search", json={"slugs": ["no-such-slug"]})
    assert gone.status_code == 404
    assert client.get(f"/api/v1/memories/{made}").status_code == 200


def test_search_digest_grounds_the_prompt_in_the_hits(client, monkeypatch):
    calls = _fake_chat(monkeypatch)
    slugs = [_make(client, "Search Digest Alpha", body="备份窗口是每天 03:15。"),
             _make(client, "Search Digest Beta", body="disk I/O error 已修复")]

    preview = client.post("/api/v1/summarize/search",
                          json={"query": "备份", "slugs": slugs, "dry_run": True}).json()

    prompt = calls[-1]["messages"][-1]["content"]
    # The query goes in as the thing to answer, not as a label on a time range.
    assert "搜索查询：备份" in prompt
    assert "## 回答" in prompt and "## 依据" in prompt and "## 记忆里没有的" in prompt
    assert "记忆里没有的日期、数字、路径" in prompt      # the grounding brief
    assert "不要用记忆之外的背景知识" in prompt
    assert "不要把本提示词里的" in prompt               # small models echo the ask
    for slug in slugs:
        assert slug in prompt                    # every hit actually reached the model
    assert preview["created"] is None and preview["dry_run"] is True
    assert preview["query"] == "备份"


def test_search_digest_asks_differently_per_query_shape(client, monkeypatch):
    """A keyword has no proposition to confirm; asking a small model to 'answer'
    one is what produces confident filler about the topic instead of a report."""
    calls = _fake_chat(monkeypatch)
    slugs = [_make(client, "Search Question Shape")]

    client.post("/api/v1/summarize/search", json={"query": "备份", "slugs": slugs,
                                                  "dry_run": True})
    keyword_prompt = calls[-1]["messages"][-1]["content"]
    assert "查询只是一个关键词" in keyword_prompt
    assert "## 摘要" not in keyword_prompt       # not the daily digest shape

    client.post("/api/v1/summarize/search", json={"query": "备份窗口是怎么定的？",
                                                  "slugs": slugs, "dry_run": True})
    question_prompt = calls[-1]["messages"][-1]["content"]
    assert "先给结论，再给支撑结论的具体事实" in question_prompt
    assert "这些记忆没有覆盖该查询" in question_prompt   # the honest out


def test_search_is_question_covers_the_shapes_that_matter():
    from msc.services.summarizer import search_is_question
    for asked in ("备份窗口是怎么定的", "为什么 reindex 失败了？", "照片源在哪里",
                  "有没有自动清理", "多少条记忆"):
        assert search_is_question(asked), asked
    for keyword in ("备份", "msc-pic", "docker compose build", ""):
        assert not search_is_question(keyword), keyword


def test_a_card_template_still_overrides_the_search_shape(client, monkeypatch):
    calls = _fake_chat(monkeypatch)
    _card(client, prompt_template="自定义：{count} 条，查询 {query}\n{sources}")
    slugs = [_make(client, "Search Custom Template")]
    client.post("/api/v1/summarize/search", json={"query": "模板", "slugs": slugs,
                                                  "dry_run": True})
    prompt = calls[-1]["messages"][-1]["content"]
    assert prompt.startswith("自定义：1 条，查询 模板")
    for slug in slugs:
        assert slug in prompt


def test_search_digest_keeps_its_sources_even_when_the_card_archives(client, monkeypatch):
    _fake_chat(monkeypatch)
    _card(client, archive_sources=True)          # the daily digest switch
    slugs = [_make(client, "Search Keep One"), _make(client, "Search Keep Two")]

    result = client.post("/api/v1/summarize/search",
                         json={"query": "保留", "slugs": slugs}).json()

    assert result["archived"] == []
    for slug in slugs:
        assert client.get(f"/api/v1/memories/{slug}").status_code == 200

    digest = client.get(f"/api/v1/memories/{result['created']['slug']}").json()
    assert {t["name"] for t in digest["tags"]} == {"summary", "search"}
    assert "原始记忆保留未删除" in digest["body"]
    assert "由搜索「保留」的命中生成。" in digest["body"]
    for slug in slugs:
        assert slug in digest["body"]            # the source list stays auditable
