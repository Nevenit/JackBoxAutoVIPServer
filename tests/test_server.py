import asyncio
import pytest
from extractor.server import RoomCodeServer


async def _connect(server):
    return await asyncio.open_connection("127.0.0.1", server.bound_port)


async def test_new_client_receives_current_code():
    server = RoomCodeServer(port=0)
    await server.start()
    await server.set_code("ABCD")
    reader, writer = await _connect(server)
    data = await asyncio.wait_for(reader.read(100), timeout=1)
    assert data == b"RoomCode:ABCD"
    writer.close()


async def test_set_code_broadcasts_to_connected_client():
    server = RoomCodeServer(port=0)
    await server.start()
    reader, writer = await _connect(server)
    await asyncio.sleep(0.05)
    await server.set_code("WXYZ")
    data = await asyncio.wait_for(reader.read(100), timeout=1)
    assert data == b"RoomCode:WXYZ"
    writer.close()


async def test_duplicate_code_not_resent():
    server = RoomCodeServer(port=0)
    await server.start()
    await server.set_code("ABCD")
    reader, writer = await _connect(server)
    assert await asyncio.wait_for(reader.read(100), timeout=1) == b"RoomCode:ABCD"
    await server.set_code("ABCD")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reader.read(100), timeout=0.2)
    writer.close()


async def test_disconnected_client_is_pruned():
    server = RoomCodeServer(port=0)
    await server.start()
    reader, writer = await _connect(server)
    await asyncio.sleep(0.05)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    await server.set_code("ZZZZ")  # must not raise on the dead writer
    assert server.client_count == 0
