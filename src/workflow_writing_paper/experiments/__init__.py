"""Сценарии экспериментов из манифеста: запуск local / ssh, разбор результатов по `mapping`, запись в реестр.

Вызывается стадией `experiments` (roles.stage_experiments), формой реестра и `wwp run <slug> experiments`.
Пишут в results.json только этот модуль и ручной ввод (registry.add_manual).
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ..core.project import Project
from ..registry import load, save

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3600  # секунд на команду сценария; переопределяется полем `timeout`
_JSON_STEP = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")
_JSON_PATH = re.compile(r"(?:\.?[^.\[\]]+|\[\d+\])+")  # весь путь после `$` — только из таких шагов
CONFLICT_PREFIX = "конфликт реестра"  # по этому префиксу страница реестра находит остановки среди задач


class ExperimentError(RuntimeError):
    """Ошибка сценария: конфигурация, запуск, разбор результатов."""


class RegistryConflict(ExperimentError):
    """Ключ уже есть в реестре, новое значение расходится больше объявленной точности."""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        self.conflicts = conflicts
        lines = [f"{c['key']}: было {c['old']}, стало {c['new']}, разница {c['diff']:+g}" for c in conflicts]
        super().__init__(f"{CONFLICT_PREFIX}, запись не произведена — подтвердите перезапись (force) или оставьте "
                         "старое значение: " + "; ".join(lines))


# --- запуск -----------------------------------------------------------------------------------------
def _shell(host: str, cwd: str, command: list[str]) -> list[str]:
    """Команда для subprocess: локально — как есть, `ssh:<alias>` — через ssh с cd в cwd."""
    if host == "local":
        return list(command)
    if host.startswith("ssh:") and host[4:]:
        remote = f"cd {shlex.quote(cwd)} && {' '.join(shlex.quote(c) for c in command)}"
        return ["ssh", "-o", "BatchMode=yes", host[4:], remote]
    raise ExperimentError(f"host должен быть local или ssh:<alias>, получено {host!r}")


def _run(host: str, cwd: str, command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Запустить команду на host с таймаутом; stdout/stderr — в результате."""
    try:
        return subprocess.run(_shell(host, cwd, command), cwd=cwd if host == "local" else None, timeout=timeout,
                              capture_output=True, text=True, check=False)
    except subprocess.TimeoutExpired as e:
        raise ExperimentError(f"сценарий не завершился за {timeout:g} с") from e
    except (FileNotFoundError, NotADirectoryError) as e:
        raise ExperimentError(f"не найдено: {e.filename}") from e


def _fetch(host: str, cwd: str, rel: str, dst: Path) -> None:
    """Файл результатов → dst: локально копия, по ssh — scp."""
    if host == "local":
        src = Path(cwd, rel)
        if not src.exists():
            raise ExperimentError(f"файл результатов не найден: {src}")
        dst.write_bytes(src.read_bytes())
        return
    remote = shlex.quote(f"{cwd.rstrip('/')}/{rel}")  # удалённую часть разбирает shell той стороны
    proc = subprocess.run(["scp", "-o", "BatchMode=yes", f"{host[4:]}:{remote}", str(dst)],
                          capture_output=True, text=True, check=False)
    if proc.returncode:
        raise ExperimentError(f"scp {rel}: {proc.stderr.strip()[-300:]}")


def _git_rev(host: str, cwd: str) -> str | None:
    """Короткая ревизия git в cwd (локально или по ssh); None, если это не репозиторий."""
    try:
        proc = _run(host, cwd, ["git", "rev-parse", "--short", "HEAD"], 30)
    except ExperimentError:
        return None
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


# --- разбор результатов ----------------------------------------------------------------------------
def json_path(data: Any, path: str) -> Any:
    """Подмножество JSONPath: `$.a.b[0].c`; ошибка, если шага нет."""
    body = path.removeprefix("$")
    if not body or not _JSON_PATH.fullmatch(body):
        raise ExperimentError(f"путь {path}: ожидается вид $.a.b[0].c")
    cur = data
    for m in _JSON_STEP.finditer(body):
        key, idx = m.group(1), m.group(2)
        try:
            cur = cur[int(idx)] if idx is not None else cur[key]
        except (KeyError, IndexError, TypeError) as e:
            raise ExperimentError(f"путь {path}: нет шага {idx if idx is not None else key!r}") from e
    return cur


def csv_cell(rows: list[dict[str, str]], path: str) -> str:
    """`столбец@строка`: строка — номер с 1 или `last`; без `@` — последняя строка."""
    col, _, row = path.partition("@")
    if not rows:
        raise ExperimentError("CSV пуст")
    if col not in rows[0]:
        raise ExperimentError(f"в CSV нет столбца {col!r}; есть {list(rows[0])}")
    if row not in ("", "last") and not row.isdigit():
        raise ExperimentError(f"{path}: строка — номер с 1 или last")
    idx = len(rows) - 1 if row in ("", "last") else int(row) - 1
    if not 0 <= idx < len(rows):
        raise ExperimentError(f"в CSV нет строки {row}")
    return rows[idx][col]


