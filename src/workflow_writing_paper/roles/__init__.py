"""Роли: сборка контекста кодом, вызов назначенной модели, все стадии конвейера (experiments — без модели)."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.project import Project
from ..experiments import ExperimentError, run_scenario
from ..library import Library
from ..providers import Completion, Provider, ProviderError, build_pool, call_logged
from ..review import ReviewError, review_artifact

logger = logging.getLogger(__name__)


class StageError(RuntimeError):
    pass


# --- вызов роли -----------------------------------------------------------------------------
def _provider(project: Project, role: str) -> Provider:
    pid = project.role_provider(role)
    pool = build_pool(project.ws.config(), text=project.ws.text)
    if not pid or pid not in pool:
        raise StageError(f"на роль {role} не назначен доступный провайдер (providers.yaml → roles)")
    return pool[pid]


def call_role(project: Project, role: str, user: str, task: str | None = None,
              max_tokens: int = 4000, **kwargs: Any) -> tuple[Completion, Provider]:
    ok, why = project.budget_left()
    if not ok:
        raise StageError(why)
    provider = _provider(project, role)
    messages = [{"role": "system", "content": project.ws.text(f"prompts/{project.language}/{role}.md")},
                {"role": "user", "content": user}]
    db = project.ws.db()
    try:
        comp = call_logged(provider, messages, role=role, log_dir=project.path / "calls", db=db,
                           project=project.slug, task=task, max_tokens=max_tokens, temperature=0.3, **kwargs)
    except ProviderError as e:
        raise StageError(f"{role} ({provider.id}): {e}") from e
    finally:
        db.close()
    return comp, provider


def _created_by(role: str, provider: Provider, comp: Completion | None = None) -> dict[str, Any]:
    return {"role": role, "provider": provider.id, "family": provider.family, "call": comp.log_path if comp else None}


def _blockers_text(blockers: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{b.get('judge', 'судья')}] строка {b.get('line', '?')}: {b.get('issue', '')}"
                     + (f" — «{b['quote']}»" if b.get("quote") else "") + (f" → {b['fix']}" if b.get("fix") else "")
                     for b in blockers)


def _low_score_notes(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Оценка ниже порога без блокеров: замечания судей становятся пунктами к правке."""
    notes = [{"judge": jid, "issue": c} for jid, r in result["reports"].items() for c in (r.get("comments") or [])]
    return notes or [{"judge": "review", "issue": f"итоговая оценка {result['min_overall']} ниже порога "
                                                   f"{result['threshold']}"}]


def _review_loop(project: Project, type_: str, task: str | None,
                 revise: Callable[[list[dict[str, Any]]], None]) -> dict[str, Any]:
    """Проверка; при блокерах — до auto_iterations правок ролью-автором, дальше человек."""
    limit = int(project.review_settings()["auto_iterations"])
    try:
        result = review_artifact(project, type_, task)
        for _ in range(limit):
            if result["accepted"] or (result["errors"] and not result["blockers"]):
                break  # принято — или судей не хватило: правка текста это не исправит
            revise(result["blockers"] or _low_score_notes(result))
            result = review_artifact(project, type_, task)
    except ReviewError as e:
        raise StageError(str(e)) from e
    return result


def _author_critic_author(project: Project, role: str, base: str, task: str | None,
                          max_tokens: int = 4000) -> tuple[Completion, Provider]:
    """Роль пишет черновик, critic его разбирает, роль переписывает с ответом на критику."""
    draft, _ = call_role(project, role, base, task, max_tokens)
    critique, _ = call_role(project, "critic", f"{base}\n\n## Черновик\n{draft.text}", task, 2500)
    return call_role(project, role, f"{base}\n\n## Твой черновик\n{draft.text}\n\n## Критика\n{critique.text}\n\n"
                     "Перепиши черновик, ответив на критику: исправь обоснованное, аргументированно отклони спорное.",
                     task, max_tokens)


def _reviser(project: Project, type_: str, role: str, base: str, task: str | None,
             max_tokens: int = 4000) -> Callable[[list[dict[str, Any]]], None]:
    """Правка артефакта ролью-автором по блокерам проверки — шаг цикла `_review_loop`."""
    def revise(blockers: list[dict[str, Any]]) -> None:
        cur = project.artifact(type_)
        fixed, p = call_role(project, role, f"{base}\n\n## Текущая версия\n{cur.body if cur else ''}\n\n"
                             f"## Блокеры проверки\n{_blockers_text(blockers)}\n\nИсправь все блокеры, остальное сохрани.",
                             task, max_tokens)
        project.write_artifact(type_, fixed.text, _created_by(role, p, fixed))
    return revise


