"""Библиотека workspace: `refs.bib` как источник ключей цитирования, PDF с текстом, индекс FTS5, предложения OpenAlex.

Файлы первичны: `index.sqlite` пересобирается из `refs.bib` и `pdf/` командой reindex.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import openalex
from .bibtex import Entry, normalize_doi, normalize_title, parse

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS refs (
    key TEXT PRIMARY KEY, type TEXT, title TEXT, authors TEXT, year TEXT, doi TEXT, pdf TEXT,
    has_text INTEGER DEFAULT 0, added_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(key UNINDEXED, title, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY, query TEXT, project TEXT, openalex_id TEXT, title TEXT, authors TEXT, year TEXT,
    doi TEXT, venue TEXT, work TEXT, status TEXT DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now'))
);
"""
CHUNK, OVERLAP = 1000, 200
_DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)")
_FTS_TOKEN = re.compile(r"\w+", re.UNICODE)


class LibraryError(RuntimeError):
    pass


def is_stub(e: Entry) -> bool:
    """Запись-заглушка для PDF без метаданных: поле `wwp = {stub}`; настоящая запись с тем же DOI её вытесняет."""
    return e.fields.get("wwp") == "stub"


class Library:
    """Каталог `workspace/library`: refs.bib, pdf/, index.sqlite."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.pdf_dir = self.root / "pdf"
        self.bib_path = self.root / "refs.bib"

    def db(self) -> sqlite3.Connection:
        """Соединение на вызов; закрывается сборщиком.

        ponytail: executescript(SCHEMA) на каждое соединение — для библиотеки одного автора незаметно;
        при тысячах записей держать одно соединение на Library.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / "index.sqlite")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    # --- записи -------------------------------------------------------------------------------
    def entries(self) -> list[Entry]:
        return parse(self.bib_path.read_text(encoding="utf-8")) if self.bib_path.exists() else []

    def keys(self) -> set[str]:
        return {e.key for e in self.entries()}

    def citable_keys(self) -> set[str]:
        """Ключи, которыми можно ссылаться в артефактах: заглушки PDF без метаданных исключены."""
        return {e.key for e in self.entries() if not is_stub(e)}

    def records(self) -> list[dict[str, Any]]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM refs ORDER BY added_at DESC, key").fetchall()]

    def add_entries(self, new: list[Entry]) -> dict[str, list[str]]:
        """Дописать записи в refs.bib; дубликаты по ключу или DOI пропускаются."""
        have = self.entries()
        keys, dois = {e.key for e in have}, {e.doi for e in have if e.doi and not is_stub(e)}
        added, skipped = [], []
        for e in new:
            if e.key in keys or (e.doi and e.doi in dois):
                skipped.append(e.key)
                continue
            keys.add(e.key)
            dois.add(e.doi)
            added.append(e.key)
            self._append_bib(e)
            self._index_entry(e)
        self._link_pdfs()
        return {"added": added, "skipped": skipped}

    def _append_bib(self, e: Entry) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.bib_path.open("a", encoding="utf-8") as f:
            f.write(("\n" if self.bib_path.stat().st_size else "") + e.dump())

    def _index_entry(self, e: Entry, pdf: str | None = None, text: str = "") -> None:
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO refs(key, type, title, authors, year, doi, pdf, has_text) "
                       "VALUES (?,?,?,?,?,?,?,?)", (e.key, e.type, e.title, e.authors, e.year, e.doi, pdf, bool(text)))
            db.execute("DELETE FROM chunks WHERE key = ?", (e.key,))
            for piece in _chunks(text) or [""]:
                db.execute("INSERT INTO chunks(key, title, text) VALUES (?,?,?)", (e.key, e.title, piece))

    def remove(self, key: str) -> None:
        """Убрать запись из refs.bib (вырезается её исходный текст, остальное не трогается) и из индекса."""
        entry = next((e for e in self.entries() if e.key == key), None)
        if entry is None:
            raise LibraryError(f"записи {key} нет")
        text = self.bib_path.read_text(encoding="utf-8")
        self.bib_path.write_text(re.sub(r"\n{3,}", "\n\n", text.replace(entry.raw, "", 1)), encoding="utf-8")
        with self.db() as db:
            db.execute("DELETE FROM refs WHERE key = ?", (key,))
            db.execute("DELETE FROM chunks WHERE key = ?", (key,))

    # --- импорт -------------------------------------------------------------------------------
    def import_path(self, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if path.suffix.lower() == ".bib":
            return self.add_entries(parse(path.read_text(encoding="utf-8", errors="replace")))
        if path.suffix.lower() == ".pdf":
            return self.import_pdf(path)
        raise LibraryError(f"{path.name}: поддерживаются .bib и .pdf")

    def import_pdf(self, path: str | Path) -> dict[str, Any]:
        """Скопировать PDF, извлечь текст, связать с записью по DOI или заголовку; иначе — заглушка @misc."""
        src = Path(path)
        dst = self.pdf_dir / re.sub(r"[^\w.\-]+", "_", src.name)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        text = pdf_text(dst)
        entry = self._match(text, self.entries())
        matched = entry is not None
        entry = entry or self._stub(dst, text)
        if entry.key not in self.keys():
            self._append_bib(entry)
        self._index_entry(entry, pdf=dst.name, text=text)
        return {"added": [entry.key], "skipped": [], "pdf": dst.name, "has_text": bool(text), "matched": matched}

    def _match(self, text: str, entries: list[Entry]) -> Entry | None:
        """Запись для текста PDF: по DOI с первых страниц, иначе по заголовку на первой странице.

        ponytail: заголовок ищется только в первых 1500 символах и только длиннее 25 знаков, чтобы работа,
        процитированная во введении, не притянула чужой PDF; надёжнее — метаданные PDF (pdfinfo).
        """
        head = text[:6000]
        doi = _DOI_IN_TEXT.search(head)
        found = normalize_doi(doi.group(1).rstrip(".,;)")) if doi else ""
        if found:
            hit = next((e for e in entries if e.doi == found and not is_stub(e)), None)
            if hit:
                return hit
        first_page = normalize_title(text[:1500])
        return next((e for e in entries if len(e.title) > 25 and normalize_title(e.title) in first_page), None)

    def _stub(self, pdf: Path, text: str) -> Entry:
        doi = _DOI_IN_TEXT.search(text[:6000])
        key = re.sub(r"[^A-Za-z0-9_]", "", pdf.stem)[:40] or "pdf"
        taken, base, i = self.keys(), key, 0
        while key in taken:
            i += 1
            key = f"{base}{chr(ord('a') + i)}"
        fields = {"title": pdf.stem, "file": f"pdf/{pdf.name}", "wwp": "stub",
                  "note": "метаданные не найдены; уточните через OpenAlex"}
        if doi:
            fields["doi"] = normalize_doi(doi.group(1).rstrip(".,;)"))
        return Entry(key=key, type="misc", fields=fields)

    def _link_pdfs(self) -> None:
        """Заглушка PDF с DOI уступает место настоящей записи с тем же DOI: текст PDF переходит к ней."""
        entries = self.entries()
        stubs = [e for e in entries if is_stub(e) and e.doi]
        for stub in stubs:
            real = next((e for e in entries if e.doi == stub.doi and e.key != stub.key), None)
            if real is None:
                continue
            pdf = self.pdf_dir / Path(stub.fields.get("file", "")).name
            self.remove(stub.key)
            self._index_entry(real, pdf=pdf.name if pdf.exists() else None, text=pdf_text(pdf) if pdf.exists() else "")

    def reindex(self) -> int:
        """Пересобрать index.sqlite из refs.bib и pdf/."""
        with self.db() as db:
            db.execute("DELETE FROM refs")
            db.execute("DELETE FROM chunks")
        entries = self.entries()
        by_pdf: dict[str, Entry] = {}
        for e in entries:
            file = e.fields.get("file", "")
            if file and (self.pdf_dir / Path(file).name).exists():
                by_pdf[Path(file).name] = e
            self._index_entry(e)
        for pdf in sorted(self.pdf_dir.glob("*.pdf")) if self.pdf_dir.exists() else []:
            text = pdf_text(pdf)
            hit = by_pdf.get(pdf.name) or self._match(text, entries)
            if hit:
                self._index_entry(hit, pdf=pdf.name, text=text)
        return len(entries)

    # --- поиск --------------------------------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """FTS5 с BM25: сначала все слова, при пустом ответе — любое из слов. Одна строка на запись.

        Вспомогательные функции FTS5 работают только в простом запросе к таблице, поэтому группировка по ключу —
        в Python.
        """
        tokens = [f'"{t}"' for t in _FTS_TOKEN.findall(query)]
        if not tokens:
            return []
        with self.db() as db:
            for joiner in (" AND ", " OR "):
                rows = db.execute("SELECT key, snippet(chunks, -1, '«', '»', '…', 14) AS snippet, bm25(chunks) AS rank "
                                  "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
                                  (joiner.join(tokens), limit * 20)).fetchall()
                if not rows:
                    continue
                best: dict[str, sqlite3.Row] = {}
                for r in rows:
                    best.setdefault(r["key"], r)
                hits = []
                for key, r in list(best.items())[:limit]:
                    ref = db.execute("SELECT title, year, authors FROM refs WHERE key = ?", (key,)).fetchone()
                    hits.append({"key": key, "title": ref["title"] if ref else "", "year": ref["year"] if ref else "",
                                 "authors": ref["authors"] if ref else "", "snippet": r["snippet"], "rank": r["rank"]})
                return hits
        return []

    # --- предложения OpenAlex -----------------------------------------------------------------
    def add_suggestions(self, works: list[dict[str, Any]], query: str, project: str | None = None) -> int:
        """Сохранить находки для подтверждения автором; уже известные DOI и повторы пропускаются."""
        known = {e.doi for e in self.entries() if e.doi}
        n = 0
        with self.db() as db:
            seen = {r[0] for r in db.execute("SELECT openalex_id FROM suggestions").fetchall()}
            for w in works:
                if w["id"] in seen or (w["doi"] and normalize_doi(w["doi"]) in known):
                    continue
                db.execute("INSERT INTO suggestions(query, project, openalex_id, title, authors, year, doi, venue, work)"
                           " VALUES (?,?,?,?,?,?,?,?,?)", (query, project, w["id"], w["title"], ", ".join(w["authors"]),
                                                          w["year"], w["doi"], w["venue"], json.dumps(w, ensure_ascii=False)))
                seen.add(w["id"])
                n += 1
        return n

    def suggestions(self, status: str = "pending") -> list[dict[str, Any]]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM suggestions WHERE status = ? ORDER BY id DESC",
                                                (status,)).fetchall()]

    def resolve_suggestion(self, sid: int, accept: bool) -> str | None:
        """Принять (запись в refs.bib) или отклонить предложение; возвращает ключ принятой записи."""
        with self.db() as db:
            row = db.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                raise LibraryError(f"предложения {sid} нет")
            db.execute("UPDATE suggestions SET status = ? WHERE id = ?", ("accepted" if accept else "rejected", sid))
        if not accept:
            return None
        entry = openalex.to_entry(json.loads(row["work"]), self.keys())
        return entry.key if self.add_entries([entry])["added"] else None

    def fetch_openalex(self, query: str, project: str | None, settings: dict[str, Any], limit: int = 5) -> int:
        """Поиск в OpenAlex по настройкам `library:` providers.yaml; сетевые ошибки — предупреждение, не сбой."""
        if settings.get("openalex", True) is False:
            return 0
        try:
            works = openalex.search(query, limit=limit, mailto=str(settings.get("mailto") or ""))
        except (OSError, ValueError, KeyError, TypeError) as e:  # сеть, таймаут, формат ответа: не ломаем стадию
            logger.warning("OpenAlex «%s»: %s", query, e)
            return 0
        return self.add_suggestions(works, query, project)


def pdf_text(path: Path) -> str:
    """Текст PDF через pdftotext (poppler); без него — пустая строка, запись индексируется по метаданным."""
    if not shutil.which("pdftotext"):
        logger.warning("pdftotext не найден; текст %s не извлечён", path.name)
        return ""
    proc = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], check=False, capture_output=True, text=True)
    if proc.returncode:
        logger.warning("pdftotext %s: %s", path.name, proc.stderr.strip()[-200:])
        return ""
    return re.sub(r"[ \t]+", " ", proc.stdout).strip()


def _chunks(text: str) -> list[str]:
    return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK - OVERLAP)] if text else []


__all__ = ["Entry", "Library", "LibraryError", "is_stub", "pdf_text"]
