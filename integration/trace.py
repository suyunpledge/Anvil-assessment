"""工单关联的原子 JSON 留痕；请求内容仅写被 git 忽略的运行目录。"""

import json
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType

from core.paths import runtime_path

CURRENT = ContextVar("anvil_workorder", default=MappingProxyType({}))


@contextmanager
def scope(**fields):
    token = CURRENT.set({**CURRENT.get(), **fields})
    try:
        yield
    finally:
        CURRENT.reset(token)


def record(kind: str, **fields) -> str:
    data = {**CURRENT.get(), "kind": kind, "ts": time.time(), **fields}
    folder = Path(os.environ.get("ANVIL_INTEGRATION_LOG_DIR") or runtime_path("integration-log"))
    name = "%s-%s.json" % (kind, uuid.uuid4().hex)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / (name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(temp, folder / name)
        return name
    except OSError:
        return ""
