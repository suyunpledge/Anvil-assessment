"""Qwen Coder 默认链路与异常边界回归，不访问模型。"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.paths import ensure_core_on_path
ensure_core_on_path()
from integration.config import settings, default_adapter
from integration.models import AgentTransport
from integration.scheduler import ModelCancelled
from service.runner import RunnerOpts, build_adapter, list_presets
from service.app import CreateReq
from mythos_adapter import MythosAdapter
from mythos_core.errors import AdapterError, ContextOverflow
from mythos_core.profiles import QWEN_CODER
from test_qwen_coder_adapter import FakeTransport, REG, content_reply
from engine import WorkOrderStateMachine
from contract import WorkOrder
from fake_adapter import ScriptedAdapter, make_sandbox, nc, tc, patch as make_patch, OLD_AVG, NEW_AVG_GOOD


class AdaptationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="anvil-qwen-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"ANVIL_CONFIG": str(ROOT / "anvil.toml"), "LOCAL_LLM_VIA": "native"})
        env.start()
        self.addCleanup(env.stop)
        runtime = patch("core.paths.RUNTIME_DIR", str(self.root / "runtime"))
        runtime.start()
        self.addCleanup(runtime.stop)
        settings.cache_clear()
        self.addCleanup(settings.cache_clear)

    def adapter(self, replies=()):
        return MythosAdapter(model=QWEN_CODER.model, spec=QWEN_CODER,
                             transport=FakeTransport(replies),
                             profile_path=str(self.root / "profile.json"))

    def test_default_agrees_across_http_runner_and_presets(self):
        presets = list_presets()
        self.assertEqual(default_adapter(), "qwen-coder")
        self.assertEqual(RunnerOpts().adapter, presets["default"])
        req = CreateReq(task="test", workdir=str(self.root))
        self.assertEqual(req.adapter, presets["default"])
        selected = next(p for p in presets["profiles"] if p["key"] == presets["default"])
        adapter = build_adapter(RunnerOpts())
        self.assertEqual(adapter.model, selected["model"])
        self.assertEqual(adapter.model, "qwen2.5-coder:7b")
        self.assertIsInstance(adapter.transport, AgentTransport)

    def test_legacy_configuration_keeps_its_model_profile(self):
        original = (ROOT / "anvil.toml").read_text(encoding="utf-8")
        old = "\n".join(line for line in original.splitlines() if not line.startswith("adapter ="))
        old = old.replace('code = "qwen2.5-coder:7b"', 'code = "qwen3.5:9b"')
        config = self.root / "legacy.toml"
        config.write_text(old, encoding="utf-8")
        with patch.dict(os.environ, {"ANVIL_CONFIG": str(config)}):
            settings.cache_clear()
            self.assertEqual(RunnerOpts().adapter, "qwen3.5")
            self.assertEqual(build_adapter(RunnerOpts()).model, "qwen3.5:9b")

    def test_explicit_models_keep_separate_runtime_profiles(self):
        coder = build_adapter(RunnerOpts())
        other = build_adapter(RunnerOpts(adapter="qwen3.5"))
        custom = build_adapter(RunnerOpts(model="qwen2.5-coder:custom"))
        self.assertEqual(other.model, "qwen3.5:9b")
        self.assertEqual(len({a.profile_path for a in (coder, other, custom)}), 3)
        self.assertTrue(Path(coder.profile_path).is_relative_to(self.root))

    def test_requests_share_small_window_and_residency_policy(self):
        ad = self.adapter([content_reply("ok")] * 3)
        for node in ("recon", "edit"):
            ad.fill_slot(node, [{"role": "user", "content": "test"}], REG)
        ad.ask([{"role": "user", "content": "test"}])
        for body in ad.transport.bodies:
            self.assertEqual(body["options"]["num_ctx"], 8192)
            self.assertEqual(body["options"]["num_predict"], 2048)
            self.assertEqual(body["keep_alive"], "10m")
            self.assertNotIn("think", body)
            self.assertNotIn("format", body)

    def test_patch_schema_rejects_line_only_calls_without_mutating_registry(self):
        registry = copy.deepcopy(REG)
        params = registry["apply_patch"]["function"]["parameters"]
        params["properties"].update({"start_line": {"type": "integer"}, "new_code": {"type": "string"}})
        params["required"] = ["path"]
        reply = {"message": {"tool_calls": [{"function": {"name": "apply_patch", "arguments": {
            "path": "calc.py", "start_line": 1, "new_code": "bad"}}}]}}
        ad = self.adapter([reply])
        selected = next(tool["function"]["parameters"] for tool in ad.tools_for("edit", registry)
                        if tool["function"]["name"] == "apply_patch")
        self.assertNotIn("start_line", selected["properties"])
        self.assertIn("start_line", params["properties"])
        result = ad.fill_slot("edit", [], registry)
        self.assertEqual(result.kind, "schema_violation")
        self.assertFalse(result.calls)

    def test_overflow_is_rejected_before_inference(self):
        ad = self.adapter()
        result = ad.fill_slot("edit", [{"role": "user", "content": "中" * 9000}], REG, n=3)
        self.assertEqual(result.kind, "context_overflow")
        self.assertFalse(ad.transport.bodies)
        self.assertFalse(result.calls)

    def test_text_patch_allows_deletion_but_requires_an_anchor(self):
        for anchor, expected in (("obsolete code", "tool_call"), ("", "schema_violation")):
            reply = {"message": {"tool_calls": [{"function": {"name": "apply_patch", "arguments": {
                "path": "calc.py", "old_string": anchor, "new_string": ""}}}]}}
            result = self.adapter([reply]).fill_slot("edit", [], REG)
            self.assertEqual(result.kind, expected)
            if result.calls:
                self.assertEqual(result.calls[0]["args"]["new_string"], "")

    def test_truncated_native_call_is_never_executed_or_retried(self):
        reply = {"done_reason": "length", "message": {"tool_calls": [{"function": {
            "name": "apply_patch", "arguments": {"path": "calc.py", "old_string": "old", "new_string": "new"}}}]}}
        ad = self.adapter([reply])
        result = ad.fill_slot("edit", [], REG, n=3)
        self.assertEqual(result.kind, "exhausted")
        self.assertFalse(result.calls)
        self.assertEqual(len(ad.transport.bodies), 1)
        self.assertEqual(self.adapter([reply]).ask([]).kind, "exhausted")

    def test_health_does_not_accept_another_model_size(self):
        ad = self.adapter()
        ad.model = "qwen2.5-coder:14b"
        self.assertFalse(ad.health()["ok"])
        ad.model = "qwen2.5-coder:7b"
        self.assertTrue(ad.health()["ok"])

    def test_native_errors_use_adapter_failure_categories(self):
        body = {"model": QWEN_CODER.model, "messages": []}
        for message, error in (("model not found", AdapterError), ("context length exceeded", ContextOverflow)):
            with patch("integration.models.invoke", side_effect=RuntimeError(message)):
                with self.assertRaises(error):
                    AgentTransport().chat(body)
        with patch("integration.models.invoke", side_effect=ModelCancelled("cancelled")):
            with self.assertRaises(ModelCancelled):
                AgentTransport().chat(body)

    def test_large_recon_uses_task_source_and_prompt_matches_schema(self):
        sm = object.__new__(WorkOrderStateMachine)
        sm.ad = self.adapter()
        sm.wo = SimpleNamespace(task="实现 average", target="calc.py")
        sm._visual_ok = False
        sm.context_provider = lambda *args: {"kept": ["target_source"], "text": "def average(nums): pass"}
        with patch("engine.execute", side_effect=[{"text": "# 无关内容\n" * 2000}, {"listing": "calc.py"}]):
            prompt = sm._user_recon(None)
        self.assertIn("def average(nums)", prompt)
        self.assertNotIn("无关内容", prompt)
        self.assertNotIn("B) start_line", sm._sys_edit("calc.py", False))

    def test_failed_anchor_refreshes_source_and_recovers_within_two_turns(self):
        project = self.root / "project"
        make_sandbox(str(project))
        adapter = ScriptedAdapter([nc("ok"), tc(make_patch("missing anchor", "new")),
                                   tc(make_patch(OLD_AVG, NEW_AVG_GOOD))])
        adapter.spec = QWEN_CODER
        wo = WorkOrder(wo_id="wo-qwen-repair", task="实现 average", workdir=str(project),
                       target="calc.py", test_path="test_calc.py")
        sm = WorkOrderStateMachine(adapter, wo, staging_root=str(self.root / "stage"),
                                   runlog_dir=str(self.root / "runs"), commit=False, verbose=False)
        result = sm.run()
        self.assertEqual(result.status, "done")
        edits = adapter.calls_for("edit")
        self.assertEqual(len(edits), 2)
        retry = edits[1]["messages"]
        self.assertEqual(len(retry), 2)
        self.assertIn(OLD_AVG, retry[1]["content"])
        self.assertIn("不要重复读取文件", retry[1]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
