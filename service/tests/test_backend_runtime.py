"""后端生命周期、并发、提交与订阅回归；无真实模型请求。"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.paths import ensure_core_on_path  # noqa: E402

ensure_core_on_path()
import engine  # noqa: E402
import tidy  # noqa: E402
import tools  # noqa: E402
from commit import commit_verified  # noqa: E402
from contract import WorkOrder  # noqa: E402
from fake_adapter import NEW_AVG_GOOD, OLD_AVG, ScriptedAdapter, make_sandbox, nc, tc  # noqa: E402
from fake_adapter import patch as code_patch  # noqa: E402

from integration import memory  # noqa: E402
from service.events import EventBuffer  # noqa: E402
from service.runner import (  # noqa: E402
    CancellableAdapter,
    Cancelled,
    RegistryBusy,
    RunnerOpts,
    RunnerRegistry,
    WorkOrderRunner,
)


class BackendRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for ctx in (
            patch.dict(os.environ, {"ANVIL_HR": "0", "ANVIL_INTEGRATION_LOG_DIR": str(self.root / "logs")}),
            patch("core.paths.RUNTIME_DIR", str(self.root / "runtime")),
        ):
            ctx.start()
            self.addCleanup(ctx.stop)
        self.project = self.root / "project"
        make_sandbox(str(self.project))

    def runner(self, **kwargs):
        opts = RunnerOpts(workdir=str(self.project), target="calc.py", test_path="test_calc.py",
                          task="实现平均值", **kwargs)
        return WorkOrderRunner(opts, ScriptedAdapter([nc("已读"), tc(code_patch(OLD_AVG, NEW_AVG_GOOD))]))

    def test_cancel_during_last_gate_prevents_auto_commit(self):
        entered, release = threading.Event(), threading.Event()
        original = engine.run_gate

        def gate(name, *args, **kwargs):
            result = original(name, *args, **kwargs)
            if name == "gate_test":
                entered.set()
                self.assertTrue(release.wait(15))
            return result

        runner = self.runner(require_confirm=False)
        with patch.object(engine, "run_gate", side_effect=gate):
            runner.start()
            try:
                self.assertTrue(entered.wait(20))
                self.assertTrue(runner.cancel())
            finally:
                release.set()
                runner.join(20)
        self.assertEqual(runner.status, "cancelled", runner.error)
        self.assertIn("NotImplementedError", (self.project / "calc.py").read_text(encoding="utf-8"))

    def test_cancel_during_last_model_call_discards_tool_result(self):
        flag = threading.Event()
        inner = Mock()
        inner.fill_slot.side_effect = lambda *a, **k: (flag.set(), tc(code_patch(OLD_AVG, NEW_AVG_GOOD)))[1]
        wrapped = CancellableAdapter(inner, flag)
        with self.assertRaises(Cancelled):
            wrapped.fill_slot("edit", [], {})

    def test_ask_shares_budget_timeout_and_trace(self):
        from integration.config import settings
        cfg = copy.deepcopy(settings())
        cfg["limits"]["model_calls"] = 1
        inner = SimpleNamespace(ask=Mock(return_value=nc("计划")))
        wrapped = CancellableAdapter(inner, threading.Event())
        with patch("integration.config.settings", return_value=cfg):
            wrapped.ask([{"role": "user", "content": "整理"}])
            with self.assertRaisesRegex(RuntimeError, "预算"):
                wrapped.ask([])
        self.assertEqual(inner.ask.call_args.kwargs["timeout"], cfg["limits"]["model_timeout"])
        records = [json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "logs").glob("*.json")]
        self.assertTrue(any(r["kind"] == "context" and r["node"] == "plan" for r in records))

    def test_cancel_after_commit_reports_false(self):
        runner = self.runner()
        runner.status = "done"
        self.assertFalse(runner.cancel())
        self.assertFalse(runner._cancel.is_set())

    def test_registry_rejects_excess_without_starting_thread(self):
        reg = RunnerRegistry(max_active=1, max_retained=2)
        first = self.runner()
        reg.add(first)
        with patch.object(WorkOrderRunner, "start") as start:
            with self.assertRaises(RegistryBusy):
                reg.create(first.opts)
        start.assert_not_called()
        self.assertEqual(len(reg.all()), 1)

    def test_registry_retains_unconfirmed_and_evicts_completed(self):
        reg = RunnerRegistry(max_active=1, max_retained=2)
        first, second, third = self.runner(), self.runner(), self.runner()
        first.status = second.status = third.status = "awaiting_confirm"
        reg.add(first)
        reg.add(second)
        with self.assertRaises(RegistryBusy):
            reg.add(third)
        first.status = "done"
        reg.add(third)
        self.assertIsNone(reg.get(first.wo_id))
        self.assertIs(reg.get(second.wo_id), second)

    def test_registry_shutdown_stops_accepting_and_can_reopen(self):
        reg = RunnerRegistry()
        self.assertEqual(reg.shutdown(), 0)
        with self.assertRaises(RegistryBusy):
            reg.create(self.runner().opts)
        reg.open()
        reg.add(self.runner())

    def test_event_consumers_are_independent_and_bounded(self):
        events = EventBuffer(capacity=3)
        for i in range(6):
            events.put({"kind": "progress", "n": i})
        first, gap = events.read_after(0)
        self.assertTrue(gap)
        second, _ = events.read_after(0)
        self.assertEqual(first, second)
        self.assertEqual([e["seq"] for e in first], [4, 5, 6])
        first[0]["n"] = "mutated"
        resumed, gap = events.read_after(5)
        self.assertFalse(gap)
        self.assertEqual(resumed[0]["n"], 5)

    def test_sse_two_clients_resume_and_validation(self):
        from fastapi.testclient import TestClient

        from service import app as svc
        reg = RunnerRegistry()
        runner = self.runner()
        runner.status = "done"
        runner.emit("progress", message="one")
        runner.emit("status", status="done")
        reg.add(runner)
        with TestClient(svc.build_app(registry=reg)) as client:
            headers = {"X-Local-Ide-Token": svc._TOKEN}
            url = "/wo/%s/events" % runner.wo_id
            one, two = client.get(url, headers=headers), client.get(url, headers=headers)
            self.assertEqual(one.text, two.text)
            self.assertIn("id: 1\n", one.text)
            resumed = client.get(url, headers={**headers, "Last-Event-ID": "1"})
            self.assertNotIn('"message": "one"', resumed.text)
            self.assertIn("id: 2\n", resumed.text)
            self.assertEqual(client.get(url, headers={**headers, "Last-Event-ID": "bad"}).status_code, 400)

    def test_api_backpressure_and_early_path_validation(self):
        from fastapi.testclient import TestClient

        from service import app as svc
        reg = RunnerRegistry(max_active=1)
        reg.add(self.runner())
        with TestClient(svc.build_app(registry=reg)) as client:
            headers = {"X-Local-Ide-Token": svc._TOKEN}
            req = {"task": "fix", "workdir": str(self.project), "target": "calc.py"}
            reply = client.post("/wo", json=req, headers=headers)
            self.assertEqual(reply.status_code, 429)
            self.assertEqual(reply.headers["Retry-After"], "2")
            reply = client.post("/wo", json={**req, "target": "../outside.py"}, headers=headers)
            self.assertEqual(reply.status_code, 400)
            self.assertEqual(len(reg.all()), 1)

    def test_concurrent_commits_allow_one_writer(self):
        target = self.project / "calc.py"
        baseline = tools.content_version(str(target))
        stages = [self.root / "a.py", self.root / "b.py"]
        for i, path in enumerate(stages):
            path.write_text("value = %d\n" % i)
        barrier = threading.Barrier(2)

        def commit(path):
            barrier.wait(5)
            try:
                commit_verified(str(self.project), "calc.py", path, baseline, tools.content_version(str(path)))
                return True
            except tools.VersionConflict:
                return False

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(commit, stages))
        self.assertEqual(sorted(results), [False, True])

    def test_commit_uses_validated_bytes_even_if_stage_changes(self):
        target, staged = self.project / "calc.py", self.root / "staged.py"
        staged.write_text("verified = True\n")
        replace = os.replace

        def swap(src, dst):
            staged.write_text("unverified = True\n")
            replace(src, dst)

        with patch("commit.os.replace", side_effect=swap):
            commit_verified(str(self.project), "calc.py", staged,
                            tools.content_version(str(target)), tools.content_version(str(staged)))
        self.assertEqual(target.read_text(), "verified = True\n")

    def test_cli_detects_target_deletion_even_without_supplied_baseline(self):
        target = self.project / "calc.py"
        wo = WorkOrder(wo_id="cli-delete", task="实现平均值", workdir=str(self.project),
                       target="calc.py", test_path="test_calc.py")
        sm = engine.WorkOrderStateMachine(self.runner().adapter, wo, verbose=False,
                                         staging_root=str(self.root / "stage"), runlog_dir=str(self.root / "runs"),
                                         _test_hook_before_commit=target.unlink)
        self.assertEqual(sm.run().status, "failed")
        self.assertFalse(target.exists())

    def test_commit_replace_failure_cleans_temp_file(self):
        target, staged = self.project / "calc.py", self.root / "staged.py"
        staged.write_text("verified = True\n")
        original = target.read_bytes()
        with patch("commit.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                commit_verified(str(self.project), "calc.py", staged,
                                tools.content_version(str(target)), tools.content_version(str(staged)))
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(list(self.project.glob(".anvil-commit-*")))

    def test_invalid_outbox_entry_does_not_abort_later_entries(self):
        folder = self.root / "runtime" / "memory-outbox"
        folder.mkdir(parents=True)
        (folder / "0-invalid.json").write_text("[]")
        with patch.object(memory, "start_worker"):
            memory.enqueue(str(self.project), "valid", "verified knowledge")
        hr = Mock()
        with patch.object(memory, "engine", return_value=hr):
            result = memory.flush()
        self.assertEqual(result, {"written": 1, "pending": 1})
        hr.ingest.assert_called_once()

    def test_other_project_retrieval_not_blocked_by_ingest(self):
        entered, release = threading.Event(), threading.Event()
        with patch.object(memory, "start_worker"):
            memory.enqueue(str(self.project), "slow", "slow knowledge")
        slow = Mock()
        slow.ingest.side_effect = lambda *a, **k: (entered.set(), release.wait(10))
        empty = Mock()
        empty.cloud_cache.count.return_value = 0
        with patch.object(memory, "engine", side_effect=lambda p: slow if p == str(self.project) else empty):
            thread = threading.Thread(target=memory.flush)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with ThreadPoolExecutor(1) as pool:
                    reply = pool.submit(memory.retrieve, "query", str(self.root / "other")).result(2)
                self.assertEqual(reply["key_sentences"], [])
            finally:
                release.set()
                thread.join(10)

    def test_memory_worker_stops_and_restarts(self):
        memory.stop_worker()
        self.addCleanup(memory.stop_worker)
        entered = threading.Event()
        with patch.object(memory, "flush", side_effect=lambda **kw: (entered.set(), {"pending": 0})[1]):
            memory.start_worker()
            self.assertTrue(entered.wait(5))
            self.assertFalse(memory.stop_worker())
            entered.clear()
            memory.start_worker()
            self.assertTrue(entered.wait(5))
            self.assertFalse(memory.stop_worker())

    def test_tidy_rejects_destination_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.project / "dest"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Windows 普通账户可建 junction；覆盖实际目录重解析点，避免跳过此回归。
            proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                  capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        plan = {"moves": [{"src": "calc.py", "dst": "dest/calc.py"}]}
        order = tidy.TidyOrder(wo_id="tidy-path", root=str(self.project), task="整理", target_dirs=["dest"])
        self.assertFalse(tidy.check_plan(str(self.project), plan, order).ok)
        with self.assertRaises(tools.ToolError):
            tidy.apply_plan(str(self.project), plan)
        self.assertFalse((outside / "calc.py").exists())

    def test_staging_does_not_copy_external_junction_contents(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("must remain outside")
        link = self.project / "external"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                  capture_output=True)
            self.assertEqual(proc.returncode, 0)
        wo = WorkOrder(wo_id="stage-links", task="代码", workdir=str(self.project), target="calc.py")
        sm = engine.WorkOrderStateMachine(ScriptedAdapter([]), wo, verbose=False,
                                         staging_root=str(self.root / "stage"))
        staged = Path(sm._stage())
        self.assertFalse((staged / "external").exists())
        self.assertTrue((outside / "secret.txt").is_file())

    def test_gateway_does_not_follow_redirect(self):
        from gateway import app as gateway
        hits = []

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/should-not-follow")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(gateway, "OLLAMA", "http://127.0.0.1:%d" % server.server_port):
                status, _ = gateway.get_ollama("/api/tags")
            self.assertNotEqual(status, 200)
            self.assertEqual(hits, ["/api/tags"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_model_failures_and_bad_response_are_recorded(self):
        from integration.models import invoke
        for response in ((200, []), RuntimeError("transport failed")):
            with patch("gateway.app.post_ollama", **(
                {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            )):
                with self.assertRaises(RuntimeError):
                    invoke("/api/chat", {"model": "test"}, 1)
        records = list((self.root / "logs").glob("model-*.json"))
        self.assertEqual(len(records), 2)

    def test_semantic_tidy_uses_shared_model_transport_and_validates_vectors(self):
        import semantic_tidy
        with patch("integration.models.invoke", return_value={"embeddings": [[float("nan")]]}) as invoke:
            self.assertIsNone(semantic_tidy.embed(["sample"]))
        self.assertEqual(invoke.call_args.args[0], "/api/embed")

    def test_configuration_rejects_nonfinite_timeout_and_invalid_dimension(self):
        from integration.config import settings
        original = (ROOT / "anvil.toml").read_text(encoding="utf-8")
        path = self.root / "invalid.toml"
        self.addCleanup(settings.cache_clear)
        for text in (original.replace("model_timeout = 180", "model_timeout = nan"),
                     original.replace("embedding_dim = 1024", "embedding_dim = true")):
            path.write_text(text, encoding="utf-8")
            with patch.dict(os.environ, {"ANVIL_CONFIG": str(path)}):
                settings.cache_clear()
                with self.assertRaises(ValueError):
                    settings()

    def test_unified_lifespan_closes_worker_and_registry(self):
        from fastapi.testclient import TestClient

        import anvil
        reg = RunnerRegistry()
        with patch.object(memory, "start_worker") as start, \
                patch.object(memory, "stop_worker", return_value=False) as stop:
            with TestClient(anvil.build_app(registry=reg)) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                start.assert_called_once()
            stop.assert_called_once()
            with self.assertRaises(RegistryBusy):
                reg.create(self.runner().opts)

    def test_embedding_batches_deduplicates_and_returns_independent_vectors(self):
        embedding = memory.Embedding(SimpleNamespace(embedding_dim=3, embedding_model="fake"))
        with patch.object(memory, "invoke", return_value={"embeddings": [[1, 0, 0], [0, 1, 0]]}) as invoke:
            result = embedding.embed_batch(["one", "two", "one"])
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(invoke.call_args.args[1]["input"], ["one", "two"])
            result[0][0] = 0
            self.assertEqual(result[2][0], 1)
            self.assertEqual(embedding.embed("one")[0], 1)
            embedding.embed_batch(["one", "two"])
            self.assertEqual(invoke.call_count, 1)

    def test_embedding_invalid_batch_does_not_cache_partial_result(self):
        embedding = memory.Embedding(SimpleNamespace(embedding_dim=3, embedding_model="fake"))
        with patch.object(memory, "invoke", return_value={"embeddings": [[1, 0, 0], [float("nan"), 0, 0]]}):
            with self.assertRaises(ValueError):
                embedding.embed_batch(["one", "two"])
        self.assertFalse(embedding.cache)

    def test_mounted_gateway_uses_service_token_only(self):
        from fastapi.testclient import TestClient

        import anvil
        from service import app as svc
        stale = "stale-gateway-token"
        with patch("gateway.app.accepted_tokens", return_value=[stale]):
            client = TestClient(anvil.build_app())
            response = client.get("/model/audit", headers={"X-Local-Ide-Token": stale})
            self.assertEqual(response.status_code, 401)
            response = client.get("/model/audit", headers={"X-Local-Ide-Token": svc._TOKEN})
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
