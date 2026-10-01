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
                .where(KnowledgeChunk.project_id == project_id)
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
            ).where(KnowledgeChunk.project_id == project_id)
        )
    ).one()
    return {"chunks": int(chunks[0]), "chars": int(chunks[1])}


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


async def search(project_id: int, query: str, limit: int = SEARCH_LIMIT) -> dict:
    """Best-matching chunks for `query`.

    Bounded work: the DB narrows to rows containing at least one term (LIKE), and
    scoring only ever touches those rows. This is what makes a 20 MB spec as cheap to
    search as a 20 KB one — the previous implementation walked every paragraph in
    Python on every call.
    """
    terms = _terms(query)
    if not terms:
        return {
            "query": query,
            "matched_blocks": 0,
            "total_blocks": 0,
            "hits": [],
            "note": "查询为空。",
        }

    async with db_session() as s:
        total = (
            await s.execute(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.project_id == project_id
                )
            )
        ).scalar_one()

        # Prefilter in SQL. ilike on each term with OR — broad on purpose, because
        # scoring decides the ranking; this only keeps the candidate set small enough
        # that a huge document does not turn into a huge Python loop.
        cond = None
        for t in terms[:12]:  # cap the OR-chain; a 50-word query is not a query
            like = f"%{t}%"
            c = KnowledgeChunk.text.ilike(like) | KnowledgeChunk.heading.ilike(like)
            cond = c if cond is None else (cond | c)
        if cond is None:
            return {"query": query, "matched_blocks": 0, "total_blocks": int(total), "hits": []}

        rows = (
            (
                await s.execute(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.project_id == project_id, cond)
                    .order_by(KnowledgeChunk.chunk_index)
                    .limit(2_000)  # hard stop; scoring below re-ranks what fits
                )
            )
            .scalars()
            .all()
        )

    scored: list[tuple[int, KnowledgeChunk]] = []
    for r in rows:
        sc = _score(r.text.lower(), terms, (r.heading or "").lower())
        if sc:
            scored.append((sc, r))
    scored.sort(key=lambda x: (-x[0], x[1].chunk_index))

    hits = [
        {
            "line": r.line_no,
            "heading": r.heading,
            "text": r.text[:CHUNK_SNIPPET_CHARS],
        }
        for _, r in scored[:limit]
    ]
    return {
        "query": query,
        "matched_blocks": len(scored),
        "total_blocks": int(total),
        "hits": hits,
        "note": "命中为空说明资料里没有；不要臆造。" if not hits else "按相关度返回的片段。",
    }


# ---- legacy migration ------------------------------------------------------


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
