"""SSE 生成器微基准：不启动 HTTP 服务或模型；令牌与日志使用临时目录。"""

import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def measure(factory, subscribers, idle_seconds):
    from service.runner import RunnerOpts, RunnerRegistry, WorkOrderRunner

    registry = RunnerRegistry()
    runner = WorkOrderRunner(RunnerOpts())
    runner.status = "running"
    registry.add(runner)
    app = factory(registry)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/wo/{wo_id}/events")
    streams = [endpoint(runner.wo_id, last_event_id="").body_iterator for _ in range(subscribers)]
    pending = []
    published = [0]

    async def receive(stream):
        chunk = await anext(stream)
        if "id: 1\n" not in chunk:
            raise AssertionError("订阅未收到发布的事件")
        return (time.perf_counter() - published[0]) * 1000

    try:
        for stream in streams:
            await anext(stream)  # 跳过初始状态快照。
        with patch.object(runner.events, "read_after", wraps=runner.events.read_after) as read:
            pending = [asyncio.create_task(receive(stream)) for stream in streams]
            await asyncio.sleep(idle_seconds)
            idle_reads = read.call_count
            published[0] = time.perf_counter()
            runner.events.put({"kind": "progress", "message": "benchmark"})
            latencies = sorted(await asyncio.wait_for(asyncio.gather(*pending), 5))
        return {
            "subscribers": subscribers,
            "idle_seconds": idle_seconds,
            "idle_read_calls": idle_reads,
            "delivery_p50_ms": round(statistics.median(latencies), 3),
            "delivery_p95_ms": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 3),
        }
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for stream in streams:
            await stream.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-service", type=Path, help="可选：本轮改动前 service/app.py 的副本")
    parser.add_argument("--subscribers", type=int, default=64)
    parser.add_argument("--idle-seconds", type=float, default=1.05)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 1 <= args.subscribers <= 1000 or not 0 < args.idle_seconds < 15:
        parser.error("订阅数须为 1–1000，空闲时间须大于 0 且小于 15 秒")

    with tempfile.TemporaryDirectory(prefix="anvil-sse-bench-") as runtime:
        os.environ["LOCAL_IDE_RUNTIME"] = runtime
        os.environ["ANVIL_INTEGRATION_LOG_DIR"] = str(Path(runtime) / "logs")
        from service.app import build_app

        report = {"scope": "in-process SSE generator; excludes HTTP, rendering and model inference"}
        if args.baseline_service:
            spec = importlib.util.spec_from_file_location("anvil_sse_baseline", args.baseline_service.resolve())
            baseline = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = baseline
            spec.loader.exec_module(baseline)

            def old_app(registry):
                baseline.REGISTRY = registry
                return baseline.build_app()

            report["baseline"] = asyncio.run(measure(old_app, args.subscribers, args.idle_seconds))
        report["current"] = asyncio.run(measure(build_app, args.subscribers, args.idle_seconds))

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
