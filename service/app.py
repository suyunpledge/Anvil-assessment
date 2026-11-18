#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py —— 交付件 B：工单服务（把状态机包成 HTTP + SSE）。

设计要点（都是为了"能被别人接手改"）：

  · 逻辑全在 `runner.py`，本文件只做 HTTP 映射 —— 想换前端不必碰这里；
  · 事件走 SSE，前端能实时看到每个节点与每个 gate 的结果；
  · **落盘必须经人确认**：工单一律先跑到"待确认"状态，`POST /wo/{id}/confirm` 才写文件；
  · 取消是协作式的：包一层适配器，在两个节点之间检查标记。

鉴权口径（2026-09-30 补齐）：
  · 仅 /health 开放；/presets 与其他业务读写接口均需令牌
  · 其余读口（/wo 列表、/wo/{id}、/events、/wo/{id}/staged）与所有写口一致要求令牌
  · 扩展侧已支持自动读取令牌文件（extension.js::loadToken），无需手动配置

跑法：python service/app.py [--port 8090]
测试：python service/tests/test_service.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets as _secrets
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# ★ 内嵌解释器是隔离模式（._pth），**脚本所在目录不进 sys.path**：
#   所以同目录的 `runner` 必须自己把 _HERE 挂上去，否则 ModuleNotFoundError。
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.paths import ensure_core_on_path, runtime_path, version  # noqa: E402

ensure_core_on_path()

from service.auth import require_auth  # noqa: E402
from service.runner import (  # noqa: E402
    STATUS_RUNNING,
    RegistryBusy,
    RunnerOpts,
    RunnerRegistry,
    list_presets,
)

# ★ FastAPI 导入放模块顶层：见 gateway/app.py 里同一处的注释（注解字符串化的坑）
try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel, Field
    _FASTAPI_OK = True
except Exception:
    FastAPI = Header = HTTPException = Request = object     # type: ignore
    BaseModel = object                     # type: ignore
    JSONResponse = StreamingResponse = object   # type: ignore
    _FASTAPI_OK = False

    def Field(*a, **kw):                   # type: ignore
        return None


#: ★ 进程级令牌：只生成/读取一次（2026-09-30 实测坑：build_app 被调两次时
#:   会生成两个不同令牌并两次覆盖文件，先读到旧令牌的一方全部 401）。
#:   放在模块级，无论 build_app 被调多少次都用同一个。
def _resolve_token() -> str:
    """进程级只算一次：环境变量优先（LOCAL_IDE_TOKEN，兼容 LOCAL_LLM_TOKEN），否则随机。"""
    v = (os.environ.get("LOCAL_IDE_TOKEN") or "").strip()
    if not v:
        v = (os.environ.get("LOCAL_LLM_TOKEN") or "").strip()
    return v or _secrets.token_urlsafe(24)


_TOKEN = _resolve_token()


