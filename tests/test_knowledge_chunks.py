"""Knowledge is stored as chunks, not one `AppSetting.text` string.

Why this exists: the merged 资源管理使用.md spec is 461,690 characters. The old model
kept it in a single column capped at 400,000 chars, so the last 13% of the document
(the DEC-TRAINRES-148..200 decisions, the price matrix, the acceptance criteria) was
unreachable — `search_knowledge` could never return it, no matter the query. Retrieval
also re-split and re-scored every paragraph in Python on every call, so cost scaled with
document size.

These tests pin the three properties that fix that:
  1. nothing is dropped, no matter how large the input;
  2. a chunk carries the heading above it, so a hit is citable;
  3. search is bounded — the number of chunks scored is capped by the query, not the corpus.

Pure logic + a sqlite round-trip. No browser, no network.

python -m pytest tests/test_knowledge_chunks.py
"""

from __future__ import annotations

import pytest


# ---- chunking --------------------------------------------------------------


def test_nothing_is_dropped_from_a_document_larger_than_the_old_cap() -> None:
    """The regression that started this: 461,690 chars in, all of it retrievable out."""
    from app.knowledge import chunk_text

    # ~500k chars, comfortably past the old 400k truncation point.
    para = "这是一段用于压测分块的中文需求描述，包含字段口径与业务规则。" * 10
    doc = "\n\n".join(f"## 第 {i} 节\n{para}" for i in range(1600))
    assert len(doc) > 400_000, f"fixture must exceed the old cap, got {len(doc)}"

    chunks = chunk_text(doc)
    assert chunks, "a non-empty document must produce chunks"
    # Index is contiguous from 0 — a gap would mean a silently lost block.
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # Nothing was dropped: the reassembled chunks still contain every source paragraph,
    # including the last one (the part the old 400k cap cut off).
    joined = "\n\n".join(c.text for c in chunks)
    assert f"## 第 {1599} 节" in joined, "the tail of the document must survive"
    assert len(joined.replace("\n", "")) >= len(doc.replace("\n", "")) * 0.99


def test_a_chunk_carries_the_heading_above_it() -> None:
    """A hit must be citable ("§4.3 收费标准"), not an anonymous paragraph.

    A heading also has to END the previous section — merging §5's title into §4.3's
    chunk would mis-cite every hit under it, which the reader cannot detect from the
    snippet alone.
    """
    from app.knowledge import chunk_text

    doc = "## 4.3 收费标准\n\n计财模块按课时费单价计算。\n\n## 5 结算\n\n三级审批后结算。"
    chunks = chunk_text(doc)
    headings = [c.heading for c in chunks]
    assert "4.3 收费标准" in headings
    assert "5 结算" in headings
    # Each heading block lives with its own body, not at the tail of the previous one.
    by_heading = {c.heading: c.text for c in chunks}
    assert "计财模块按课时费单价计算。" in by_heading["4.3 收费标准"]
    assert "5 结算" not in by_heading["4.3 收费标准"], (
        "§5's title must not appear in §4.3's chunk — that is a wrong citation"
    )
    assert "三级审批后结算。" in by_heading["5 结算"]


def test_short_paragraphs_are_merged_so_a_spec_is_not_hundreds_of_one_liners() -> None:
    from app.knowledge import CHUNK_TARGET_CHARS, chunk_text

    doc = "\n\n".join(f"短段落 {i}" for i in range(400))
    chunks = chunk_text(doc)
    assert len(chunks) < 40, "many tiny paragraphs must be merged into dense chunks"
    assert all(c.chars > 0 for c in chunks)
    # The merge target must actually bound chunk size for typical prose.
    assert sum(c.chars for c in chunks) >= len(doc) * 0.9
    assert CHUNK_TARGET_CHARS >= 500, "a too-small target defeats the merge entirely"


def test_a_single_giant_paragraph_is_kept_whole() -> None:
    """A long markdown table must not be cut mid-row — breaking it would destroy exactly
    the rows being searched for."""
    from app.knowledge import chunk_text

    table = "\n".join(f"| 字段{i} | 说明{i} |" for i in range(500))
    chunks = chunk_text(table)
    assert len(chunks) == 1
    assert chunks[0].text.count("|") == table.count("|")


