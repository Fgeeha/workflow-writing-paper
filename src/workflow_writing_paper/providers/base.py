"""Единый интерфейс провайдера: вызов, извлечение JSON с повтором, цена, очередь к GPU."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

Message = dict[str, str]  # {"role": "system" | "user" | "assistant", "content": "..."}

JSON_RETRIES = 2
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ProviderError(RuntimeError):
    """Ошибка вызова; в text — последний ответ модели, если он был."""

    def __init__(self, msg: str, text: str = "") -> None:
        super().__init__(msg)
        self.text = text


@dataclass(frozen=True)
class Completion:
    text: str
    data: Any = None
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0


def extract_json(text: str) -> Any:
    """JSON из ответа модели: целиком, из блока ```json``` или первый разбираемый объект."""
    text = _THINK.sub("", text).strip()
    for cand in [text, *_FENCE.findall(text)]:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                return decoder.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    return None


_slots: dict[str, threading.Semaphore] = {}
_slots_guard = threading.Lock()


class Provider:
    """Базовый адаптер. Наследник реализует `_call`, по желанию `ping`."""

    kind = ""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.id: str = cfg["id"]
        self.family: str = cfg.get("family") or self.id
        self.model: str = str(cfg.get("model", ""))

    def _call(self, messages: list[Message], *, json_schema: dict | None, max_tokens: int,
              temperature: float, seed: int | None) -> tuple[str, int, int]:
        """Вернуть (текст, токены на вход, токены на выход)."""
        raise NotImplementedError

    def ping(self) -> tuple[bool, str]:
        return True, "ok"

    @contextmanager
    def _slot(self) -> Iterator[None]:
        """GPU-провайдеры по одному (и по одному на exclusive_group); облачные — до max_parallel."""
        if self.cfg.get("gpu"):
            key, size = "gpu:" + str(self.cfg.get("exclusive_group") or self.id), 1
        else:
            key, size = "p:" + self.id, int(self.cfg.get("max_parallel", 4))
        with _slots_guard:
            sem = _slots.setdefault(key, threading.Semaphore(size))
        with sem:
            yield

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        price = self.cfg.get("price") or {}
        return (tokens_in * float(price.get("input_per_1m", 0))
                + tokens_out * float(price.get("output_per_1m", 0))) / 1e6

    def complete(self, messages: list[Message], *, json_schema: dict | None = None,
                 max_tokens: int = 2048, temperature: float = 0.2, seed: int | None = None) -> Completion:
        msgs = list(messages)
        if json_schema is not None:
            hint = ("Ответь только JSON-объектом без пояснений, по схеме:\n"
                    + json.dumps(json_schema, ensure_ascii=False))
            msgs.append({"role": "user", "content": hint})
        started = time.monotonic()
        tin = tout = 0
        data: Any = None
        with self._slot():
            for attempt in range(JSON_RETRIES + 1):
                text, i, o = self._call(msgs, json_schema=json_schema, max_tokens=max_tokens,
                                        temperature=temperature, seed=seed)
                tin, tout = tin + i, tout + o
                if "<think>" in text and "</think>" not in text:
                    raise ProviderError(f"{self.id}: ответ оборван внутри рассуждения <think> — не хватило "
                                        f"токенов; включите think: true или увеличьте num_predict", text)
                if json_schema is None:
                    break
                data = extract_json(text)
                if isinstance(data, dict):
                    break
                if attempt == JSON_RETRIES:
                    raise ProviderError(f"{self.id}: ответ не JSON после {JSON_RETRIES} повторов", text)
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Это не корректный JSON. Верни только JSON-объект."}]
        return Completion(text=_THINK.sub("", text).strip(), data=data, tokens_in=tin, tokens_out=tout,
                          duration_ms=int((time.monotonic() - started) * 1000), cost_usd=self.cost(tin, tout))