def _approved(project: Project, type_: str, title: str) -> str:
    """Раздел контекста из одобренного артефакта; пустая строка, если его нет или он не одобрен."""
    art = project.artifact(type_)
    return f"## {title}\n{art.body}" if art and art.status == "approved" else ""


# --- novelty --------------------------------------------------------------------------------
def stage_novelty(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    seed = str(payload.get("input") or "").strip()
    prev = project.artifact("novelty")
    corpus = project.artifact("corpus")
    # только текст автора, не модели: его абзац, прежний ввод или тема, с которой начат сбор литературы
    source = seed or str((prev.meta.get("source") if prev else None) or "") or str(
        (corpus.meta.get("source") if corpus and corpus.status == "approved" else None) or "")
    if not source:
        raise StageError("нужен абзац новизны от автора (payload.input)")
    base = f"{project.context_header(with_registry=False)}\n\n## Формулировка автора\n{source}"
    review = _approved(project, "literature", "Одобренный обзор литературы")
    if review:  # шаблон explore: литература раньше новизны — новизна выводится из пробелов обзора
        base += (f"\n\n{review}\n\nФормулировки новизны выведи из раздела «Пробелы» и оценки новизны обзора; "
                 "опирайся только на работы обзора, ссылки — в форме [@ключ]. Что должен выбрать автор (метод, "
                 "устройство, данные), помечай «[уточнить: …]». Без вступлений и обращений к автору — только текст.")
    final, provider = _author_critic_author(project, "ideator", base, task)
    project.write_artifact("novelty", final.text, _created_by("ideator", provider, final), source=source)
    return _review_loop(project, "novelty", task, _reviser(project, "novelty", "ideator", base, task))


# --- literature -----------------------------------------------------------------------------
QUERIES_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
                  "required": ["queries"]}


def _library_records(lib: Library, queries: list[str]) -> str:
    """Записи по FTS5-запросам, одна строка на ключ; заглушки PDF без метаданных модели не показываются."""
    citable = lib.citable_keys()
    found: dict[str, dict[str, Any]] = {}
    for q in queries:
        for hit in lib.search(q, 8):
            if hit["key"] in citable:
                found.setdefault(hit["key"], hit)
    if not found:
        return "(по этим запросам в библиотеке ничего нет — напиши, какие работы нужно импортировать)"
    return "\n".join(f"- [@{k}] {h['title']} ({h['year']}; {h['authors']}) — {h['snippet']}" for k, h in found.items())


