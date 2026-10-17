"""仓库内配置；环境变量保留模型服务和运行目录覆盖能力。"""

import os
import tomllib
from functools import lru_cache

from . import ROOT


@lru_cache(maxsize=1)
def settings() -> dict:
    path = os.environ.get("ANVIL_CONFIG", str(ROOT / "anvil.toml"))
    with open(path, "rb") as stream:
        cfg = tomllib.load(stream)
    for key in (
        "model_calls",
        "vision_requests",
        "output_tokens",
        "model_timeout",
        "memory_timeout",
        "vision_timeout",
        "image_bytes",
    ):
        if not isinstance(cfg["limits"][key], (int, float)) or cfg["limits"][key] <= 0:
            raise ValueError("limits.%s 必须为正数" % key)
    for key in ("model_calls", "vision_requests", "output_tokens", "image_bytes"):
        if isinstance(cfg["limits"][key], bool) or not isinstance(cfg["limits"][key], int):
            raise ValueError("limits.%s 必须为整数" % key)
    cfg["models"]["ollama"] = os.environ.get("LOCAL_LLM_OLLAMA", cfg["models"]["ollama"])
    return cfg
