"""Детерминированный провайдер для тестов и разработки без GPU и ключей."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .base import Message, Provider


class FakeProvider(Provider):
    """Текст: `reply` из конфига или эхо с хэшем. JSON: объект по схеме, числа = `score`.

    `bad_json: N` — первые N JSON-ответов намеренно не JSON (проверка повторов).
    """

    kind = "fake"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self._bad_left = int(cfg.get("bad_json", 0))

    def _call(self, messages: list[Message], *, json_schema: dict | None, max_tokens: int,
              temperature: float, seed: int | None) -> tuple[str, int, int]:
        prompt = "\n".join(m["content"] for m in messages)
        digest = hashlib.sha1(prompt.encode()).hexdigest()[:8]
        if json_schema is None:
            text = self.cfg.get("reply") or f"[{self.id}] ответ {digest}"
        elif self._bad_left > 0:
            self._bad_left -= 1
            text = "не JSON"
        else:
            text = json.dumps(self._fill(json_schema, f"{self.id} {digest}"), ensure_ascii=False)
        return text, len(prompt) // 4, len(text) // 4

    def _fill(self, spec: dict[str, Any], filler: str) -> Any:
        typ = spec.get("type")
        score = self.cfg.get("score", 7)
        if typ == "object":
            return {k: self._fill(v, filler) for k, v in spec.get("properties", {}).items()}
        return {"integer": int(score), "number": float(score), "array": []}.get(str(typ), filler)
