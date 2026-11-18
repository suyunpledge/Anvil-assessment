"""有界模型调度、前后台公平性、取消和预算回归；不调真实模型。"""

import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integration.scheduler import ModelCancelled, ModelQueueBusy, ModelScheduler, model_scope


class SchedulerTests(unittest.TestCase):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_parallel_limit_and_release_on_failure(self):
        scheduler = ModelScheduler(max_parallel=2)
        release = threading.Event()
        entered = []

        def work(index):
            with scheduler.slot(3):
                entered.append(index)
                release.wait(2)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(work, i) for i in range(6)]
            try:
                self.wait_for(lambda: scheduler.snapshot()["waiting"] == 4)
                self.assertEqual(scheduler.snapshot()["active"], 2)
                self.assertEqual(len(entered), 2)
            finally:
                release.set()
            for future in futures:
                future.result(3)
        with self.assertRaises(ValueError), scheduler.slot(1):
            raise ValueError("upstream failure")
        self.assertEqual(scheduler.snapshot()["active"], 0)

    def test_foreground_priority_preserves_fifo_and_background_fairness(self):
        scheduler = ModelScheduler(foreground_burst=2)
        order = []

        def work(name, background):
            with model_scope(background=background), scheduler.slot(3):
                order.append(name)

        with ThreadPoolExecutor(max_workers=4) as pool:
            with model_scope(background=True), scheduler.slot(3):
                futures = [pool.submit(work, "background", True)]
                self.wait_for(lambda: scheduler.snapshot()["waiting"] == 1)
                for i in range(3):
                    futures.append(pool.submit(work, "foreground-%s" % i, False))
                    self.wait_for(lambda: scheduler.snapshot()["waiting"] == i + 2)
            for future in futures:
                future.result(3)
        self.assertEqual(order, ["foreground-0", "foreground-1", "background", "foreground-2"])

    def test_queue_full_and_timeout_do_not_leak_tickets(self):
        scheduler = ModelScheduler(max_waiting=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with scheduler.slot(2):
                def wait():
                    with scheduler.slot(0.2):
                        self.fail("should not acquire")
                pending = pool.submit(wait)
                self.wait_for(lambda: scheduler.snapshot()["waiting"] == 1)
                with self.assertRaisesRegex(ModelQueueBusy, "已满"), scheduler.slot(1):
                    pass
                with self.assertRaisesRegex(ModelQueueBusy, "超时"):
                    pending.result(2)
        self.assertEqual(scheduler.snapshot()["waiting"], 0)
        self.assertEqual(scheduler.snapshot()["active"], 0)

    def test_cancellation_removes_waiter_before_inference(self):
        scheduler = ModelScheduler()
        cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with scheduler.slot(2):
                def work():
                    with model_scope(cancel=cancel), scheduler.slot(2):
                        self.fail("cancelled request must not run")
                future = pool.submit(work)
                self.wait_for(lambda: scheduler.snapshot()["waiting"] == 1)
                cancel.set()
                with self.assertRaises(ModelCancelled):
                    future.result(1)
                self.assertEqual(scheduler.snapshot()["waiting"], 0)
        with model_scope(cancel=cancel), self.assertRaises(ModelCancelled), scheduler.slot(1):
            pass

    def test_invoke_deducts_queue_time_and_records_it(self):
        from integration.models import invoke
        scheduler = ModelScheduler()
        with patch("integration.models.scheduler", return_value=scheduler), \
                patch("gateway.app.post_ollama", return_value=(200, {"message": {"content": "ok"}})) as post, \
                patch("gateway.app.audit"), patch("integration.models.record") as record, \
                ThreadPoolExecutor(max_workers=1) as pool:
            with scheduler.slot(2):
                future = pool.submit(invoke, "/api/chat", {"model": "test"}, 1)
                self.wait_for(lambda: scheduler.snapshot()["waiting"] == 1)
                time.sleep(0.03)
            self.assertEqual(future.result(2)["message"]["content"], "ok")
            self.assertLess(post.call_args.kwargs["timeout"], 0.98)
            self.assertGreater(record.call_args.kwargs["queue_sec"], 0.02)

    def test_runner_shutdown_cancels_queued_inference(self):
        from integration.models import invoke
        from service.runner import RunnerOpts, WorkOrderRunner
        scheduler = ModelScheduler()
        with tempfile.TemporaryDirectory() as root, \
                patch.dict(os.environ, {"ANVIL_INTEGRATION_LOG_DIR": root}), \
                patch("integration.models.scheduler", return_value=scheduler), \
                patch("gateway.app.post_ollama") as post, patch("gateway.app.audit"):
            runner = WorkOrderRunner(RunnerOpts(workdir=root))
            with patch.object(runner, "_run_code", side_effect=lambda: invoke("/api/chat", {}, 3)):
                with scheduler.slot(2):
                    runner.start()
                    try:
                        self.wait_for(lambda: scheduler.snapshot()["waiting"] == 1)
                        runner.request_stop()
                    finally:
                        runner.join(1)
                runner.join(2)
            self.assertEqual(runner.status, "cancelled")
            post.assert_not_called()

    def test_invalid_capacity_rejected(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ModelScheduler(max_parallel=value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
