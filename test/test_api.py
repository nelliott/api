import a0
import aiohttp
import asyncio
import base64
import enum
import json
import os
import pytest
import shutil
import subprocess
import threading
import types

pytestmark = pytest.mark.asyncio


class RunApi:

    class State(enum.Enum):
        DEAD = 0
        CREATED = 1
        STARTED = 2

    def __init__(self):
        self._proc = subprocess.Popen(["./entrypoint.py"],
                                      env=os.environ.copy())

        ns = types.SimpleNamespace()

        ns.state = [RunApi.State.CREATED]
        ns.state_cv = threading.Condition()

        def _on_heartbeat_detected():
            with ns.state_cv:
                ns.state[0] = RunApi.State.STARTED
                ns.state_cv.notify_all()

        def _on_heartbeat_missed():
            with ns.state_cv:
                ns.state[0] = RunApi.State.DEAD
                ns.state_cv.notify_all()

        self._heartbeat_listener = a0.HeartbeatListener("api",
                                                        _on_heartbeat_detected,
                                                        _on_heartbeat_missed)

        self._state = ns.state
        self._state_cv = ns.state_cv

    def __del__(self):
        self._proc.kill()
        self._proc.wait()
        self._proc = None

    def WaitUntilStarted(self, timeout=None):
        with self._state_cv:
            return self._state_cv.wait_for(
                lambda: self._state[0] == RunApi.State.STARTED, timeout=timeout)

    async def WaitUntilStartedAsync(self, timeout=None):
        loop = asyncio.get_event_loop()
        evt = asyncio.Event()

        def unblock():
            self.WaitUntilStarted(timeout=timeout)
            loop.call_soon_threadsafe(evt.set)

        t = threading.Thread(target=unblock)
        t.start()
        await evt.wait()
        t.join()

        with self._state_cv:
            return self._state[0] == RunApi.State.STARTED

    def is_alive(self):
        return self._proc.poll() is None


@pytest.fixture()
async def sandbox():
    os.environ["A0_ROOT"] = "/dev/shm/test_ls/"
    yield RunApi()
    shutil.rmtree("/dev/shm/test_ls", ignore_errors=True)


async def test_ls(sandbox):
    await sandbox.WaitUntilStartedAsync(timeout=1.0)
    async with aiohttp.ClientSession() as session:
        async with session.get('http://localhost:24880/api/ls') as resp:
            assert resp.status == 200
            assert await resp.json() == [
                {
                    "filename": "a0_heartbeat__api",
                    "protocol": "heartbeat",
                    "container": "api",
                },
            ]

        a0.File("a0_pubsub__aaa__bbb")
        a0.File("a0_pubsub__aaa__ccc")
        a0.File("a0_rpc__bbb__ddd")

        async with session.get('http://localhost:24880/api/ls') as resp:
            assert resp.status == 200
            assert await resp.json() == [
                {
                    "filename": "a0_heartbeat__api",
                    "protocol": "heartbeat",
                    "container": "api",
                },
                {
                    "filename": "a0_pubsub__aaa__bbb",
                    "protocol": "pubsub",
                    "container": "aaa",
                    "topic": "bbb",
                },
                {
                    "filename": "a0_pubsub__aaa__ccc",
                    "protocol": "pubsub",
                    "container": "aaa",
                    "topic": "ccc",
                },
                {
                    "filename": "a0_rpc__bbb__ddd",
                    "protocol": "rpc",
                    "container": "bbb",
                    "topic": "ddd",
                },
            ]


async def test_pub(sandbox):
    await sandbox.WaitUntilStartedAsync(timeout=1.0)
    async with aiohttp.ClientSession() as session:
        endpoint = "http://localhost:24880/api/pub"
        pub_data = {
            "container": "aaa",
            "topic": "bbb",
            "packet": {
                "payload": base64.b64encode(b"Hello, World!").decode("utf-8"),
            },
        }
        async with session.post(endpoint, data=json.dumps(pub_data)) as resp:
            assert resp.status == 200
            assert await resp.text() == "success"

        pub_data["packet"]["payload"] = base64.b64encode(
            b"Goodbye, World!").decode("utf-8")
        async with session.post(endpoint, data=json.dumps(pub_data)) as resp:
            assert resp.status == 200
            assert await resp.text() == "success"

        tm = a0.TopicManager({"container": "aaa"})
        sub = a0.SubscriberSync(tm.publisher_topic("bbb"), a0.INIT_OLDEST,
                                a0.ITER_NEXT)
        msgs = []
        while sub.has_next():
            msgs.append(sub.next().payload)
        assert len(msgs) == 2
        assert msgs == [b'Hello, World!', b'Goodbye, World!']