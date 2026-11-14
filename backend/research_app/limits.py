"""Bound request bodies before multipart parsing can spool them to disk."""

from starlette.responses import JSONResponse


class RequestBodyLimit:
    def __init__(self, app, upload_limit):
        self.app, self.upload_limit = app, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "PUT"}:
            return await self.app(scope, receive, send)
        limit = self.upload_limit if scope["path"] == "/api/documents" else 1024 * 1024
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await JSONResponse(
                    {"detail": "Request body exceeds allowed size"}, status_code=413
                )(scope, receive, send)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)
