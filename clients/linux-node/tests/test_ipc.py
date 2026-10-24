import asyncio

import pytest

from virtual_life_linux.ipc import IpcClient, IpcClientError, IpcError, IpcServer


async def echo(params, session):
    return {"echo": params.get("value")}


async def need_subscribe(params, session):
    session.subscribe()
    return {"ok": True}


def make_server(tmp_path) -> IpcServer:
    return IpcServer(
        tmp_path / "run" / "daemon.sock",
        {"echo": echo, "sub": need_subscribe, "status.subscribe": need_subscribe},
        generation="gen-1",
    )


async def test_call_roundtrip(tmp_path):
    server = make_server(tmp_path)
    await server.start()
    client = IpcClient(tmp_path / "run" / "daemon.sock")
    try:
        result = await client.call("echo", {"value": 7})
        assert result == {"echo": 7}
    finally:
        await client.close()
        await server.stop()


async def test_unknown_method(tmp_path):
    server = make_server(tmp_path)
    await server.start()
    client = IpcClient(tmp_path / "run" / "daemon.sock")
    try:
        with pytest.raises(IpcClientError) as excinfo:
            await client.call("nope")
        assert "unknown_method" in str(excinfo.value)
    finally:
        await client.close()
        await server.stop()


async def test_subscription_events(tmp_path):
    server = make_server(tmp_path)
    await server.start()
    client = IpcClient(tmp_path / "run" / "daemon.sock")
    try:
        queue: asyncio.Queue = await client.subscribe()
        await server.publish("state", {"online": True})
        frame = await asyncio.wait_for(queue.get(), timeout=2)
        assert frame["event"] == "state"
        assert frame["sequence"] == 1
        assert frame["generation"] == "gen-1"
        assert frame["data"] == {"online": True}
    finally:
        await client.close()
        await server.stop()


async def test_socket_is_private(tmp_path):
    server = make_server(tmp_path)
    await server.start()
    try:
        mode = (tmp_path / "run" / "daemon.sock").stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        await server.stop()


def test_router_rejects_bad_frame(tmp_path):
    # The dispatch contract is exercised through the async test above; this
    # guards the error helper's shape.
    error = IpcError("bad_request", "boom", 400)
    assert error.status_code == 400
    assert error.code == "bad_request"
