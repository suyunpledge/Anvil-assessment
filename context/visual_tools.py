"""受控的原生 SVSG 工具：沙箱读图 → 本地视觉模型 → 结构化证据。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

VISUAL_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "visual_query",
        "description": "读取沙箱内图片并做视觉分析；返回结构化断言和复审状态。",
        "parameters": {
            "type": "object",
            "properties": {"image_path": {"type": "string"}, "query": {"type": "string"}},
            "required": ["image_path", "query"],
            "additionalProperties": False,
        },
    },
}


def visual_query(ctx: Any, image_path: str = "", query: str = "", **extra: Any) -> Dict[str, Any]:
    from tools import ToolError

    from integration.config import settings
    from integration.trace import record
    from integration.vision import analyze

    if not image_path or not query:
        raise ToolError("visual_query 需要 image_path 与 query")
    path = ctx.resolve(image_path)
    if os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        raise ToolError("不支持的图片格式")
    try:
        limit = settings()["limits"]["image_bytes"]
        with open(path, "rb") as stream:
            image = stream.read(limit + 1)
        if len(image) > limit:
            raise ValueError("图片超过大小上限")
        result = analyze(image, query, image_path)
        if not isinstance(result, dict) or result.get("status") not in ("delivered", "delivered_with_review"):
            raise ValueError(
                "视觉结果未交付：%s" % (result.get("status") if isinstance(result, dict) else "非法响应")
            )
    except Exception as exc:
        record("visual_query", image=image_path, query=query, ok=False, error=str(exc)[:300])
        raise ToolError("视觉分析失败（不可据此推断图片内容）：%s" % exc) from exc
    claims = result.get("claims") or []
    lines = ["结论：%s" % str(result.get("final_answer") or "")[:2400]]
    for item in claims[:8]:
        lines.append("断言：%s" % json.dumps(item, ensure_ascii=False)[:400])
    if result.get("human_review_required"):
        lines.append("注意：结果需要人工复审，不能当作已验证事实。")
    record("visual_query", image=image_path, query=query, ok=True, result=result)
    return {
        **result,
        "image": image_path,
        "query": query,
        "text": "\n".join(lines),
        "claims_count": len(claims),
    }


def install_visual_tools() -> int:
    import tools

    new = int("visual_query" not in tools.REGISTRY)
    tools.REGISTRY["visual_query"] = VISUAL_QUERY_SCHEMA
    tools.EXECUTORS["visual_query"] = visual_query
    return new
