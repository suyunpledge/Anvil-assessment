"""原生 SVSG：像素 + IR 双通道输入；真实检测和锚定，不用演示检测桩。"""

import asyncio
import base64
import hashlib
import json
import threading
import uuid
from collections import Counter
from pathlib import Path

from . import ROOT
from .config import settings
from .models import chat, invoke
from .trace import record

_compiler = None
_lock = threading.Lock()


def warmup():
    cfg = settings()
    # 将冷加载移出区域复核的短超时预算；空提示只加载本地模型，不生成内容。
    invoke(
        "/api/generate",
        {"model": cfg["models"]["vision"], "prompt": "", "stream": False},
        cfg["limits"]["vision_timeout"],
    )


class PixelLLM:
    def __init__(self, image: bytes):
        self.image = base64.b64encode(image).decode("ascii")

    async def complete(self, system_prompt: str, context: str):
        from svsg.l3_orchestrator.openai_adapter import parse_l3_answer

        cfg = settings()
        system_prompt = system_prompt.replace(
            "你看不到图像，\n只能基于 L2 提供的结构化证据作答。",
            "你会收到原始图片和 L2 结构化证据。请看图核对检测结果；可交付的核心断言仍须有结构化证据支持。",
        )
        evidence = json.loads(context)
        detections = evidence.get("detections", [])
        counts = dict(Counter(item["class"] for item in detections))
        system_prompt += (
            "\n计数契约：field=count 只表示全部检测实例总数，此次为 %d。"
            "每类数量为 %s；只数某类须使用 count:类别，不能写成 count。"
            "如果问题只问某物体是否存在，优先输出该 instance_id 的 class 或 exists 断言，"
            "无需附加计数。输出完整 JSON 对象，不要 Markdown。"
            % (len(detections), json.dumps(counts, ensure_ascii=False))
        )
        record(
            "vision_context",
            model=cfg["models"]["vision"],
            system=system_prompt,
            context=context,
            image_sha256=hashlib.sha256(self.image.encode()).hexdigest(),
            image_base64_chars=len(self.image),
        )
        response = await asyncio.to_thread(
            chat,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context, "images": [self.image]},
            ],
            model=cfg["models"]["vision"],
            timeout=cfg["limits"]["vision_timeout"],
        )
        return parse_l3_answer(response["message"]["content"])


class PixelVerifier:
    """对低置信实例做一次真实看图复核；失败交给 SVSG 保守降级。"""

    def __init__(self, image: bytes, ir):
        self.image = base64.b64encode(image).decode("ascii")
        self.instances = {d.instance_id: d for d in ir.detections}

    async def verify_batch(self, image_id: str, instance_ids: list[int]):
        from svsg.contracts import VerificationResult
        from svsg.l3_orchestrator.openai_adapter import _extract_json_object

        cfg = settings()
        regions = [
            {
                "instance_id": index,
                "detector_class": self.instances[index].object_class,
                "bbox_px": list(self.instances[index].bbox_px),
            }
            for index in instance_ids
        ]
        prompt = (
            "查看图片中的指定矩形区域，复核物体是否存在和类别。不要沿用检测器猜测。"
            '不确定则class填ambiguous。只输出JSON：{"results":[{"instance_id":1,'
            '"class":"ambiguous","exists":true,"confidence":0.5}]}。'
            "每个指定instance_id必须返回一次。矩形为原图像素坐标。"
        )
        record(
            "vision_verification_context",
            image_id=image_id,
            regions=regions,
            model=cfg["models"]["vision"],
            prompt=prompt,
        )
        response = await asyncio.to_thread(
            chat,
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(regions), "images": [self.image]},
            ],
            model=cfg["models"]["vision"],
            timeout=cfg["limits"]["vision_timeout"] / 4,
        )
        content = _extract_json_object(response["message"]["content"])
        parsed = json.loads(content or "{}")
        results = [VerificationResult.model_validate(item) for item in parsed.get("results", [])]
        if {r.instance_id for r in results} != set(instance_ids) or len(results) != len(instance_ids):
            raise ValueError("视觉复核实例集合与请求不符")
        record("vision_verification_result", results=[r.model_dump(by_alias=True) for r in results])
        return results


def analyze(image: bytes, query: str, filename: str) -> dict:
    from svsg.l1_compiler import ImageMeta, L1Compiler, YoloDetector, image_dimensions
    from svsg.l3_orchestrator import Orchestrator, OrchestratorConfig
    from svsg.l15_verifier import L15Verifier, VerifierConfig

    cfg = settings()
    if not image or len(image) > cfg["limits"]["image_bytes"]:
        raise ValueError("图片为空或超过大小上限")
    if not query.strip() or len(query) > 2000:
        raise ValueError("视觉问题须为 1—2000 字")
    trace_id = uuid.uuid4().hex
    # YOLO 实例非并发安全；统一限流也避免小显存重复加载。
    global _compiler
    with _lock:
        if _compiler is None:
            weights = Path(cfg["vision"]["weights"])
            if not weights.is_absolute():
                weights = ROOT / weights
            if cfg["vision"]["detector"] != "yolo" or not weights.is_file():
                raise RuntimeError("请配置本地 YOLO 权重：vision.weights；不会自动下载或改用桩")
            _compiler = L1Compiler(YoloDetector(str(weights)))
        width, height = image_dimensions(image)
        ir = _compiler.compile(image, ImageMeta(image_id=trace_id, width=width, height=height))
        warmup()
        orchestrator = Orchestrator(
            L15Verifier(
                PixelVerifier(image, ir), VerifierConfig(timeout_s=cfg["limits"]["vision_timeout"] / 4)
            ),
            PixelLLM(image),
            OrchestratorConfig(llm_timeout_s=cfg["limits"]["vision_timeout"]),
        )
        result = asyncio.run(orchestrator.run(query, ir))
    output = {
        "status": result.status,
        "trace_id": trace_id,
        "final_answer": result.answer.final_answer if result.answer else None,
        "claims": [c.model_dump() for c in result.answer.claims] if result.answer else [],
        "human_review_required": result.human_review_required,
        "anchor_violations": result.anchor_violations,
        "error": result.error,
        "session_summary": result.session_summary,
        "verification_backend": "local-vlm (与主视觉模型相同，不是独立模型交叉验证)",
    }
    record("vision_result", filename=filename, query=query, result=output, model=cfg["models"]["vision"])
    return output
