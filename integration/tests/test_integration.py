"""原生集成与真实故障回归；模型使用确定性替身，HR / FSM / API 真实执行。"""

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch as mockpatch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.paths import ensure_core_on_path

ensure_core_on_path()
from contract import WorkOrder
from engine import WorkOrderStateMachine
from fake_adapter import (
    NEW_AVG_BAD_SYNTAX,
    NEW_AVG_GOOD,
    OLD_AVG,
    ScriptedAdapter,
    make_sandbox,
    nc,
    patch,
    tc,
)

from integration import memory, vision
from integration.trace import record, scope
from service.runner import CancellableAdapter, RunnerOpts, WorkOrderRunner


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mockpatch.dict(
            os.environ, {"ANVIL_HR": "0", "ANVIL_INTEGRATION_LOG_DIR": str(self.root / "logs")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        runtime = mockpatch("core.paths.RUNTIME_DIR", str(self.root / "runtime"))
        runtime.start()
        self.addCleanup(runtime.stop)
        memory._engines.clear()
        self.project = self.root / "project"
        make_sandbox(str(self.project))

    def runner(self, script=None, **kwargs):
        runner = WorkOrderRunner(
            RunnerOpts(
                workdir=str(self.project),
                target="calc.py",
                test_path="test_calc.py",
                task="实现平均值",
                **kwargs,
            ),
            ScriptedAdapter(script or [nc("已读"), tc(patch(OLD_AVG, NEW_AVG_GOOD))]),
        )
        runner.start()
        runner.join(30)
        self.assertFalse(runner.alive)
        return runner

    def test_refusal_cannot_commit(self):
        runner = self.runner()
        self.assertEqual(runner.status, "awaiting_confirm")
        self.assertTrue(runner.cancel())
        self.assertEqual(runner.status, "cancelled")
        self.assertFalse(runner.confirm()["ok"])
        self.assertIn("NotImplementedError", (self.project / "calc.py").read_text(encoding="utf-8"))

    def test_deleted_target_cannot_be_recreated_by_confirm(self):
        runner = self.runner()
        (self.project / "calc.py").unlink()
        self.assertFalse(runner.confirm()["ok"])
        self.assertFalse((self.project / "calc.py").exists())

    def test_tampered_stage_cannot_commit(self):
        runner = self.runner()
        Path(runner._staged_target).write_text("malicious = True", encoding="utf-8")
        self.assertFalse(runner.confirm()["ok"])

    def test_gate_failure_repair_is_logged_and_reproducible(self):
        runner = self.runner(
            [nc("已读"), tc(patch(OLD_AVG, NEW_AVG_BAD_SYNTAX)), tc(patch(NEW_AVG_BAD_SYNTAX, NEW_AVG_GOOD))]
        )
        self.assertEqual(runner.status, "awaiting_confirm", runner.error)
        self.assertTrue(any(not item["ok"] for item in runner.result["gates"]))
        records = [
            json.loads(path.read_text(encoding="utf-8")) for path in (self.root / "logs").glob("*.json")
        ]
        self.assertTrue(any(r["kind"] == "context" and r.get("wo_id") == runner.wo_id for r in records))
        self.assertTrue(any(r["kind"] == "tool" and r.get("args") for r in records))

    def test_visual_permission_survives_wrapper_without_persisting(self):
        import tools
        from mythos_adapter import MythosAdapter

        from context.visual_tools import install_visual_tools

        install_visual_tools()
        ad = MythosAdapter(profile_path=str(self.root / "profile.json"))
        wrapped = CancellableAdapter(ad, threading.Event())
        wo = WorkOrder(wo_id="visual", workdir=str(self.project), target="calc.py", task="看图")
        wo.constraints["visual_tasks"] = True
        sm = WorkOrderStateMachine(wrapped, wo, verbose=False)
        sm._enable_visual_tools()
        self.assertIn("visual_query", [t["function"]["name"] for t in ad.tools_for("recon", tools.REGISTRY)])
        ad.record("recon", "read_file", True)
        other = MythosAdapter(profile_path=str(self.root / "profile.json"))
        self.assertNotIn(
            "visual_query", [t["function"]["name"] for t in other.tools_for("recon", tools.REGISTRY)]
        )
        wo2 = WorkOrder(wo_id="text", workdir=str(self.project), target="calc.py", task="代码")
        sm2 = WorkOrderStateMachine(other, wo2, verbose=False)
        from tools import ToolContext, ToolError

        with self.assertRaises(ToolError):
            sm2._execute_call(
                ToolContext(str(self.project), "calc.py"), {"name": "visual_query", "args": {}}, "recon", 1
            )

    def test_short_file_retains_memory_after_full_source_fallback(self):
        wo = WorkOrder(wo_id="context", workdir=str(self.project), target="calc.py", task="代码")
        sm = WorkOrderStateMachine(
            ScriptedAdapter(),
            wo,
            verbose=False,
            context_provider=lambda *a: {
                "text": "x" * 2000,
                "memory_text": "历史规则：空列表返回0",
                "hr_enabled": True,
            },
        )
        from tools import ToolContext

        text = sm._user_edit(ToolContext(str(self.project), "calc.py"), {"target_source": "short"}, "")
        self.assertIn("历史规则", text)

    def test_hr_real_storage_project_isolation_and_cache(self):
        import numpy as np

        def embed(path, payload, *args):
            vector = np.ones(1024).tolist()
            return ({"embeddings": [vector for _ in payload["input"]]} if path == "/api/embed"
                    else {"embedding": vector})

        with mockpatch("integration.memory.invoke", side_effect=embed) as calls:
            with memory._lock:
                first = memory.engine(str(self.project))
                first.ingest("平均值实现：空列表返回 0.0，非空用总和除以长度。")
            hits = memory.retrieve("平均值", str(self.project))
            self.assertTrue(hits["key_sentences"])
            self.assertFalse(memory.retrieve("平均值", str(self.root / "other"))["key_sentences"])
            before = calls.call_count
            memory.retrieve("平均值", str(self.project))
            self.assertEqual(before, calls.call_count)

    def test_failed_memory_outbox_is_retained_and_retried(self):
        with mockpatch("integration.memory.start_worker"):
            item = memory.enqueue(str(self.project), "wo-outbox", "已验证的项目知识")
        with mockpatch("integration.memory.engine", side_effect=RuntimeError("注入嵌入服务故障")):
            self.assertEqual(memory.flush()["pending"], 1)
        pending = self.root / "runtime" / "memory-outbox" / item
        self.assertTrue(pending.exists())
        with mockpatch("integration.memory.invoke", side_effect=lambda path, payload, *a:
                       {"embeddings": [[1.0] * 1024 for _ in payload["input"]]} if path == "/api/embed"
                       else {"embedding": [1.0] * 1024}):
            self.assertEqual(memory.flush()["written"], 1)
        self.assertFalse(pending.exists())

    def test_vision_receives_pixels_and_passes_svsg_anchor(self):
        from PIL import Image
        from svsg.l1_compiler import L1Compiler, RawDetection, StubDetector

        image = io.BytesIO()
        Image.new("RGB", (100, 100)).save(image, "PNG")
        compiler = L1Compiler(StubDetector([RawDetection("bus", (10, 10, 80, 80), 0.99, 0.01)]))
        answer = {
            "message": {
                "content": json.dumps(
                    {
                        "claims": [{"instance_id": 1, "field": "class", "value": "bus"}],
                        "final_answer": "检测到公交车。",
                    },
                    ensure_ascii=False,
                )
            }
        }
        with (
            mockpatch.object(vision, "_compiler", compiler),
            mockpatch("integration.vision.warmup"),
            mockpatch("integration.vision.chat", return_value=answer) as model,
        ):
            result = vision.analyze(image.getvalue(), "是什么物体", "sample.png")
        self.assertEqual(result["status"], "delivered", result)
        messages = model.call_args.args[0]
        self.assertTrue(messages[1]["images"][0])
        self.assertNotIn("你看不到图像", messages[0]["content"])

    def test_rejected_vision_never_becomes_successful_tool(self):
        from tools import ToolContext, ToolError

        from context.visual_tools import visual_query

        (self.project / "a.png").write_bytes(b"image")
        with mockpatch("integration.vision.analyze", return_value={"status": "rejected"}):
            with self.assertRaises(ToolError):
                visual_query(ToolContext(str(self.project), "calc.py"), "a.png", "看图")

    def test_api_unified_auth_and_console(self):
        from fastapi.testclient import TestClient

        from anvil import build_app

        with mockpatch("integration.memory.start_worker"):
            app = build_app()
        client = TestClient(app)
        for path in ("/wo", "/integrations", "/model/v1/models", "/demo/config"):
            self.assertEqual(client.get(path).status_code, 401, path)
        self.assertEqual(client.get("/console").status_code, 200)

    def test_api_explicit_rejection_never_commits(self):
        from fastapi.testclient import TestClient

        from anvil import build_app
        from service.app import _TOKEN

        runner = self.runner()
        with mockpatch("integration.memory.start_worker"):
            app = build_app()
        app.state.registry.add(runner)
        response = TestClient(app).post(
            "/wo/%s/confirm" % runner.wo_id, headers={"X-Local-Ide-Token": _TOKEN}, json={"approve": False}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(runner.status, "cancelled")
        self.assertIn("NotImplementedError", (self.project / "calc.py").read_text(encoding="utf-8"))

    def test_visual_task_cannot_finish_without_seeing_image(self):
        runner = self.runner([nc("只给文字"), nc("仍未调用"), nc("未看图")], visual_tasks=True)
        self.assertEqual(runner.status, "needs_input", runner.error)

    def test_pixel_verifier_uses_requested_regions(self):
        from svsg.l1_compiler import ImageMeta, L1Compiler, RawDetection, StubDetector

        ir = L1Compiler(StubDetector([RawDetection("bus", (10, 10, 80, 80), 0.99, 0.01)])).compile(
            b"", ImageMeta(image_id="v", width=100, height=100)
        )
        response = {"message": {"content": '{"results":[{"instance_id":1,"class":"bus","exists":true}]}'}}
        with mockpatch("integration.vision.chat", return_value=response) as model:
            results = asyncio.run(vision.PixelVerifier(b"pixels", ir).verify_batch("v", [1]))
        self.assertEqual(results[0].class_label, "bus")
        self.assertIn("images", model.call_args.args[0][1])

    def test_trace_concurrency_does_not_overwrite_records(self):
        from concurrent.futures import ThreadPoolExecutor

        def emit(index):
            with scope(wo_id="parallel"):
                record("burst", index=index)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(emit, range(30)))
        self.assertEqual(len(list((self.root / "logs").glob("burst-*.json"))), 30)

    def test_qwen_patch_schema_is_narrow_and_registry_stays_intact(self):
        import tools
        from adapter_factory import build_adapter

        adapter = build_adapter("qwen3.5", profile_path=str(self.root / "profile.json"))
        schema = next(
            item
            for item in adapter.tools_for("edit", tools.REGISTRY)
            if item["function"]["name"] == "apply_patch"
        )
        params = schema["function"]["parameters"]
        self.assertEqual(params["required"], ["path", "old_string", "new_string"])
        self.assertNotIn("start_line", params["properties"])
        self.assertIn("start_line", tools.REGISTRY["apply_patch"]["function"]["parameters"]["properties"])

    def test_context_uses_effective_window_instead_of_model_maximum(self):
        from types import SimpleNamespace

        from context.builder import build

        result = build(
            "calc.py",
            "实现平均值",
            SimpleNamespace(ctx_max=262144, ctx_for=lambda node: 4096),
            workdir=str(self.project),
        )
        self.assertEqual(result["effective_ctx"], 4096)
        self.assertLessEqual(result["limit_tokens"], 4096)

    def test_success_is_queued_only_after_confirmation(self):
        with (
            mockpatch.dict(os.environ, {"ANVIL_HR": "1"}),
            mockpatch("integration.memory.enqueue", return_value="pending.json") as enqueue,
            mockpatch("context.hr_bridge.retrieve", return_value={"text": ""}),
        ):
            runner = self.runner()
            enqueue.assert_not_called()
            self.assertTrue(runner.confirm()["ok"])
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.args[1], runner.wo_id)

    def test_unknown_adapter_fails_instead_of_leaving_running_order(self):
        runner = WorkOrderRunner(RunnerOpts(workdir=str(self.project), target="calc.py", adapter="missing"))
        runner.start()
        runner.join(5)
        self.assertEqual(runner.status, "failed")
        self.assertIn("未知适配", runner.error)

    def test_generated_profiles_are_importable(self):
        import generated_profiles

        self.assertGreater(len(generated_profiles.GENERATED), 0)

    def test_visual_evidence_flows_from_recon_into_edit(self):
        (self.project / "bus.png").write_bytes(b"pixels")
        result = {
            "status": "delivered",
            "claims": [{"field": "class", "value": "bus"}],
            "final_answer": "图片含公交车",
            "trace_id": "test",
        }
        script = [
            tc(("visual_query", {"image_path": "bus.png", "query": "图中是什么"})),
            nc("视觉侦察完成"),
            tc(patch(OLD_AVG, NEW_AVG_GOOD)),
        ]
        with mockpatch("integration.vision.analyze", return_value=result):
            runner = self.runner(script, visual_tasks=True)
        self.assertEqual(runner.status, "awaiting_confirm", runner.error)
        edit = next(call for call in runner.adapter.calls if call["node"] == "edit")
        self.assertIn("图片含公交车", edit["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
