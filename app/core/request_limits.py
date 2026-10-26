"""
Two pieces of request hygiene that belong in front of every route.

BodySizeLimitMiddleware
    Multipart uploads are parsed, and spooled to disk, before a handler can
    check how big they are - and POST /build-a-cake/enquiries/{id}/image is open
    to anyone. Without a cap a single request could fill the disk. A declared
    Content-Length over the limit is refused before the app is called; a body
    that arrives without one is counted as it streams and cut off at the same
    point. FastAPI re-raises an HTTPException thrown while it reads the body, so
    the client gets a 413 rather than a generic parse error.

MediaHeadersMiddleware
    /media is served by StaticFiles, which picks a Content-Type from the file
    extension. Upload handlers now derive that extension from the file's own
    bytes, but files written before that fix kept whatever name the browser
    sent. `Content-Security-Policy: sandbox` makes any document served from
    there inert - no script, no same-origin access - while images display as
    before. nosniff goes on every response so no browser second-guesses a type.
"""

from fastapi import HTTPException


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                size = -1
            if size < 0 or size > self.max_bytes:
                return await self._too_large(send)

        received = 0

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        await self.app(scope, counted_receive, send)

    async def _too_large(self, send):
        body = b'{"detail":"Request body too large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


class MediaHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        is_media = scope.get("path", "").startswith("/media/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-content-type-options", b"nosniff"))
                if is_media:
                    headers.append((b"content-security-policy", b"sandbox"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
