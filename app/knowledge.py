"""Project knowledge as chunks: split, store, retrieve.

Replaces the old single-string model (`AppSetting.proj_<pid>_knowledge`). See
`KnowledgeChunk` in app/models.py for why.

The three jobs here, in order of who calls them:

* `chunk_text` — one document in, ordered chunks out. Called on paste and on upload.
* `replace_knowledge` — swap a project's chunks atomically. Called by PUT /knowledge
  and by the one-off backfill of legacy `proj_<pid>_knowledge` values.
* `search` — score chunks for a query and return the best blocks. Called by the
  assistant's `search_knowledge` tool and by the UI preview.

Retrieval is deliberately simple (keyword/term overlap, no embeddings): the corpus is
one spec document per project, the assistant already knows the domain vocabulary, and
this keeps the whole path dependency-free and instant. What matters is that the cost
no longer scales with document size — see `search` for how that is bounded.
"""

from __future__ import annotations

from app import embeddings
import logging
import re
from dataclasses import dataclass

from sqlalchemy import delete, func, select

from app.db import db_session
from app.models import KnowledgeChunk

log = logging.getLogger("potato-test.knowledge")

# ---- chunking --------------------------------------------------------------

# Soft ceiling for one chunk. Split happens on a blank line, so a single long
# paragraph/table can exceed this — that is intentional, breaking mid-table would
# destroy exactly the rows a caller is searching for.
CHUNK_TARGET_CHARS = 2_000

# Hard ceiling for what the extraction layer will hand us. Not a truncation: files
# above this are rejected upstream (docparse.MAX_UPLOAD_BYTES) so the failure is
# visible instead of silently dropping the tail of a spec.
MAX_KNOWLEDGE_CHARS = 20_000_000

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class Chunk:
    index: int
    line_no: int
    heading: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


def chunk_text(text: str, target: int = CHUNK_TARGET_CHARS) -> list[Chunk]:
    """Split a document into searchable blocks.

    Blocks are separated by blank lines (paragraphs, markdown tables, list runs).
    Consecutive blocks are merged until `target` is reached, so a short-headed spec
    produces a few dense chunks instead of hundreds of one-liners.

    A markdown heading always starts a new chunk. Merging across a heading would label
    the whole merged run with the FIRST heading, so a hit from §5 would be reported as
    "§4.3 收费标准" — a wrong citation is worse than an extra chunk, and it is exactly
    the kind of error a reader cannot detect from the snippet alone.
    """
    if not text or not text.strip():
        return []

    lines = text.splitlines()
    blocks: list[tuple[int, str, bool]] = []  # (start line, block text, is_heading_block)
    buf: list[str] = []
    start = 1

    for i, line in enumerate(lines, 1):
        if line.strip():
            if not buf:
                start = i
            buf.append(line)
        elif buf:
            body = "\n".join(buf)
            blocks.append((start, body, bool(_HEADING_RE.match(body.split("\n", 1)[0]))))
            buf = []
    if buf:
        body = "\n".join(buf)
        blocks.append((start, body, bool(_HEADING_RE.match(body.split("\n", 1)[0]))))

    out: list[Chunk] = []
    cur_lines: list[str] = []
    cur_start = 1
    cur_heading = ""
    pending_heading = ""

    def flush() -> None:
        nonlocal cur_lines
        if cur_lines:
            out.append(
                Chunk(
                    index=len(out),
                    line_no=cur_start,
                    heading=cur_heading[:400],
                    text="\n\n".join(cur_lines),
                )
            )
        cur_lines = []

    for start_line, block, is_heading in blocks:
        # A heading ends the previous section. Flushing here (not after) is what keeps a
        # heading out of its predecessor's chunk — otherwise §5's title lands at the tail
        # of §4.3's chunk, and a snippet shown to the reader appears to be under §4.3.
        if is_heading:
            flush()
            pending_heading = _HEADING_RE.match(block.split("\n", 1)[0]).group(2).strip()[:400]

        if not cur_lines:
            cur_start = start_line
            cur_heading = pending_heading
        cur_lines.append(block)

        if sum(len(x) + 2 for x in cur_lines) >= target:
            flush()

    flush()
    return out


# ---- storage ---------------------------------------------------------------


async def replace_knowledge(
    session,
    project_id: int,
    text: str,
    source: str = "",
) -> dict:
    """Replace every chunk of a project with the chunks of `text` (delete + insert).

    Delete-then-insert rather than diffing: the caller semantics are "this is the
    project's spec now" (PUT /knowledge is a full replace), so a diff would only add
    failure modes. Returns a small summary for the API response.
    """
    chunks = chunk_text(text or "")
    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.project_id == project_id))
    total = 0
    for c in chunks:
        session.add(
            KnowledgeChunk(
                project_id=project_id,
                chunk_index=c.index,
                line_no=c.line_no,
                heading=c.heading,
                text=c.text,
                chars=c.chars,
                source=source[:400],
            )
        )
        total += c.chars
    await session.flush()
    return {"chunks": len(chunks), "chars": total}


