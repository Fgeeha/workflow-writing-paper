"""Живое A/B: стадия выполняется дважды — моделью роли (A) и моделью B, — автор выбирает версию.

Обе версии проходят полный цикл стадии с судьями и лежат в history/, поэтому сравнивать их можно на странице
diff. Выбор пишется в `ab_choices` и попадает в таблицу стенда долей побед.
"""

from __future__ import annotations

import threading
from contextlib import closing
from typing import Any

from ..core.project import STAGE_ARTIFACT, Project

STAGE_ROLE = {"novelty": "ideator", "literature": "literature_scout", "experiment_plan": "experiment_planner",
              "draft": "section_writer"}


def stage_ab(project: Project, stage: str, provider_b: str, payload: dict[str, Any],
             task: str | None = None) -> dict[str, Any]:
    from ..providers import build_pool
    from . import StageError, run_stage

    role = STAGE_ROLE.get(stage)
    if role is None:
        raise StageError(f"A/B есть только у стадий {', '.join(STAGE_ROLE)}")
    provider_a = project.role_provider(role)
    if not provider_a or provider_a == provider_b:
        raise StageError(f"для A/B нужна модель B, отличная от назначенной на {role} ({provider_a})")
    if provider_b not in build_pool(project.ws.config(), text=project.ws.text):  # до платного прогона A
        raise StageError(f"провайдера {provider_b} нет в пуле")
    type_ = STAGE_ARTIFACT.get(stage, stage)
    before = project.artifact(type_)
    result_a = run_stage(project, stage, payload, task)
    version_a = project.artifact(type_).version  # type: ignore[union-attr]
    payload_b = {**payload, "_draft_base": before.body if before else ""} if stage == "draft" else payload
    roles = project.manifest.setdefault("roles", {})
    saved = roles.get(role)
    roles[role] = provider_b  # только в памяти: манифест на диск не пишется
    error_b = None
    try:
        result_b = run_stage(project, stage, payload_b, task)
    except StageError as e:
        result_b, error_b = {}, str(e)
    finally:
        if saved is None:
            roles.pop(role, None)
        else:
            roles[role] = saved
    art = project.artifact(type_)
    assert art is not None
    if art.version == version_a:  # B не успела написать версию — сравнивать нечего, A остаётся текущей
        raise StageError(f"A готова (v{version_a}), модель B упала: {error_b}")
    art.meta["ab"] = {"role": role, "a": {"provider": provider_a, "version": version_a},
                      "b": {"provider": provider_b, "version": art.version, "error": error_b}}
    project.update_artifact(art)
    return {"accepted": False, "blockers": [], "ab": art.meta["ab"],
            "accepted_a": result_a.get("accepted"), "accepted_b": result_b.get("accepted")}


_choose_lock = threading.Lock()  # ponytail: один замок на процесс; хватает одного автора и одного сервера


def choose(project: Project, type_: str, chosen: str) -> dict[str, Any]:
    """Выбор автора `a` | `b`. Версия A восстанавливается новой версией с её отчётами судей, на проверке."""
    with _choose_lock:
        art = project.artifact(type_)
        ab = (art.meta.get("ab") if art else None) or {}
        if not ab or chosen not in ("a", "b"):
            raise ValueError("у артефакта нет незавершённого A/B или выбор не a/b")
        assert art is not None
        winner = ab[chosen]
        if chosen == "a":
            old = project.artifact_version(type_, int(winner["version"]))
            if old is None:
                raise ValueError(f"версии {type_} v{winner['version']} нет в истории")
            art = project.write_artifact(type_, old.body, old.meta.get("created_by") or {}, status="in_review")
            art.meta.update({k: old.meta.get(k) for k in ("reviews", "review_summary")})
        art.meta.pop("ab", None)
        art.meta["ab_chosen"] = {**ab, "chosen": chosen, "version": art.version}
        project.update_artifact(art)
        with closing(project.ws.db()) as db:
            db.execute("INSERT INTO ab_choices(project, artifact, role, provider_a, provider_b, chosen, version) "
                       "VALUES (?,?,?,?,?,?,?)", (project.slug, type_, ab["role"], ab["a"]["provider"],
                                                  ab["b"]["provider"], winner["provider"], art.version))
            db.commit()
        return {"chosen": winner["provider"], "version": art.version}


def wins(db: Any, role: str) -> dict[str, tuple[int, int]]:
    """Провайдер → (побед, сравнений) по выборам автора в A/B роли."""
    out: dict[str, tuple[int, int]] = {}
    for r in db.execute("SELECT provider_a, provider_b, chosen FROM ab_choices WHERE role = ?", (role,)).fetchall():
        for p in (r["provider_a"], r["provider_b"]):
            w, n = out.get(p, (0, 0))
            out[p] = (w + (r["chosen"] == p), n + 1)
    return out


__all__ = ["STAGE_ROLE", "choose", "stage_ab", "wins"]
