"""Веб-интерфейс: пул моделей, стенд, журнал вызовов (здесь); проекты и артефакты — web/projects.py."""

from __future__ import annotations

import json
import logging
import threading
import time
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..core import Workspace
from ..core.engine import Worker
from ..eval import run_bench, summary
from ..providers import build_pool, role_providers
from .projects import register

logger = logging.getLogger(__name__)


def create_app(ws: Workspace | None = None, worker: bool = True) -> FastAPI:
    ws = ws or Workspace()
    ws.load_env()
    app = FastAPI(title="workflow-writing-paper")
    if worker:
        Worker(ws).start()
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    jobs: dict[str, dict[str, Any]] = {}

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    # --- пул моделей ------------------------------------------------------------------
    @app.get("/pool")
    def pool_page(request: Request) -> HTMLResponse:
        cfg = ws.config()
        roles = {name: ", ".join(role_providers(cfg, name)) for name in (cfg.get("roles") or {})}
        return render(request, "pool.html", providers=cfg.get("providers") or [], roles=roles,
                      profiles=cfg.get("profiles") or {})

    @app.get("/pool/ping/{pid}", response_class=HTMLResponse)
    def ping(pid: str) -> str:
        pool = build_pool(ws.config(), include_disabled=True, text=ws.text)
        if pid not in pool:
            raise HTTPException(404)
        ok, msg = pool[pid].ping()
        return f'<span class="{"ok" if ok else "bad"}">{"доступен" if ok else "нет"}: {escape(msg)}</span>'

    @app.post("/pool/roles")
    async def save_roles(request: Request) -> RedirectResponse:
        form = await request.form()
        cfg = ws.config()
        known = {p["id"] for p in cfg.get("providers") or []}
        roles: dict[str, Any] = {}
        for key, value in form.items():
            if not key.startswith("role:"):
                continue
            ids = [x.strip() for x in str(value).split(",") if x.strip()]
            unknown = [x for x in ids if x not in known]
            if unknown:
                raise HTTPException(400, f"нет провайдера: {', '.join(unknown)}")
            roles[key[5:]] = ids if (len(ids) > 1 or key == "role:judge") else (ids[0] if ids else "")
        cfg["roles"] = roles
        ws.save_config(cfg)
        return RedirectResponse("/pool", status_code=303)

    @app.post("/pool/profile/{name}")
    def apply_profile(name: str) -> RedirectResponse:
        cfg = ws.config()
        profile = (cfg.get("profiles") or {}).get(name)
        if profile is None:
            raise HTTPException(404)
        cfg["roles"] = {**(cfg.get("roles") or {}), **profile}
        ws.save_config(cfg)
        return RedirectResponse("/pool", status_code=303)

    # --- стенд ------------------------------------------------------------------------
    @app.get("/bench")
    def bench_page(request: Request, role: str = "critic", run: str = "") -> HTMLResponse:
        cfg = ws.config()
        task_dir = ws.root / "eval" / "tasks"
        roles = sorted(p.name for p in task_dir.iterdir() if p.is_dir()) if task_dir.exists() else []
        db = ws.db()
        try:
            run_ids = [r[0] for r in db.execute("SELECT DISTINCT run FROM bench_runs WHERE role = ? ORDER BY run DESC",
                                                (role,)).fetchall()]
            if not run and run_ids:
                run = run_ids[0]
            table, recommendation = summary(db, role, run if run != "all" else None)
            runs = db.execute("SELECT * FROM bench_runs WHERE role = ? ORDER BY id DESC LIMIT 200", (role,)).fetchall()
        finally:
            db.close()
        enabled = [p["id"] for p in cfg.get("providers") or [] if p.get("enabled", True) is not False]
        return render(request, "bench.html", role=role, roles=roles, run=run, run_ids=run_ids, table=table,
                      recommendation=recommendation, runs=runs, providers=enabled, judges=role_providers(cfg, "judge"))

    @app.post("/bench/run")
    async def bench_run(request: Request) -> RedirectResponse:
        form = await request.form()
        role = str(form.get("role") or "")
        pids = [str(p) for p in form.getlist("providers")]
        if not role or not pids:
            raise HTTPException(400, "выберите роль и хотя бы одну модель")
        job_id = f"{role}-{time.strftime('%H%M%S')}"
        jobs[job_id] = {"role": role, "providers": pids, "done": 0, "total": 0, "note": "в очереди",
                        "error": None, "finished": False}

        def work() -> None:
            def progress(done: int, total: int, note: str) -> None:
                jobs[job_id].update(done=done, total=total, note=note)
            try:
                run_bench(ws, role, pids, progress=progress)
            except Exception as e:  # граница потока: ошибку показываем в интерфейсе
                logger.exception("bench %s", job_id)
                jobs[job_id]["error"] = str(e)
            finally:
                jobs[job_id]["finished"] = True

        threading.Thread(target=work, daemon=True).start()
        return RedirectResponse(f"/bench?role={role}", status_code=303)

    @app.get("/bench/jobs", response_class=HTMLResponse)
    def bench_jobs() -> str:
        if not jobs:
            return "<p class=muted>Прогонов в этой сессии сервера не было.</p>"
        items = []
        for jid, j in reversed(list(jobs.items())):
            state = "ошибка: " + j["error"] if j["error"] else ("завершён" if j["finished"] else "идёт")
            items.append(f"<li><b>{escape(jid)}</b> {escape(', '.join(j['providers']))} — "
                         f"{j['done']}/{j['total']} · {escape(j['note'])} · {escape(state)}</li>")
        return "<ul>" + "".join(items) + "</ul>"

    @app.post("/bench/score/{rid}", response_class=HTMLResponse)
    async def bench_score(rid: int, request: Request) -> str:
        score = int(str((await request.form()).get("score") or 0))
        if not 1 <= score <= 5:
            raise HTTPException(400, "оценка 1–5")
        db = ws.db()
        try:
            db.execute("UPDATE bench_runs SET human_score = ? WHERE id = ?", (score, rid))
            db.commit()
        finally:
            db.close()
        return f"<b>{score}</b>"

    @app.get("/bench/answer/{rid}")
    def bench_answer(rid: int, request: Request) -> HTMLResponse:
        db = ws.db()
        try:
            row = db.execute("SELECT * FROM bench_runs WHERE id = ?", (rid,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise HTTPException(404)
        answer = Path(row["answer_path"]).read_text(encoding="utf-8") if row["answer_path"] else ""
        judges = json.dumps(json.loads(row["judge_scores"] or "{}"), ensure_ascii=False, indent=2)
        blocks = [("Ответ модели", answer), ("Оценки судей", judges)]
        if row["error"]:
            blocks.insert(0, ("Ошибка", row["error"]))
        return render(request, "call.html", title=f"{row['task']} · {row['provider']}", blocks=blocks)

    # --- журнал вызовов ---------------------------------------------------------------
    @app.get("/calls")
    def calls_page(request: Request, role: str = "", provider: str = "") -> HTMLResponse:
        sql = "SELECT * FROM calls WHERE 1=1"
        args: list[str] = []
        if role:
            sql, args = sql + " AND role = ?", [*args, role]
        if provider:
            sql, args = sql + " AND provider = ?", [*args, provider]
        db = ws.db()
        try:
            rows = db.execute(sql + " ORDER BY id DESC LIMIT 300", args).fetchall()
            totals = db.execute("SELECT count(*) n, sum(tokens_in) tin, sum(tokens_out) tout, sum(cost_usd) usd "
                                "FROM calls").fetchone()
        finally:
            db.close()
        return render(request, "calls.html", rows=rows, totals=totals, role=role, provider=provider)

    @app.get("/calls/{cid}")
    def call_page(cid: int, request: Request) -> HTMLResponse:
        db = ws.db()
        try:
            row = db.execute("SELECT * FROM calls WHERE id = ?", (cid,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise HTTPException(404)
        path = Path(row["prompt_path"])
        record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        blocks = [(f"{m['role']}", m["content"]) for m in record.get("messages", [])]
        blocks.append(("ответ", record.get("text") or ""))
        if record.get("error"):
            blocks.append(("ошибка", record["error"]))
        meta = {k: record.get(k) for k in ("provider", "family", "model", "tokens_in", "tokens_out",
                                           "duration_ms", "cost_usd", "params")}
        blocks.insert(0, ("параметры", json.dumps(meta, ensure_ascii=False, indent=2, default=str)))
        return render(request, "call.html", title=f"Вызов {cid} · {row['role']} · {row['provider']}", blocks=blocks)

    register(app, ws, render)
    return app
