"""Командная строка `wwp`."""

from __future__ import annotations

import argparse
import logging
import sys

from .core import Workspace
from .library import LibraryError


def _fmt(value: float | None, pattern: str) -> str:
    return pattern % value if value is not None else "—"


def cmd_init(ws: Workspace, args: argparse.Namespace) -> int:
    created = ws.init()
    print(f"workspace: {ws.root}")
    for rel in created:
        print(f"  + {rel}")
    print("Дальше: заполните workspace/env и workspace/providers.yaml, затем `wwp serve`.")
    return 0


def cmd_serve(ws: Workspace, args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(ws), host=args.host, port=args.port)
    return 0


def cmd_bench(ws: Workspace, args: argparse.Namespace) -> int:
    from .eval import run_bench, summary
    from .providers import role_providers

    ws.load_env()
    pids = [p for p in (args.providers or "").split(",") if p] or role_providers(ws.config(), args.role)
    run = run_bench(ws, args.role, pids, n_judges=args.judges,
                    progress=lambda d, t, note: print(f"[{d}/{t}] {note}", file=sys.stderr))
    db = ws.db()
    table, rec = summary(db, args.role, run)
    db.close()
    print(f"прогон {run}; итог по роли {args.role}:")
    print(f"{'модель':28} {'задач':>5} {'ошиб':>4} {'судьи':>6} {'автор':>6} {'покрыт':>7} {'с':>7} {'$':>8}")
    for t in table:
        print(f"{t['provider']:28} {t['n']:>5} {t['errors']:>4} {_fmt(t['judge'], '%.1f'):>6} "
              f"{_fmt(t['human'], '%.1f'):>6} {_fmt(t['coverage'] and t['coverage'] * 100, '%.0f%%'):>7} "
              f"{_fmt(t['duration_ms'] and t['duration_ms'] / 1000, '%.1f'):>7} {_fmt(t['cost_usd'], '%.4f'):>8}")
    print(f"рекомендация: {rec or 'нет оценок'}")
    return 0


def cmd_new(ws: Workspace, args: argparse.Namespace) -> int:
    from .core import Project

    project = Project.create(ws, args.slug, template=args.template, path=args.path, language=args.language,
                             journal=args.journal)
    project.set_title_from(args.title or "")
    print(f"проект {project.slug}: {project.path}")
    return 0


def cmd_run(ws: Workspace, args: argparse.Namespace) -> int:
    from .core.engine import enqueue, run_task

    ws.load_env()
    payload: dict[str, str] = {}
    if args.input:
        payload["input"] = args.input
    if args.section:
        payload["section"] = args.section
    if args.scenario:
        payload["scenario"] = args.scenario
    if args.force:
        payload["force"] = "1"
    if args.ab:
        payload["ab"] = args.ab
    tid = enqueue(ws, args.slug, args.stage, payload)
    res = run_task(ws, tid)
    if res["error"]:
        print(f"задача {tid}: ошибка: {res['error']}", file=sys.stderr)
        return 1
    if args.stage == "collect":
        print(f"задача {tid}: запросы: {'; '.join(res['queries'])}\nнайдено {res['found']}, в корпусе {res['selected']}, "
              f"полных текстов {res['pdf']}, не оценено моделью {res['unscored']}. Корпус ждёт решения: wwp serve → проект → corpus, "
              "одобрить или вычеркнуть лишнее.")
        return 0
    if args.stage == "experiments":
        print(f"задача {tid}: сценарий {res['scenario']} → реестр: "
              + ", ".join(f"{k}={res['values'][k]:g}" for k in res["written"]) + f"; журнал runs/{res['run_dir']}")
        return 0
    if res.get("ab"):
        ab = res["ab"]
        print(f"задача {tid}: A/B — v{ab['a']['version']} {ab['a']['provider']} (принято судьями: {res['accepted_a']}) "
              f"против v{ab['b']['version']} {ab['b']['provider']} (принято: {res['accepted_b']}); "
              f"выбор: wwp decide {args.slug} {args.stage if args.stage != 'collect' else 'corpus'} a|b")
        return 0
    print(f"задача {tid}: готово; принято судьями: {res.get('accepted')}; блокеров: {len(res.get('blockers', []))}")
    for b in res.get("blockers", []):
        print(f"  - [{b.get('judge')}] строка {b.get('line', '?')}: {b.get('issue')}")
    return 0


