"""三种模型共用本地出网闸、禁代理传输与审计。"""

import time

from .config import settings
from .scheduler import ModelCancelled, ModelQueueBusy, scheduler
from .trace import record


def invoke(path: str, payload: dict, timeout: float) -> dict:
    from gateway import app as gateway

    start = time.monotonic()
    queue_sec = 0.0
    cancelled = None
    try:
        with scheduler().slot(timeout) as permit:
            queue_sec = permit["queue_sec"]
            code, data = gateway.post_ollama(path, payload, timeout=permit["timeout"])
        if not isinstance(data, dict):
            code, data = 502, {"error": "模型响应必须为 JSON 对象"}
    except ModelCancelled as exc:
        cancelled = exc
        queue_sec = time.monotonic() - start
        code, data = 499, {"error": str(exc)}
    except ModelQueueBusy as exc:
        queue_sec = time.monotonic() - start
        code, data = 503, {"error": str(exc)}
    except Exception as exc:
        code, data = 0, {"error": gateway.redact(str(exc))[:400]}
    usage = {key: data.get(key) for key in ("prompt_eval_count", "eval_count")}
    fields = dict(
        model=payload.get("model"),
        endpoint=path,
        status=code,
        sec=round(time.monotonic() - start, 3),
        queue_sec=round(queue_sec, 3),
        error=data.get("error"),
        **usage,
    )
    gateway.audit({"kind": "integrated.model", **fields})
    record("model", **fields)
    if cancelled is not None:
        raise cancelled
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
        from mythos_core.errors import AdapterError, ContextOverflow
        from mythos_core.transport import _is_context_overflow

        started = time.monotonic()
        body, _ = apply_profile(body["model"], body, load_profiles())
        try:
            response = invoke("/api/chat", body, timeout or settings()["limits"]["model_timeout"])
        except ModelCancelled:
            raise
        except RuntimeError as exc:
            if _is_context_overflow(str(exc), 0):
                raise ContextOverflow(str(exc)) from exc
            raise AdapterError(str(exc)) from exc
        return response, time.monotonic() - started

    def list_models(self):
        from gateway.app import get_ollama

        code, data = get_ollama("/api/tags")
        return data if code == 200 else {"models": []}
