"""三种模型共用本地出网闸、禁代理传输与审计。"""

import time

from .config import settings
from .trace import record


def invoke(path: str, payload: dict, timeout: float) -> dict:
    from gateway import app as gateway

    start = time.monotonic()
    code, data = gateway.post_ollama(path, payload, timeout=timeout)
    usage = {key: data.get(key) for key in ("prompt_eval_count", "eval_count")}
    fields = dict(
        model=payload.get("model"),
        endpoint=path,
        status=code,
        sec=round(time.monotonic() - start, 3),
        **usage,
    )
    gateway.audit({"kind": "integrated.model", **fields})
    record("model", **fields)
    if code != 200 or data.get("error"):
        raise RuntimeError("本地模型调用失败：%s" % data.get("error", code))
    return data


def chat(messages: list, *, model: str, timeout: float) -> dict:
    return invoke(
        "/api/chat",
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0, "num_predict": settings()["limits"]["output_tokens"]},
        },
        timeout,
    )


class AgentTransport:
    """适配器与网关的进程内连接；复用原画像策略，无需向自己发 HTTP。"""

    def chat(self, body: dict, timeout=None):
        from gateway.app import apply_profile, load_profiles

        started = time.monotonic()
        body, _ = apply_profile(body["model"], body, load_profiles())
        response = invoke("/api/chat", body, timeout or settings()["limits"]["model_timeout"])
        return response, time.monotonic() - started

    def list_models(self):
        from gateway.app import get_ollama

        code, data = get_ollama("/api/tags")
        return data if code == 200 else {"models": []}
