"""Qwen Coder 实机验收：独立 HTTP 服务与沙箱，验证默认模型及两轮代码任务。

运行：.venv/Scripts/python scripts/qwen_acceptance.py
只确认本脚本创建的沙箱补丁；不依赖未安装的视觉/嵌入模型。
"""
import ast
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.paths import ensure_core_on_path
ensure_core_on_path()
from fake_adapter import make_sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="test an Ollama tag using its preset or the qwen-coder profile")
    args = parser.parse_args()
    runroot = ROOT / ".runtime" / "qwen-acceptance" / uuid.uuid4().hex[:10]
    project = runroot / "project"
    make_sandbox(str(project))
    token = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers={
            "Content-Type": "application/json", "X-Local-Ide-Token": token})
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    report = {"project": str(project), "cases": [], "ok": False}
    environment = {**os.environ, "ANVIL_CONFIG": str(ROOT / "anvil.toml"),
                   "LOCAL_IDE_RUNTIME": str(runroot / "runtime"), "LOCAL_IDE_TOKEN": token,
                   "ANVIL_HR": "0", "LOCAL_LLM_VIA": "native", "PYTHONUNBUFFERED": "1",
                   "ANVIL_INTEGRATION_LOG_DIR": str(runroot / "records"),
                   "LOCAL_LLM_AUDIT": str(runroot / "audit.jsonl")}
    print("report:", runroot / "report.json", flush=True)
    with (runroot / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "anvil.py"), "--port", str(port)],
                                   cwd=ROOT, env=environment, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 45
            while True:
                try:
                    request("/health")
                    break
                except OSError:
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RuntimeError("测试服务启动失败，详见 server.log")
                    time.sleep(.25)
            presets = request("/presets")
            assert presets["default"] == "qwen-coder", presets
            candidate_preset = next((p for p in presets["profiles"] if p["model"] == args.model), None)
            report["diagnostics"] = request("/integrations")
            assert report["diagnostics"]["code_ready"], report["diagnostics"]
            tasks = [
                "在 calc.py 实现 average(nums)：空列表返回 0.0，否则返回算术平均值。保留类型注解，不修改其他函数。",
                "在 calc.py 将 average 的文档字符串改为：返回列表的算术平均值，空列表结果固定为0.0。逐字替换文档字符串，保持逻辑、类型注解与其他函数不变。",
            ]
            for task in tasks:
                original = (project / "calc.py").read_bytes()
                tests_before = (project / "test_calc.py").read_bytes()
                started = time.monotonic()
                # 没有候选模型时省略 adapter/model，验证 HTTP 请求的真实默认值。
                body = {"task": task, "workdir": str(project), "target": "calc.py", "test_path": "test_calc.py"}
                if candidate_preset:
                    body["adapter"] = candidate_preset["key"]
                elif args.model:
                    body.update(adapter="qwen-coder", model=args.model)
                created = request("/wo", body)
                wo = created["wo_id"]
                deadline = time.monotonic() + 420
                while True:
                    state = request("/wo/" + wo)
                    if state["status"] not in ("pending", "running"):
                        break
                    if time.monotonic() > deadline:
                        request("/wo/" + wo + "/cancel", {})
                        raise TimeoutError("模型任务超时")
                    time.sleep(.5)
                elapsed = round(time.monotonic() - started, 3)
                case = {"task": task, "seconds_to_review": elapsed, "state": state}
                report["cases"].append(case)
                print(wo, state["status"], elapsed, "s", flush=True)
                if args.model:
                    assert state["model"] == args.model, state
                assert state["status"] == "awaiting_confirm", state
                assert (project / "calc.py").read_bytes() == original
                case["confirm"] = request("/wo/" + wo + "/confirm", {})
                assert case["confirm"]["ok"], case["confirm"]
                assert (project / "calc.py").read_bytes() != original
                assert (project / "test_calc.py").read_bytes() == tests_before
                before_tree = ast.parse(original.decode("utf-8"))
                after_tree = ast.parse((project / "calc.py").read_text(encoding="utf-8"))
                other_functions = lambda tree: {n.name: ast.dump(n) for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name != "average"}
                assert other_functions(before_tree) == other_functions(after_tree)
                if task == tasks[1]:
                    average = next(n for n in after_tree.body if isinstance(n, ast.FunctionDef) and n.name == "average")
                    assert ast.get_docstring(average) == "返回列表的算术平均值，空列表结果固定为0.0。"
                case["records"] = request("/wo/" + wo + "/records")
            tests = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=project,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
            report["final_tests"] = {"exit_code": tests.returncode, "output": tests.stdout + tests.stderr}
            assert tests.returncode == 0, report["final_tests"]
            report["ok"] = True
        finally:
            (runroot / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    print("PASS: 两轮真实模型修改、校验、确认写入和最终测试。", flush=True)


if __name__ == "__main__":
    main()
