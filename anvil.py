"""统一入口：一个进程承载工单、模型网关、原生记忆和视觉。"""

import os
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def configure(port=8099):
    from integration.config import settings

    cfg = settings()
    os.environ.setdefault("LOCAL_LLM_OLLAMA", cfg["models"]["ollama"])
    os.environ.setdefault("LOCAL_LLM_GATEWAY", "http://127.0.0.1:%d/model" % port)
    os.environ["LOCAL_LLM_VIA"] = "native"


def diagnostics(scope: Literal["code", "memory", "full"] = "full"):
    import importlib.util

    from gateway.app import get_ollama
    from integration.config import settings
    from integration.scheduler import scheduler

    cfg = settings()
    code, data = get_ollama("/api/tags", timeout=3)
    if scope not in ("code", "memory", "full"):
        raise ValueError("scope must be code, memory or full")
    inventory = {m["name"]: m for m in data.get("models", [])}
    available = set(inventory)
    models = {
        key: {"name": cfg["models"][key], "ready": cfg["models"][key] in available,
              "digest": inventory.get(cfg["models"][key], {}).get("digest")}
        for key in ("code", "vision", "embedding")
    }
    weights = Path(cfg["vision"]["weights"])
    if not weights.is_absolute():
        weights = ROOT / weights
    deps = {
        name: importlib.util.find_spec(name) is not None
        for name in ("fastapi", "uvicorn", "pydantic", "requests", "httpx",
                     "numpy", "faiss", "PIL", "ultralytics")
    }
    required_models = ["code"] + (["embedding"] if scope != "code" else [])
    required_deps = ["fastapi", "uvicorn", "pydantic", "requests", "httpx"]
    if scope != "code":
        required_deps += ["numpy", "faiss"]
    if scope == "full":
        required_models.append("vision")
        required_deps += ["PIL", "ultralytics"]
    missing = ["model:" + key for key in required_models if not models[key]["ready"]]
    missing += ["dependency:" + key for key in required_deps if not deps[key]]
    if scope == "full" and not weights.is_file():
        missing.append("vision:weights")
    if code != 200:
        missing.append("ollama:unavailable")
    return {
        "ok": not missing,
        "scope": scope,
        "required_models": required_models,
        "missing": missing,
        "mode": "native",
        "inference_tested": False,
        "readiness": "resources-only",
        "ollama": code == 200,
        "models": models,
        "available_models": sorted(available) if code == 200 else None,
        "default_adapter": cfg["models"]["adapter"],
        "code_ready": code == 200 and models["code"]["ready"],
        "dependencies": deps,
        "vision_weights": weights.is_file(),
        "limits": cfg["limits"],
        "inference": scheduler().snapshot(),
        "memory": "project-scoped",
        "visual_verifier": "local-vlm (same model)",
    }


def build_app(registry=None):
    configure()
    import asyncio
    import json
    from contextlib import asynccontextmanager

    from fastapi.responses import FileResponse

    from core.paths import runtime_path
    from gateway.app import build_app as gateway_app
    from integration.memory import start_worker, stop_worker
    from integration.trace import record
    from service.app import build_app as service_app
    from service.auth import require_auth

    app = service_app(registry=registry)
    registry = app.state.registry
    app.title = "Anvil · 原生集成工作台"
    app.mount("/model", gateway_app(token=app.state.token))
    service_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(_app):
        async with service_lifespan(_app):
            start_worker()
            record("runtime", event="started", mode="native")
            try:
                yield
            finally:
                # 先请求取消工单，避免关闭期间仍有提交；模型中的线程按单次超时退出。
                remaining = await asyncio.to_thread(registry.shutdown)
                memory_busy = await asyncio.to_thread(stop_worker)
                record("runtime", event="stopped", unfinished_workers=remaining,
                       memory_worker_busy=memory_busy)

    app.router.lifespan_context = lifespan

    auth = require_auth(app.state.token, what="集成状态与记录")

    @app.get("/console", include_in_schema=False)
    def console():
        return FileResponse(ROOT / "webui" / "console.html")

    @app.get("/integrations", dependencies=[auth])
    def integrations(scope: Literal["code", "memory", "full"] = "full"):
        return diagnostics(scope)

    @app.get("/wo/{wo_id}/records", dependencies=[auth])
    def records(wo_id: str):
        from fastapi import HTTPException

        if registry.get(wo_id) is None:
            raise HTTPException(status_code=404, detail="工单不存在")
        items = []
        folder = Path(os.environ.get("ANVIL_INTEGRATION_LOG_DIR") or runtime_path("integration-log"))
        for path in folder.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(item, dict) and item.get("wo_id") == wo_id
                        and isinstance(item.get("ts"), (int, float))):
                    items.append(item)
            except (OSError, ValueError):
                continue
        return {"wo_id": wo_id, "items": sorted(items, key=lambda item: item["ts"])}

    @app.post("/memory/retry", dependencies=[auth])
    def retry_memory():
        start_worker()
        return {"ok": True, "message": "待入库记忆已安排重试"}

    @app.get("/demo/config", dependencies=[auth])
    def demo_config():
        return {"workdir": runtime_path("demo-sandbox", "integrated")}

    return app


def main():
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Anvil 原生集成工作台")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--scope", choices=("code", "memory", "full"), default="full",
                    help="doctor 的资源检查范围；code 不要求记忆或视觉资源")
    ap.add_argument("--prepare-demo", action="store_true")
    ap.add_argument("--image", default="", help="为视觉演示准备本地公交车图片")
    ap.add_argument("--memory-flush", action="store_true")
    args = ap.parse_args()
    configure(args.port)
    if args.doctor:
        result = diagnostics(args.scope)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.prepare_demo:
        from scripts.prepare_demo import prepare

        print(prepare(args.image))
        return 0
    if args.memory_flush:
        from integration.memory import flush

        result = flush()
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["pending"] else 0
    import uvicorn

    print("Anvil：http://127.0.0.1:%d/console" % args.port)
    print("令牌：.runtime/service-token；无需另起 HR / SVSG / gateway")
    uvicorn.run(build_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
