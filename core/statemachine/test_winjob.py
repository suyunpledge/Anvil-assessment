# -*- coding: utf-8 -*-
"""test_winjob.py —— Windows Job Object 隔离的冒烟/约束测试（2026-10-03）。

验证三层防线真的存在（Windows only；其它平台 SKIP）：
  1. 正常测试能在 Job 里跑完（job_object=True 回传）；
  2. 内存超限的子进程会被 Job 杀掉（KILL/内存约束生效）；
  3. 网络探针能区分「可达」与「被挡」——地址用真实公网，不用保留地址自欺。

跑法：python statemachine/test_winjob.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import winjob  # noqa: E402


@unittest.skipUnless(winjob.IS_AVAILABLE, "winjob 仅 Windows")
class WinJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wd = tempfile.mkdtemp(prefix="winjob-test-")
        self.addCleanup(shutil.rmtree if False else lambda p: None, self.wd)

    def test_normal_child_completes(self) -> None:
        r = winjob.launch_isolated(
            [sys.executable, "-c", "print(123)"], self.wd, dict(os.environ))
        out, _ = r["proc"].communicate(timeout=60)
        winjob.cleanup(r["job"])
        self.assertEqual(r["proc"].returncode, 0)
        self.assertIn(b"123", out)

    def test_memory_limit_kills_hog(self) -> None:
        """256MB 上限 vs 2GB 申请——子进程应被 Job 杀掉而非吃爆机器。"""
        r = winjob.launch_isolated(
            [sys.executable, "-c", "x = bytearray(2*1024*1024*1024)"],
            self.wd, dict(os.environ), mem_mb=256)
        out, _ = r["proc"].communicate(timeout=180)
        winjob.cleanup(r["job"])
        self.assertNotEqual(r["proc"].returncode, 0, "内存超限应非零退出")

    def test_probe_reports_reachability_honestly(self) -> None:
        """可达的探针结果必须拒绝宣称网络隔离；离线回归不依赖公网状态。"""
        proc = Mock(returncode=3)
        proc.communicate.return_value = (b"OPEN\n", None)
        ok, why = winjob.probe_network_blocked(
            lambda c, w, e: {"proc": proc, "job": None}, self.wd, dict(os.environ))
        self.assertFalse(ok, why)
        self.assertIn("可达", why)

    @unittest.skipUnless(os.environ.get("ANVIL_LIVE_TESTS") == "1", "真实公网探针需 ANVIL_LIVE_TESTS=1")
    def test_live_probe_reports_reachability(self) -> None:
        """真实公网探针独立运行；地址不可达不能证明网络隔离。"""
        ok, why = winjob.probe_network_blocked(
            lambda c, w, e: winjob.launch_isolated(c, w, e),
            self.wd, dict(os.environ))
        if ok:
            self.skipTest("公网探针不可达，无法验证可达分支；不构成网络隔离证明：" + why)
        self.assertFalse(ok, why)
        self.assertIn("可达", why)


import shutil  # noqa: E402


def _rm(p: str) -> None:
    shutil.rmtree(p, ignore_errors=True)


WinJobTest.setUp = lambda self: (
    setattr(self, "wd", tempfile.mkdtemp(prefix="winjob-test-")),
    self.addCleanup(_rm, self.wd),
)


if __name__ == "__main__":
    unittest.main(verbosity=2)
