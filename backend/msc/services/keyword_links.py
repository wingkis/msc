"""Automatic graph linking from high-frequency exact-match keywords.

A keyword is a whole token: an ASCII word or a segmented Chinese word.
Substring or fuzzy matches never count — 记忆 links memories that contain the
word 记忆, not memories that merely contain the character 忆.

The pass is pair-based rather than per-word: memories A and B are connected
only when they share at least ``min_shared`` distinct keywords, and a keyword
only counts when its coverage is *interesting*:

    min_freq <= memories_containing(word) <= total_memories * max_coverage

Lower bound: a word in a single memory links nothing. Upper bound: a word in
most memories is ambient vocabulary (msc, session, api), and letting it create
edges turns the graph into a clique. Shared-word count is the strength;
coverage caps out the generic hubs (an inverse-document-frequency guard).

Created edges have relation_type 'keyword', so they stay distinguishable from
hand-made relations and can be rebuilt with ?rebuild=true.
"""

import re
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

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
    "下面", "中间", "对于", "关于", "这样", "那样",
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


def build_keyword_links(db: Session, min_freq: int = 3, max_coverage: float = 0.4,
                        min_shared: int = 2, rebuild: bool = False) -> dict:
    """Connect memories sharing several exact whole-word keywords.

    Returns: {relations_created, words_used, generic_words, pairs}
    where pairs lists the connected memory ids and the words behind them.
    """
    docs: dict[int, set[str]] = {}
    for memory in db.query(Memory).filter(Memory.archived == 0).all():
        text = " ".join(filter(None, (memory.name, memory.description, memory.body)))
        text += " " + " ".join(tag.name for tag in memory.tags)
        docs[memory.id] = tokenize(text)

    total = len(docs)
    if total == 0:
        return {"relations_created": 0, "words_used": 0, "generic_words": [], "pairs": []}

    coverage_cap = max(min_freq, int(total * max_coverage))

    word_docs: defaultdict[str, set[int]] = defaultdict(set)
    for memory_id, words in docs.items():
        for word in words:
            word_docs[word].add(memory_id)

    generic = []
    pair_words: defaultdict[tuple[int, int], set[str]] = defaultdict(set)
    for word, memory_ids in word_docs.items():
        count = len(memory_ids)
        if count < min_freq:
            continue
        if count > coverage_cap:
            generic.append({"word": word, "memories": count})
            continue
        for a, b in combinations(sorted(memory_ids), 2):
            pair_words[(a, b)].add(word)

    if rebuild:
        db.query(MemoryRelation).filter(MemoryRelation.relation_type == "keyword").delete()

    linked_pairs = {
        frozenset((rel.memory_id_a, rel.memory_id_b))
        for rel in db.query(MemoryRelation).all()
    }

    created = []
    now = datetime.now(timezone.utc).isoformat()
    for (a, b), shared in sorted(pair_words.items(), key=lambda kv: (-len(kv[1]), kv[0], kv[1])):
        if len(shared) < min_shared:
            continue
        pair = frozenset((a, b))
        if pair in linked_pairs:
            continue
        # Both directions, like every other linking route, so either side
        # resolves the relation symmetrically.
        db.add(MemoryRelation(memory_id_a=a, memory_id_b=b,
                             relation_type="keyword", created_at=now))
        db.add(MemoryRelation(memory_id_a=b, memory_id_b=a,
                             relation_type="keyword", created_at=now))
        linked_pairs.add(pair)
        created.append({"a": a, "b": b, "words": sorted(shared)})

    db.commit()
    generic.sort(key=lambda g: -g["memories"])
    return {
        "relations_created": len(created),
        "words_used": sum(len(pair["words"]) for pair in created),
        "generic_words": generic[:10],
        "pairs": created,
    }
