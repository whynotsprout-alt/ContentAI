from __future__ import annotations

from functools import partial
from types import TracebackType
from typing import Any

import httpx
from anyio.from_thread import BlockingPortal, start_blocking_portal


class ApiClient:
    __test__ = False

    def __init__(self, app: Any) -> None:
        self.app = app
        self._lifespan_manager: Any | None = None
        self._client: httpx.AsyncClient | None = None
        self._portal_context: Any | None = None
        self._portal: BlockingPortal | None = None

    def __enter__(self) -> ApiClient:
        self._portal_context = start_blocking_portal()
        self._portal = self._portal_context.__enter__()
        self._portal.call(self._start)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._portal is not None:
            self._portal.call(self._stop, exc_type, exc, traceback)
        if self._portal_context is not None:
            self._portal_context.__exit__(exc_type, exc, traceback)

    async def _start(self) -> None:
        self._lifespan_manager = self.app.router.lifespan_context(self.app)
        await self._lifespan_manager.__aenter__()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def _stop(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._client is not None:
                await self._client.aclose()
        finally:
            if self._lifespan_manager is not None:
                await self._lifespan_manager.__aexit__(exc_type, exc, traceback)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call_request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call_request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call_request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call_request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._call_request("DELETE", url, **kwargs)

    def _call_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._portal is None:
            raise RuntimeError("TestClient must be used as a context manager.")
        return self._portal.call(partial(self._request, method, url, **kwargs))

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("TestClient must be used as a context manager.")
        return await self._client.request(method, url, **kwargs)
