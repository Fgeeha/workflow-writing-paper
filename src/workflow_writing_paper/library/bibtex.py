"""Разбор и запись BibTeX без внешних зависимостей: достаточно для ключей, заголовков, DOI и слияния файлов."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_ENTRY_START = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,")
_FIELD = re.compile(r"(\w+)\s*=\s*")
_WORD = re.compile(r"\w+", re.UNICODE)
_TRANSLIT = str.maketrans({c: t for c, t in zip("абвгдежзийклмнопрстуфхцчшщъыьэюяё", [
    "a", "b", "v", "g", "d", "e", "zh", "z", "i", "j", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
    "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya", "e"], strict=True)})
_STOP = {"a", "an", "the", "on", "of", "for", "and", "in", "to", "with", "от", "для", "и", "в", "на", "о"}


@dataclass
class Entry:
    key: str
    type: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""  # исходный текст записи; при записи в refs.bib сохраняется как есть

    @property
    def title(self) -> str:
        return self.fields.get("title", "")

    @property
    def year(self) -> str:
        return re.sub(r"\D", "", self.fields.get("year", ""))[:4]

    @property
    def doi(self) -> str:
        return normalize_doi(self.fields.get("doi", ""))

    @property
    def authors(self) -> str:
        return self.fields.get("author", "")

    def dump(self) -> str:
        if self.raw:
            return self.raw.strip() + "\n"
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in self.fields.items())
        return f"@{self.type}{{{self.key},\n{body}\n}}\n"


def _balanced(text: str, start: int) -> int:
    """Индекс закрывающей скобки для `{`, стоящей в text[start]."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("незакрытая скобка в BibTeX")


def _read_value(text: str, pos: int) -> tuple[str, int]:
    """Значение поля с позиции pos: {…}, "…", число или имя макроса; возвращает (значение, позиция после)."""
    if pos >= len(text):
        return "", pos
    if text[pos] == "{":
        end = _balanced(text, pos)
        return text[pos + 1:end], end + 1
    if text[pos] == '"':  # закрывающая кавычка — вне фигурных скобок: "Deep {"}Learning{"}" допустимо
        depth, i = 0, pos + 1
        while i < len(text) and (text[i] != '"' or depth):
            depth += (text[i] == "{") - (text[i] == "}")
            i += 1
        return text[pos + 1:i], i + 1
    m = re.compile(r"[^,}\s]+").match(text, pos)
    return (m.group(0), m.end()) if m else ("", pos)


def parse(text: str) -> list[Entry]:
    """Все записи файла; @comment/@preamble/@string пропускаются."""
    entries: list[Entry] = []
    pos_text = 0
    while (m := _ENTRY_START.search(text, pos_text)) is not None:
        type_ = m.group(1).lower()
        brace = text.index("{", m.start())
        try:
            end = _balanced(text, brace)
        except ValueError:
            break
        pos_text = end + 1  # поиск продолжается после записи: «@…{» внутри полей — не запись
        if type_ in ("comment", "preamble", "string"):
            continue
        inner, pos, fields = text[brace + 1:end], m.end() - brace - 1, {}
        while True:
            fm = _FIELD.search(inner, pos)
            if not fm:
                break
            value, pos = _read_value(inner, fm.end())
            fields[fm.group(1).lower()] = clean(value)
        entries.append(Entry(key=m.group(2), type=type_, fields=fields, raw=text[m.start():end + 1]))
    return entries


def clean(value: str) -> str:
    """Убрать защитные скобки и переносы: `{Deep} Learning` → `Deep Learning`."""
    return re.sub(r"\s+", " ", value.replace("{", "").replace("}", "")).strip()


def normalize_doi(doi: str) -> str:
    return re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi.strip().lower())


def normalize_title(title: str) -> str:
    """Для сопоставления PDF ↔ BibTeX: буквы и цифры в нижнем регистре через пробел."""
    return " ".join(_WORD.findall(clean(title).lower()))


def make_key(author: str, year: str, title: str, taken: set[str]) -> str:
    """Ключ вида `smith2020deep`; при совпадении — суффикс b, c, …"""
    surname = (_WORD.findall(author.split(" and ")[0].split(",")[0]) or ["anon"])[-1].lower()
    surname = re.sub(r"[^a-z0-9]", "", surname.translate(_TRANSLIT)) or "anon"
    first = next((w for w in _WORD.findall(title.lower()) if w not in _STOP and len(w) > 2), "")
    base = f"{surname}{year}{re.sub(r'[^a-z0-9]', '', first.translate(_TRANSLIT))}"
    key, i = base, 0
    while key in taken:
        i += 1
        key = f"{base}{chr(ord('a') + i)}"
    return key


def entry_from_dict(type_: str, key: str, fields: dict[str, Any]) -> Entry:
    return Entry(key=key, type=type_, fields={k: str(v) for k, v in fields.items() if v not in (None, "")})


__all__ = ["Entry", "clean", "entry_from_dict", "make_key", "normalize_doi", "normalize_title", "parse"]
