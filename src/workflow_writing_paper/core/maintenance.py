"""Обслуживание: пересборка wwp.sqlite из файлов и очистка старого журнала вызовов и истории."""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .artifact import Artifact
from .project import Project
from .workspace import Workspace

logger = logging.getLogger(__name__)

_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)")
_HISTORY = re.compile(r"^(?P<type>[a-z_]+)\.v(?P<version>\d+)\.md$")


def _created_at(path: Path) -> str:
    """Время из имени файла вызова `2026-10-31T21-48-53-…json`, иначе mtime файла."""
    m = _STAMP.match(path.name)
    if m:
        return f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(path.stat().st_mtime))


def _versions(project: Project) -> list[Artifact]:
    """Все версии всех артефактов проекта: текущие и из history/."""
    arts = project.path / "artifacts"
    out = []
    for p in sorted(arts.glob("*.md")) + sorted((arts / "history").glob("*.md")):
        m = _HISTORY.match(p.name)
        out.append(Artifact.parse(p.read_text(encoding="utf-8"), m.group("type") if m else p.stem))
    return out


def reindex(ws: Workspace) -> dict[str, int]:
    """Добавить в wwp.sqlite то, что есть в файлах, но нет в базе. Ничего не удаляет.

    Восстанавливаются: проекты в workspace/projects/, вызовы моделей (calls/*.json проектов и прогонов
    стенда), решения автора «одобрить / override / отклонить» и выборы A/B из заголовков артефактов. Очередь задач,
    решения «вернуть на правку» и оценки стенда хранятся только в базе и из файлов не восстанавливаются.
    """
    added = {"projects": 0, "calls": 0, "approvals": 0, "ab_choices": 0}
    with closing(ws.db()) as db:
        for manifest in sorted((ws.root / "projects").glob("*/paper-workflow.yaml")):
            project = Project(ws, manifest.parent)
            cur = db.execute("INSERT OR IGNORE INTO projects(slug, path, template) VALUES (?,?,?)",
                             (project.slug, str(project.path), project.manifest.get("template")))
            added["projects"] += cur.rowcount
        known = {r["prompt_path"] for r in db.execute("SELECT prompt_path FROM calls")}
        sources: list[tuple[str | None, Path]] = [(None, d) for d in sorted((ws.root / "eval" / "runs").glob("*/calls"))]
        projects = []
        for row in db.execute("SELECT slug, path FROM projects").fetchall():
            if (Path(row["path"]) / "paper-workflow.yaml").exists():
                projects.append(Project(ws, row["path"]))
                sources.append((row["slug"], Path(row["path"]) / "calls"))
        for slug, folder in sources:
            for p in sorted(folder.glob("*.json")):
                if str(p) in known:
                    continue
                try:
                    rec: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    logger.warning("пропущен %s: %s", p, e)
                    continue
                db.execute("INSERT INTO calls(project, task, role, provider, family, prompt_path, tokens_in,"
                           " tokens_out, duration_ms, cost_usd, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (slug, rec.get("task"), rec.get("role"), rec.get("provider"), rec.get("family"), str(p),
                            rec.get("tokens_in") or 0, rec.get("tokens_out") or 0, rec.get("duration_ms") or 0,
                            rec.get("cost_usd") or 0.0, rec.get("error"), _created_at(p)))
                added["calls"] += 1
        have = {(r["project"], r["artifact"], r["version"], r["decision"])
                for r in db.execute("SELECT project, artifact, version, decision FROM approvals")}
        ab_have = {(r["project"], r["artifact"], r["provider_a"], r["provider_b"], r["chosen"], r["version"])
                   for r in db.execute("SELECT * FROM ab_choices")}
        for project in projects:
            for art in _versions(project):
                ab = art.meta.get("ab_chosen")
                if ab:  # одна запись на выбор; ключ — версия, ставшая текущей после выбора
                    row = (project.slug, art.type, ab["a"]["provider"], ab["b"]["provider"], ab[ab["chosen"]]["provider"],
                           ab.get("version"))
                    if row not in ab_have:
                        db.execute("INSERT INTO ab_choices(project, artifact, role, provider_a, provider_b, chosen, version)"
                                   " VALUES (?,?,?,?,?,?,?)", (*row[:2], ab.get("role"), *row[2:]))
                        ab_have.add(row)
                        added["ab_choices"] += 1
                approved_at = art.meta.get("approved_at")
                if approved_at:
                    decision = "override" if art.meta.get("override_reason") else "approve"
                elif art.status == "rejected":
                    decision = "reject"
                else:
                    continue
                key = (project.slug, art.type, art.version, decision)
                if key in have:
                    continue
                db.execute("INSERT INTO approvals(project, artifact, version, decision, override_reason, created_at)"
                           " VALUES (?,?,?,?,?,coalesce(?, datetime('now')))",
                           (*key, art.meta.get("override_reason"), str(approved_at).replace("T", " ") if approved_at
                            else None))
                have.add(key)
                added["approvals"] += 1
        db.commit()
    return added


def gc(ws: Workspace, older_than_days: float, apply: bool = False) -> list[Path]:
    """Файлы старше срока: записи вызовов проектов и неодобренные версии из history/ с их отчётами судей.

    Не трогает: вызовы, на которые ссылается любая версия артефакта (`created_by.call`), одобренные версии,
    текущие артефакты, прогоны стенда. Без `apply` только возвращает список.
    """
    cutoff = time.time() - older_than_days * 86400
    doomed: list[Path] = []
    with closing(ws.db()) as db:
        rows = db.execute("SELECT path FROM projects").fetchall()
    for row in rows:
        root = Path(row["path"])
        if not (root / "paper-workflow.yaml").exists():
            continue
        versions = _versions(Project(ws, root))
        kept = {str(Path(str(c)).resolve()) for a in versions if (c := (a.meta.get("created_by") or {}).get("call"))}
        doomed += [p for p in sorted((root / "calls").glob("*.json"))
                   if p.stat().st_mtime < cutoff and str(p.resolve()) not in kept]
        for p in sorted((root / "artifacts" / "history").glob("*.md")):
            m = _HISTORY.match(p.name)
            if not m or p.stat().st_mtime >= cutoff:
                continue
            if Artifact.parse(p.read_text(encoding="utf-8"), m.group("type")).meta.get("approved_at"):
                continue
            doomed.append(p)
            doomed += sorted((root / "artifacts" / "reviews").glob(f"{m.group('type')}.v{m.group('version')}.*.json"))
    if apply:
        for p in doomed:
            p.unlink(missing_ok=True)
        with closing(ws.db()) as db:
            db.executemany("DELETE FROM calls WHERE prompt_path = ?", [(str(p),) for p in doomed])
            db.commit()
    return doomed


__all__ = ["gc", "reindex"]