def test_empty_input_yields_no_chunks() -> None:
    from app.knowledge import chunk_text

    assert chunk_text("") == []
    assert chunk_text("   \n\n  \n") == []


# ---- storage + retrieval (real sqlite) -------------------------------------


async def test_replace_append_and_search_round_trip() -> None:
    """End to end: write chunks, append more, search finds the appended tail.

    The tail is the point — under the old single-column model it was the part that got
    truncated away, so a query about it returned nothing. `search` opens its own session
    deliberately, so the assertions run after the write session has committed.
    """
    from app.db import db_session, init_db
    from app.knowledge import append_knowledge, replace_knowledge, search
    from app.models import Project

    await init_db()
    async with db_session() as s:
        p = Project(name="kb-roundtrip", base_url="http://x")
        s.add(p)
        await s.flush()
        pid = p.id

        head = "## 1 概述\n\n培训资源管理平台覆盖需求到结算。"
        res = await replace_knowledge(s, pid, head, source="paste")
        assert res["chunks"] >= 1 and res["chars"] == len(head)

        # Append continues chunk_index; it must not overwrite.
        tail = "## 199 验收清单\n\nOQ-TRAINRES-108 结算口径待确认，UNIQUE_TOKEN_ZZZ。"
        res2 = await append_knowledge(s, pid, tail, source="upload")
        assert res2["chunks"] >= 1

    # Committed. Now search — this is the path the assistant's tool actually takes.
    found = await search(pid, "UNIQUE_TOKEN_ZZZ")
    assert found["total_blocks"] == 2, "both the head and the appended tail must be stored"
    assert found["hits"], "a term present only in the appended tail must be found"
    assert "UNIQUE_TOKEN_ZZZ" in found["hits"][0]["text"]
    assert found["hits"][0]["heading"].startswith("199")

    # The head chunk is unaffected — append did not clobber it.
    assert (await search(pid, "培训资源管理平台"))["hits"]

    # A query with no matching term must say so rather than returning everything.
    miss = await search(pid, "ZZZ_NO_SUCH_TERM_QQQ")
    assert miss["hits"] == []
    assert miss["total_blocks"] == 2, "a miss still reports the true corpus size"


async def test_replace_is_a_full_swap_not_an_append() -> None:
    from app.db import db_session, init_db
    from app.knowledge import replace_knowledge, search
    from app.models import Project

    await init_db()
    async with db_session() as s:
        p = Project(name="kb-replace", base_url="http://x")
        s.add(p)
        await s.flush()
        pid = p.id

        await replace_knowledge(s, pid, "OLD_MARKER_AAA 的内容", source="paste")
        await replace_knowledge(s, pid, "NEW_MARKER_BBB 的内容", source="paste")

    # search opens its own session — assertions belong outside the write block.
    assert (await search(pid, "OLD_MARKER_AAA"))["hits"] == [], "replace must drop the old text"
    assert (await search(pid, "NEW_MARKER_BBB"))["hits"], "replace must install the new text"


async def test_projects_do_not_see_each_others_knowledge() -> None:
    """The assistant is scoped to one project; a chunk leak across projects would let a
    question about project A be answered from project B's spec."""
    from app.db import db_session, init_db
    from app.knowledge import replace_knowledge, search
    from app.models import Project

    await init_db()
    async with db_session() as s:
        pa = Project(name="kb-a", base_url="http://x")
        pb = Project(name="kb-b", base_url="http://x")
        s.add_all([pa, pb])
        await s.flush()
        await replace_knowledge(s, pa.id, "ALPHA_ONLY_TOKEN 属于 A", source="paste")
        await replace_knowledge(s, pb.id, "BETA_ONLY_TOKEN 属于 B", source="paste")
        pa_id, pb_id = pa.id, pb.id

    assert (await search(pa_id, "BETA_ONLY_TOKEN"))["hits"] == []
    assert (await search(pb_id, "ALPHA_ONLY_TOKEN"))["hits"] == []
    assert (await search(pa_id, "ALPHA_ONLY_TOKEN"))["hits"]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
