"""Common assets and response policy for both browser frontends."""

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SHARED_DIR = Path(__file__).with_name("shared")
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)


class BrowserHeaders:
    def __init__(
        self,
        app: ASGIApp,
        documentation_endpoints: frozenset[Callable[..., object]],
    ) -> None:
        self.app = app
        self.documentation_endpoints = documentation_endpoints

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # FastAPI's generated docs use CDN assets and inline scripts.
                # Match the registered endpoints so other pages keep the CSP,
                # including when the app is mounted under a URL prefix.
                if scope.get("endpoint") not in self.documentation_endpoints:
                    headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_headers)


def install_browser_assets(app: FastAPI) -> None:
    documentation_paths = {
        app.docs_url,
        app.redoc_url,
        app.swagger_ui_oauth2_redirect_url,
    }
    documentation_endpoints = frozenset(
        route.endpoint
        for route in app.routes
        if isinstance(route, Route) and route.path in documentation_paths
    )
    app.add_middleware(BrowserHeaders, documentation_endpoints=documentation_endpoints)
    app.mount("/shared", StaticFiles(directory=SHARED_DIR), name="shared")
