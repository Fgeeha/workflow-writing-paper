"""Маршруты v0.2: главная, проекты, проект, артефакт, реестр результатов, настройки."""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core import Artifact, Project, Workspace
from ..core.engine import enqueue
from ..core.project import STAGE_ARTIFACT
from ..core.workspace import DEFAULTS
from ..registry import add_manual
from ..roles import NOT_YET

Render = Callable[..., HTMLResponse]

SETTINGS_DIRS = ("prompts", "rubrics", "journals", "templates", "eval/tasks", "style")
ARTIFACT_TYPES = tuple(dict.fromkeys(STAGE_ARTIFACT.values()))


def _projects(ws: Workspace) -> list[dict[str, Any]]:
    db = ws.db()
    rows = db.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    db.close()
    return [{"slug": r["slug"], "path": r["path"], "template": r["template"],
             "missing": not Path(r["path"], "paper-workflow.yaml").exists()} for r in rows]


def _open(ws: Workspace, slug: str) -> Project:
    try:
        return Project.open(ws, slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


def _reviews(project: Project, art: Artifact) -> list[dict[str, Any]]:
    reports = []
    for rel in art.meta.get("reviews") or []:
        p = project.path / rel
        if p.exists():
            reports.append(json.loads(p.read_text(encoding="utf-8")))
    return reports


def _safe_rel(rel: str) -> str:
    if not rel or ".." in rel or rel.startswith("/") or not rel.startswith(SETTINGS_DIRS):
        raise HTTPException(400, "путь вне каталогов настроек")
    return rel


def register(app: FastAPI, ws: Workspace, render: Render) -> None:
    # --- главная --------------------------------------------------------------------------
    @app.get("/")
    def index(request: Request) -> HTMLResponse:
        pending, stale = [], []
        for item in _projects(ws):
            if item["missing"]:
                continue
            project = Project(ws, item["path"])
            for t in ARTIFACT_TYPES:
                art = project.artifact(t)
                if art and art.status == "in_review":
                    pending.append({"slug": project.slug, "type": t, "version": art.version,
                                    "summary": art.meta.get("review_summary") or {}})
                elif art and art.status == "stale":
                    stale.append({"slug": project.slug, "type": t, "version": art.version})
        db = ws.db()
        failed = db.execute("SELECT * FROM tasks WHERE status='failed' ORDER BY id DESC LIMIT 20").fetchall()
        running = db.execute("SELECT * FROM tasks WHERE status IN ('queued','running') ORDER BY id").fetchall()
        db.close()
        return render(request, "index.html", pending=pending, stale=stale, failed=failed, running=running)

    # --- проекты --------------------------------------------------------------------------
    @app.get("/projects")
    def projects_page(request: Request) -> HTMLResponse:
        tpl_dir, jr_dir = ws.root / "templates", ws.root / "journals"
        templates = sorted(p.stem for p in tpl_dir.glob("*.yaml")) if tpl_dir.exists() else ["experimental"]
        journals = sorted(p.stem for p in jr_dir.glob("*.yaml")) if jr_dir.exists() else ["generic"]
        return render(request, "projects.html", projects=_projects(ws), templates=templates, journals=journals)

    @app.post("/projects")
    async def create_project(request: Request) -> RedirectResponse:
        form = await request.form()
        slug = str(form.get("slug") or "").strip()
        if not slug or not slug.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400, "slug: латиница, цифры, дефис")
        path = str(form.get("path") or "").strip() or None
        try:
            Project.create(ws, slug, template=str(form.get("template") or "experimental"), path=path,
                           language=str(form.get("language") or "ru"), journal=str(form.get("journal") or "generic"))
        except (FileExistsError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.post("/projects/{slug}/path")
    async def rebind_path(slug: str, request: Request) -> RedirectResponse:
        new_path = str((await request.form()).get("path") or "").strip()
        if not Path(new_path, "paper-workflow.yaml").exists():
            raise HTTPException(400, f"в {new_path} нет paper-workflow.yaml")
        db = ws.db()
        db.execute("UPDATE projects SET path = ? WHERE slug = ?", (str(Path(new_path).resolve()), slug))
        db.commit()
        db.close()
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.get("/projects/{slug}")
    def project_page(slug: str, request: Request) -> HTMLResponse:
        project = _open(ws, slug)
        stages = []
        for st in project.stages():
            sid = st["id"]
            can, why = project.can_run(sid)
            stages.append({**st, "state": project.stage_state(sid), "can_run": can, "why": why,
                           "not_yet": NOT_YET.get(sid), "artifact": project.artifact(STAGE_ARTIFACT.get(sid, sid))})
        db = ws.db()
        tasks = db.execute("SELECT * FROM tasks WHERE project = ? ORDER BY id DESC LIMIT 30", (slug,)).fetchall()
        db.close()
        draft = project.artifact("draft")
        written = draft.body if draft else ""
        sections = [{"name": s, "written": f"## {s}" in written} for s in project.sections()]
        return render(request, "project.html", project=project, stages=stages, tasks=tasks, sections=sections,
                      spent=project.spent(), budget=project.manifest.get("budget") or {})

    @app.post("/projects/{slug}/run/{stage}")
    async def run_stage_route(slug: str, stage: str, request: Request) -> RedirectResponse:
        form = await request.form()
        payload = {k: str(v) for k, v in form.items() if v}
        try:
            enqueue(ws, slug, stage, payload)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.post("/projects/{slug}/skip/{stage}")
    async def skip_stage_route(slug: str, stage: str, request: Request) -> RedirectResponse:
        reason = str((await request.form()).get("reason") or "").strip()
        if not reason:
            raise HTTPException(400, "укажите причину пропуска")
        _open(ws, slug).skip_stage(stage, reason)
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    # --- артефакт -----------------------------------------------------------------------------
    @app.get("/projects/{slug}/artifacts/{type_}")
    def artifact_page(slug: str, type_: str, request: Request, version: int = 0) -> HTMLResponse:
        project = _open(ws, slug)
        current = project.artifact(type_)
        if current is None:
            raise HTTPException(404, f"артефакта {type_} нет")
        art = project.artifact_version(type_, version) if version else current
        if art is None:
            raise HTTPException(404, f"версии {version} нет")
        prev = project.artifact_version(type_, art.version - 1) if art.version > 1 else None
        diff = ""
        if prev:
            diff = "\n".join(difflib.unified_diff(prev.body.splitlines(), art.body.splitlines(),
                                                  f"v{prev.version}", f"v{art.version}", lineterm="", n=2))
        return render(request, "artifact.html", project=project, art=art, current=current,
                      reviews=_reviews(project, art), diff=diff, versions=list(range(1, current.version + 1)))

    @app.post("/projects/{slug}/artifacts/{type_}/decide")
    async def decide_route(slug: str, type_: str, request: Request) -> RedirectResponse:
        form = await request.form()
        decision = str(form.get("decision") or "")
        reason = str(form.get("reason") or "").strip() or None
        if decision == "override" and not reason:
            raise HTTPException(400, "override требует причины")
        try:
            _open(ws, slug).decide(type_, decision, reason)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse(f"/projects/{slug}/artifacts/{type_}", status_code=303)

    @app.post("/projects/{slug}/artifacts/{type_}/review")
    def rereview(slug: str, type_: str) -> RedirectResponse:
        try:
            enqueue(ws, slug, "review", {"artifact": type_}, gate=False)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.post("/projects/{slug}/artifacts/{type_}/to-eval")
    async def to_eval(slug: str, type_: str, request: Request) -> RedirectResponse:
        """Сохранить одобренный артефакт как эталонную задачу стенда для роли, которая его создала."""
        project = _open(ws, slug)
        art = project.artifact(type_)
        if art is None:
            raise HTTPException(404)
        role = str((art.meta.get("created_by") or {}).get("role") or "ideator")
        folder = ws.root / "eval" / "tasks" / role
        folder.mkdir(parents=True, exist_ok=True)
        n = len(list(folder.glob("*.yaml"))) + 1
        task = {"role": role, "id": f"{role}-{slug}-{n:03d}", "language": project.language, "input": art.body,
                "expected": {"must_mention": [], "must_not": []}, "rubric": f"rubrics/{role}.md",
                "note": "из одобренного артефакта; заполните expected"}
        (folder / f"{task['id']}.yaml").write_text(yaml.safe_dump(task, allow_unicode=True, sort_keys=False),
                                                   encoding="utf-8")
        return RedirectResponse(f"/bench?role={role}", status_code=303)

    # --- реестр результатов -------------------------------------------------------------------
    @app.get("/projects/{slug}/registry")
    def registry_page(slug: str, request: Request) -> HTMLResponse:
        project = _open(ws, slug)
        return render(request, "registry.html", project=project, data=project.registry_data(),
                      scenarios=project.manifest.get("scenarios") or [])

    @app.post("/projects/{slug}/registry")
    async def registry_add(slug: str, request: Request) -> RedirectResponse:
        form = await request.form()
        key = str(form.get("key") or "").strip()
        try:
            value = float(str(form.get("value") or "").replace(",", "."))
        except ValueError as e:
            raise HTTPException(400, "значение должно быть числом") from e
        if not key.replace("_", "").isalnum():
            raise HTTPException(400, "ключ: латиница, цифры, подчёркивание")
        project = _open(ws, slug)
        add_manual(project.path / "results.json", key, value, str(form.get("unit") or ""), str(form.get("note") or ""))
        return RedirectResponse(f"/projects/{slug}/registry", status_code=303)

    # --- настройки ----------------------------------------------------------------------------
    @app.get("/settings")
    def settings_page(request: Request) -> HTMLResponse:
        rels: set[str] = set()
        for base in (ws.root, DEFAULTS):
            for d in SETTINGS_DIRS:
                rels |= {p.relative_to(base).as_posix() for p in (base / d).rglob("*") if p.is_file()}
        files = [{"rel": rel, "overridden": (ws.root / rel).exists()} for rel in sorted(rels)]
        return render(request, "settings.html", files=files)

    @app.get("/settings/edit")
    def settings_edit(request: Request, rel: str) -> HTMLResponse:
        _safe_rel(rel)
        try:
            text = ws.text(rel)
        except FileNotFoundError as e:
            raise HTTPException(404, rel) from e
        return render(request, "settings_edit.html", rel=rel, text=text)

    @app.post("/settings/edit")
    async def settings_save(request: Request) -> RedirectResponse:
        form = await request.form()
        rel = _safe_rel(str(form.get("rel") or ""))
        target = ws.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(form.get("text") or "").replace("\r\n", "\n"), encoding="utf-8")
        return RedirectResponse(f"/settings/edit?rel={rel}", status_code=303)
