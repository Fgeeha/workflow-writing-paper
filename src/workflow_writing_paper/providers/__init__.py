"""Пул провайдеров: сборка из providers.yaml, назначение на роли, журналируемый вызов."""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

from .base import Completion, Message, Provider, ProviderError, extract_json
from .cli import PRESETS, CliProvider
from .decision import DecisionProvider
from .fake import FakeProvider
from .http import GigaChatProvider, OllamaProvider, OpenAIProvider, OpenRouterProvider

KINDS: dict[str, type[Provider]] = {
    "ollama": OllamaProvider,
    "ollama-decision": DecisionProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
    "gigachat": GigaChatProvider,
    "generic-cli": CliProvider,
    "fake": FakeProvider,
    **{kind: CliProvider for kind in PRESETS},
}


def build_pool(cfg: dict[str, Any], include_disabled: bool = False) -> dict[str, Provider]:
    pool: dict[str, Provider] = {}
    for item in cfg.get("providers") or []:
        if not include_disabled and item.get("enabled", True) is False:
            continue
        kind = item.get("kind")
        if kind not in KINDS:
            raise ValueError(f"провайдер {item.get('id')}: неизвестный kind {kind!r}; допустимо {sorted(KINDS)}")
        pool[item["id"]] = KINDS[kind](item)
    return pool


def role_providers(cfg: dict[str, Any], role: str) -> list[str]:
    value = (cfg.get("roles") or {}).get(role) or []
    return [value] if isinstance(value, str) else list(value)


def pick_judges(pool: dict[str, Provider], candidates: list[str], author_family: str, n: int) -> list[Provider]:
    """До n судей с попарно разными семействами, отличными от семейства автора."""
    chosen: list[Provider] = []
    seen = {author_family}
    for pid in candidates:
        p = pool.get(pid)
        if p is not None and p.family not in seen:
            chosen.append(p)
            seen.add(p.family)
        if len(chosen) == n:
            break
    return chosen


def call_logged(provider: Provider, messages: list[Message], *, role: str, log_dir: Path, db: Any = None,
                project: str | None = None, task: str | None = None, **kwargs: Any) -> Completion:
    """Вызов с полной записью: файл <log_dir>/<ts>-<role>-<provider>.json и строка в таблице calls."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S") + f"-{time.monotonic_ns() % 1_000_000:06d}"
    path = log_dir / f"{stamp}-{role}-{provider.id}.json"
    record: dict[str, Any] = {"provider": provider.id, "family": provider.family, "model": provider.model,
                              "role": role, "task": task, "params": kwargs, "messages": messages}
    comp, error = None, None
    try:
        comp = provider.complete(messages, **kwargs)
        record.update(text=comp.text, data=comp.data, tokens_in=comp.tokens_in, tokens_out=comp.tokens_out,
                      duration_ms=comp.duration_ms, cost_usd=comp.cost_usd)
    except ProviderError as e:
        error = str(e)
        record.update(error=error, text=e.text)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if db is not None:
        db.execute("INSERT INTO calls(project, task, role, provider, family, prompt_path, tokens_in, tokens_out,"
                   " duration_ms, cost_usd, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (project, task, role, provider.id, provider.family, str(path), record.get("tokens_in", 0),
                    record.get("tokens_out", 0), record.get("duration_ms", 0), record.get("cost_usd", 0.0), error))
        db.commit()
    if comp is None:
        raise ProviderError(error or "ошибка", record.get("text", ""))
    return dataclasses.replace(comp, log_path=str(path))


__all__ = [
    "KINDS",
    "Completion",
    "Message",
    "Provider",
    "ProviderError",
    "build_pool",
    "call_logged",
    "extract_json",
    "pick_judges",
    "role_providers",
]