def extract(path: Path, mapping: dict[str, str]) -> dict[str, float]:
    """Значения по `mapping` {путь: ключ реестра} из файла: пути `$…` — для JSON, `столбец@строка` — для CSV."""
    is_csv = path.suffix.lower() == ".csv"
    text = path.read_text(encoding="utf-8")
    rows = list(csv.DictReader(text.splitlines())) if is_csv else []
    data = None if is_csv else json.loads(text)
    out: dict[str, float] = {}
    for src, key in mapping.items():
        if src.startswith("$") == is_csv:
            continue  # путь для другого формата файла
        raw = csv_cell(rows, src) if is_csv else json_path(data, src)
        try:
            out[key] = float(str(raw).replace(",", "."))
        except ValueError as e:
            raise ExperimentError(f"{src} → {key}: значение {raw!r} не число") from e
    return out


# --- сценарий ---------------------------------------------------------------------------------------
def scenario(project: Project, scenario_id: str) -> dict[str, Any]:
    """Сценарий манифеста по id; пустой id допустим, если сценарий один."""
    items = project.manifest.get("scenarios") or []
    if not scenario_id:
        if len(items) == 1:
            return dict(items[0])
        raise ExperimentError("укажите сценарий: " + (", ".join(s["id"] for s in items) or "в манифесте нет сценариев"))
    hit = next((s for s in items if s.get("id") == scenario_id), None)
    if hit is None:
        raise ExperimentError(f"сценария {scenario_id} нет в манифесте")
    return dict(hit)


def run_scenario(project: Project, scenario_id: str = "", force: bool = False) -> dict[str, Any]:
    """Выполнить сценарий, забрать результаты, записать числа в реестр. Конфликт — RegistryConflict.

    `force=True` — перезаписать расходящиеся значения (решение автора). stdout/stderr и run.json — в
    `<project>/runs/<время>-<id>/`.
    """
    sc = scenario(project, scenario_id)
    host = str(sc.get("host") or "local")
    cwd = str(sc.get("cwd") or ".")
    if host == "local":
        cwd = str((project.path / cwd).resolve())
    command = [str(c) for c in sc.get("command") or []]
    mapping = {str(k): str(v) for k, v in (sc.get("mapping") or {}).items()}
    results = [str(r) for r in sc.get("results") or []]
    if not command or not mapping or not results:
        raise ExperimentError(f"сценарий {sc['id']}: нужны command, results и mapping")

    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = project.path / "runs" / f"{stamp}-{sc['id']}"
    for n in range(2, 1000):  # несколько запусков в одну секунду не затирают друг друга
        if not run_dir.exists():
            break
        run_dir = run_dir.with_name(f"{stamp}-{sc['id']}-{n}")
    run_dir.mkdir(parents=True)
    started = time.monotonic()
    proc = _run(host, cwd, command, float(sc.get("timeout") or DEFAULT_TIMEOUT))
    (run_dir / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode:
        raise ExperimentError(f"сценарий {sc['id']} завершился с кодом {proc.returncode}; "
                              f"stderr: {proc.stderr.strip()[-300:] or '(пусто)'}; журнал в runs/{run_dir.name}")

    values: dict[str, float] = {}
    for rel in results:
        local = run_dir / rel.replace("/", "__")  # out/a.json и eval/a.json не затирают друг друга
        _fetch(host, cwd, rel, local)
        values.update(extract(local, mapping))
    missing = sorted(set(mapping.values()) - set(values))
    if missing:
        raise ExperimentError(f"ключи {', '.join(missing)} не найдены в файлах результатов")
    meta = {"source": sc["id"], "run": time.strftime("%Y-%m-%dT%H:%M:%S"), "seed": sc.get("seed"),
            "command_hash": hashlib.sha1(" ".join(command).encode()).hexdigest()[:12],
            "git_rev": _git_rev(host, cwd), "host": host, "duration_s": round(time.monotonic() - started, 1)}
    units = {str(k): str(v) for k, v in (sc.get("units") or {}).items()}
    try:
        written = write_registry(project, values, units, meta, force)
    finally:
        (run_dir / "run.json").write_text(json.dumps({**meta, "values": values}, ensure_ascii=False, indent=1,
                                                     default=str), encoding="utf-8")
    return {"scenario": sc["id"], "values": values, "written": written, "run_dir": run_dir.name, **meta}


def write_registry(project: Project, values: dict[str, float], units: dict[str, str], meta: dict[str, Any],
                   force: bool) -> list[str]:
    """Записать значения в results.json; расхождение с имеющимся больше точности журнала — конфликт.

    Атомарно: при любом конфликте ничего не записывается (числа одного прогона не смешиваются с другим),
    поднимается RegistryConflict. `force` снимает проверку.
    """
    path = project.path / "results.json"
    data = load(path)
    rounding = int((project.journal().get("numbers") or {}).get("rounding", 2))
    conflicts = []
    for key, new in values.items():
        prev: dict[str, Any] = data[key] if isinstance(data.get(key), dict) else {}
        old = prev.get("value")
        if (isinstance(old, (int, float)) and not isinstance(old, bool) and not force
                and round(float(old), rounding) != round(new, rounding)):
            conflicts.append({"key": key, "old": old, "new": new, "diff": new - float(old)})
    if conflicts:
        raise RegistryConflict(conflicts)
    for key, new in values.items():
        prev = data[key] if isinstance(data.get(key), dict) else {}
        data[key] = {"value": new, "unit": units.get(key, prev.get("unit", "")), **meta}
    save(path, data)
    return list(values)


__all__ = ["CONFLICT_PREFIX", "ExperimentError", "RegistryConflict", "csv_cell", "extract", "json_path",
           "run_scenario", "scenario", "write_registry"]
