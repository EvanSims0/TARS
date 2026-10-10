"""The shared HTTP client: one quiet retry when the network blips.

Reads (GET) are retried once after a dropped or refused connection, or a 502/503/504 from a
service that is briefly overloaded. Anything that changes something (POST, PATCH, DELETE) is
retried only when the request never left the PC, so an email can't be sent twice. A request that
changes nothing despite its method (a token refresh, a route lookup) can opt in with
`extensions={"idempotent": True}`. Slow answers aren't retried: a second wait would leave a
long silence on a voice reply.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

READ_METHODS = {"GET", "HEAD", "OPTIONS"}
BUSY = {502, 503, 504}
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
BLIPS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)
PAUSE = 0.4


class Client(httpx.AsyncClient):
    """An AsyncClient that tries once more after a blip. It wraps `send`, not the transport, so
    proxy settings from the environment still apply."""

    def __init__(self, *args: Any, pause: float = PAUSE, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.pause = pause

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        safe = request.method in READ_METHODS or bool(request.extensions.get("idempotent"))
        try:
            response = await super().send(request, **kwargs)
        except BLIPS as e:
            if not (safe or isinstance(e, NEVER_SENT)):
                raise
            logger.info(f"{request.method} {request.url.host}: {type(e).__name__}, trying once more")
        else:
            if not (safe and response.status_code in BUSY):
                return response
            logger.info(f"{request.method} {request.url.host}: {response.status_code}, trying once more")
            await response.aclose()
        await asyncio.sleep(self.pause)
        return await super().send(request, **kwargs)


def client(timeout: float = 15, pause: float = PAUSE) -> Client:
    return Client(timeout=httpx.Timeout(timeout, connect=5), pause=pause)
