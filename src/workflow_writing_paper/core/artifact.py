"""Артефакт: markdown с YAML-заголовком (тип, версия, статус, автор, проверки, одобрение)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)



@dataclass
class Artifact:
    """Статусы: draft | in_review | approved | rejected | stale."""

    type: str
    version: int = 0
    status: str = "draft"
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)  # created_by, reviews, approved_by, approved_at, ...

    @classmethod
    def parse(cls, text: str, type_hint: str = "") -> Artifact:
        """Разобрать markdown с YAML-заголовком; без заголовка — тело целиком, тип из подсказки."""
        m = _FRONT.match(text)
        if not m:
            return cls(type=type_hint, body=text)
        head = yaml.safe_load(m.group(1)) or {}
        return cls(type=head.pop("type", type_hint), version=int(head.pop("version", 0)),
                   status=head.pop("status", "draft"), body=m.group(2), meta=head)

    def dump(self) -> str:
        """Собрать markdown с YAML-заголовком."""
        head = {"type": self.type, "version": self.version, "status": self.status, **self.meta}
        return "---\n" + yaml.safe_dump(head, allow_unicode=True, sort_keys=False) + "---\n" + self.body
