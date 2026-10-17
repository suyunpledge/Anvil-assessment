"""统一入口：一个进程承载工单、模型网关、原生记忆和视觉。"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def configure(port=8099):
    from integration.config import settings

    cfg = settings()
    os.environ.setdefault("LOCAL_LLM_OLLAMA", cfg["models"]["ollama"])
    os.environ.setdefault("LOCAL_LLM_GATEWAY", "http://127.0.0.1:%d/model" % port)
    os.environ["LOCAL_LLM_VIA"] = "native"


def diagnostics():
    import importlib.util

    from gateway.app import get_ollama
    from integration.config import settings

    cfg = settings()
    code, data = get_ollama("/api/tags", timeout=3)
    available = {m["name"] for m in data.get("models", [])}
    models = {
        key: {"name": cfg["models"][key], "ready": cfg["models"][key] in available}
        for key in ("code", "vision", "embedding")
    }
    weights = Path(cfg["vision"]["weights"])
    if not weights.is_absolute():
        weights = ROOT / weights
    deps = {
        name: importlib.util.find_spec(name) is not None
        for name in ("fastapi", "uvicorn", "numpy", "faiss", "PIL", "ultralytics")
    }
    return {
        "ok": code == 200
        and all(m["ready"] for m in models.values())
        and all(deps.values())
        and weights.is_file(),
        "mode": "native",
        "ollama": code == 200,
        "models": models,
        "dependencies": deps,
        "vision_weights": weights.is_file(),
        "limits": cfg["limits"],
        "memory": "project-scoped",
        "visual_verifier": "local-vlm (same model)",
    }


def build_app():
    configure()
    import json
    from contextlib import asynccontextmanager

    from fastapi.responses import FileResponse

    from core.paths import runtime_path
    from gateway.app import build_app as gateway_app
    from integration.memory import start_worker
    from integration.trace import record
    from service.app import _TOKEN, REGISTRY
    from service.app import build_app as service_app
    from service.auth import require_auth

    app = service_app()
    app.title = "Anvil · 原生集成工作台"
    app.mount("/model", gateway_app())

    @asynccontextmanager
    async def lifespan(_app):
        start_worker()
        record("runtime", event="started", mode="native")
        yield
        record("runtime", event="stopped")

    app.router.lifespan_context = lifespan

    auth = require_auth(_TOKEN, what="集成状态与记录")

    @app.get("/console", include_in_schema=False)
    def console():
        return FileResponse(ROOT / "webui" / "console.html")

    @app.get("/integrations", dependencies=[auth])
    def integrations():
        return diagnostics()

    @app.get("/wo/{wo_id}/records", dependencies=[auth])
    def records(wo_id: str):
        from fastapi import HTTPException

        if REGISTRY.get(wo_id) is None:
            raise HTTPException(status_code=404, detail="工单不存在")
        items = []
        folder = Path(os.environ.get("ANVIL_INTEGRATION_LOG_DIR") or runtime_path("integration-log"))
        for path in folder.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("wo_id") == wo_id:
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
    ap.add_argument("--prepare-demo", action="store_true")
    ap.add_argument("--image", default="", help="为视觉演示准备本地公交车图片")
    ap.add_argument("--memory-flush", action="store_true")
    args = ap.parse_args()
    configure(args.port)
    if args.doctor:
        result = diagnostics()
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
