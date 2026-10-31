"""Decision-модели ollama (`/v1/systemone`): не пишут текст, а возвращают вероятности по вариантам.

Используются только как судьи-оценщики: вопрос `score` на каждый критерий рубрики.
"""

from __future__ import annotations

from typing import Any

from .base import Message, Provider, ProviderError
from .http import _request

# Уровни шкалы критерия; текст легенды — что означает каждый уровень.
LEVELS = {"0": "критерий не выполнен", "1": "выполнен слабо", "2": "выполнен частично",
          "3": "выполнен в основном", "4": "выполнен полностью"}


class DecisionProvider(Provider):
    """`kind: ollama-decision`. `complete` не поддерживается — модель не генерирует текст."""

    kind = "ollama-decision"

    @property
    def url(self) -> str:
        return str(self.cfg.get("url", "http://127.0.0.1:11434")).rstrip("/")

    def _call(self, messages: list[Message], *, json_schema: dict | None, max_tokens: int,
              temperature: float, seed: int | None) -> tuple[str, int, int]:
        raise ProviderError(f"{self.id}: decision-модель не генерирует текст; используйте score()")

    def score(self, state: str, criteria: dict[str, str]) -> tuple[dict[str, Any], int]:
        """Оценить `state` по критериям {код: описание}. Возвращает ({код: {score 0–10, probabilities}}, токены)."""
        questions = {code: {"type": "score", "question": f"Насколько ответ соответствует критерию: {text}",
                            "legend": LEVELS} for code, text in criteria.items()}
        body = {"model": self.model, "state": state, "questions": questions}
        with self._slot():
            resp = _request(self.url + "/v1/systemone", body, timeout=self.cfg.get("timeout", 300))
        answers = resp.get("answers") or {}
        out: dict[str, Any] = {}
        for code in criteria:
            a = answers.get(code)
            if not a or "score" not in a:
                raise ProviderError(f"{self.id}: нет оценки для {code}: {str(resp)[:300]}")
            # score — матожидание уровня 0..4; переводим в шкалу 0–10
            out[code] = {"score": round(float(a["score"]) / (len(LEVELS) - 1) * 10, 2),
                         "probabilities": a.get("probabilities"), "confidence": a.get("confidence")}
        return out, int((resp.get("usage") or {}).get("input_tokens", 0))

    def ping(self) -> tuple[bool, str]:
        try:
            names = [m["name"] for m in _request(self.url + "/api/tags", timeout=5)["models"]]
        except ProviderError as e:
            return False, str(e)
        ok = self.model in names
        return ok, "ok" if ok else f"модель {self.model} не загружена в ollama"
