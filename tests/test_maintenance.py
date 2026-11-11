"""wwp reindex, wwp gc, wwp export."""

import os
import shutil
import time

import pytest

from workflow_writing_paper.cli import main
from workflow_writing_paper.core import Project, Workspace
from workflow_writing_paper.core.maintenance import gc, reindex
from workflow_writing_paper.roles import StageError, export

OLD = time.time() - 100 * 86400


def _project(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.init()
    project = Project.create(ws, "demo")
    calls = project.path / "calls"
    used = calls / "2026-10-31T12-00-00-000001-ideator-writer.json"
    used.write_text('{"provider": "writer", "family": "a", "role": "ideator", "tokens_in": 10, "tokens_out": 5}')
    stray = calls / "2026-10-31T12-00-01-000002-critic-crit.json"
    stray.write_text('{"provider": "crit", "family": "b", "role": "critic", "cost_usd": 0.01}')
    project.write_artifact("novelty", "## v1\n", {"role": "ideator", "provider": "writer", "family": "a",
                                                    "call": str(used)})
    project.decide("novelty", "approve")
    project.write_artifact("novelty", "## v2\n", {"role": "ideator", "provider": "writer", "family": "a"})
    project.decide("novelty", "reject")
    project.write_artifact("novelty", "## v3\n", {"role": "ideator", "provider": "writer", "family": "a"})
    (project.path / "artifacts" / "reviews" / "novelty.v2.judge-x.json").write_text("{}")
    return ws, project, used, stray


def test_reindex_restores_db_from_files(tmp_path):
    ws, _, used, _ = _project(tmp_path)
    (ws.root / "wwp.sqlite").unlink()
    assert reindex(ws) == {"projects": 1, "calls": 2, "approvals": 2, "ab_choices": 0}
    assert reindex(ws) == {"projects": 0, "calls": 0, "approvals": 0, "ab_choices": 0}  # повтор ничего не дублирует
    db = ws.db()
    rows = db.execute("SELECT artifact, version, decision FROM approvals ORDER BY version").fetchall()
    call = db.execute("SELECT * FROM calls WHERE prompt_path = ?", (str(used),)).fetchone()
    db.close()
    assert [tuple(r) for r in rows] == [("novelty", 1, "approve"), ("novelty", 2, "reject")]
    assert call["project"] == "demo" and call["tokens_in"] == 10 and call["created_at"] == "2026-10-31 12:00:00"
    assert Project.open(ws, "demo").artifact("novelty").version == 3


def test_gc_keeps_referenced_and_approved_and_needs_apply(tmp_path):
    ws, project, used, stray = _project(tmp_path)
    reindex(ws)
    history = project.path / "artifacts" / "history"
    for p in [used, stray, *history.glob("*.md")]:
        os.utime(p, (OLD, OLD))
    review = project.path / "artifacts" / "reviews" / "novelty.v2.judge-x.json"
    expected = {stray, history / "novelty.v2.md", review}
    assert set(gc(ws, 90)) == expected and stray.exists()  # без apply — только список
    assert gc(ws, 200) == []
    assert main(["--workspace", str(ws.root), "gc", "--older-than", "90d", "--apply"]) == 0
    assert not any(p.exists() for p in expected)
    assert used.exists() and (history / "novelty.v1.md").exists()  # вызов из created_by и одобренная версия
    db = ws.db()
    assert db.execute("SELECT count(*) FROM calls WHERE prompt_path = ?", (str(stray),)).fetchone()[0] == 0
    db.close()


def test_export_requires_paper_and_pandoc(tmp_path):
    ws, project, _, _ = _project(tmp_path)
    with pytest.raises(StageError, match="paper.md"):
        export(project)
    (project.path / "paper.md").write_text("# demo\n\nТекст.\n", encoding="utf-8")
    if not shutil.which("pandoc"):
        with pytest.raises(StageError, match="pandoc"):
            export(project)
        return
    assert export(project, "docx").exists()
    assert main(["--workspace", str(ws.root), "export", "demo"]) == 0
