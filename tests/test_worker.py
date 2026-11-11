"""Воркер: проекты параллельно, у проекта одна задача за раз."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from test_pipeline import make

from workflow_writing_paper.core import Project, engine
from workflow_writing_paper.core.engine import Worker, enqueue


def test_worker_parallel_projects_serial_within_project(tmp_path, monkeypatch):
    ws, _ = make(tmp_path)
    Project.create(ws, "other", template="experimental")
    running: dict[str, int] = {}
    peak = {"total": 0, "per_project": 0}
    lock = threading.Lock()

    def fake_run_task(ws_, tid):
        with ws_.db() as db:
            project = db.execute("SELECT project FROM tasks WHERE id=?", (tid,)).fetchone()["project"]
            db.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        with lock:
            running[project] = running.get(project, 0) + 1
            peak["total"] = max(peak["total"], sum(running.values()))
            peak["per_project"] = max(peak["per_project"], running[project])
        time.sleep(0.2)
        with lock:
            running[project] -= 1
        return {}

    monkeypatch.setattr(engine, "run_task", fake_run_task)
    for slug in ("demo", "demo", "other"):
        enqueue(ws, slug, "review", {"artifact": "novelty"}, gate=False)
    worker = Worker(ws)
    with ThreadPoolExecutor(2) as pool:
        first = worker.tick(pool, 2)
        assert len(first) == 2  # по одной задаче demo и other; вторая demo ждёт
        assert worker.tick(pool, 2) == []  # оба проекта заняты
        wait(first)
        wait(worker.tick(pool, 2))
    assert peak == {"total": 2, "per_project": 1}
    with ws.db() as db:
        assert db.execute("SELECT count(*) FROM tasks WHERE status='queued'").fetchone()[0] == 0


def test_run_task_claims_once(tmp_path):
    ws, _ = make(tmp_path)
    tid = enqueue(ws, "demo", "novelty", {"input": "x"})
    assert engine.run_task(ws, tid)["error"] is None
    assert "уже done" in engine.run_task(ws, tid)["error"]  # повторный захват той же задачи


def test_interrupted_running_task_marked_failed(tmp_path):
    ws, _ = make(tmp_path)
    tid = enqueue(ws, "demo", "novelty", {"input": "x"})
    with ws.db() as db:
        db.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    assert engine.recover_interrupted(ws) == 1
    with ws.db() as db:
        row = db.execute("SELECT status, error FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "failed" and "прервано" in row["error"]
    assert engine.recover_interrupted(ws) == 0
