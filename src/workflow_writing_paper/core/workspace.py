"""Рабочая папка: пути, ключи, конфигурация пула, SQLite."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import yaml

DEFAULTS = Path(__file__).resolve().parents[3] / "defaults"

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY, project TEXT, task TEXT, role TEXT, provider TEXT, family TEXT,
    prompt_path TEXT, tokens_in INTEGER, tokens_out INTEGER, duration_ms INTEGER,
    cost_usd REAL, error TEXT, created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY, path TEXT, template TEXT, created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, project TEXT, stage TEXT, status TEXT, payload TEXT, result TEXT,
    created_at TEXT DEFAULT (datetime('now')), started_at TEXT, finished_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, project TEXT, artifact TEXT, version INTEGER, decision TEXT, override_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS bench_runs (
    id INTEGER PRIMARY KEY, run TEXT, role TEXT, task TEXT, provider TEXT, family TEXT,
    answer_path TEXT, judge_scores TEXT, judge_overall REAL, coverage REAL, human_score INTEGER,
    duration_ms INTEGER, cost_usd REAL, error TEXT, created_at TEXT DEFAULT (datetime('now'))
);
"""


class Workspace:
    """Всё состояние приложения в одной папке; переезд — копирование папки."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.environ.get("WWP_WORKSPACE") or "workspace").resolve()

    def init(self) -> list[str]:
        """Скопировать defaults в workspace, не затирая уже изменённые файлы."""
        created: list[str] = []
        self.root.mkdir(parents=True, exist_ok=True)
        for src in sorted(DEFAULTS.rglob("*")):
            if src.is_dir():
                continue
            rel = src.relative_to(DEFAULTS)
            if rel.as_posix() == "providers.example.yaml":
                rel = Path("providers.yaml")
            elif rel.parts[0] == "eval":
                rel = Path("eval", "tasks", *rel.parts[1:])
            dst = self.root / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                created.append(rel.as_posix())
        env = self.root / "env"
        if not env.exists():
            env.write_text("# KEY=VALUE, например OPENROUTER_API_KEY=...\n", encoding="utf-8")
            created.append("env")
        self.db().close()
        return created

    def load_env(self) -> None:
        env = self.root / "env"
        if not env.exists():
            return
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

    def config(self) -> dict[str, Any]:
        path = self.root / "providers.yaml"
        if not path.exists():
            raise FileNotFoundError(f"{path} не найден; выполните `wwp init`")
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def save_config(self, cfg: dict[str, Any]) -> None:
        text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
        (self.root / "providers.yaml").write_text(text, encoding="utf-8")

    def text(self, rel: str) -> str:
        """Файл из workspace, иначе из defaults (промпты, рубрики)."""
        for base in (self.root, DEFAULTS):
            if (base / rel).exists():
                return (base / rel).read_text(encoding="utf-8")
        raise FileNotFoundError(rel)

    def db(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / "wwp.sqlite", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn
