"""Поиск в OpenAlex (без ключа) и перевод найденной работы в запись BibTeX."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from .. import __version__
from .bibtex import Entry, make_key

API = "https://api.openalex.org/works"
FIELDS = "id,doi,title,publication_year,authorships,primary_location,type"
_TYPES = {"article": "article", "book": "book", "book-chapter": "incollection", "conference-paper": "inproceedings",
          "proceedings-article": "inproceedings", "dissertation": "phdthesis", "preprint": "misc"}
_VENUE_FIELD = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle"}


def search(query: str, limit: int = 10, mailto: str = "", timeout: float = 15.0) -> list[dict[str, Any]]:
    """Работы по текстовому запросу: словари с полями id, doi, title, year, authors, venue, type."""
    params = {"search": query, "per-page": str(limit), "select": FIELDS}
    if mailto:
        params["mailto"] = mailto
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": f"wwp/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise TypeError("OpenAlex: неожиданный формат ответа")
    return [work_summary(w) for w in data["results"]]


def work_summary(work: dict[str, Any]) -> dict[str, Any]:
    source = (work.get("primary_location") or {}).get("source") or {}
    authors = [str((a.get("author") or {}).get("display_name") or "") for a in work.get("authorships") or []]
    return {"id": str(work.get("id") or ""), "doi": str(work.get("doi") or "").replace("https://doi.org/", ""),
            "title": str(work.get("title") or ""), "year": str(work.get("publication_year") or ""),
            "authors": [a for a in authors if a], "venue": str(source.get("display_name") or ""),
            "type": str(work.get("type") or "")}


def to_entry(work: dict[str, Any], taken: set[str]) -> Entry:
    """Запись BibTeX из сводки работы; ключ — фамилия первого автора + год + первое слово заголовка."""
    author = " and ".join(work["authors"]) if work.get("authors") else ""
    type_ = _TYPES.get(work.get("type", ""), "misc")
    fields = {"title": work.get("title", ""), "author": author, "year": work.get("year", ""),
              "doi": work.get("doi", ""), "url": work.get("id", "")}
    if work.get("venue"):
        fields[_VENUE_FIELD.get(type_, "howpublished")] = work["venue"]
    key = make_key(author, work.get("year", ""), work.get("title", ""), taken)
    return Entry(key=key, type=type_, fields={k: v for k, v in fields.items() if v})


__all__ = ["search", "to_entry", "work_summary"]
