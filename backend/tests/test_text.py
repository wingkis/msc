"""Body hygiene: metadata must never be mistaken for prose."""

from msc.text import body_hash, clean_body, normalize_tags, strip_stray_frontmatter

FENCED = "---\nname: Legacy\ndescription: old\n---"          # whole file imported as a body
RESIDUE = "name: Legacy\ndescription: old\ntags: test\n---"  # opening fence already consumed


def test_both_leaked_frontmatter_shapes_are_dropped():
    assert clean_body(f"{FENCED}\nreal body") == "real body"
    assert clean_body(f"{RESIDUE}\nreal body") == "real body"
    # The test-memory.md shape: two metadata blocks, then the actual text.
    assert clean_body(f"{FENCED}\n{RESIDUE}\nreal body") == "real body"


def test_ordinary_markdown_is_left_alone():
    assert clean_body("Heading\n---\nbody") == "Heading\n---\nbody"
    # Key-ish lines, but none of them is a metadata key.
    untouched = "Note: remember\nSee: that\n---\nbody"
    assert strip_stray_frontmatter(untouched) == untouched


def test_write_pull_round_trip_hashes_the_cleaned_body():
    cleaned = "real body"
    assert body_hash(f"{FENCED}\n{cleaned}") == body_hash(cleaned) == body_hash(f"{RESIDUE}\n{cleaned}")
    assert normalize_tags("['codex, sandbox']") == ["codex", "sandbox"]
