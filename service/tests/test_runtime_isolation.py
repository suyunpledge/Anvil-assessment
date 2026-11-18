"""实例隔离、事件通知和有界关闭回归；不启动模型或写入真实运行目录。"""

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from service.events import EventBuffer  # noqa: E402
from service.runner import RegistryBusy, RunnerOpts, RunnerRegistry, WorkOrderRunner  # noqa: E402


class EventNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_thread_wakes_every_subscriber(self):
        events = EventBuffer()
        pending = [asyncio.create_task(events.wait_after(0, 2)) for _ in range(20)]
        await asyncio.sleep(0)
        await asyncio.to_thread(events.put, {"kind": "progress", "message": "ready"})
        self.assertEqual(await asyncio.gather(*pending), [True] * 20)
        self.assertEqual(events.read_after(0)[0][0]["message"], "ready")
        self.assertFalse(events._waiters)

    async def test_event_between_read_and_wait_is_not_lost(self):
        events = EventBuffer()
        self.assertEqual(events.read_after(0), ([], False))
        await asyncio.to_thread(events.put, {"kind": "done"})
        self.assertTrue(await events.wait_after(0, 0.1))
        self.assertEqual(events.read_after(0)[0][0]["seq"], 1)

    async def test_timeout_and_disconnect_remove_waiters(self):
        events = EventBuffer()
        self.assertFalse(await events.wait_after(0, 0.01))
        self.assertFalse(events._waiters)
        pending = asyncio.create_task(events.wait_after(0, 30))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(events._waiters)
        events.put({"kind": "done"})

    async def test_disconnect_racing_with_publish_is_harmless(self):
        events = EventBuffer()
        pending = asyncio.create_task(events.wait_after(0, 30))
        await asyncio.sleep(0)
        events.put({"kind": "done"})
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0)
        self.assertFalse(events._waiters)
        self.assertEqual(events.read_after(0)[0][0]["seq"], 1)

    async def test_finished_worker_wakes_stream_and_allows_confirmation_replay(self):
        events = EventBuffer()
        events.put({"kind": "status", "status": "awaiting_confirm"})
        pending = asyncio.create_task(events.wait_after(1, 2))
        await asyncio.sleep(0)
        await asyncio.to_thread(events.finish)
        self.assertTrue(await pending)
        self.assertTrue(events.finished)
        events.put({"kind": "status", "status": "done"})
        self.assertEqual(events.read_after(1)[0][0]["status"], "done")

    async def test_empty_buffer_with_future_cursor_does_not_wait(self):
        events = EventBuffer()
        self.assertTrue(await events.wait_after(9, 0.1))
        self.assertEqual(events.read_after(9), ([], True))

    async def test_capacity_must_keep_at_least_one_event(self):
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                EventBuffer(capacity)


class IsolatedRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="anvil-runtime-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for context in (
            patch("core.paths.RUNTIME_DIR", str(self.root)),
            patch.dict(os.environ, {"ANVIL_HR": "0", "ANVIL_INTEGRATION_LOG_DIR": str(self.root / "log")}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def runner(self):
        return WorkOrderRunner(RunnerOpts(workdir=str(self.root), target="sample.py"))

    def test_apps_do_not_share_workorders_or_shutdown(self):
        from fastapi.testclient import TestClient

        from service.app import build_app

        first, second = build_app(), build_app()
        runner = self.runner()
        runner.status = "done"
        first.state.registry.add(runner)
        headers = {"X-Local-Ide-Token": first.state.token}
        with TestClient(second) as other:
            with TestClient(first) as client:
                self.assertEqual(client.get("/wo", headers=headers).json()["count"], 1)
                self.assertEqual(other.get("/wo", headers=headers).json()["count"], 0)
                self.assertEqual(other.get("/wo/" + runner.wo_id, headers=headers).status_code, 404)
            with self.assertRaises(RegistryBusy):
                first.state.registry.add(self.runner())
            second.state.registry.add(self.runner())
            self.assertEqual(other.get("/health").json()["wos"], 1)

    def test_unified_routes_share_the_injected_registry(self):
        from fastapi.testclient import TestClient

        import anvil
        from integration.trace import record

        registry = RunnerRegistry()
        runner = self.runner()
        registry.add(runner)
        record("example", wo_id=runner.wo_id, message="same runtime")
        app = anvil.build_app(registry=registry)
        self.assertIs(app.state.registry, registry)
        with patch("integration.memory.flush", return_value={"pending": 0, "written": 0}):
            with TestClient(app) as client:
                headers = {"X-Local-Ide-Token": app.state.token}
                self.assertEqual(client.get("/wo/" + runner.wo_id, headers=headers).status_code, 200)
                response = client.get("/wo/" + runner.wo_id + "/records", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"][0]["message"], "same runtime")

    def test_shutdown_deadline_includes_busy_commit_lock(self):
        registry = RunnerRegistry()
        runner = self.runner()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        remaining = []

        def work():
            with runner._confirm_lock:
                runner.status = "running"
                entered.set()
                release.wait(5)
                runner.status = "done"

        def shutdown():
            remaining.append(registry.shutdown(timeout=0.05))
            stopped.set()

        registry.add(runner)
        with patch.object(runner, "_run_scoped", side_effect=work):
            runner.start()
            closer = threading.Thread(target=shutdown, daemon=True)
            try:
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertTrue(stopped.wait(1), "关闭不得等待提交锁")
                self.assertEqual(remaining, [1])
                self.assertTrue(runner._cancel.is_set())
                with self.assertRaises(RegistryBusy):
                    registry.open()
            finally:
                release.set()
                runner.join(2)
                if closer.ident is not None:
                    closer.join(2)
        self.assertEqual(runner.status, "done", "已经进入的提交允许完成")
        self.assertTrue(runner.events.finished)
        registry.open()

    def test_idle_sse_waits_for_notification_and_cleans_up_on_disconnect(self):
        from service.app import build_app

        runner = self.runner()
        runner.status = "running"
        app = build_app()
        app.state.registry.add(runner)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/wo/{wo_id}/events")

        async def exercise():
            response = endpoint(runner.wo_id, last_event_id="")
            stream = response.body_iterator
            self.assertIn('"kind": "snapshot"', await anext(stream))
            with patch.object(runner.events, "read_after", wraps=runner.events.read_after) as read:
                pending = asyncio.create_task(anext(stream))
                await asyncio.sleep(0.02)
                self.assertEqual(read.call_count, 1)
                self.assertEqual(len(runner.events._waiters), 1)
                await asyncio.to_thread(runner.emit, "progress", message="notified")
                self.assertIn('"message": "notified"', await asyncio.wait_for(pending, 1))
                pending = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                await stream.aclose()
                self.assertFalse(runner.events._waiters)

        asyncio.run(exercise())

    def test_worker_completion_delivers_tail_without_waiting_for_heartbeat(self):
        from service.app import build_app

        runner = self.runner()
        runner.status = "running"
        app = build_app()
        app.state.registry.add(runner)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/wo/{wo_id}/events")

        async def exercise():
            response = endpoint(runner.wo_id, last_event_id="")
            stream = response.body_iterator
            await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            runner.emit("status", status="done")
            runner.events.finish()
            self.assertIn('"status": "done"', await asyncio.wait_for(pending, 1))
            self.assertIn("[DONE]", await asyncio.wait_for(anext(stream), 1))
            await stream.aclose()

        asyncio.run(exercise())

    def test_heartbeat_idle_timeout_and_future_cursor_remain_bounded(self):
        from service import app as service_app

        runner = self.runner()
        runner.status = "running"
        app = service_app.build_app()
        app.state.registry.add(runner)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/wo/{wo_id}/events")
        clock = [0]
        waits = []

        async def wait_after(cursor, timeout):
            self.assertEqual(cursor, 0, "空日志中的超前游标必须归零")
            self.assertEqual(timeout, 15)
            waits.append(timeout)
            clock[0] += timeout
            return False

        async def exercise():
            response = endpoint(runner.wo_id, after=100, last_event_id="")
            chunks = [chunk async for chunk in response.body_iterator]
            self.assertIn('"reset": true', chunks[1])
            self.assertEqual(sum(chunk == ": heartbeat\n\n" for chunk in chunks), 59)
            self.assertIn("event: timeout", chunks[-1])
            self.assertEqual(len(waits), 60)

        with patch.object(service_app, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(runner.events, "wait_after", side_effect=wait_after):
            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main(verbosity=2)
