from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber

ALIASES: dict[str, tuple[str, ...]] = {
    "侦查": ("侦查", "侦察", "调查", "搜查", "搜索", "寻找线索", "观察"),
    "聆听": ("聆听", "倾听", "听见", "声音", "动静", "偷听"),
    "图书馆使用": ("图书馆使用", "查资料", "检索资料", "文献", "档案"),
    "心理学": ("心理学", "察言观色", "判断说谎", "情绪", "动机"),
    "理智": ("理智", "san", "疯狂", "恐惧", "精神", "惊吓"),
    "战斗": ("战斗", "攻击", "伤害", "闪避", "反击", "格斗", "射击"),
    "检定难度": ("检定", "普通成功", "困难成功", "极难成功", "大成功", "孤注一掷"),
    "奖励骰惩罚骰": ("奖励骰", "惩罚骰", "十位骰", "优势", "劣势"),
    "追逐": ("追逐", "追赶", "逃跑", "移动行动", "速度"),
    "幸运": ("幸运", "luck", "运气"),
    "魔法": ("魔法", "法术", "咒文", "魔法值", "施法"),
}

RULE_PHRASES = (
    "临时疯狂",
    "不定时疯狂",
    "永久疯狂",
    "理智检定",
    "疯狂发作",
    "奖励骰",
    "惩罚骰",
    "孤注一掷",
    "大成功",
    "大失败",
    "困难成功",
    "极难成功",
    "成长检定",
    "信用评级",
    "克苏鲁神话",
    "魔法值",
    "战斗轮",
    "追逐轮",
)

SECTION_HINTS: dict[str, tuple[str, ...]] = {
    "理智": ("理智", "疯狂", "san"),
    "游戏系统": ("检定", "奖励骰", "惩罚骰", "幸运", "孤注一掷"),
    "战斗": ("战斗", "攻击", "闪避", "反击", "伤害"),
    "追逐": ("追逐", "追赶", "逃跑"),
    "魔法": ("魔法", "法术", "咒文", "施法"),
}


