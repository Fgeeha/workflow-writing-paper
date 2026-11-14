"""Проект-статья: каталог с манифестом, артефактами, реестром результатов и журналом вызовов."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from ..library import Library
from .artifact import Artifact
from .workspace import Workspace

logger = logging.getLogger(__name__)

REGISTRY_RULE = ("цитируемы только поля value; note — пояснение, числа из него в текст не переносятся; в тексте value "
                 "можно округлить до numbers.rounding знаков (половина вверх) и писать с разделителем профиля журнала")

# Стадия → артефакт, который она производит. experiments пишет в реестр, assembly — paper.md.
STAGE_ARTIFACT = {"collect": "corpus", "novelty": "novelty", "literature": "literature", "experiment_plan": "experiment_plan",
                  "draft": "draft", "review": "draft", "assembly": "paper"}


class Project:
    """Каталог проекта: манифест, артефакты с историей, реестр результатов, стадии и решения автора."""

    def __init__(self, ws: Workspace, path: str | Path) -> None:
        self.ws = ws
        self.path = Path(path).resolve()
        self.manifest_path = self.path / "paper-workflow.yaml"
        self.manifest: dict[str, Any] = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8")) or {}

    @property
    def slug(self) -> str:
        return str(self.manifest.get("name") or self.path.name)

    @property
    def language(self) -> str:
        return str(self.manifest.get("language", "ru"))

    # --- создание ---------------------------------------------------------------------
    @classmethod
    def create(cls, ws: Workspace, slug: str, template: str = "experimental", path: str | Path | None = None,
               language: str = "ru", journal: str = "generic") -> Project:
        root = Path(path).resolve() if path else ws.root / "projects" / slug
        if (root / "paper-workflow.yaml").exists():
            raise FileExistsError(f"проект уже есть: {root}")
        tpl = yaml.safe_load(ws.text(f"templates/{template}.yaml")) or {}
        manifest = {"name": slug, "title": "", "language": language, "template": template, "journal": journal,
                    "chapter": None, "budget": {"tokens": 5_000_000, "usd": 20},
                    "review": {"judges": 2, "threshold": None, "auto_iterations": 2}, "roles": {},
                    "stages": tpl.get("stages") or [], "scenarios": [], "library": tpl.get("library", "workspace")}
        for sub in ("artifacts/history", "artifacts/reviews", "calls", "style/samples"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "paper-workflow.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                                                  encoding="utf-8")
        (root / "results.json").write_text("{}\n", encoding="utf-8")
        (root / "style" / "banned_phrases.txt").write_text(ws.text("style/banned_phrases.txt"), encoding="utf-8")
        if not (root / ".git").exists() and shutil.which("git"):
            _git(root, "init", "-q")
        db = ws.db()
        db.execute("INSERT OR REPLACE INTO projects(slug, path, template) VALUES (?,?,?)", (slug, str(root), template))
        db.commit()
        db.close()
        return cls(ws, root)

    @classmethod
    def open(cls, ws: Workspace, slug: str) -> Project:
        db = ws.db()
        row = db.execute("SELECT path FROM projects WHERE slug = ?", (slug,)).fetchone()
        db.close()
        if row is None:
            raise FileNotFoundError(f"проект {slug} не зарегистрирован")
        if not Path(row["path"], "paper-workflow.yaml").exists():
            raise FileNotFoundError(f"каталог проекта {slug} отсутствует: {row['path']}")
        return cls(ws, row["path"])

    def set_title_from(self, topic: str) -> None:
        """Заголовок статьи из темы, если его ещё нет: первая строка, не длиннее 120 знаков, по границе слова."""
        if self.manifest.get("title") or not topic.strip():
            return
        line = topic.strip().splitlines()[0]
        self.manifest["title"] = line if len(line) <= 120 else line[:120].rsplit(" ", 1)[0] + "…"
        self.save_manifest()

    def save_manifest(self) -> None:
        self.manifest_path.write_text(yaml.safe_dump(self.manifest, allow_unicode=True, sort_keys=False),
                                      encoding="utf-8")

    # --- артефакты ----------------------------------------------------------------------
    def artifact_path(self, type_: str) -> Path:
        return self.path / "artifacts" / f"{type_}.md"

    def artifact(self, type_: str) -> Artifact | None:
        """Текущая версия артефакта или None."""
        p = self.artifact_path(type_)
        return Artifact.parse(p.read_text(encoding="utf-8"), type_) if p.exists() else None

    def artifact_version(self, type_: str, version: int) -> Artifact | None:
        """Конкретная версия: текущая или из history/."""
        cur = self.artifact(type_)
        if cur and cur.version == version:
            return cur
        p = self.path / "artifacts" / "history" / f"{type_}.v{version}.md"
        return Artifact.parse(p.read_text(encoding="utf-8"), type_) if p.exists() else None

    def write_artifact(self, type_: str, body: str, created_by: dict[str, Any], status: str = "draft",
                       source: str | None = None) -> Artifact:
        """Новая версия: прежняя уходит в history/, одобренные зависимые артефакты помечаются stale.

        `source` — исходные материалы автора; наследуется от предыдущей версии, судьи видят его как контекст.
        """
        prev = self.artifact(type_)
        if source is None and prev is not None:
            source = prev.meta.get("source")
        version = (prev.version if prev else 0) + 1
        if prev:
            (self.path / "artifacts" / "history" / f"{type_}.v{prev.version}.md").write_text(prev.dump(),
                                                                                                encoding="utf-8")
        art = Artifact(type=type_, version=version, status=status, body=body,
                       meta={"created_by": created_by, "reviews": [], "approved_by": None, "approved_at": None,
                             "override_reason": None, "chapter": self.manifest.get("chapter"), "source": source})
        self.artifact_path(type_).write_text(art.dump(), encoding="utf-8")
        self._mark_stale_after(type_)
        return art

    def update_artifact(self, art: Artifact) -> None:
        self.artifact_path(art.type).write_text(art.dump(), encoding="utf-8")

    def _mark_stale_after(self, type_: str) -> None:
        order: list[str] = []
        for s in self.stages():
            a = STAGE_ARTIFACT.get(s["id"])
            if a and a not in order:
                order.append(a)
        if type_ not in order:
            return
        for later in order[order.index(type_) + 1:]:
            art = self.artifact(later)
            if art and art.status == "approved":
                art.status = "stale"
                self.update_artifact(art)

    def decide(self, type_: str, decision: str, reason: str | None = None) -> Artifact:
        """Решение автора: approve | override | return | reject. Одобрение коммитится в git проекта (без пуша)."""
        art = self.artifact(type_)
        if art is None:
            raise FileNotFoundError(type_)
        if decision in ("approve", "override") and art.meta.get("ab"):
            raise ValueError("сначала выбор A/B: какая из двух версий остаётся")
        blockers = int((art.meta.get("review_summary") or {}).get("blockers") or 0)
        if decision == "approve" and blockers:
            raise ValueError(f"{type_} v{art.version}: блокеров {blockers} — исправьте и перепроверьте "
                             "или одобрите через override с причиной")
        if decision in ("approve", "override"):
            art.status = "approved"
            art.meta.update(approved_by="human", approved_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                            override_reason=reason if decision == "override" else None)
        elif decision == "return":
            art.status = "draft"
        elif decision == "reject":
            art.status = "rejected"
        else:
            raise ValueError(f"неизвестное решение {decision}")
        self.update_artifact(art)
        if art.status == "approved":
            self._git_commit(f"{type_} v{art.version} одобрен")
        db = self.ws.db()
        db.execute("INSERT INTO approvals(project, artifact, version, decision, override_reason) VALUES (?,?,?,?,?)",
                   (self.slug, type_, art.version, decision, reason))
        db.commit()
        db.close()
        return art

    def _git_commit(self, message: str) -> None:
        if not (self.path / ".git").exists() or not shutil.which("git"):
            return
        _git(self.path, "add", "-A")
        named = subprocess.run(["git", "config", "user.email"], cwd=self.path, check=False, capture_output=True,
                               text=True).stdout.strip()
        who = [] if named else ["-c", "user.name=wwp", "-c", "user.email=wwp@localhost"]  # git без имени не коммитит
        _git(self.path, *who, "commit", "-q", "-m", message)

    # --- стадии ----------------------------------------------------------------------------
    def stages(self) -> list[dict[str, Any]]:
        """Стадии манифеста в порядке выполнения."""
        return list(self.manifest.get("stages") or [])

    def stage_state(self, stage_id: str) -> str:
        """done | complete | skipped | in_review | in_progress | pending | stale."""
        st = next((s for s in self.stages() if s["id"] == stage_id), None)
        if st is None:
            return "pending"
        if st.get("skipped"):
            return "skipped"
        if stage_id == "experiments":  # done — есть хотя бы одно число из сценария (не ручной ввод)
            return "done" if any(v.get("source") != "manual" for v in self.registry_data().values()
                                 if isinstance(v, dict)) else "pending"
        art = self.artifact(STAGE_ARTIFACT.get(stage_id, stage_id))
        if art is None:
            return "pending"
        if stage_id == "draft":  # complete — все разделы написаны, одобрения автора ещё нет
            return "done" if art.status == "approved" else "complete" if self.draft_complete() else "in_progress"
        return {"approved": "done", "in_review": "in_review", "stale": "stale"}.get(art.status, "in_progress")

    def can_run(self, stage_id: str) -> tuple[bool, str]:
        """Гейт: все обязательные стадии до этой завершены или пропущены."""
        for st in self.stages():
            if st["id"] == stage_id:
                return True, ""
            state = self.stage_state(st["id"])
            if st.get("required", True) and state not in ("done", "skipped", "complete"):
                return False, f"стадия {st['id']} не завершена ({state})"
        return False, f"стадии {stage_id} нет в манифесте"

    def skip_stage(self, stage_id: str, reason: str) -> None:
        """Пропустить необязательную стадию с указанием причины."""
        for st in self.manifest.get("stages") or []:
            if st["id"] == stage_id:
                if st.get("required", True):
                    raise ValueError(f"стадия {stage_id} обязательна, пропустить нельзя")
                st["skipped"] = reason
        self.save_manifest()

    # --- библиотека ----------------------------------------------------------------------------
    def library(self) -> Library:
        """Своя библиотека проекта (`library: project`, шаблон explore) или общая библиотека workspace."""
        own = self.manifest.get("library") == "project"
        return Library(self.path / "library" if own else self.ws.root / "library")

    # --- журнал, реестр, профиль --------------------------------------------------------------
    def review_settings(self) -> dict[str, Any]:
        """judges, threshold (None — из рубрики), auto_iterations с дефолтами."""
        return {"judges": 2, "threshold": None, "auto_iterations": 2, **(self.manifest.get("review") or {})}

    def judge_candidates(self) -> list[str]:
        """Судьи из манифеста проекта, иначе из providers.yaml."""
        own = (self.manifest.get("roles") or {}).get("judge")
        value = own or (self.ws.config().get("roles") or {}).get("judge") or []
        return [value] if isinstance(value, str) else list(value)

    def context_header(self, with_registry: bool) -> str:
        """Профиль журнала (+ реестр результатов) — общий контекст для ролей и судей."""
        parts = [f"## Профиль журнала\n{yaml.safe_dump(self.journal(), allow_unicode=True)}"]
        if with_registry:
            parts.append(f"## Реестр результатов — единственный источник чисел ({REGISTRY_RULE})\n"
                         + json.dumps(self.registry_data(), ensure_ascii=False, indent=1))
        return "\n\n".join(parts)

    def journal(self) -> dict[str, Any]:
        """Профиль журнала из workspace/journals/ или defaults."""
        return yaml.safe_load(self.ws.text(f"journals/{self.manifest.get('journal', 'generic')}.yaml")) or {}

    def sections(self) -> list[str]:
        """Разделы статьи из профиля журнала."""
        return [str(s) for s in self.journal().get("sections") or []]

    def draft_complete(self) -> bool:
        """Все разделы профиля журнала присутствуют в черновике."""
        art = self.artifact("draft")
        return art is not None and all(f"## {s}" in art.body for s in self.sections())

    def registry_data(self) -> dict[str, Any]:
        """Содержимое results.json."""
        return json.loads((self.path / "results.json").read_text(encoding="utf-8") or "{}")

    def role_provider(self, role: str) -> str | None:
        """Провайдер роли: переопределение манифеста, иначе первый из providers.yaml."""
        own = (self.manifest.get("roles") or {}).get(role)
        if own:
            return str(own)
        cfg = (self.ws.config().get("roles") or {}).get(role)
        return cfg if isinstance(cfg, str) else (cfg[0] if cfg else None)

    def spent(self) -> dict[str, float]:
        """Потрачено по журналу вызовов проекта: токены и доллары."""
        db = self.ws.db()
        row = db.execute("SELECT coalesce(sum(tokens_in + tokens_out), 0) t, coalesce(sum(cost_usd), 0) u "
                         "FROM calls WHERE project = ?", (self.slug,)).fetchone()
        db.close()
        return {"tokens": float(row["t"]), "usd": float(row["u"])}

    def budget_left(self, extra_usd: float = 0.0) -> tuple[bool, str]:
        """Остаток бюджета; `extra_usd` — предварительная оценка предстоящего расхода."""
        budget = self.manifest.get("budget") or {}
        spent = self.spent()
        for key in ("tokens", "usd"):
            planned = spent[key] + (extra_usd if key == "usd" else 0.0)
            if key in budget and planned >= float(budget[key]):
                return False, f"бюджет {key} исчерпан: {planned:g} из {budget[key]}"
        return True, ""

    def style(self) -> dict[str, Any]:
        """Образцы стиля автора (до трёх) и запрещённые фразы."""
        samples = sorted((self.path / "style" / "samples").glob("*.md"))
        banned_path = self.path / "style" / "banned_phrases.txt"
        banned = [ln.strip() for ln in banned_path.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")] if banned_path.exists() else []
        return {"samples": [p.read_text(encoding="utf-8") for p in samples[:3]], "banned": banned}


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)
    if proc.returncode:
        logger.warning("git %s в %s: %s", args[0], cwd, proc.stderr.strip()[-300:])
