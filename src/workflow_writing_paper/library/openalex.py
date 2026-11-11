"""Поиск в OpenAlex (без ключа) и перевод найденной работы в запись BibTeX."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from .bibtex import Entry, clean, make_key

logger = logging.getLogger(__name__)

API = "https://api.openalex.org/works"
FIELDS = ("id,doi,title,publication_year,authorships,primary_location,type,abstract_inverted_index,cited_by_count,"
          "referenced_works,best_oa_location,locations,is_retracted")
MAX_IDS = 50  # фильтр ids.openalex принимает до 50 значений через «|»
_TYPES = {"article": "article", "book": "book", "book-chapter": "incollection", "conference-paper": "inproceedings",
          "proceedings-article": "inproceedings", "dissertation": "phdthesis", "preprint": "misc"}
_VENUE_FIELD = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle"}


def search(query: str, limit: int = 10, mailto: str = "", timeout: float = 15.0) -> list[dict[str, Any]]:
    """Работы по текстовому запросу: сводки `work_summary`."""
    return _get({"search": query, "per-page": str(limit)}, mailto, timeout)


def by_ids(ids: list[str], mailto: str = "", timeout: float = 15.0) -> list[dict[str, Any]]:
    """Работы по идентификаторам OpenAlex (`https://openalex.org/W…` или `W…`), не больше MAX_IDS за запрос."""
    short = [i.rsplit("/", 1)[-1] for i in ids[:MAX_IDS] if i]
    if not short:
        return []
    return _get({"filter": "ids.openalex:" + "|".join(short), "per-page": str(len(short))}, mailto, timeout)


def _get(params: dict[str, str], mailto: str, timeout: float) -> list[dict[str, Any]]:
    params = {**params, "select": FIELDS}
    if mailto:
        params["mailto"] = mailto
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": f"wwp/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise TypeError("OpenAlex: неожиданный формат ответа")
    return [work_summary(w) for w in data["results"]]


def abstract(inverted: dict[str, list[int]] | None) -> str:
    """Аннотация из инвертированного индекса OpenAlex: слово → позиции."""
    if not isinstance(inverted, dict):
        return ""
    at = {pos: word for word, positions in inverted.items() for pos in positions if isinstance(pos, int)}
    return " ".join(at[i] for i in sorted(at))


def work_summary(work: dict[str, Any]) -> dict[str, Any]:
    source = (work.get("primary_location") or {}).get("source") or {}
    authors = [str((a.get("author") or {}).get("display_name") or "") for a in work.get("authorships") or []]
    best = str((work.get("best_oa_location") or {}).get("pdf_url") or "")
    others = [str(loc.get("pdf_url")) for loc in work.get("locations") or [] if isinstance(loc, dict) and loc.get("pdf_url")]
    return {"id": str(work.get("id") or ""), "doi": str(work.get("doi") or "").replace("https://doi.org/", ""),
            "title": str(work.get("title") or ""), "year": str(work.get("publication_year") or ""),
            "authors": [a for a in authors if a], "venue": str(source.get("display_name") or ""),
            "type": str(work.get("type") or ""), "abstract": abstract(work.get("abstract_inverted_index")),
            "cited_by": int(work.get("cited_by_count") or 0), "refs": [str(r) for r in work.get("referenced_works") or []],
            "pdf_urls": list(dict.fromkeys(u for u in [best, *others] if u)),  # лучший первым, затем копии
            "retracted": bool(work.get("is_retracted"))}


def download_pdf(url: str, dst: Path, max_bytes: int = 30 * 2**20, timeout: float = 30.0) -> bool:
    """Скачать открытый PDF. Файл из сети недоверенный: только http(s), лимит размера, сигнатура `%PDF-`.

    Любой сбой (сеть, HTML-страница вместо PDF, превышение размера) — False, файла нет.
    """
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return False
    tmp = dst.with_suffix(".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"wwp/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as out:
            head = resp.read(5)
            if head != b"%PDF-":
                raise ValueError("не PDF")
            out.write(head)
            size = len(head)
            while chunk := resp.read(1 << 16):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("больше лимита")
                out.write(chunk)
        tmp.replace(dst)
        return True
    except (OSError, ValueError) as e:
        logger.info("PDF %s не скачан: %s", url, e)
        tmp.unlink(missing_ok=True)
        return False


def to_entry(work: dict[str, Any], taken: set[str]) -> Entry:
    """Запись BibTeX из сводки работы; ключ — фамилия первого автора + год + первое слово заголовка."""
    author = " and ".join(work["authors"]) if work.get("authors") else ""
    type_ = _TYPES.get(work.get("type", ""), "misc")
    fields = {"title": work.get("title", ""), "author": author, "year": work.get("year", ""),
              "doi": work.get("doi", ""), "url": work.get("id", ""), "abstract": clean(work.get("abstract", ""))}
    if work.get("venue"):
        fields[_VENUE_FIELD.get(type_, "howpublished")] = work["venue"]
    key = make_key(author, work.get("year", ""), work.get("title", ""), taken)
    return Entry(key=key, type=type_, fields={k: v for k, v in fields.items() if v})


__all__ = ["abstract", "by_ids", "download_pdf", "search", "to_entry", "work_summary"]
