"""Automatic graph linking from high-frequency exact-match keywords.

A keyword is a whole token: an ASCII word or a segmented Chinese word.
Substring or fuzzy matches never count — 记忆 links memories that contain the
word 记忆, not memories that merely contain the character 忆. Memories that
share a keyword appearing in at least ``min_freq`` memories are connected with
a ``related`` relation. Running the pass twice is idempotent: pairs that are
already linked are skipped.

jieba is optional: without it the pass still links ASCII technical terms
(MSC, docker, hook …), which is most of what a bilingual memory corpus needs.
"""

import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from msc.models import Memory, MemoryRelation

_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,29}")
_CJK_WORD = re.compile(r"[一-鿿]{2,8}")

_STOP_WORDS = {
    # English function words long enough to pass the >=3-letter filter.
    "that", "this", "these", "those", "with", "from", "have", "will", "just",
    "like", "can", "use", "used", "into", "your", "ours", "also", "more",
    "what", "how", "when", "where", "which", "because", "there", "here",
    "they", "their", "them", "been", "being", "does", "doing", "about",
    "other", "should", "would", "could", "every", "between", "something",
    # Chinese function words that jieba emits as standalone tokens.
    "我们", "你们", "他们", "她们", "它们", "这个", "那个", "一些", "一个",
    "什么", "怎么", "如何", "因为", "所以", "但是", "或者", "以及", "并且",
    "已经", "可以", "需要", "进行", "通过", "当前", "时候", "里面", "上面",
    "下面", "中间", "以及", "对于", "关于", "这样", "那样",
}

_jieba = None
_jieba_tried = False


def _get_jieba():
    global _jieba, _jieba_tried
    if not _jieba_tried:
        _jieba_tried = True
        try:
            import jieba
            _jieba = jieba
        except ImportError:
            _jieba = None
    return _jieba


def tokenize(text: str) -> set[str]:
    """Exact whole-word tokens of a memory: ASCII terms + Chinese words."""
    tokens = set()
    for word in _ASCII_WORD.findall(text):
        word = word.lower()
        if len(word) >= 3 and word not in _STOP_WORDS:
            tokens.add(word)
    jieba = _get_jieba()
    if jieba is not None:
        for word in jieba.cut(text):
            word = word.strip()
            if _CJK_WORD.fullmatch(word) and word not in _STOP_WORDS:
                tokens.add(word)
    return tokens


# A mega-word (e.g. "记忆" in 40 memories) would create hundreds of edges.
# Link the newest memories sharing it, capped so a pass is bounded — and
# deterministic, so a second pass is a pure no-op.
_MAX_MEMORIES_PER_WORD = 6


def build_keyword_links(db: Session, min_freq: int = 3) -> list[dict]:
    """Link memories that share a keyword appearing in >= min_freq memories.

    Returns the per-word result: [{word, memories, pairs_created}, ...].
    """
    docs: dict[int, set[str]] = {}
    for memory in db.query(Memory).filter(Memory.archived == 0).all():
        text = " ".join(filter(None, (memory.name, memory.description, memory.body)))
        text += " " + " ".join(tag.name for tag in memory.tags)
        docs[memory.id] = tokenize(text)

    word_docs: defaultdict[str, list[int]] = defaultdict(list)
    for memory_id, words in docs.items():
        for word in words:
            word_docs[word].append(memory_id)

    linked_pairs = {
        frozenset((rel.memory_id_a, rel.memory_id_b))
        for rel in db.query(MemoryRelation).all()
    }

    created = []
    now = datetime.now(timezone.utc).isoformat()
    for word, memory_ids in sorted(word_docs.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(memory_ids) < min_freq:
            continue
        # Newest first, deterministic: the same word always links the same set.
        members = sorted(memory_ids, reverse=True)[:_MAX_MEMORIES_PER_WORD]
        pairs = 0
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pair = frozenset((members[i], members[j]))
                if pair in linked_pairs:
                    continue
                a, b = members[i], members[j]
                # Relations are stored in both directions, like every other
                # linking route, so either side resolves them symmetrically.
                db.add(MemoryRelation(memory_id_a=a, memory_id_b=b,
                                     relation_type="related", created_at=now))
                db.add(MemoryRelation(memory_id_a=b, memory_id_b=a,
                                     relation_type="related", created_at=now))
                linked_pairs.add(pair)
                pairs += 1
        if pairs:
            created.append({"word": word, "memories": len(memory_ids), "pairs_created": pairs})

    db.commit()
    return created
