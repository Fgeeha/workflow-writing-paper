"""Маршруты v0.3: библиотека — импорт PDF/BibTeX, поиск FTS5, предложения OpenAlex на подтверждение."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile  # request.form() отдаёт класс Starlette, не FastAPI

from ..core import Workspace
from ..library import Library, LibraryError

Render = Callable[..., HTMLResponse]


def register_library(app: FastAPI, ws: Workspace, render: Render) -> None:
    lib = Library(ws.root / "library")

    @app.get("/library")
    def library_page(request: Request, q: str = "") -> HTMLResponse:
        return render(request, "library.html", records=lib.records(), q=q, hits=lib.search(q) if q else [],
                      suggestions=lib.suggestions(), bib=lib.bib_path)

    @app.post("/library/import")
    async def library_import(request: Request) -> RedirectResponse:
        form = await request.form()
        files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        # ponytail: путь на сервере читается как есть — приложение для одного пользователя на localhost;
        # при выносе в сеть ограничить корнем из providers.yaml.
        path = str(form.get("path") or "").strip()
        if not files and not path:
            raise HTTPException(400, "выберите файлы .bib/.pdf или укажите путь")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for f in files:
                    dst = Path(tmp) / Path(str(f.filename)).name
                    dst.write_bytes(await f.read())
                    await run_in_threadpool(lib.import_path, dst)  # pdftotext блокирует — не в event loop
            if path:
                p = Path(path).expanduser()
                for item in (sorted(p.glob("*.bib")) + sorted(p.glob("*.pdf"))) if p.is_dir() else [p]:
                    await run_in_threadpool(lib.import_path, item)
        except (LibraryError, FileNotFoundError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        return RedirectResponse("/library", status_code=303)

    @app.post("/library/openalex")
    async def library_openalex(request: Request) -> RedirectResponse:
        query = str((await request.form()).get("q") or "").strip()
        if not query:
            raise HTTPException(400, "пустой запрос")
        settings = {**(ws.config().get("library") or {}), "openalex": True}  # явный запрос автора — ищем всегда
        await run_in_threadpool(lib.fetch_openalex, query, None, settings, 10)
        return RedirectResponse("/library", status_code=303)

    @app.post("/library/suggestions/{sid}/{action}")
    def library_suggestion(sid: int, action: str) -> RedirectResponse:
        if action not in ("accept", "reject"):
            raise HTTPException(400, "accept | reject")
        try:
            lib.resolve_suggestion(sid, action == "accept")
        except LibraryError as e:
            raise HTTPException(404, str(e)) from e
        return RedirectResponse("/library", status_code=303)

    @app.post("/library/remove/{key}")
    def library_remove(key: str) -> RedirectResponse:
        try:
            lib.remove(key)
        except LibraryError as e:
            raise HTTPException(404, str(e)) from e
        return RedirectResponse("/library", status_code=303)

    @app.post("/library/reindex")
    def library_reindex() -> RedirectResponse:
        lib.reindex()
        return RedirectResponse("/library", status_code=303)
