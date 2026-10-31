"""Командная строка `wwp`."""

from __future__ import annotations

import argparse
import logging
import sys

from .core import Workspace


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
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ws = Workspace(args.workspace)
    handlers = {"init": cmd_init, "serve": cmd_serve, "bench": cmd_bench}
    try:
        return handlers[args.cmd](ws, args)
    except (FileNotFoundError, ValueError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
