"""Produce assessment evidence: offline regression, deterministic release, optional real inference.

python scripts/assess.py --live --scope code
Reports always distinguish model substitutes from live inference and untested capabilities.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.pack import build_archive, digest, iter_files, source_info


def run_step(name, command, folder, timeout):
    print(f"[{name}] running", flush=True)
    started = time.monotonic()
    log = folder / (name + ".log")
    with log.open("w", encoding="utf-8") as stream:
        try:
            result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=timeout)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
            stream.write("\nAssessment step exceeded its time limit.\n")
    print(f"[{name}] {'PASS' if code == 0 else 'FAIL'} ({time.monotonic() - started:.1f}s)", flush=True)
    return {"ok": code == 0, "exit_code": code,
            "elapsed_sec": round(time.monotonic() - started, 3), "log": log.name}


def public_live_result(report):
    # Allowlist summary fields; prompts, source snippets, runtime paths and tokens stay local.
    calls = [item for item in report.get("code_records", []) if item.get("kind") == "model"]
    result = {key: report[key] for key in ("ok", "mode", "scope", "checks", "not_tested", "elapsed_sec")
              if key in report}
    result["models"] = report.get("doctor", {}).get("models", {})
    result["gates"] = [{key: gate[key] for key in ("gate", "ok", "round") if key in gate}
                       for gate in report.get("gates", [])]
    result["usage"] = {"recorded_calls": len(calls),
                       "prompt_tokens": sum(item.get("prompt_eval_count") or 0 for item in calls),
                       "output_tokens": sum(item.get("eval_count") or 0 for item in calls),
                       "missing_usage_calls": sum(item.get("eval_count") is None for item in calls)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Also call installed local models")
    parser.add_argument("--scope", choices=("code", "memory", "full"), default="code")
    parser.add_argument("--vision-image", type=Path)
    parser.add_argument("--output", type=Path, help="New output directory; existing folders are not overwritten")
    args = parser.parse_args()
    if args.scope == "full" and args.live and not args.vision_image:
        parser.error("--live --scope full requires --vision-image")
    if args.vision_image and (not args.live or args.scope != "full"):
        parser.error("--vision-image requires --live --scope full")
    out = (args.output or ROOT / ".runtime" / "assessment" / uuid.uuid4().hex[:10]).resolve()
    out.mkdir(parents=True)
    publish = out / "publish"
    publish.mkdir()
    packages = {}
    for name in ("fastapi", "uvicorn", "pydantic", "requests", "httpx", "numpy", "faiss-cpu", "pillow"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    source = {rel: digest(path.read_bytes()) for path, rel in iter_files(ROOT)}
    report = {"schema": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
              "source": source_info(ROOT), "source_files": source,
              "environment": {"python": platform.python_version(), "system": platform.system(),
                              "processor": platform.processor(),
                              "machine": platform.machine(), "packages": packages},
              "scope": args.scope, "live_requested": args.live, "steps": {},
              "theory_learning": "reference material supplied; learner completion is not attested",
              "publication": "local artifacts only; platform upload is a separate step"}
    try:
        report["steps"]["offline"] = run_step("offline", [
            sys.executable, "scripts/selftest.py", "--core", "--json", str(publish / "offline.json")],
            out, 600)
        if args.live:
            command = [sys.executable, "scripts/integration_e2e.py", "--scope", args.scope,
                       "--output", str(out / "live")]
            if args.vision_image:
                command += ["--vision-image", str(args.vision_image.resolve())]
            # E2E bounds each order and cleans up its own service in finally.
            report["steps"]["live"] = run_step("live", command, out, 2400)
            path = out / "live" / "report.json"
            if path.exists():
                report["live"] = public_live_result(json.loads(path.read_text(encoding="utf-8")))
            else:
                report["steps"]["live"]["ok"] = False
        archive = publish / "Anvil-assessment.zip"
        package = build_archive(ROOT, archive)
        repeat = build_archive(ROOT, out / "repeat.zip")
        report["steps"]["release"] = {"ok": package["sha256"] == repeat["sha256"],
                                       "sha256": package["sha256"], "files": package["files"]}
        report["source_unchanged_during_assessment"] = source == {
            rel: digest(path.read_bytes()) for path, rel in iter_files(ROOT)}
        report["ok"] = (all(step["ok"] for step in report["steps"].values())
                        and report["source_unchanged_during_assessment"])
    except Exception as exc:
        report["ok"] = False
        # Do not publish arbitrary exception messages which may contain local data.
        report["error_type"] = type(exc).__name__
        print(f"Assessment failed: {exc}", file=sys.stderr)
    (publish / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    rows = [f"| {name} | {'PASS' if step['ok'] else 'FAIL'} |" for name, step in report["steps"].items()]
    (publish / "assessment.md").write_text(
        "# Anvil 考核验收\n\n"
        f"生成时间（UTC）：{report['started_utc']}。整体结果：{'PASS' if report['ok'] else 'FAIL'}。\n\n"
        "| 检查 | 结果 |\n|---|---|\n" + "\n".join(rows) + "\n\n"
        + (f"真实模型验收范围：`{args.scope}`。\n\n" if args.live else "本次仅离线验收，未进行真实模型推理。\n\n")
        + "离线结果使用模型替身，不代表模型效果；真实结果也仅覆盖报告中的固定任务。"
        "理论资料不证明个人学习已完成；本地产物不证明已发布到平台。"
        "完整环境、源码哈希、模型摘要与用量见 summary.json。\n", encoding="utf-8")
    print("Reviewable artifacts: " + str(publish), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