async def append_knowledge(
    session,
    project_id: int,
    text: str,
    source: str = "",
) -> dict:
    """Append `text` as new chunks after the project's existing ones.

    Used by upload: the operator's spec box already holds earlier docs, and the SPA
    appends the extracted text rather than replacing it.
    """
    chunks = chunk_text(text or "")
    if not chunks:
        return {"chunks": 0, "chars": 0}
    base = (
        await session.execute(
            select(func.coalesce(func.max(KnowledgeChunk.chunk_index), -1)).where(
                KnowledgeChunk.project_id == project_id
            )
        )
    ).scalar_one()
    # Line numbers continue from the last chunk so citations stay monotonic.
    base_line = (
        await session.execute(
            select(func.coalesce(func.max(KnowledgeChunk.line_no), 0)).where(
                KnowledgeChunk.project_id == project_id
            )
        )
    ).scalar_one()

    added = 0
    for i, c in enumerate(chunks):
        session.add(
            KnowledgeChunk(
                project_id=project_id,
                chunk_index=base + 1 + i,
                line_no=base_line + c.line_no,
                heading=c.heading,
                text=c.text,
                chars=c.chars,
                source=source[:400],
            )
        )
        added += c.chars
    await session.flush()
    return {"chunks": len(chunks), "chars": added}


