"""Очередь задач в SQLite и фоновый воркер в процессе сервера."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from .project import Project
from .workspace import Workspace

logger = logging.getLogger(__name__)


def enqueue(ws: Workspace, slug: str, stage: str, payload: dict[str, Any] | None = None, gate: bool = True) -> int:
    """Поставить стадию в очередь. `gate=False` — без проверки порядка стадий (перепроверка артефакта)."""
    project = Project.open(ws, slug)
    ok, why = project.can_run(stage) if gate else (True, "")
    if not ok:
        raise ValueError(why)
    ok, why = project.budget_left()
    if not ok:
        raise ValueError(why)
    db = ws.db()
    try:
        cur = db.execute("INSERT INTO tasks(project, stage, status, payload) VALUES (?,?,'queued',?)",
                         (slug, stage, json.dumps(payload or {}, ensure_ascii=False)))
        db.commit()
        return int(cur.lastrowid or 0)
    finally:
        db.close()


def run_task(ws: Workspace, task_id: int) -> dict[str, Any]:
    from ..roles import StageError, run_stage  # поздний импорт: roles тянет провайдеров

    db = ws.db()
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        db.close()
        raise ValueError(f"задачи {task_id} нет")
    db.execute("UPDATE tasks SET status='running', started_at=datetime('now') WHERE id = ?", (task_id,))
    db.commit()
    db.close()
    result: dict[str, Any] = {}
    error = None
    try:
        project = Project.open(ws, row["project"])
        result = run_stage(project, row["stage"], json.loads(row["payload"] or "{}"), task=str(task_id))
    except (StageError, FileNotFoundError, ValueError) as e:
        error = str(e)
    except Exception as e:
        logger.exception("task %s", task_id)
        error = f"{type(e).__name__}: {e}"
    result.pop("reports", None)  # отчёты лежат в artifacts/reviews/, в задаче — итог
    db = ws.db()
    db.execute("UPDATE tasks SET status=?, finished_at=datetime('now'), error=?, result=? WHERE id = ?",
               ("failed" if error else "done", error, json.dumps(result, ensure_ascii=False, default=str), task_id))
    db.commit()
    db.close()
    return {"error": error, **result}


def run_pending(ws: Workspace) -> int:
    db = ws.db()
    ids = [r["id"] for r in db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY id").fetchall()]
    db.close()
    for tid in ids:
        run_task(ws, tid)
    return len(ids)


class Worker(threading.Thread):
    """Поток в процессе сервера: раз в секунду выполняет задачи из очереди по одной."""

    def __init__(self, ws: Workspace, interval: float = 1.0) -> None:
        super().__init__(daemon=True, name="wwp-worker")
        self.ws, self.interval, self._stop = ws, interval, threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                run_pending(self.ws)
            except Exception:
                logger.exception("worker")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