def cmd_decide(ws: Workspace, args: argparse.Namespace) -> int:
    from .core.project import Project

    project = Project.open(ws, args.slug)
    if args.decision in ("a", "b"):
        from .roles.ab import choose

        res = choose(project, args.artifact, args.decision)
        print(f"{args.artifact}: выбрана {res['chosen']}, текущая версия v{res['version']}")
        return 0
    if args.decision == "override" and not args.reason:
        raise ValueError("для override нужна причина (--reason)")
    art = project.decide(args.artifact, args.decision, args.reason)
    print(f"{art.type} v{art.version}: {art.status}")
    return 0


def cmd_profile(ws: Workspace, args: argparse.Namespace) -> int:
    cfg = ws.config()
    profiles = cfg.get("profiles") or {}
    if args.name not in profiles:
        raise ValueError(f"профиля {args.name} нет; есть: {', '.join(profiles) or '—'}")
    cfg["roles"] = {**(cfg.get("roles") or {}), **profiles[args.name]}
    ws.save_config(cfg)
    for role, pid in cfg["roles"].items():
        print(f"{role}: {', '.join(pid) if isinstance(pid, list) else pid}")
    return 0


def cmd_review(ws: Workspace, args: argparse.Namespace) -> int:
    from .core.engine import enqueue, run_task

    ws.load_env()
    res = run_task(ws, enqueue(ws, args.slug, "review", {"artifact": args.artifact}, gate=False))
    if res["error"]:
        print(f"ошибка: {res['error']}", file=sys.stderr)
        return 1
    print(f"{args.artifact}: принято судьями: {res.get('accepted')}; min {res.get('min_overall')} / порог "
          f"{res.get('threshold')}; блокеров {len(res.get('blockers', []))}; ошибок судей {len(res.get('errors', []))}")
    for b in res.get("blockers", []):
        print(f"  - [{b.get('judge')}] {b.get('issue')}")
    for e in res.get("errors", []):
        print(f"  ! {e}")
    return 0


def cmd_check(ws: Workspace, args: argparse.Namespace) -> int:
    from .core import Project
    from .review.checks import deterministic_checks

    project = Project.open(ws, args.slug)
    code = 0
    for type_ in args.artifacts or ("novelty", "literature", "experiment_plan", "draft", "paper"):
        art = project.artifact(type_)
        if art is None:
            continue
        blockers = deterministic_checks(project, art, ws.text(f"rubrics/{type_}.md"))
        print(f"{type_} v{art.version}: блокеров {len(blockers)}")
        for b in blockers:
            print(f"  - строка {b['line']}: {b['issue']}" + (f" — «{b['quote']}»" if b.get("quote") else ""))
        code |= bool(blockers)
    return code


def cmd_lib(ws: Workspace, args: argparse.Namespace) -> int:
    from .library import Library

    lib = Library(ws.root / "library")
    if args.lib_cmd == "import":
        for path in args.paths:
            res = lib.import_path(path)
            print(f"{path}: добавлено {', '.join(res['added']) or '—'}"
                  + (f"; пропущено {', '.join(res['skipped'])}" if res["skipped"] else "")
                  + ("" if res.get("has_text", True) else "; текст PDF не извлечён")
                  + ("; запись-заглушка, уточните через OpenAlex" if res.get("matched") is False else ""))
        return 0
    if args.lib_cmd == "search":
        hits = lib.search(" ".join(args.query))
        for h in hits:
            print(f"[@{h['key']}] {h['title']} ({h['year']})" + (f"\n    {h['snippet']}" if h["snippet"] else ""))
        return 0 if hits else 1
    print(f"записей: {lib.reindex()}")
    return 0


def cmd_export(ws: Workspace, args: argparse.Namespace) -> int:
    from .core import Project
    from .roles import StageError, export

    try:
        out = export(Project.open(ws, args.slug), args.to)
    except StageError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


def cmd_reindex(ws: Workspace, args: argparse.Namespace) -> int:
    from .core.maintenance import reindex

    added = reindex(ws)
    print("добавлено в wwp.sqlite: " + ", ".join(f"{k} {v}" for k, v in added.items()))
    return 0


def _days(text: str) -> float:
    """`90d`, `12h` или число дней."""
    unit = {"d": 1.0, "h": 1 / 24}.get(text[-1:])
    return float(text[:-1]) * unit if unit else float(text)


