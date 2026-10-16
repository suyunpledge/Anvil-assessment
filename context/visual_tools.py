# -*- coding: utf-8 -*-
"""visual_tools.py —— SVSG 视觉网关工具（考核版接入件，2026-10-17）。

它只回答一个问题：**模型说「我需要看一眼图片/截图」时，框架能给什么？**

背景（考核叙事）：
  Anvil v1 的工具面全是文本代码类（read_file/apply_patch/run_tests）。
  但真实工单里有一类任务绕不开视觉：确认 UI 截图、核对渲染结果、识别图里的报错。
  SVSG（Structured Vision Gateway，127.0.0.1:3002）是本机另一套视觉网关，
  把「图片 → 结构化断言」做成了服务（YOLO 检测 + L1 IR + LLM 分析链）。
  把它的 /v1/analyze-image 挂进工具注册表，模型就有了受控的视觉能力——
  不是给模型一张图片让它「看图说话」，而是把视觉输入变成结构化 claims。

设计纪律：
  · 只读：visual_query 不写任何东西，天然无沙箱风险；
  · 工单级开关：工单 constraints.visual_tasks=True 才挂载（默认不暴露），
    挂载入口在 engine._enable_visual_tools()——纯代码工单永远 import 不到本模块
    （R1「绝不暴露全集」的最小暴露原则不变）；
  · 图片从沙箱内读（resolve 过路径检查），只发本机 SVSG，不出网；
  · 与 hr_bridge 同款纪律：SVSG 挂了抛 ToolError（引擎照常归类），绝不阻断整单。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict

SVSG_BASE = os.environ.get("ANVIL_SVSG_BASE", "http://127.0.0.1:3002")
SVSG_TIMEOUT_SEC = float(os.environ.get("ANVIL_SVSG_TIMEOUT", "30.0"))
##: SVSG 鉴权：默认 X-API-Key 头（SVSG .env 的 SVSG_API_KEY；登录机制 v0 开启后可切 Bearer）。
#: key 从环境变量 ANVIL_SVSG_API_KEY 读，不硬编码在本仓。
SVSG_API_KEY = os.environ.get("ANVIL_SVSG_API_KEY", "")
INTEGRATION_LOG_DIR = os.environ.get(
    "ANVIL_INTEGRATION_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 ".runtime", "integration-log"))


def _log_integration(record: Dict[str, Any]) -> None:
    try:
        os.makedirs(INTEGRATION_LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % int((time.time() % 1) * 1000)
        path = os.path.join(INTEGRATION_LOG_DIR, "svsg-%s.json" % ts)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ============================================================
# 工具本体：visual_query（由 install_visual_tools 挂进 tools.REGISTRY/EXECUTORS）
# ============================================================

VISUAL_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "visual_query",
        "description": (
            "对沙箱内的一张图片做结构化视觉分析。"
            "参数：image_path（沙箱内相对路径，支持 png/jpg/jpeg/bmp/webp）、"
            "query（想从图里确认什么，如「按钮是否显示」「报错文字是什么」）。"
            "返回结构化分析结果（结论 + 断言列表）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["image_path", "query"],
            "additionalProperties": False,
        },
    },
}


def visual_query(ctx: Any, image_path: str = "", query: str = "",
                 **extra: Any) -> Dict[str, Any]:
    """visual_query 工具执行体。ctx 是 tools.ToolContext。

    流程：沙箱内读图 → multipart 上传 SVSG /v1/analyze-image → 压结果文本。
    失败（SVSG 挂了/图片不存在/格式错）抛 ToolError，由引擎照常归类（不阻断整单）。
    """
    from tools import ToolError  # 同 statemachine 目录（engine 加载时已在 sys.path）

    if not image_path or not query:
        raise ToolError("visual_query 需要 image_path 与 query")
    p = ctx.resolve(image_path)
    if not os.path.exists(p):
        raise ToolError("图片不存在：%s" % image_path)
    ext = os.path.splitext(p)[1].lower().lstrip(".")
    if ext not in ("png", "jpg", "jpeg", "bmp", "webp"):
        raise ToolError("不支持的图片格式：%s（支持 png/jpg/jpeg/bmp/webp）" % ext)

    t0 = time.time()
    record: Dict[str, Any] = {"kind": "visual_query", "ts": time.time(),
                              "image": image_path, "query": query}
    try:
        with open(p, "rb") as f:
            img_bytes = f.read()
        boundary = "----anvil-" + uuid.uuid4().hex
        body = (
            ("--%s\r\nContent-Disposition: form-data; name=\"query\"\r\n\r\n%s\r\n"
             % (boundary, query)).encode("utf-8")
            + ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
               "Content-Type: application/octet-stream\r\n\r\n"
               % (boundary, os.path.basename(p))).encode("utf-8")
            + img_bytes
            + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        )
        req = urllib.request.Request(
            "%s/v1/analyze-image" % SVSG_BASE, data=body, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        if SVSG_API_KEY:
            req.add_header("X-API-Key", SVSG_API_KEY)
        with urllib.request.urlopen(req, timeout=SVSG_TIMEOUT_SEC) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        record.update(ok=False, reason="SVSG 不可达或响应异常",
                      error=str(e)[:200], sec=round(time.time() - t0, 3))
        _log_integration(record)
        raise ToolError("视觉网关不可用：%s（本工具不阻断工单，可跳过视觉步骤）" % e)

    # ---- 压结果（给模型看的文本面；result_to_text 认 "text" 键）----
    claims = res.get("claims") or []
    final = res.get("final_answer") or ""
    lines = []
    if final:
        lines.append("结论：%s" % final)
    for i, c in enumerate(claims[:8], 1):
        txt = str(c.get("text") or c.get("claim") or json.dumps(c, ensure_ascii=False))
        lines.append("断言%d：%s" % (i, txt[:200]))
    if not lines:
        lines.append("(SVSG 返回 status=%s，无可用断言)" % res.get("status"))

    record.update(ok=True, sec=round(time.time() - t0, 3),
                  status=res.get("status"), trace=res.get("trace_id"))
    _log_integration(record)
    return {"image": image_path, "query": query,
            "text": "\n".join(lines),
            "status": res.get("status"), "trace_id": res.get("trace_id"),
            "claims_count": len(claims)}


def install_visual_tools() -> int:
    """把 visual_query 挂进 tools.REGISTRY / EXECUTORS（幂等）。返回本次新增数。

    只能由 engine 按工单 constraints.visual_tasks 调用——全局 REGISTRY 保持纯净。
    """
    import tools as _T
    n = 0
    if "visual_query" not in _T.REGISTRY:
        _T.REGISTRY["visual_query"] = VISUAL_QUERY_SCHEMA
        n += 1
    if "visual_query" not in _T.EXECUTORS:
        _T.EXECUTORS["visual_query"] = visual_query
        n += 1
    return n