async def get_all_text(session, project_id: int) -> str:
    """Reassemble the whole document. Only for the UI editor / export — NOT retrieval.

    Reading everything back is exactly what chunking exists to avoid, so nothing on
    the assistant path may call this.
    """
    rows = (
        (
            await session.execute(
                select(KnowledgeChunk)
                .where(await effective_condition(session, project_id))
                .order_by(KnowledgeChunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    return "\n\n".join(r.text for r in rows)


async def stats(session, project_id: int) -> dict:
    chunks = (
        await session.execute(
            select(
                func.count(KnowledgeChunk.id),
                func.coalesce(func.sum(KnowledgeChunk.chars), 0),
            ).where(await effective_condition(session, project_id))
        )
    ).one()
    return {"chunks": int(chunks[0]), "chars": int(chunks[1])}


# ---- 「大系统」共享知识（2026-10-13）---------------------------------------------


async def embed_stats(session, project_id: int) -> dict:
    """向量化进度（2026-10-19）。界面据此决定要不要显示「补算向量」。"""
    scope = await effective_condition(session, project_id)
    total = (await session.execute(select(func.count(KnowledgeChunk.id)).where(scope))).scalar_one()
    done = (
        await session.execute(
            select(func.count(KnowledgeChunk.id)).where(
                scope,
                KnowledgeChunk.embedding.isnot(None),
                KnowledgeChunk.embed_model == embeddings.MODEL_TAG,
            )
        )
    ).scalar_one()
    return {
        "embedded": int(done),
        "total": int(total),
        "model": embeddings.MODEL_TAG,
        "available": embeddings.available(),
        "pending": int(total) - int(done),
    }


async def effective_condition(session, project_id: int):
    """「这个项目能用的知识块」的 SQL 条件：**自己的 + 所属大系统共享的**。

    用户的系统是一个大系统、下面挂着若干模块项目，每个模块都要用同一份系统级需规。
    让每个项目各存一份既费空间又必然漂移，所以大系统的资料只存一份
    （`KnowledgeChunk.group_id` 指向它、`project_id=0` 哨兵，见该模型），
    检索时合并进来 —— 上层传一次，下面各模块都搜得到。
    """
    from sqlalchemy import or_

    from app.models import Project

    gid = (
        await session.execute(select(Project.group_id).where(Project.id == project_id))
    ).scalar_one_or_none()
    cond = KnowledgeChunk.project_id == project_id
    if gid:
        cond = or_(cond, KnowledgeChunk.group_id == gid)
    return cond


async def replace_group_knowledge(session, group_id: int, text: str, source: str = "") -> dict:
    """换掉某个大系统的**共享**知识块（只动 group 的，不碰任何项目自己的）。"""
    chunks = chunk_text(text or "")
    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.group_id == group_id))
    total = 0
    for c in chunks:
        session.add(
            KnowledgeChunk(
                project_id=0,  # 哨兵：不属于任何项目，只属于这个大系统
                group_id=group_id,
                chunk_index=c.index,
                line_no=c.line_no,
                heading=c.heading,
                text=c.text,
                chars=c.chars,
                source=source[:400],
            )
        )
        total += c.chars
    await session.flush()
    return {"chunks": len(chunks), "chars": total}


async def get_group_text(session, group_id: int) -> str:
    rows = (
        (
            await session.execute(
                select(KnowledgeChunk.text)
                .where(KnowledgeChunk.group_id == group_id)
                .order_by(KnowledgeChunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    return "\n\n".join(rows)


async def group_stats(session, group_id: int) -> dict:
    row = (
        await session.execute(
            select(
                func.count(KnowledgeChunk.id),
                func.coalesce(func.sum(KnowledgeChunk.chars), 0),
            ).where(KnowledgeChunk.group_id == group_id)
        )
    ).one()
    return {"chunks": int(row[0]), "chars": int(row[1])}


async def delete_group_knowledge(session, group_id: int) -> None:
    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.group_id == group_id))


# ---- retrieval -------------------------------------------------------------

# How many chunks a single search may return, and how much text each contributes.
# These are the real bounds on the assistant's context cost, independent of how big
# the underlying document is.
SEARCH_LIMIT = 8
CHUNK_SNIPPET_CHARS = 1_200


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"[\s,，。;；、/|\\()（）\[\]【】:：]+", query or "") if t]


def _score(text_low: str, terms: list[str], heading_low: str) -> int:
    """Term-overlap score. A hit in the heading counts double.

    Deliberately crude. With one spec document per project and a domain-savvy
    question, overlap on distinctive terms (table names, field names, Chinese
    business nouns) separates the right blocks from the noise well enough — and it
    costs no external calls, which matters because this runs inside a chat turn.
    """
    if not terms:
        return 0
    score = 0
    for t in terms:
        tl = t.lower()
        if tl in heading_low:
            score += 2
        if tl in text_low:
            score += 1
    return score


VEC_MIN_COS = 0.50  # 向量的最低可用相似度：低于这个基本是"语义上不相关"（实测噪声区）


async def embed_project(project_id: int, force: bool = False) -> dict:
    """给项目的知识块算向量（2026-10-19）。

    `force=False` 只补没算过 / 模型换过的块 —— 重传一份需规只会给新增块做增量，不重算全部。
    """
    if not embeddings.available():
        return {"ok": False, "reason": "向量模型不可用（缺模型文件或 onnxruntime）", "embedded": 0}

    async with db_session() as s:
        scope = await effective_condition(s, project_id)
        total = (await s.execute(select(func.count(KnowledgeChunk.id)).where(scope))).scalar_one()
        q = select(KnowledgeChunk).where(scope)
        if not force:
            q = q.where(
                (KnowledgeChunk.embedding.is_(None))
                | (KnowledgeChunk.embed_model.is_(None))
                | (KnowledgeChunk.embed_model != embeddings.MODEL_TAG)
            )
        rows = (await s.execute(q.order_by(KnowledgeChunk.chunk_index))).scalars().all()
        pending = [(r.id, (r.heading + "\n" + r.text) if r.heading else r.text) for r in rows]

    if not pending:
        return {"ok": True, "embedded": 0, "total": int(total), "model": embeddings.MODEL_TAG,
                "note": "所有块都已经是当前模型的向量"}

    vecs = embeddings.embed_documents([t for _, t in pending])
    if vecs is None:
        return {"ok": False, "reason": "推理失败，本次没有写入向量", "embedded": 0, "total": int(total)}

    async with db_session() as s:
        for (cid, _), v in zip(pending, vecs):
            row = await s.get(KnowledgeChunk, cid)
            if row is not None:
                row.embedding = embeddings.dumps(v)
                row.embed_model = embeddings.MODEL_TAG
    return {"ok": True, "embedded": len(pending), "total": int(total), "model": embeddings.MODEL_TAG}


async def search(project_id: int, query: str, limit: int = SEARCH_LIMIT) -> dict:
    """Best-matching chunks for `query` —— 向量 + 关键词**混合**检索（2026-10-19）。

    两路各取所长：
      * **向量**：能命中"换了说法"的问题（「场地费」→「教学场地收费标准」），关键词做不到；
      * **关键词**：精确词（字段名、表名、错误码）向量反而容易漏，靠它兜底。

    合并：向量（余弦 ≥ VEC_MIN_COS）优先，再用关键词结果补满 limit，按块 id 去重。
    模型不可用 / 这个项目还没算向量 → **完全退回原来的关键词检索**，行为不变。
    """
    q = (query or "").strip()
    terms = _terms(query)
    qvec = embeddings.embed_query(q) if q else None

    async with db_session() as s:
        scope = await effective_condition(s, project_id)
        total = (await s.execute(select(func.count(KnowledgeChunk.id)).where(scope))).scalar_one()

        rows_vec: list[KnowledgeChunk] = []
        if qvec is not None:
            rows_vec = (
                (
                    await s.execute(
                        select(KnowledgeChunk)
                        .where(scope, KnowledgeChunk.embedding.isnot(None))
                        .order_by(KnowledgeChunk.chunk_index)
                        .limit(5_000)  # 单项目上限；超过这个规模该换成专用向量库了
                    )
                )
                .scalars()
                .all()
            )

        rows_kw: list[KnowledgeChunk] = []
        if terms:
            cond = None
            for t in terms[:12]:
                like = f"%{t}%"
                c = KnowledgeChunk.text.ilike(like) | KnowledgeChunk.heading.ilike(like)
                cond = c if cond is None else (cond | c)
            if cond is not None:
                rows_kw = (
                    (
                        await s.execute(
                            select(KnowledgeChunk)
                            .where(scope, cond)
                            .order_by(KnowledgeChunk.chunk_index)
                            .limit(2_000)
                        )
                    )
                    .scalars()
                    .all()
                )

    # 向量排序（都已 L2 归一化 → 点积就是余弦）
    vec_scored: list[tuple[float, KnowledgeChunk]] = []
    if qvec:
        for r in rows_vec:
            v = embeddings.loads(r.embedding or "")
            if not v or len(v) != len(qvec):
                continue
            cos = sum(a * b for a, b in zip(qvec, v))
            if cos >= VEC_MIN_COS:
                vec_scored.append((cos, r))
        vec_scored.sort(key=lambda x: -x[0])

    # 关键词排序（原逻辑不动）
    kw_scored: list[tuple[int, KnowledgeChunk]] = []
    for r in rows_kw:
        sc = _score(r.text.lower(), terms, (r.heading or "").lower())
        if sc:
            kw_scored.append((sc, r))
    kw_scored.sort(key=lambda x: (-x[0], x[1].chunk_index))

    # 合并：**两路各留配额**，再互相补位。
    # 早期版本是"向量填满、剩多少给关键词"，实测发现的坑：向量命中多时关键词一个名额都没有，
    # 而精确词（字段名/错误码）恰恰只有关键词能命中。所以按 2:1 预留，谁不够对方补。
    def _hit(r, via: str, score=None) -> dict:
        h = {"line": r.line_no, "heading": r.heading, "text": r.text[:CHUNK_SNIPPET_CHARS], "via": via}
        if score is not None:
            h["score"] = score
        return h

    vec_quota = max(1, limit - max(1, limit // 3))
    picked: list[dict] = []
    seen: set[int] = set()
    for cos, r in vec_scored[:vec_quota]:
        if r.id in seen:
            continue
        seen.add(r.id)
        picked.append(_hit(r, "vector", round(cos, 3)))
    for _sc, r in kw_scored:
        if len(picked) >= limit:
            break
        if r.id in seen:
            continue
        seen.add(r.id)
        picked.append(_hit(r, "keyword"))
    for cos, r in vec_scored:  # 关键词不够，向量补满
        if len(picked) >= limit:
            break
        if r.id in seen:
            continue
        seen.add(r.id)
        picked.append(_hit(r, "vector", round(cos, 3)))
    for _sc, r in kw_scored:  # 还差就再用关键词补（两边都少见的极端情况）
        if len(picked) >= limit:
            break
        if r.id in seen:
            continue
        seen.add(r.id)
        picked.append(_hit(r, "keyword"))

    mode = "vector" if vec_scored else ("keyword" if kw_scored else "none")
    return {
        "query": query,
        "mode": mode,
        "matched_blocks": len(vec_scored) + len(kw_scored),
        "total_blocks": int(total),
        "hits": picked,
        "note": "命中为空说明资料里没有；不要臆造。" if not picked else "按相关度返回的片段。",
    }


async def backfill_from_settings(session, project_ids: list[int]) -> int:
    """Move legacy `proj_<pid>_knowledge` strings into chunks, once.

    Runs at startup. A project is only migrated when it has NO chunks yet — so a
    project whose knowledge was already edited through the new UI is never touched.
    The old AppSetting rows are left in place as a safety net; they are simply no
    longer read.
    """
    from app.models import AppSetting

    moved = 0
    for pid in project_ids:
        existing = (
            await session.execute(
                select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.project_id == pid)
            )
        ).scalar_one()
        if existing:
            continue
        row = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == f"proj_{pid}_knowledge")
            )
        ).scalar_one_or_none()
        if not row or not (row.value or "").strip():
            continue
        res = await replace_knowledge(session, pid, row.value, source="(migrated)")
        log.info(
            "knowledge: migrated project %s legacy text into %s chunks (%s chars)",
            pid,
            res["chunks"],
            res["chars"],
        )
        moved += 1
    return moved
