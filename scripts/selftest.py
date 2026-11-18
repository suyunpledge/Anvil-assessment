#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py —— 一键跑完所有不需要模型的检查（离线，约 5 秒）。

包含：
  · gateway 单测          （熔断 / 画像 / 映射 / 路由 / 审计）
  · context 单测          （切块 / repo map / 预算 / 组装）
  · service 单测          （状态机包装 / 未确认不落盘 / 取消 / 整理）
  · 扩展静态检查          （命令注册 / 配置键 / 入口 / 转义 / 无外连）
  · 核心回归（可选 --core）——内嵌快照自带的四套测试

跑法：
    python scripts/selftest.py
    python scripts/selftest.py --core        # 加上核心回归（约 4 秒）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

SUITES = [
    ("考核资源检查与交付包", ["scripts/tests/test_assessment.py"]),
    ("模型调度与排队取消", ["integration/tests/test_scheduler.py"]),
    ("运行时隔离与事件通知", ["service/tests/test_runtime_isolation.py"]),
    ("后端并发与生命周期", ["service/tests/test_backend_runtime.py"]),
    ("原生集成与故障回归", ["integration/tests/test_integration.py"]),
    ("网关单测", ["gateway/tests/test_gateway.py"]),
    ("网关鉴权（1005 补齐）", ["gateway/tests/test_gateway_auth.py"]),
    ("上下文层单测", ["context/tests/test_context.py"]),
    ("工单服务单测", ["service/tests/test_service.py"]),
    ("安全回归（审查 7 条）", ["service/tests/test_security_fixes.py"]),
    ("自查修复（0929 审查）", ["service/tests/test_review_fixes.py"]),
    ("读口鉴权（0930 补齐）", ["service/tests/test_read_auth_2026_09_30.py"]),
    ("整理链删除全链路（同意+备份）", ["service/tests/test_delete_flow.py"]),
]

# 端到端 + 并发取消验证（需要起服务，不算\"单元\"，单独跑）
# 它们被 scripts/e2e_check.py / service/tests/cancel_concurrency.py 独立处理

CORE_SUITES = [
    ("核心·Qwen 默认链路与预算", ["integration/tests/test_qwen_adaptation.py"]),
    ("核心·状态机", ["core/statemachine/test_statemachine.py"]),
    ("核心·文件整理", ["core/statemachine/test_tidy.py"]),
    ("核心·上下文与语义整理优化", ["core/statemachine/test_optimizations.py"]),
    ("核心·Mythos 适配层", ["core/compatibility/test_mythos_adapter.py"]),
    ("核心·Qwen2.5-Coder 适配层", ["core/compatibility/test_qwen_coder_adapter.py"]),
    ("核心·GLM4 适配层", ["core/compatibility/test_glm4_adapter.py"]),
    ("核心·整理工具与删除安全", ["core/statemachine/test_ops_tools.py"]),
    ("核心·Job Object 隔离", ["core/statemachine/test_winjob.py"]),
]


def run_py(path: str) -> tuple:
    # ★ 内嵌解释器是隔离模式：明说 PYTHONPATH 没用，改成在子进程里显式补路径
    code = (
        "import sys, os, runpy;"
        "root = os.path.dirname(os.path.dirname(os.path.abspath(%r)));"
        "[sys.path.insert(0, p) for p in (root, os.path.join(root, 'service'),"
        " os.path.join(root, 'gateway'), os.path.join(root, 'context'),"
        " os.path.join(root, 'core', 'statemachine'),"
        " os.path.join(root, 'core', 'compatibility')) if p not in sys.path];"
        "runpy.run_path(%r, run_name='__main__')"
    ) % (path, path)
    # 每个套件隔离运行目录，防止回归覆盖正在演示的服务令牌、日志或记忆。
    with tempfile.TemporaryDirectory(prefix="anvil-selftest-") as runtime:
        env = {**os.environ, "LOCAL_IDE_RUNTIME": runtime,
               "ANVIL_INTEGRATION_LOG_DIR": os.path.join(runtime, "integration-log"),
               "LOCAL_LLM_AUDIT": os.path.join(runtime, "audit.jsonl")}
        p = subprocess.run([sys.executable, "-c", code],
                           cwd=_ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
    tail = ((p.stdout or "") + (p.stderr or "")).strip().split("\n")
    summary = ""
    for line in reversed(tail):
        if line.startswith(("Ran ", "OK", "FAILED", "合计")):
            summary = line.strip()
            break
    return p.returncode == 0, summary if p.returncode == 0 else "\n".join(tail[-18:])


def run_node(rel: str) -> tuple:
    js = os.path.join(_ROOT, rel)
    try:
        p = subprocess.run(["node", js], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "找不到 node（扩展静态检查需要它）"
    out = ((p.stdout or "") + (p.stderr or ""))
    summary = ""
    for line in reversed(out.strip().split("\n")):
        if line.startswith("合计"):
            summary = line.strip()
            break
    return p.returncode == 0, summary or out.strip()[:80]


def main(argv=None) -> int:
    # 离线回归必须隔离真实嵌入调用；原生链路由 integration/tests 单独验证。
    os.environ["ANVIL_HR"] = "0"
    ap = argparse.ArgumentParser(description="本地 IDE 离线自检")
    ap.add_argument("--core", action="store_true", help="一并跑核心回归")
    ap.add_argument("--backend-only", action="store_true", help="只检查后端，跳过扩展检查")
    ap.add_argument("--json", type=Path, help="保存机器可读结果（不会省略失败/跳过摘要）")
    args = ap.parse_args(argv)
    started = time.monotonic()

    suites = list(SUITES)
    if args.core:
        suites += CORE_SUITES

    print("离线自检：%s\n" % _ROOT)
    results = []
    for name, files in suites:
        for f in files:
            ok, summary = run_py(f)
            results.append((name, ok, summary))
            print("  [%s] %-22s %s" % ("PASS" if ok else "FAIL", name, summary))

    if not args.backend_only:
        ok, summary = run_node("extension/tests/check.js")
        results.append(("扩展静态检查", ok, summary))
        print("  [%s] %-22s %s" % ("PASS" if ok else "FAIL", "扩展静态检查", summary))

    bad = [r for r in results if not r[1]]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "ok": not bad, "mode": "offline-scripted-model", "core": args.core,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "suite_count": len(results), "passed_suites": len(results) - len(bad),
            "suites": [{"name": name, "ok": ok, "summary": summary}
                       for name, ok, summary in results],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n合计：%d/%d 通过%s" % (len(results) - len(bad), len(results),
                                   "  ✓" if not bad else "  ✗"))
    for name, _, summary in bad:
        print("  ✗ %s  %s" % (name, summary))
    if not bad:
        print("\n下一步：python scripts/integration_e2e.py （原生集成，需要本地模型）")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
