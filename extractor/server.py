"""TCP server broadcasting the current room code to connected clients.

Wire protocol (unchanged from the original C# server): each client receives the
ASCII bytes ``RoomCode:<CODE>`` (no terminator) on connect (if a code is known)
and again whenever the code changes. Binds 0.0.0.0 so LAN clients can connect.
"""
import asyncio


class RoomCodeServer:
    def __init__(self, port: int = 38469, host: str = "0.0.0.0"):
        self._port = port
        self._host = host
        self._code: str | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None

    @property
    def bound_port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    @property
    def client_count(self) -> int:
        return len(self._writers)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self._host, self._port)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        if self._code is not None:
            await self._safe_send(writer, self._code)
        try:
            while await reader.read(4096):
                pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def set_code(self, code: str) -> None:
        if code == self._code:
            return
        self._code = code
        for writer in list(self._writers):
            await self._safe_send(writer, code)

    async def _safe_send(self, writer: asyncio.StreamWriter, code: str) -> None:
        try:
            writer.write(f"RoomCode:{code}".encode())
            await writer.drain()
        except (ConnectionError, RuntimeError, OSError):
            self._writers.discard(writer)
