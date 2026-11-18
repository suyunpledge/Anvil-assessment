"""仓库内配置；环境变量保留模型服务和运行目录覆盖能力。"""

import math
import os
import tomllib
from functools import lru_cache

from . import ROOT


@lru_cache(maxsize=1)
def settings() -> dict:
    path = os.environ.get("ANVIL_CONFIG", str(ROOT / "anvil.toml"))
    with open(path, "rb") as stream:
        cfg = tomllib.load(stream)
    if "adapter" not in cfg["models"]:
        # 兼容旧配置：已有模型继续使用对应画像，不被新默认值覆盖。
        from core.compatibility.mythos_core.profiles import PROFILE_BY_MODEL
        cfg["models"]["adapter"] = PROFILE_BY_MODEL.get(cfg["models"].get("code"), "qwen3.5")
    cfg.setdefault("service", {})
    for key, default in (("max_active", 4), ("max_retained", 100)):
        value = cfg["service"].setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("service.%s 必须为正整数" % key)
    if cfg["service"]["max_retained"] < cfg["service"]["max_active"]:
        raise ValueError("service.max_retained 不能小于 max_active")
    cfg.setdefault("inference", {})
    for key, default in (("max_parallel", 1), ("max_waiting", 32), ("foreground_burst", 8)):
        value = cfg["inference"].setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("inference.%s 必须为正整数" % key)
    for key in (
        "model_calls",
        "vision_requests",
        "output_tokens",
        "model_timeout",
        "memory_timeout",
        "vision_timeout",
        "image_bytes",
    ):
        value = cfg["limits"][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("limits.%s 必须为正数" % key)
    for key in ("model_calls", "vision_requests", "output_tokens", "image_bytes"):
        if isinstance(cfg["limits"][key], bool) or not isinstance(cfg["limits"][key], int):
            raise ValueError("limits.%s 必须为整数" % key)
    for key in ("code", "adapter", "vision", "embedding", "ollama"):
        value = cfg["models"][key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("models.%s 必须为非空字符串" % key)
    dimension = cfg["models"]["embedding_dim"]
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ValueError("models.embedding_dim 必须为正整数")
    cfg["models"]["ollama"] = os.environ.get("LOCAL_LLM_OLLAMA", cfg["models"]["ollama"])
    return cfg


def default_adapter() -> str:
    """HTTP、工单与 IDE 下拉框使用同一个配置默认值。"""
    return settings()["models"]["adapter"]
