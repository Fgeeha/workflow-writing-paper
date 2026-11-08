"""v0.4: сценарии экспериментов — запуск local и ssh, парсер JSON/CSV по mapping, конфликты реестра."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml
from test_pipeline import NOVELTY_OK, make

from workflow_writing_paper.core import Project
from workflow_writing_paper.core.engine import enqueue, run_pending, run_task
from workflow_writing_paper.experiments import (
    ExperimentError,
    RegistryConflict,
    csv_cell,
    extract,
    json_path,
    run_scenario,
)
from workflow_writing_paper.registry import add_manual, load

# скрипт эксперимента: число берётся из аргумента, пишет JSON и CSV
BENCH = """import json, sys, pathlib
v = float(sys.argv[1]); pathlib.Path("out").mkdir(exist_ok=True)
json.dump({"latency_ms": {"p50": v, "p95": v * 2}, "runs": [{"f1": 0.81}, {"f1": 0.83}]}, open("out/metrics.json", "w"))
open("out/table.csv", "w").write("epoch,acc\\n1,0.5\\n2,0.9\\n")
print("done", v)
"""


def scenario(value: str, host: str = "local", cwd: str = ".") -> dict:
    return {"id": "bench", "cwd": cwd, "host": host, "command": [sys.executable, "bench.py", value],
            "results": ["out/metrics.json", "out/table.csv"], "seed": 42, "units": {"latency_p50_ms": "ms"},
            "mapping": {"$.latency_ms.p50": "latency_p50_ms", "$.runs[1].f1": "f1_best", "acc@last": "acc_final",
                        "acc@1": "acc_first"}}


def prepare(tmp_path: Path, value: str = "103.32") -> tuple:
    ws, project = make(tmp_path)
    (project.path / "bench.py").write_text(BENCH, encoding="utf-8")
    project.manifest["scenarios"] = [scenario(value)]
    project.save_manifest()
    project.write_artifact("novelty", NOVELTY_OK, {"role": "ideator", "provider": "writer", "family": "a"})
    project.decide("novelty", "approve")
    return ws, project


def test_parsers():
    data = {"a": {"b": [10, {"c": 2.5}]}}
    assert json_path(data, "$.a.b[1].c") == 2.5 and json_path(data, "a.b[0]") == 10
    with pytest.raises(ExperimentError, match="нет шага 'x'"):
        json_path(data, "$.a.x")
    rows = [{"epoch": "1", "acc": "0,5"}, {"epoch": "2", "acc": "0.9"}]
    assert csv_cell(rows, "acc@last") == "0.9" and csv_cell(rows, "acc@1") == "0,5" and csv_cell(rows, "acc") == "0.9"
    with pytest.raises(ExperimentError, match="нет столбца"):
        csv_cell(rows, "loss@1")
    with pytest.raises(ExperimentError, match="нет строки"):
        csv_cell(rows, "acc@3")
    with pytest.raises(ExperimentError, match="номер с 1 или last"):
        csv_cell(rows, "acc@foo")
    with pytest.raises(ExperimentError, match="ожидается вид"):
        json_path(data, "$.a[x]")


def test_extract_json_and_csv(tmp_path):
    (tmp_path / "m.json").write_text('{"x": {"y": "1,5"}, "z": "abc"}')
    (tmp_path / "t.csv").write_text("a,b\n1,2\n3,4\n")
    mapping = {"$.x.y": "k1", "a@2": "k2", "b": "k3"}
    assert extract(tmp_path / "m.json", mapping) == {"k1": 1.5}  # пути CSV пропущены
    assert extract(tmp_path / "t.csv", mapping) == {"k2": 3.0, "k3": 4.0}
    with pytest.raises(ExperimentError, match="не число"):
        extract(tmp_path / "m.json", {"$.z": "bad"})


def test_local_scenario_writes_registry_with_provenance(tmp_path):
    ws, project = prepare(tmp_path)
    res = run_task(ws, enqueue(ws, "demo", "experiments", {}))  # единственный сценарий — id не нужен
    assert res["error"] is None, res["error"]
    assert res["values"] == {"latency_p50_ms": 103.32, "f1_best": 0.83, "acc_final": 0.9, "acc_first": 0.5}
    assert sorted(res["written"]) == ["acc_final", "acc_first", "f1_best", "latency_p50_ms"]
    reg = load(project.path / "results.json")
    rec = reg["latency_p50_ms"]
    assert rec["value"] == 103.32 and rec["unit"] == "ms" and rec["source"] == "bench" and rec["seed"] == 42
    assert len(rec["command_hash"]) == 12 and rec["host"] == "local" and rec["run"].startswith("20")
    assert rec["git_rev"]  # проект — git-репозиторий, ревизия после коммита одобрения novelty
    assert reg["f1_best"]["unit"] == ""
    run_dir = project.path / "runs" / res["run_dir"]
    assert (run_dir / "stdout.txt").read_text().startswith("done") and (run_dir / "out__metrics.json").exists()
    assert json.loads((run_dir / "run.json").read_text())["values"]["acc_final"] == 0.9
    assert project.stage_state("experiments") == "done"


def test_conflict_stops_and_force_overwrites(tmp_path):
    ws, project = prepare(tmp_path, "95.5")
    add_manual(project.path / "results.json", "latency_p50_ms", 103.32, "ms")  # автор уже внёс другое значение
    add_manual(project.path / "results.json", "acc_final", 0.9)  # совпадает с точностью журнала
    assert project.stage_state("experiments") == "pending"  # ручной ввод стадию не закрывает
    res = run_task(ws, enqueue(ws, "demo", "experiments", {"scenario": "bench"}))
    assert res["error"] and "конфликт реестра" in res["error"] and "было 103.32, стало 95.5" in res["error"]
    reg = load(project.path / "results.json")
    assert reg["latency_p50_ms"]["value"] == 103.32 and reg["latency_p50_ms"]["source"] == "manual"  # не перезаписан
    assert reg["acc_final"]["source"] == "manual" and "f1_best" not in reg  # атомарно: ничего из прогона не записано
    with pytest.raises(RegistryConflict) as ei:
        run_scenario(project, "bench")
    assert ei.value.conflicts[0]["key"] == "latency_p50_ms" and ei.value.conflicts[0]["diff"] == pytest.approx(-7.82)

    res = run_task(ws, enqueue(ws, "demo", "experiments", {"scenario": "bench", "force": "1"}))
    assert res["error"] is None and load(project.path / "results.json")["latency_p50_ms"]["value"] == 95.5
    assert len(list((project.path / "runs").iterdir())) == 3  # три запуска в одну секунду — три каталога


def test_scenario_errors(tmp_path):
    _ws, project = prepare(tmp_path)
    with pytest.raises(ExperimentError, match="сценария nope нет"):
        run_scenario(project, "nope")
    project.manifest["scenarios"].append({**scenario("1"), "id": "second"})
    project.save_manifest()
    with pytest.raises(ExperimentError, match="укажите сценарий: bench, second"):
        run_scenario(project, "")
    project.manifest["scenarios"] = [{**scenario("1"), "command": [sys.executable, "-c", "import sys; sys.exit(3)"]}]
    project.save_manifest()
    with pytest.raises(ExperimentError, match="кодом 3"):
        run_scenario(project, "bench")
    project.manifest["scenarios"] = [{**scenario("1"), "mapping": {"$.nothing": "k"}}]
    project.save_manifest()
    with pytest.raises(ExperimentError, match="нет шага 'nothing'"):
        run_scenario(project, "bench")
    project.manifest["scenarios"] = [{**scenario("1"), "host": "ftp:x"}]
    project.save_manifest()
    with pytest.raises(ExperimentError, match="host должен быть"):
        run_scenario(project, "bench")
    project.manifest["scenarios"] = [{**scenario("1"), "timeout": 0.01,
                                      "command": [sys.executable, "-c", "import time; time.sleep(5)"]}]
    project.save_manifest()
    with pytest.raises(ExperimentError, match="не завершился"):
        run_scenario(project, "bench")


def test_ssh_scenario_via_stub_binaries(tmp_path, monkeypatch):
    """ssh/scp подменяются скриптами: ssh выполняет команду локально, scp копирует файл — проверяем сборку команд."""
    _ws, project = prepare(tmp_path)
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "bench.py").write_text(BENCH, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ssh.log"
    ssh = bin_dir / "ssh"
    ssh.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$3" = "gpu-box" ] || exit 9\nexec sh -c "$4"\n')
    scp = bin_dir / "scp"
    scp.write_text(f'#!/bin/sh\necho "scp $@" >> {log}\nsrc="${{3#gpu-box:}}"\ncp "$src" "$4"\n')
    for f in (ssh, scp):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    project.manifest["scenarios"] = [scenario("77.7", host="ssh:gpu-box", cwd=str(remote))]
    project.save_manifest()
    res = run_scenario(project, "bench")
    assert res["values"]["latency_p50_ms"] == 77.7 and res["host"] == "ssh:gpu-box"
    lines = log.read_text().splitlines()
    assert lines[0].startswith("-o BatchMode=yes gpu-box cd ") and "bench.py 77.7" in lines[0]
    assert any(ln.startswith("scp -o BatchMode=yes gpu-box:") and ln.endswith("metrics.json") for ln in lines)
    assert (remote / "out" / "metrics.json").exists()  # команда действительно выполнялась «на удалённой машине»
    assert load(project.path / "results.json")["latency_p50_ms"]["host"] == "ssh:gpu-box"


def test_web_registry_scenarios(tmp_path):
    from fastapi.testclient import TestClient

    from workflow_writing_paper.web import create_app

    ws, project = prepare(tmp_path, "50")
    project.manifest["scenarios"] = []
    project.save_manifest()
    client = TestClient(create_app(ws, worker=False))
    page = client.get("/projects/demo/registry").text
    assert "Сценариев нет" in page and "Зарегистрировать сценарий" in page
    assert client.post("/projects/demo/scenarios", data={"yaml": "id: x\ncommand: [a]"}).status_code == 400  # нет results
    assert client.post("/projects/demo/scenarios", data={"yaml": ":: не yaml"}).status_code == 400
    bad = yaml.safe_dump({**scenario("1"), "command": "python bench.py"})  # строка вместо списка
    assert client.post("/projects/demo/scenarios", data={"yaml": bad}).status_code == 400
    assert client.post("/projects/demo/scenarios", data={"yaml": yaml.safe_dump({**scenario("1"), "host": "ftp:x"})}).status_code == 400
    r = client.post("/projects/demo/scenarios", data={"yaml": yaml.safe_dump(scenario("50"))}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/projects/demo/scenarios", data={"yaml": yaml.safe_dump(scenario("50"))}).status_code == 400
    assert Project.open(ws, "demo").manifest["scenarios"][0]["id"] == "bench"
    page = client.get("/projects/demo/registry").text
    assert "/scenarios/bench/run" in page and "запустить с перезаписью" in page
    assert client.post("/projects/demo/scenarios/bench/run", follow_redirects=False).status_code == 303
    assert run_pending(ws) == 1
    page = client.get("/projects/demo/registry").text
    assert "50.0" in page and "bench" in page and "/runs/" in page
    run = next(p.name for p in (project.path / "runs").iterdir())
    assert client.get(f"/projects/demo/runs/{run}/stdout.txt").text.startswith("<pre>done")
    assert client.get(f"/projects/demo/runs/{run}/secret.txt").status_code == 400
    assert client.get(f"/projects/demo/runs/..%2F{run}/stdout.txt").status_code in (400, 404)
    # конфликт виден на странице реестра и на главной
    add_manual(project.path / "results.json", "latency_p50_ms", 1.0)
    client.post("/projects/demo/scenarios/bench/run", follow_redirects=False)
    run_pending(ws)
    assert "конфликт реестра" in client.get("/projects/demo/registry").text
    assert "конфликт реестра" in client.get("/").text
    assert "сценарии и запуск" in client.get("/projects/demo").text


def test_cli_run_experiments(tmp_path, capsys):
    from workflow_writing_paper.cli import main

    ws, project = prepare(tmp_path, "12.5")
    assert main(["--workspace", str(ws.root), "run", "demo", "experiments", "--scenario", "bench"]) == 0
    out = capsys.readouterr().out
    assert "latency_p50_ms=12.5" in out and "runs/" in out
    add_manual(project.path / "results.json", "f1_best", 0.1)
    assert main(["--workspace", str(ws.root), "run", "demo", "experiments"]) == 1
    assert "конфликт реестра" in capsys.readouterr().err
    assert main(["--workspace", str(ws.root), "run", "demo", "experiments", "--force"]) == 0
    assert load(project.path / "results.json")["f1_best"]["value"] == 0.83
