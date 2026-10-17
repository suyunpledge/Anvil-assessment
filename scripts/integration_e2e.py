"""一条命令验证原生集成：真实 HTTP + 本地模型 + 确认 + HR 入库/召回。

python scripts/integration_e2e.py [--vision-image 本地公交车图片路径]
只修改新建沙箱，退出时关闭本脚本启动的服务，保留运行记录。
"""

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
    parser.add_argument("--vision-image", default="")
    args = parser.parse_args()
    runroot = ROOT / ".runtime" / "acceptance" / uuid.uuid4().hex[:10]
    runroot.mkdir(parents=True)
    project = runroot / "project"
    make_sandbox(str(project))
    token = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://127.0.0.1:%s" % port
    environment = {
        **os.environ,
        "LOCAL_IDE_RUNTIME": str(runroot / "runtime"),
        "LOCAL_IDE_TOKEN": token,
        "LOCAL_LLM_VIA": "native",
        "ANVIL_HR": "1",
        "ANVIL_INTEGRATION_LOG_DIR": str(runroot / "records"),
        "PYTHONUNBUFFERED": "1",
    }

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url + path, data=data, headers={"Content-Type": "application/json", "X-Local-Ide-Token": token}
        )
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    def workorder(task, target="calc.py", test_path="test_calc.py", visual=False):
        result = request(
            "/wo",
            {
                "workdir": str(project),
                "target": target,
                "test_path": test_path,
                "task": task,
                "visual_tasks": visual,
            },
        )
        wo = result["wo_id"]
        deadline = time.monotonic() + 420
        last = None
        while time.monotonic() < deadline:
            state = request("/wo/" + wo)
            if state["status"] != last:
                print(wo, state["status"], flush=True)
                last = state["status"]
            if state["status"] not in ("pending", "running"):
                if state["status"] != "awaiting_confirm":
                    (runroot / "last-failure.json").write_text(
                        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    raise RuntimeError(
                        "工单 %s 状态 %s：%s；详见 last-failure.json"
                        % (wo, state["status"], state.get("error", ""))
                    )
                return wo, state
            time.sleep(1)
        request("/wo/" + wo + "/cancel", {})
        raise TimeoutError("工单超过 420 秒")

    report = {"started": time.time(), "checks": {}}
    with (runroot / "server.log").open("w", encoding="utf-8") as logfile:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "anvil.py"), "--port", str(port)],
            cwd=ROOT,
            env=environment,
            stdout=logfile,
            stderr=logfile,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    request("/health")
                    break
                except OSError:
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RuntimeError("服务未启动；查看 " + str(runroot / "server.log"))
                    time.sleep(0.25)
            doctor = request("/integrations")
            report["doctor"] = doctor
            assert doctor["ok"], doctor
            original = (project / "calc.py").read_bytes()
            wo, state = workorder(
                "在 calc.py 实现 average(nums)：空列表返回0.0，否则返回平均值。替换原函数体，不要留下旧实现。只做最小补丁。"
            )
            assert (project / "calc.py").read_bytes() == original
            assert request("/wo/" + wo + "/confirm", {})["ok"]
            assert (project / "calc.py").read_bytes() != original
            report["checks"]["code_confirm_and_gates"] = wo
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                records = request("/wo/" + wo + "/records")["items"]
                if any(item["kind"] == "memory_ingested" for item in records):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("记忆未在120秒内完成入库")
            report["checks"]["memory_ingested"] = True
            print("HR 成功入库", flush=True)
            wo2, _ = workorder(
                "在 calc.py 将 average 的文档字符串改为：返回列表的算术平均值，空列表结果固定为0.0。这段文字与旧文档不同，请逐字替换文档字符串。保持逻辑与类型注解不变，确保测试通过。参考已确认的平均值任务记录。"
            )
            records = request("/wo/" + wo2 + "/records")["items"]
            assert any(item["kind"] == "hr_retrieve" and item.get("hits") for item in records)
            assert any(
                item["kind"] == "context" and "项目记忆" in json.dumps(item, ensure_ascii=False)
                for item in records
            )
            request("/wo/" + wo2 + "/cancel", {})
            report["checks"]["memory_recalled_into_context"] = wo2
            print("HR 已召回并进入模型上下文", flush=True)
            if args.vision_image:
                # 使用独立演示目录生成源码，再仅将必要文件复制到本次沙箱。
                import shutil

                from scripts.prepare_demo import prepare

                demo = Path(prepare(args.vision_image))
                for name in ("vision_classify.py", "test_vision_classify.py"):
                    shutil.copyfile(demo / name, project / name)
                (project / "sample").mkdir()
                shutil.copyfile(args.vision_image, project / "sample" / "bus.jpg")
                wo3, _ = workorder(
                    "先在侦察阶段调用 visual_query 看 sample/bus.jpg，问题是图中是否有bus公交车（硬性要求，不看图无依据）。然后实现 vision_classify.py 的 classify_image(path)：视觉确认本演示样本含公交车后，路径存在返回'vehicle'，不存在返回'other'。保留类型注解。注意：visual_query 只是你侦察阶段的工具，交付代码中禁止 import 或调用它，用纯 Python 实现。",
                    "vision_classify.py",
                    "test_vision_classify.py",
                    True,
                )
                records = request("/wo/" + wo3 + "/records")["items"]
                assert any(item["kind"] == "visual_query" and item.get("ok") for item in records)
                assert request("/wo/" + wo3 + "/confirm", {})["ok"]
                report["checks"]["vision_workorder"] = wo3
            report["ok"] = True
        except Exception as exc:
            report["ok"] = False
            report["error"] = str(exc)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    report["elapsed_sec"] = round(time.time() - report["started"], 2)
    path = runroot / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print("报告：", path)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