def cmd_gc(ws: Workspace, args: argparse.Namespace) -> int:
    from .core.maintenance import gc

    files = gc(ws, _days(args.older_than), apply=args.apply)
    for p in files:
        print(p)
    verb = "удалено" if args.apply else "к удалению (запустите с --apply)"
    print(f"{verb}: {len(files)} файлов")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wwp", description="Конвейер написания научных статей")
    parser.add_argument("--workspace", help="папка workspace (по умолчанию ./workspace или $WWP_WORKSPACE)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="создать workspace из defaults")
    serve = sub.add_parser("serve", help="веб-интерфейс")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    bench = sub.add_parser("bench", help="сравнить модели на эталонных задачах роли")
    bench.add_argument("--role", required=True)
    bench.add_argument("--providers", help="id через запятую; по умолчанию назначенные на роль")
    bench.add_argument("--judges", type=int, default=2, help="число судей других семейств")
    new = sub.add_parser("new", help="создать проект-статью")
    new.add_argument("slug")
    new.add_argument("--title", help="заголовок статьи; по умолчанию — тема из collect")
    new.add_argument("--template", default="experimental")
    new.add_argument("--journal", default="generic")
    new.add_argument("--language", default="ru")
    new.add_argument("--path", help="внешний каталог проекта")
    run = sub.add_parser("run", help="выполнить стадию проекта (синхронно, без сервера)")
    run.add_argument("slug")
    run.add_argument("stage")
    run.add_argument("--input", help="novelty: абзац новизны; collect: тема исследования")
    run.add_argument("--section", help="раздел для стадии draft")
    run.add_argument("--scenario", help="id сценария для стадии experiments (если в манифесте не один)")
    run.add_argument("--force", action="store_true", help="experiments: перезаписать расходящиеся значения реестра")
    run.add_argument("--ab", metavar="PROVIDER", help="живое A/B: выполнить стадию ещё раз этой моделью, выбор за автором")
    decide = sub.add_parser("decide", help="решение автора по артефакту: одобрить, override, вернуть, отклонить, выбор A/B")
    decide.add_argument("slug")
    decide.add_argument("artifact")
    decide.add_argument("decision", choices=("approve", "override", "return", "reject", "a", "b"))
    decide.add_argument("--reason", help="причина (обязательна для override)")
    sub.add_parser("profile", help="назначить роли по профилю из providers.yaml (local-only, quality …)").add_argument("name")
    review = sub.add_parser("review", help="перепроверить артефакт судьями без перегенерации")
    review.add_argument("slug")
    review.add_argument("artifact", nargs="?", default="draft")
    check = sub.add_parser("check", help="только детерминированные проверки артефактов")
    check.add_argument("slug")
    check.add_argument("artifacts", nargs="*")
    lib = sub.add_parser("lib", help="библиотека: импорт .bib/.pdf, поиск, переиндексация")
    lib_sub = lib.add_subparsers(dest="lib_cmd", required=True)
    lib_sub.add_parser("import", help="импортировать файлы .bib и .pdf").add_argument("paths", nargs="+")
    lib_sub.add_parser("search", help="полнотекстовый поиск").add_argument("query", nargs="+")
    lib_sub.add_parser("reindex", help="пересобрать index.sqlite из refs.bib и pdf/")
    export = sub.add_parser("export", help="paper.md → docx или pdf через pandoc")
    export.add_argument("slug")
    export.add_argument("--to", choices=("docx", "pdf"), default="docx")
    sub.add_parser("reindex", help="добавить в wwp.sqlite проекты, вызовы и решения из файлов")
    gc = sub.add_parser("gc", help="удалить старые записи вызовов и неодобренные версии из истории")
    gc.add_argument("--older-than", default="90d", help="срок: 90d, 12h или число дней (по умолчанию 90d)")
    gc.add_argument("--apply", action="store_true", help="удалить; без флага только показать список")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ws = Workspace(args.workspace)
    handlers = {"init": cmd_init, "serve": cmd_serve, "bench": cmd_bench, "new": cmd_new, "run": cmd_run,
                "check": cmd_check, "review": cmd_review, "decide": cmd_decide, "profile": cmd_profile, "lib": cmd_lib, "export": cmd_export,
                "reindex": cmd_reindex, "gc": cmd_gc}
    try:
        return handlers[args.cmd](ws, args)
    except (FileNotFoundError, FileExistsError, ValueError, LibraryError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
