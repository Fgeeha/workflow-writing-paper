"""Проект-статья: каталог с манифестом, артефактами, реестром результатов и журналом вызовов."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from .artifact import Artifact
from .workspace import Workspace

# Стадия → артефакт, который она производит. experiments пишет в реестр, assembly — paper.md.
STAGE_ARTIFACT = {"novelty": "novelty", "literature": "literature", "experiment_plan": "experiment_plan",
                  "draft": "draft", "review": "draft", "assembly": "paper"}


class Project:
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
                    "review": {"judges": 2, "threshold": 7.0, "auto_iterations": 2}, "roles": {},
                    "stages": tpl.get("stages") or [], "scenarios": []}
        for sub in ("artifacts/history", "artifacts/reviews", "calls", "style/samples"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "paper-workflow.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                                                  encoding="utf-8")
        (root / "results.json").write_text("{}\n", encoding="utf-8")
        (root / "style" / "banned_phrases.txt").write_text(ws.text("style/banned_phrases.txt"), encoding="utf-8")
        if not (root / ".git").exists() and shutil.which("git"):
            subprocess.run(["git", "init", "-q"], cwd=root, check=False, capture_output=True)
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

    def save_manifest(self) -> None:
        self.manifest_path.write_text(yaml.safe_dump(self.manifest, allow_unicode=True, sort_keys=False),
                                      encoding="utf-8")

    # --- артефакты ----------------------------------------------------------------------
    def artifact_path(self, type_: str) -> Path:
        return self.path / "artifacts" / f"{type_}.md"

    def artifact(self, type_: str) -> Artifact | None:
        p = self.artifact_path(type_)
        return Artifact.parse(p.read_text(encoding="utf-8"), type_) if p.exists() else None

    def artifact_version(self, type_: str, version: int) -> Artifact | None:
        cur = self.artifact(type_)
        if cur and cur.version == version:
            return cur
        p = self.path / "artifacts" / "history" / f"{type_}.v{version}.md"
        return Artifact.parse(p.read_text(encoding="utf-8"), type_) if p.exists() else None

    def write_artifact(self, type_: str, body: str, created_by: dict[str, Any], status: str = "draft") -> Artifact:
        """Новая версия: прежняя уходит в history/, одобренные зависимые артефакты помечаются stale."""
        prev = self.artifact(type_)
        version = (prev.version if prev else 0) + 1
        if prev:
            (self.path / "artifacts" / "history" / f"{type_}.v{prev.version}.md").write_text(prev.dump(),
                                                                                                encoding="utf-8")
        art = Artifact(type=type_, version=version, status=status, body=body,
                       meta={"created_by": created_by, "reviews": [], "approved_by": None, "approved_at": None,
                             "override_reason": None, "chapter": self.manifest.get("chapter")})
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
        """Решение автора: approve | override | return | reject."""
        art = self.artifact(type_)
        if art is None:
            raise FileNotFoundError(type_)
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
        subprocess.run(["git", "add", "-A"], cwd=self.path, check=False, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.path, check=False, capture_output=True)

    # --- стадии ----------------------------------------------------------------------------
    def stages(self) -> list[dict[str, Any]]:
        return list(self.manifest.get("stages") or [])

    def stage_state(self, stage_id: str) -> str:
        """done | skipped | in_review | in_progress | pending | stale."""
        st = next((s for s in self.stages() if s["id"] == stage_id), None)
        if st is None:
            return "pending"
        if st.get("skipped"):
            return "skipped"
        if stage_id == "experiments":
            return "done" if self.registry_data() else "pending"
        art = self.artifact(STAGE_ARTIFACT.get(stage_id, stage_id))
        if art is None:
            return "pending"
        if stage_id == "draft":
            return "done" if art.status == "approved" or self.draft_complete() else "in_progress"
        return {"approved": "done", "in_review": "in_review", "stale": "stale"}.get(art.status, "in_progress")

    def can_run(self, stage_id: str) -> tuple[bool, str]:
        for st in self.stages():
            if st["id"] == stage_id:
                return True, ""
            state = self.stage_state(st["id"])
            if st.get("required", True) and state not in ("done", "skipped"):
                return False, f"стадия {st['id']} не завершена ({state})"
        return False, f"стадии {stage_id} нет в манифесте"

    def skip_stage(self, stage_id: str, reason: str) -> None:
        for st in self.manifest.get("stages") or []:
            if st["id"] == stage_id:
                st["skipped"] = reason
        self.save_manifest()

    # --- журнал, реестр, профиль --------------------------------------------------------------
    def journal(self) -> dict[str, Any]:
        return yaml.safe_load(self.ws.text(f"journals/{self.manifest.get('journal', 'generic')}.yaml")) or {}

    def sections(self) -> list[str]:
        return [str(s) for s in self.journal().get("sections") or []]

    def draft_complete(self) -> bool:
        art = self.artifact("draft")
        return art is not None and all(f"## {s}" in art.body for s in self.sections())

    def registry_data(self) -> dict[str, Any]:
        return json.loads((self.path / "results.json").read_text(encoding="utf-8") or "{}")

    def role_provider(self, role: str) -> str | None:
        own = (self.manifest.get("roles") or {}).get(role)
        if own:
            return str(own)
        cfg = (self.ws.config().get("roles") or {}).get(role)
        return cfg if isinstance(cfg, str) else (cfg[0] if cfg else None)

    def spent(self) -> dict[str, float]:
        db = self.ws.db()
        row = db.execute("SELECT coalesce(sum(tokens_in + tokens_out), 0) t, coalesce(sum(cost_usd), 0) u "
                         "FROM calls WHERE project = ?", (self.slug,)).fetchone()
        db.close()
        return {"tokens": float(row["t"]), "usd": float(row["u"])}

    def budget_left(self) -> tuple[bool, str]:
        budget = self.manifest.get("budget") or {}
        spent = self.spent()
        for key in ("tokens", "usd"):
            if key in budget and spent[key] >= float(budget[key]):
                return False, f"бюджет {key} исчерпан: {spent[key]:g} из {budget[key]}"
        return True, ""

    def style(self) -> dict[str, Any]:
        samples = sorted((self.path / "style" / "samples").glob("*.md"))
        banned_path = self.path / "style" / "banned_phrases.txt"
        banned = [ln.strip() for ln in banned_path.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")] if banned_path.exists() else []
        return {"samples": [p.read_text(encoding="utf-8") for p in samples[:3]], "banned": banned}