# ============================================================
# 服务端策略（外部审查 #2：不能让客户端决定“要不要人确认”）
# ============================================================
class ConfirmPolicy:
    """确认与目录范围策略，只需环境变量配置。

    ``LOCAL_IDE_REQUIRE_CONFIRM``
        1（默认）= 服务端强制人确认，客户端传 false 也无效
        0        = 允许客户端自己决定（仅建议在完全可信的本机环境下用）
    ``LOCAL_IDE_ALLOWED_ROOTS``
        允许操作的工作目录根，多个用 ``;`` 分隔；不设则不限制。
    令牌不可关（2026-10-05）
        历史上这里有一个 ``LOCAL_IDE_REQUIRE_TOKEN`` 开关，README 也承诺「0 = 关闭（不推荐）」。
        但 2026-09-30 补齐读口鉴权后，所有需要保护的端点都无条件挂 ``READ_AUTH``，
        那个开关事实上是**死的**：把它设成 0，错误令牌照样回 401，等于文档在承诺一个
        不存在的功能。
        本机单用户工具，令牌启动时生成、写在 ``.runtime/service-token``，成本≈0；
        而本仓自己的交接记录里写着「别再让读口不鉴权复活」。所以处理方式是**删掉开关**
        而不是给它接线 —— 少一个配置面，就少一处失真。
        ``require_token`` 字段保留（恒 True）——``/health`` 与既有回归都在读它。
    """

    def __init__(self, force_confirm: bool, allowed_roots: List[str], require_token: bool) -> None:
        self.force_confirm = force_confirm
        self.allowed_roots = [os.path.normcase(os.path.realpath(r.strip())) for r in allowed_roots if r.strip()]
        self.require_token = require_token

    @classmethod
    def from_env(cls) -> "ConfirmPolicy":
        force = os.environ.get("LOCAL_IDE_REQUIRE_CONFIRM", "1") != "0"
        roots = [p for p in (os.environ.get("LOCAL_IDE_ALLOWED_ROOTS", "") or "").split(";") if p.strip()]
        # 令牌不可关：见类文档（2026-10-05 删掉了那个从未生效的 off-switch）。恒 True。
        return cls(force, roots, True)

    def effective_confirm(self, client_wants: bool, allow_delete: bool = False) -> bool:
        """策略优先：要确认时必须确认。

        ★ 2026-10-03：工单一旦声明 allow_delete（具备删除能力），**无条件强制确认**——
        这条不受 LOCAL_IDE_REQUIRE_CONFIRM=0 影响。理由：删除不可逆，
        「每次删除都要人同意」是用户明确要求，不能被任何环境变量关掉。
        """
        if allow_delete:
            return True
        return True if self.force_confirm else bool(client_wants)

    def workdir_allowed(self, path: str) -> bool:
        if not self.allowed_roots:
            return True
        p = os.path.normcase(os.path.realpath(path or ""))
        for root in self.allowed_roots:
            if p == root or p.startswith(root + os.sep):
                return True
        return False

    def summary(self) -> Dict[str, Any]:
        return {"force_confirm": self.force_confirm,
                "allowed_roots": self.allowed_roots or "(不限制)",
                "require_token": self.require_token}


def _token_path() -> str:
    return runtime_path("service-token")


def _write_token_file(token: str) -> None:
    """把令牌写到 .runtime/service-token（仅本用户可读），供本地扩展读取。

    这不是“把密钥写在代码里”：文件在用户目录下、每次启动重新生成、且只用于本机回环通信。
    """
    try:
        p = _token_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass


if _FASTAPI_OK:
    class ConfirmReq(BaseModel):
        approve: bool = True

    class CreateReq(BaseModel):
        """建单请求。字段名与 RunnerOpts 对齐（少传就有默认值）。"""
        kind: Literal["code", "tidy"] = "code"
        task: str = ""
        workdir: str = ""
        target: str = ""
        test_path: str = ""
        adapter: str = Field(default_factory=lambda: RunnerOpts().adapter)
        model: str = ""
        mode: Literal["dry_run", "execute"] = "dry_run"
        dirs: List[str] = Field(default_factory=list)
        semantic: bool = False
        max_repair_rounds: int = Field(default=3, ge=0, le=8)
        # tidy 专用：开启删除（服务端会强制人确认，且执行时自动备份）
        allow_delete: bool = False
        require_confirm: bool = True
        # code 专用：视觉工单（装载 visual_query，工单级授权）
        visual_tasks: bool = False
else:
    class CreateReq:                       # type: ignore
        pass


