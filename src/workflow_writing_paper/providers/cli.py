"""Консольные агенты: codex, claude, opencode и любой другой через generic-cli.

Агент запускается во временном пустом каталоге, без инструментов там, где CLI это позволяет;
промпт — плоский текст из сообщений.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import Message, Provider, ProviderError

PRESETS: dict[str, dict[str, Any]] = {
    "codex-cli": {
        "command": ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                    "-o", "{outfile}", "-"],
        "stdin": True, "model_flag": "-m",
    },
    "claude-cli": {
        "command": ["claude", "-p", "--safe-mode", "--tools", "", "--no-session-persistence",
                    "--strict-mcp-config", "--output-format", "json"],
        "stdin": True, "model_flag": "--model", "output": "json",
        "text_path": "result", "tokens_in_path": "usage.input_tokens", "tokens_out_path": "usage.output_tokens",
    },
    "opencode-cli": {"command": ["opencode", "run", "{prompt}"], "stdin": False, "model_flag": "-m"},
}

_TITLES = {"system": "ИНСТРУКЦИЯ", "user": "ЗАПРОС", "assistant": "ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ"}


def _dig(obj: Any, path: str) -> Any:
    for key in path.split(".") if path else []:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj if path else None


def render_prompt(messages: list[Message]) -> str:
    return "\n\n".join(f"### {_TITLES[m['role']]}\n{m['content']}" for m in messages)


class CliProvider(Provider):
    """generic-cli: `command` с подстановками {prompt} {outfile} {model}, `stdin`, `output: text|json`."""

    kind = "generic-cli"

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)
        self.spec = {**PRESETS.get(cfg.get("kind", ""), {}), **cfg}

    def _call(self, messages: list[Message], *, json_schema: dict | None, max_tokens: int,
              temperature: float, seed: int | None) -> tuple[str, int, int]:
        prompt = render_prompt(messages)
        with tempfile.TemporaryDirectory(prefix="wwp-") as tmp:
            outfile = Path(tmp) / "out.txt"
            cmd = [a.replace("{outfile}", str(outfile)).replace("{prompt}", prompt).replace("{model}", self.model)
                   for a in self.spec["command"]]
            if self.spec.get("model_flag") and self.model not in ("", "default"):
                cmd += [self.spec["model_flag"], self.model]
            try:
                proc = subprocess.run(cmd, input=prompt if self.spec.get("stdin") else None, cwd=tmp, check=False,
                                      capture_output=True, text=True, timeout=self.cfg.get("timeout", 900))
            except (OSError, subprocess.TimeoutExpired) as e:
                raise ProviderError(f"{self.id}: {e}") from e
            if proc.returncode != 0:
                raise ProviderError(f"{self.id}: код {proc.returncode}: {proc.stderr[-800:]}", proc.stdout)
            out = outfile.read_text(encoding="utf-8") if outfile.exists() else proc.stdout
        if self.spec.get("output") != "json":
            return out.strip(), 0, 0
        try:
            doc = json.loads(out)
        except json.JSONDecodeError as e:
            raise ProviderError(f"{self.id}: вывод не JSON", out) from e
        if isinstance(doc, list):  # поток событий: берём итоговое
            doc = next((d for d in reversed(doc) if isinstance(d, dict) and d.get("type") == "result"), doc[-1])
        if doc.get("is_error"):
            raise ProviderError(f"{self.id}: {doc.get('result')}")
        text = _dig(doc, self.spec.get("text_path", "result")) or ""
        tin = _dig(doc, self.spec.get("tokens_in_path", "")) or 0
        tout = _dig(doc, self.spec.get("tokens_out_path", "")) or 0
        return str(text), int(tin), int(tout)

    def ping(self) -> tuple[bool, str]:
        exe = self.spec["command"][0]
        found = shutil.which(exe)
        return (True, found) if found else (False, f"{exe} не найден в PATH")
