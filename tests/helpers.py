"""Shared plumbing for tests that need a real relay on a real socket."""
import asyncio
import contextlib
import json

import httpx
import uvicorn


class Clock:
    """An injectable wall clock that only moves when a test moves it."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@contextlib.asynccontextmanager
async def serving(app):
    """Run the app on a real uvicorn server bound to an ephemeral port.

    httpx's ASGITransport drains the whole app coroutine before returning a
    response, which deadlocks against the deliberately-infinite SSE generator
    (encode/httpx#2186). A real server streams properly.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                await task  # surfaces a startup failure instead of spinning
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            yield http
    finally:
        server.should_exit = True
        await task


async def first_event(response, timeout=3.0):
    """The first `data:` payload off an SSE response. Raises on timeout."""

    async def read():
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        return None

    return await asyncio.wait_for(read(), timeout)


async def next_events(response, count, timeout=3.0):
    """The next `count` `data:` payloads off an SSE response, in one pass.

    httpx cannot iterate a response twice, so first_event works once per
    response; a test that wants several events has to take them all here.
    """

    async def read():
        events = []
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
                if len(events) == count:
                    break
        return events

    return await asyncio.wait_for(read(), timeout)


async def stream_ends(response, timeout=3.0):
    """True once the server has closed the stream rather than holding it open."""

    async def drain():
        async for _ in response.aiter_lines():
            pass
        return True

    return await asyncio.wait_for(drain(), timeout)


async def new_session(http):
    """Create a session over HTTP and return its JSON body."""
    response = await http.post("/sessions")
    assert response.status_code == 201, response.text
    return response.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}