def build_app(registry: Optional[RunnerRegistry] = None):
    if not _FASTAPI_OK:
        raise SystemExit("未找到 fastapi；请用带 fastapi/uvicorn 的解释器运行（本机 AutoClaw 内嵌 python 自带）")

    registry = registry if registry is not None else RunnerRegistry()

    @asynccontextmanager
    async def lifespan(_app):
        registry.open()
        try:
            yield
        finally:
            await asyncio.to_thread(registry.shutdown)

    app = FastAPI(title="Local IDE Work-Order Service", version="0.3.1", lifespan=lifespan)
    app.state.registry = registry

    # ------------------------------------------------------------------
    # 安全加固（2026-09-28 外部审查 #2 → 2026-09-30 补齐读口）
    # ------------------------------------------------------------------
    # CORS 收窄到 localhost / 127.0.0.1 / vscode-webview
    allow_origin_regex = os.environ.get(
        "LOCAL_IDE_CORS_REGEX", r"^(https?://(127\.0\.0\.1|localhost)(:\d+)?|vscode-webview://.*)$")
    app.add_middleware(CORSMiddleware, allow_origin_regex=allow_origin_regex,
                       allow_methods=["GET", "POST"], allow_headers=["*"],
                       allow_credentials=False)

    # 令牌：进程级（模块加载时已定），这里只负责落盘
    token = _TOKEN
    app.state.token = token
    _write_token_file(token)
    policy = ConfirmPolicy.from_env()

    # 仅健康检查公开；画像、工单与事件统一鉴权。
    READ_AUTH = require_auth(token, what="查询接口")

    # 2026-10-05 删除：_write_auth 曾是实现「按策略开关鉴权」的地方，但它从未被任何
    # 路由引用（读口与写口统一挂 READ_AUTH），是死代码，也是上面那个开关"看起来存在"
    # 的由来。删掉它，`LOCAL_IDE_REQUIRE_TOKEN` 这整条线就干净了。
    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"ok": True, "core": version(),
                "runtime": os.path.dirname(runtime_path("x")),
                "wos": len(registry.all()),
                "policy": policy.summary(),
                "auth": {"token_required": policy.require_token,
                         "token_file": _token_path()}}

    @app.get("/presets", dependencies=[READ_AUTH])
    def presets() -> Dict[str, Any]:
        return list_presets()

    @app.post("/wo", dependencies=[READ_AUTH])
    def create(req: CreateReq, request: Request):
        opts = RunnerOpts(**req.model_dump())
        if opts.kind == "code" and not (opts.workdir and opts.target):
            return JSONResponse(status_code=400,
                                content={"error": "code 工单需要 workdir 与 target"})
        if opts.kind == "tidy" and not opts.workdir:
            return JSONResponse(status_code=400, content={"error": "tidy 工单需要 workdir"})
        # ★ 服务端策略：策略要求确认时，客户端传 require_confirm=false 不生效
        opts.require_confirm = policy.effective_confirm(opts.require_confirm,
                                                        opts.allow_delete)
        # ★ 工作目录白名单（设了才生效）
        if not policy.workdir_allowed(opts.workdir):
            return JSONResponse(status_code=403,
                                content={"error": "该工作目录不在允许范围内（LOCAL_IDE_ALLOWED_ROOTS）",
                                         "allowed": policy.allowed_roots})
        if not os.path.isdir(opts.workdir):
            return JSONResponse(status_code=400, content={"error": "工作目录不存在"})
        if opts.kind == "code":
            from tools import ToolContext, ToolError
            try:
                ctx = ToolContext(root=opts.workdir, target=opts.target)
                if not os.path.isfile(ctx.resolve(opts.target)):
                    raise ToolError("目标文件不存在")
                if opts.test_path:
                    ctx.resolve(opts.test_path)
            except ToolError as exc:
                return JSONResponse(status_code=400, content={"error": str(exc)})
        # 固定建单时的目录，避免后续 chdir 或相对目录造成作用域漂移。
        opts.workdir = os.path.realpath(opts.workdir)
        try:
            r = registry.create(opts)
        except RegistryBusy as exc:
            return JSONResponse(status_code=429, content={"error": str(exc)},
                                headers={"Retry-After": "2"})
        return {"wo_id": r.wo_id, "status": r.status,
                "require_confirm": opts.require_confirm,
                "policy": policy.summary()}

    @app.get("/wo", dependencies=[READ_AUTH])
    def list_wo() -> Dict[str, Any]:
        items = [{"wo_id": r.wo_id, "status": r.status, "kind": r.opts.kind,
                  "task": r.opts.task[:80]} for r in registry.all()]
        return {"count": len(items), "items": items}

    @app.get("/wo/{wo_id}", dependencies=[READ_AUTH])
    def get_wo(wo_id: str):
        r = registry.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        return r.snapshot()

    @app.get("/wo/{wo_id}/staged", dependencies=[READ_AUTH])
    def staged(wo_id: str):
        """暂存内容 + diff（给 diff 视图用）。

        ★ 鉴权（2026-09-29 自查）：这个接口返回**文件内容与本地路径**，
          属于敏感读口，不能因为“是读接口”就豁免令牌。
        ★ 2026-09-30 改为不返回本地绝对路径，只回文件名。
        """
        r = registry.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        real = str(r.result.get("real_path") or "")
        return {"wo_id": wo_id, "content": r.result.get("staged_content", ""),
                "diff": r.result.get("diff", ""),
                "filename": os.path.basename(real) if real else "",
                "status": r.status}

    @app.post("/wo/{wo_id}/cancel", dependencies=[READ_AUTH])
    def cancel(wo_id: str):
        r = registry.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        return {"ok": r.cancel(), "status": r.status}

    @app.post("/wo/{wo_id}/confirm", dependencies=[READ_AUTH])
    def confirm(wo_id: str, req: Optional[ConfirmReq] = None):
        r = registry.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})
        if req is not None and not req.approve:
            return {"ok": r.cancel(), "status": r.status}
        res = r.confirm()
        return JSONResponse(status_code=(200 if res.get("ok") else 409), content=res)

    @app.get("/wo/{wo_id}/events", dependencies=[READ_AUTH])
    def events(wo_id: str, once: int = 0, after: int = 0,
               last_event_id: str = Header(default="")) -> Any:
        """SSE 事件流。`?once=1` 时只回一次快照（便于脚本化测试）。"""
        r = registry.get(wo_id)
        if not r:
            return JSONResponse(status_code=404, content={"error": "工单不存在"})

        try:
            cursor_start = int(last_event_id) if last_event_id else after
            if cursor_start < 0:
                raise ValueError
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "事件游标必须为非负整数"})

        async def gen():
            yield "data: %s\n\n" % json.dumps({"kind": "snapshot", "wo": r.wo_id,
                                               "status": r.status}, ensure_ascii=False)
            if once:
                yield "event: end\ndata: [DONE]\n\n"
                return
            cursor = cursor_start
            last_activity = time.monotonic()
            last_heartbeat = last_activity
            while True:
                batch, gap = r.events.read_after(cursor)
                if gap:
                    if not batch:
                        cursor = 0
                    yield "data: %s\n\n" % json.dumps(
                        {"kind": "snapshot", "reset": True, **r.snapshot()},
                        ensure_ascii=False, default=str)
                if batch:
                    last_activity = time.monotonic()
                    for ev in batch:
                        cursor = ev["seq"]
                        yield "id: %d\ndata: %s\n\n" % (cursor, json.dumps(ev, ensure_ascii=False, default=str))
                if r.events.finished or (not r.alive and r.status not in ("pending", STATUS_RUNNING)):
                    # 收到生产完成通知后再读一轮，覆盖结束检查前刚写入的尾事件。
                    remaining, _ = r.events.read_after(cursor)
                    for ev in remaining:
                        cursor = ev["seq"]
                        yield "id: %d\ndata: %s\n\n" % (cursor, json.dumps(ev, ensure_ascii=False, default=str))
                    yield "event: end\ndata: [DONE]\n\n"
                    return
                now = time.monotonic()
                if now - last_activity >= 900:
                    yield "event: timeout\ndata: [DONE]\n\n"
                    return
                if now - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await r.events.wait_after(
                    cursor, timeout=min(15 - (now - last_heartbeat), 900 - (now - last_activity)))

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地 IDE 工单服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args(argv)
    import uvicorn
    print("工单服务：http://%s:%d" % (args.host, args.port))
    print("核心版本：%s" % version())
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
