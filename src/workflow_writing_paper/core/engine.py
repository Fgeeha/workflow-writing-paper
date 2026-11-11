"""Очередь задач в SQLite и фоновый воркер в процессе сервера."""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
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
    with closing(ws.db()) as db:
        cur = db.execute("INSERT INTO tasks(project, stage, status, payload) VALUES (?,?,'queued',?)",
                         (slug, stage, json.dumps(payload or {}, ensure_ascii=False)))
        db.commit()
        return int(cur.lastrowid or 0)


def run_task(ws: Workspace, task_id: int) -> dict[str, Any]:
    """Выполнить задачу синхронно: running → done | failed; ошибка стадии записывается в задачу."""
    from ..roles import StageError, run_stage  # поздний импорт: roles тянет провайдеров

    with closing(ws.db()) as db:
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"задачи {task_id} нет")
        cur = db.execute("UPDATE tasks SET status='running', started_at=datetime('now') WHERE id = ? AND status = 'queued'",
                         (task_id,))
        db.commit()
        if cur.rowcount == 0:  # задачу уже забрал воркер сервера или другой процесс
            return {"error": f"задача {task_id} уже {row['status']}"}
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
    with closing(ws.db()) as db:
        db.execute("UPDATE tasks SET status=?, finished_at=datetime('now'), error=?, result=? WHERE id = ?",
                   ("failed" if error else "done", error, json.dumps(result, ensure_ascii=False, default=str), task_id))
        db.commit()
    return {"error": error, **result}


def run_pending(ws: Workspace) -> int:
    """Выполнить все задачи в очереди по одной; вернуть их число."""
    with closing(ws.db()) as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY id").fetchall()]
    for tid in ids:
        run_task(ws, tid)
    return len(ids)


class Worker(threading.Thread):
    """Поток в процессе сервера: раз в секунду раздаёт задачи очереди пулу.

    Проекты — параллельно, до `worker.parallel` из providers.yaml (по умолчанию 2); у проекта одна задача за раз:
    стадии пишут одни и те же артефакты. GPU-модели всё равно идут по одной — семафор провайдера.
    """

    def __init__(self, ws: Workspace, interval: float = 1.0) -> None:
        super().__init__(daemon=True, name="wwp-worker")
        self.ws, self.interval, self._stop = ws, interval, threading.Event()
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def tick(self, pool: ThreadPoolExecutor, parallel: int) -> list[Future[Any]]:
        """Запустить по первой задаче очереди каждого свободного проекта, пока есть места."""
        with closing(self.ws.db()) as db:
            rows = db.execute("SELECT id, project FROM tasks WHERE status='queued' ORDER BY id").fetchall()
        started = []
        with self._lock:
            for r in rows:
                if r["project"] in self._busy or len(self._busy) >= parallel:
                    continue
                self._busy.add(r["project"])
                started.append(pool.submit(self._run, r["id"], r["project"]))
        return started

    def _run(self, task_id: int, project: str) -> None:
        try:
            run_task(self.ws, task_id)
        except Exception:
            logger.exception("task %s", task_id)
        finally:
            with self._lock:
                self._busy.discard(project)

    def run(self) -> None:
        parallel = max(1, int((self.ws.config().get("worker") or {}).get("parallel", 2)))
        with ThreadPoolExecutor(parallel, thread_name_prefix="wwp-task") as pool:
            while not self._stop.is_set():
                try:
                    self.tick(pool, parallel)
                except Exception:
                    logger.exception("worker")
                self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