def stage_literature(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    """literature_scout: запросы → поиск в библиотеке (и OpenAlex на подтверждение) → обзор только по ключам refs.bib."""
    novelty = project.artifact("novelty")
    corpus = project.artifact("corpus")
    origin = novelty if novelty is not None else corpus if corpus and corpus.status == "approved" else None
    if origin is None:
        raise StageError("нет артефакта novelty (или одобренного корпуса collect)")
    # шаблон explore: новизны ещё нет — обзор по теме собранного корпуса
    subject = f"## Новизна\n{origin.body}" if origin is novelty else f"## Тема исследования\n{origin.meta.get('source') or ''}"
    lib = project.library()
    hint = str(payload.get("input") or "").strip()
    base = f"{project.context_header(with_registry=False)}\n\n{subject}"
    if hint:
        base += f"\n\n## Указания автора\n{hint}"
    comp, _ = call_role(project, "literature_scout", f"{base}\n\nСформулируй 3–6 коротких поисковых запросов "
                        "(2–5 слов, на языке литературы по теме) для поиска близких работ. Ответ — JSON {\"queries\": [...]}.",
                        task, 600, json_schema=QUERIES_SCHEMA)
    queries = [str(q).strip() for q in (comp.data or {}).get("queries", []) if str(q).strip()][:6]
    queries = queries or [str(origin.meta.get("source") or origin.body)[:120]]
    settings = project.ws.config().get("library") or {}
    # своя библиотека уже собрана стадией collect — предложения OpenAlex только для общей
    suggested = 0 if corpus is not None else sum(lib.fetch_openalex(q, project.slug, settings) for q in queries[:3])
    ask = (f"{base}\n\n## Записи библиотеки — цитировать только их, строго в форме [@ключ]\n{_library_records(lib, queries)}"
           "\n\nНапиши обзор литературы по шаблону системного промпта. Работы, которых нет в списке, не цитируй: "
           "в разделе «Пробелы» опиши недостающие темой и поисковым запросом, без авторов, годов и названий.")
    final, provider = _author_critic_author(project, "literature_scout", ask, task, 5000)
    project.write_artifact("literature", final.text, _created_by("literature_scout", provider, final))
    result = _review_loop(project, "literature", task, _reviser(project, "literature", "literature_scout", ask, task, 5000))
    return {**result, "queries": queries, "openalex_suggested": suggested}


# --- experiment_plan ------------------------------------------------------------------------
def stage_experiment_plan(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    """experiment_planner: на каждое утверждение новизны — эксперимент, метрика, базовые линии, ключ реестра."""
    novelty = project.artifact("novelty")
    if novelty is None:
        raise StageError("нет артефакта novelty")
    parts = [project.context_header(with_registry=True), f"## Новизна\n{novelty.body}",
             _approved(project, "literature", "Обзор литературы")]
    hint = str(payload.get("input") or "").strip()
    if hint:
        parts.append(f"## Указания автора (ресурсы, данные, ограничения)\n{hint}")
    base = "\n\n".join(p for p in parts if p)
    final, provider = _author_critic_author(project, "experiment_planner", base, task, 5000)
    project.write_artifact("experiment_plan", final.text, _created_by("experiment_planner", provider, final))
    return _review_loop(project, "experiment_plan", task,
                        _reviser(project, "experiment_plan", "experiment_planner", base, task, 5000))


# --- draft ----------------------------------------------------------------------------------
_SECTION = re.compile(r"^##\s*(.+?)\s*$.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


def merge_section(body: str, section: str, text: str, order: list[str]) -> str:
    """Заменить раздел `## section` или вставить его по порядку журнала."""
    text = text.strip()
    first = text.splitlines()[0] if text else ""
    if not re.match(rf"^##\s*{re.escape(section)}\s*$", first, re.IGNORECASE):
        text = f"## {section}\n\n{text}"
    if not text.split("\n", 1)[1:] or not text.split("\n", 1)[1].strip():  # пустой раздел не затирает написанный
        raise StageError(f"раздел «{section}» пуст: модель не вернула текста, черновик не изменён")
    parts = {m.group(1).strip(): m.group(0).rstrip() for m in _SECTION.finditer(body)}
    parts = {k if k.lower() != section.lower() else section: v for k, v in parts.items()}
    parts[section] = text
    ordered = [parts[s] for s in order if s in parts] + [v for k, v in parts.items() if k not in order]
    return "\n\n".join(ordered) + "\n"


def _draft_context(project: Project) -> str:
    parts = [project.context_header(with_registry=True)]
    parts += [sec for t in ("novelty", "literature", "experiment_plan")
              if (sec := _approved(project, t, f"Одобренный артефакт «{t}»"))]
    style = project.style()
    if style["samples"]:
        parts.append("## Образцы стиля автора\n" + "\n---\n".join(style["samples"]))
    if style["banned"]:
        parts.append("## Запрещённые фразы\n" + "\n".join(f"- {b}" for b in style["banned"]))
    return "\n\n".join(parts)


def _edit(project: Project, ctx: str, text: str, task: str | None) -> str:
    """Проход редактора по разделу, если роль editor назначена; иначе текст без изменений."""
    if not project.role_provider("editor"):
        return text
    edited, _ = call_role(project, "editor", f"{ctx}\n\n## Раздел для правки\n{text}", task, 6000)
    return edited.text if edited.text.lstrip().startswith("## ") else text  # формат сломан — не берём


def stage_draft(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    section = str(payload.get("section") or "").strip()
    sections = project.sections()
    if section not in sections:
        raise StageError(f"раздел «{section}» не из профиля журнала: {', '.join(sections)}")
    ctx = _draft_context(project)
    prev = project.artifact("draft")
    body_prev = prev.body if prev else ""
    ask = f"{ctx}\n\n## Уже написанные разделы\n{body_prev or '(пусто)'}\n\nНапиши раздел «{section}»."
    comp, provider = call_role(project, "section_writer", ask, task, 6000)
    text = _edit(project, ctx, comp.text, task)
    project.write_artifact("draft", merge_section(body_prev, section, text, sections),
                           _created_by("section_writer", provider, comp))

    def revise(blockers: list[dict[str, Any]]) -> None:
        cur = project.artifact("draft")
        text = cur.body if cur else ""
        fixed, p = call_role(project, "section_writer", f"{ctx}\n\n## Текущий черновик\n{text}\n\n"
                             f"## Блокеры проверки\n{_blockers_text(blockers)}\n\nИсправь блокеры в разделе «{section}»"
                             " и верни только этот раздел целиком.", task, 6000)
        project.write_artifact("draft", merge_section(text, section, fixed.text, sections),
                               _created_by("section_writer", p, fixed))

    return _review_loop(project, "draft", task, revise)


# --- experiments ----------------------------------------------------------------------------
def stage_experiments(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    """Без модели: запуск сценария манифеста и запись чисел в реестр; конфликт — остановка с вопросом автору."""
    try:
        res = run_scenario(project, str(payload.get("scenario") or ""), force=bool(payload.get("force")))
    except ExperimentError as e:
        raise StageError(str(e)) from e
    return {"accepted": True, "blockers": [], **res}


# --- review, assembly ----------------------------------------------------------------------
def stage_review(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    """Проверка целиком: по умолчанию draft; payload.artifact — перепроверить любой артефакт без перегенерации."""
    type_ = str(payload.get("artifact") or "draft")
    if project.artifact(type_) is None:
        raise StageError(f"артефакта {type_} нет")
    try:
        return review_artifact(project, type_, task)
    except ReviewError as e:
        raise StageError(str(e)) from e


def stage_assembly(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    draft = project.artifact("draft")
    if draft is None or draft.status != "approved":
        raise StageError("черновик не одобрен")
    title = project.manifest.get("title") or project.slug
    paper = f"# {title}\n\n{draft.body}"
    art = project.write_artifact("paper", paper, {"role": "assembly", "provider": "code", "family": "code"},
                                 status="approved")
    (project.path / "paper.md").write_text(paper, encoding="utf-8")
    outputs = ["paper.md"]
    if shutil.which("pandoc"):
        try:
            outputs.append(export(project, "docx").name)
        except StageError as e:
            logger.warning("%s", e)
    return {"accepted": True, "outputs": outputs, "version": art.version}


def export(project: Project, to: str = "docx") -> Path:
    """paper.md → paper.<to> через внешний pandoc; ссылки [@ключ] разворачиваются по refs.bib (--citeproc)."""
    src = project.path / "paper.md"
    if not src.exists():
        raise StageError("paper.md нет: сначала стадия assembly")
    if not shutil.which("pandoc"):
        raise StageError("pandoc не установлен")
    out = project.path / f"paper.{to}"
    cmd = ["pandoc", str(src), "-o", str(out)]
    bib = project.library().bib_path
    if bib.exists():
        cmd += ["--citeproc", "--bibliography", str(bib)]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode:
        raise StageError(f"pandoc → {to}: {proc.stderr.strip()[-300:]}")
    return out


def stage_collect(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    """Сбор литературы по теме: см. roles/collect.py."""
    from .collect import stage_collect as run

    return run(project, payload, task)


STAGES = {"collect": stage_collect, "novelty": stage_novelty, "literature": stage_literature, "experiment_plan": stage_experiment_plan,
          "experiments": stage_experiments, "draft": stage_draft, "review": stage_review, "assembly": stage_assembly}
NOT_YET: dict[str, str] = {}  # стадии, ещё не реализованные: id → версия


def run_stage(project: Project, stage: str, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    if stage in NOT_YET:
        raise StageError(f"стадия {stage} появится в {NOT_YET[stage]}; пока её можно пропустить с указанием причины")
    if stage not in STAGES:
        raise StageError(f"неизвестная стадия {stage}")
    return STAGES[stage](project, payload, task)


__all__ = ["NOT_YET", "STAGES", "StageError", "call_role", "export", "merge_section", "run_stage"]
