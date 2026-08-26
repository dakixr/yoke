"""Packaged browser application routes for the Yoke HTTP daemon."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
ASSET_ROOT = WEB_ROOT / "assets"
INDEX_PATH = WEB_ROOT / "index.html"


class NoStoreStaticFiles(StaticFiles):
    """Static file server tuned for a refresh-driven local development loop."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def install_web_routes(app: FastAPI) -> None:
    """Serve the packaged no-build browser app from the API process."""
    app.mount(
        "/assets",
        NoStoreStaticFiles(directory=ASSET_ROOT),
        name="web-assets",
    )

    async def shell(_request: Request) -> FileResponse:
        return FileResponse(
            INDEX_PATH,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    app.add_api_route("/", shell, methods=["GET"], include_in_schema=False)
    app.add_api_route("/new", shell, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/session/{session_id}",
        shell,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/settings",
        shell,
        methods=["GET"],
        include_in_schema=False,
    )