@dataclass(frozen=True, slots=True)
class RuleSource:
    book_id: str
    title: str
    path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_text(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\x00", "")
    text = re.sub(r"\(cid:\d+\)", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _heading(text: str, fallback: str) -> str:
    for line in text.splitlines()[:12]:
        candidate = line.strip(" ·•—-\t")
        if 2 <= len(candidate) <= 40 and not re.search(r"[。！？；]$", candidate):
            return candidate
    return fallback


def _split_chunks(text: str, *, target: int = 1200, overlap: int = 120) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = (
            [paragraph[index : index + target] for index in range(0, len(paragraph), target)]
            if len(paragraph) > target
            else [paragraph]
        )
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if current and len(candidate) > target:
                chunks.append(current)
                current = f"{current[-overlap:]}\n\n{piece}".strip()
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def build_rule_index(output: Path, sources: Iterable[RuleSource]) -> dict[str, Any]:
    """离线构建只包含抽取文本的 FTS5 索引；不复制原 PDF。"""
    source_list = list(sources)
    if {item.book_id for item in source_list} != {"investigator", "keeper", "magic"}:
        raise ValueError("规则索引必须同时提供 investigator、keeper、magic 三本书")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_name(f".{output.name}.building")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    page_count = 0
    chunk_count = 0
    warnings: list[str] = []
    try:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE rule_chunks (
                chunk_id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                source_title TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                pdf_page INTEGER NOT NULL,
                section TEXT NOT NULL,
                text TEXT NOT NULL,
                search_text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE rule_chunks_fts USING fts5(
                chunk_id UNINDEXED, search_text, tokenize='trigram'
            );
            """
        )
        for source in source_list:
            path = source.path.expanduser().resolve()
            if not path.is_file() or path.suffix.lower() != ".pdf":
                raise ValueError(f"规则书不是可读取的 PDF：{path}")
            source_hash = _sha256(path)
            empty_pages: list[int] = []
            with pdfplumber.open(path) as document:
                page_count += len(document.pages)
                for page_index, page in enumerate(document.pages, start=1):
                    raw = page.extract_text(
                        x_tolerance=2, y_tolerance=3, use_text_flow=True, layout=False
                    )
                    text = _clean_text(raw or "")
                    if len(text) < 20:
                        empty_pages.append(page_index)
                        continue
                    heading = _heading(text, f"PDF page {page_index}")
                    for chunk_index, chunk in enumerate(_split_chunks(text), start=1):
                        chunk_id = f"{source.book_id}:p{page_index:04d}:c{chunk_index:02d}"
                        tags = [
                            canonical
                            for canonical, aliases in ALIASES.items()
                            if any(
                                alias.lower() in f"{heading}\n{chunk}".lower() for alias in aliases
                            )
                        ]
                        search_text = " ".join([source.title, heading, *tags, chunk]).lower()
                        connection.execute(
                            """INSERT INTO rule_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                chunk_id,
                                source.book_id,
                                source.title,
                                source_hash,
                                page_index,
                                heading,
                                chunk,
                                search_text,
                            ),
                        )
                        connection.execute(
                            "INSERT INTO rule_chunks_fts VALUES (?, ?)", (chunk_id, search_text)
                        )
                        chunk_count += 1
            if empty_pages:
                warnings.append(f"{source.book_id}: {len(empty_pages)} 页未抽取到有效文本")
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("page_count", str(page_count)),
                ("chunk_count", str(chunk_count)),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    temporary.chmod(0o600)
    temporary.replace(output)
    return {"page_count": page_count, "chunk_count": chunk_count, "warnings": warnings}


def _query_terms(query: str) -> list[str]:
    normalized = query.lower().strip()
    terms = [phrase for phrase in RULE_PHRASES if phrase in normalized]
    simplified = re.sub(
        r"如何|怎么|怎样|是否|可以|应该|判定|规则|是什么|有什么|多少|进行|需要|时候|关于",
        " ",
        normalized,
    )
    terms.extend(re.findall(r"[\u4e00-\u9fff]{2,12}|[a-zA-Z]{2,20}", simplified))
    for canonical, aliases in ALIASES.items():
        if canonical.lower() in normalized or any(alias.lower() in normalized for alias in aliases):
            terms.extend((canonical, *aliases))
    return list(dict.fromkeys(term.strip().lower() for term in terms if len(term.strip()) >= 2))


def _query_signals(query: str) -> list[str]:
    normalized = query.lower().strip()
    signals = [phrase for phrase in RULE_PHRASES if phrase in normalized]
    simplified = re.sub(
        r"如何|怎么|怎样|是否|可以|应该|判定|规则|是什么|有什么|多少|进行|需要|时候|关于",
        " ",
        normalized,
    )
    signals.extend(re.findall(r"[\u4e00-\u9fff]{2,12}|[a-zA-Z]{2,20}", simplified))
    return list(dict.fromkeys(signal for signal in signals if len(signal) >= 2))


def _relevance(row: sqlite3.Row, query: str, signals: list[str]) -> float:
    text = re.sub(r"\s+", "", str(row["text"]).lower())
    section = re.sub(r"\s+", "", str(row["section"]).lower())
    normalized_query = re.sub(r"\s+", "", query.lower())
    score = -float(row["rank"])
    if normalized_query in text:
        score += 100.0
    positions: list[int] = []
    for position, term in enumerate(signals):
        compact = term.replace(" ", "")
        offset = text.find(compact)
        if offset >= 0:
            positions.append(offset)
            score += len(compact) * (12.0 if position < 3 else 4.0)
            score += min(text.count(compact), 3) * 3.0
        if compact and compact in section:
            score += 16.0
    if len(positions) >= 2 and max(positions) - min(positions) <= 400:
        score += 60.0
    normalized_query_with_spaces = query.lower()
    for section_name, hints in SECTION_HINTS.items():
        if section_name in section and any(hint in normalized_query_with_spaces for hint in hints):
            score += 220.0
    return score


class RuleIndex:
    def __init__(self, path: Path) -> None:
        self.path = path

    def health(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"ready": False, "error": f"规则索引不存在：{self.path}"}
        try:
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
                books = [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT book_id FROM rule_chunks ORDER BY book_id"
                    )
                ]
            return {
                "ready": set(books) == {"investigator", "keeper", "magic"},
                "books": books,
                "page_count": int(metadata.get("page_count", 0)),
                "chunk_count": int(metadata.get("chunk_count", 0)),
            }
        except (sqlite3.Error, ValueError) as error:
            return {"ready": False, "error": f"规则索引损坏：{error}"}

    def search(self, query: str, *, book: str, limit: int) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("规则查询不能为空")
        if book not in {"all", "investigator", "keeper", "magic"}:
            raise ValueError("book 必须是 all、investigator、keeper 或 magic")
        if not 1 <= limit <= 6:
            raise ValueError("limit 必须在 1 到 6 之间")
        health = self.health()
        if not health.get("ready"):
            raise RuntimeError(str(health.get("error") or "三本规则书索引尚未准备完成"))
        terms = _query_terms(query)
        signals = _query_signals(query)
        match = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:20])
        book_clause = "" if book == "all" else "AND chunk.book_id = ?"
        parameters: list[Any] = [match]
        if book != "all":
            parameters.append(book)
        candidate_limit = max(240, limit * 40)
        parameters.append(candidate_limit)
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows: list[sqlite3.Row] = []
            if match:
                try:
                    rows = connection.execute(
                        f"""SELECT chunk.*, bm25(rule_chunks_fts) AS rank
                            FROM rule_chunks_fts
                            JOIN rule_chunks AS chunk USING (chunk_id)
                            WHERE rule_chunks_fts MATCH ? {book_clause}
                            ORDER BY rank, chunk.pdf_page LIMIT ?""",
                        parameters,
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if signals:
                direct_clause = " OR ".join("search_text LIKE ?" for _ in signals[:5])
                direct_parameters: list[Any] = [f"%{signal}%" for signal in signals[:5]]
                if book != "all":
                    direct_parameters.append(book)
                direct_parameters.append(candidate_limit)
                direct_rows = connection.execute(
                    f"""SELECT chunk.*, 0.0 AS rank FROM rule_chunks AS chunk
                        WHERE ({direct_clause}) {book_clause}
                        ORDER BY pdf_page LIMIT ?""",
                    direct_parameters,
                ).fetchall()
                by_id = {str(row["chunk_id"]): row for row in rows}
                for row in direct_rows:
                    by_id.setdefault(str(row["chunk_id"]), row)
                rows = list(by_id.values())
            if not rows:
                fallback_parameters: list[Any] = [f"%{query.strip().lower()}%"]
                if book != "all":
                    fallback_parameters.append(book)
                fallback_parameters.append(candidate_limit)
                rows = connection.execute(
                    f"""SELECT chunk.*, 0.0 AS rank FROM rule_chunks AS chunk
                        WHERE search_text LIKE ? {book_clause}
                        ORDER BY pdf_page LIMIT ?""",
                    fallback_parameters,
                ).fetchall()
        ranked = sorted(
            rows,
            key=lambda row: (_relevance(row, query, signals), -int(row["pdf_page"])),
            reverse=True,
        )[:limit]
        return [
            {
                "chunk_id": str(row["chunk_id"]),
                "book": str(row["book_id"]),
                "source_title": str(row["source_title"]),
                "pdf_page": int(row["pdf_page"]),
                "section": str(row["section"]),
                "text": str(row["text"]),
                "score": round(_relevance(row, query, signals), 4),
            }
            for row in ranked
        ]
