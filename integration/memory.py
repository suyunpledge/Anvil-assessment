"""项目隔离的原生 HR；串行写索引，成功工单通过持久化 outbox 入库。"""

import hashlib
import json
import os
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

from core.paths import runtime_path

from .config import settings
from .models import invoke
from .scheduler import model_scope
from .trace import record, scope

_engines = {}
_lock = threading.RLock()
_project_locks = {}
_flush_lock = threading.Lock()
_worker_lock = threading.Lock()
_worker = None
_wake = threading.Event()
_stop = threading.Event()


def project_lock(project):
    with _lock:
        return _project_locks.setdefault(project_id(project), threading.RLock())


class Embedding:
    def __init__(self, cfg):
        self.config = cfg
        self.dimension = cfg.embedding_dim
        self.model_name = cfg.embedding_model
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def _vector(self, raw):
        import numpy as np
        vec = np.asarray(raw, dtype=np.float32)
        if vec.shape != (self.dimension,) or not np.isfinite(vec).all() or np.linalg.norm(vec) <= 1e-6:
            raise ValueError("嵌入向量为空、维度错误或含非有限值")
        return vec / np.linalg.norm(vec)

    def embed(self, text, strict=False):
        # HR 反复对同一查询做 topic / sentence / source 检索；只算一次向量。
        with self.lock:
            if text in self.cache:
                self.cache.move_to_end(text)
                return self.cache[text].copy()
            data = invoke(
                "/api/embeddings",
                {"model": self.model_name, "prompt": text},
                settings()["limits"]["memory_timeout"],
            )
            vec = self._vector(data.get("embedding", []))
            self.cache[text] = vec
            if len(self.cache) > 256:
                self.cache.popitem(last=False)
            return vec.copy()

    def embed_batch(self, texts, strict=False):
        if len(texts) <= 1:
            return [self.embed(text, strict=strict) for text in texts]
        with self.lock:
            vectors = {text: self.cache[text] for text in texts if text in self.cache}
            pending = list(dict.fromkeys(text for text in texts if text not in vectors))
            # HR 原接口虽名为 batch，旧适配逐句发请求。统一为有界的真实批量推理。
            for start in range(0, len(pending), 64):
                batch = pending[start:start + 64]
                data = invoke("/api/embed", {"model": self.model_name, "input": batch},
                              settings()["limits"]["memory_timeout"])
                raw = data.get("embeddings")
                if not isinstance(raw, list) or len(raw) != len(batch):
                    raise ValueError("批量嵌入返回数量与输入不符")
                normalized = [self._vector(v) for v in raw]
                vectors.update(zip(batch, normalized))
            for text in dict.fromkeys(texts):
                self.cache[text] = vectors[text]
                self.cache.move_to_end(text)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return [vectors[text].copy() for text in texts]


def project_id(project: str) -> str:
    canonical = os.path.normcase(os.path.realpath(project))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def engine(project: str):
    from hierarchical_retrieval import HConfig, HierarchicalRetrieval

    key = project_id(project)
    with _lock:
        if key not in _engines:
            models = settings()["models"]
            cfg = HConfig(
                storage_root=runtime_path("memory", key),
                embedding_model=models["embedding"],
                embedding_dim=models["embedding_dim"],
                embedding_request_timeout=settings()["limits"]["memory_timeout"],
            )
            _engines[key] = HierarchicalRetrieval(config=cfg, embedding=Embedding(cfg))
        return _engines[key]


def retrieve(query: str, project: str, **kwargs) -> dict:
    with project_lock(project):
        hr = engine(project)
        if hr.cloud_cache.count() == 0:
            return {"key_sentences": [], "full_contexts": []}
        return hr.retrieve(query, **kwargs)


def enqueue(project: str, wo_id: str, text: str) -> str:
    folder = Path(runtime_path("memory-outbox"))
    folder.mkdir(parents=True, exist_ok=True)
    # 内容地址使重复确认不会重复排队；HR 自身也按内容去重。
    key = hashlib.sha256((project_id(project) + wo_id + text).encode()).hexdigest()
    path = folder / (key + ".json")
    temp = folder / (uuid.uuid4().hex + ".tmp")
    temp.write_text(
        json.dumps({"project": project, "wo_id": wo_id, "text": text}, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temp, path)
    record("memory_queued", wo_id=wo_id, item=path.name)
    start_worker()
    return path.name


def flush(stop_event=None) -> dict:
    folder = Path(runtime_path("memory-outbox"))
    result = {"written": 0, "pending": 0}
    with _flush_lock:
        for path in sorted(folder.glob("*.json")):
            if stop_event is not None and stop_event.is_set():
                break
            item = {}
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not item[key].strip()
                    for key in ("wo_id", "project", "text")
                ):
                    raise ValueError("记忆队列记录必须包含非空 wo_id / project / text")
                with project_lock(item["project"]), scope(wo_id=item["wo_id"], project=item["project"]), \
                        model_scope(cancel=stop_event, background=True):
                    engine(item["project"]).ingest(
                        item["text"], metadata={"wo_id": item["wo_id"], "source": "verified_workorder"}
                    )
                    path.unlink()
                    record("memory_ingested", ok=True, item=path.name)
                result["written"] += 1
            except Exception as exc:
                record(
                    "memory_ingest_failed",
                    wo_id=item.get("wo_id") if isinstance(item, dict) else None,
                    item=path.name,
                    error=str(exc)[:300],
                    retry="后台退避重试 / 下次启动 / memory-flush",
                )
        result["pending"] = len(list(folder.glob("*.json")))
    return result


def start_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _stop.clear()

            def work():
                delay = None
                while not _stop.is_set():
                    _wake.wait(delay)
                    _wake.clear()
                    if _stop.is_set():
                        break
                    try:
                        pending = flush(stop_event=_stop)["pending"]
                    except Exception as exc:
                        record("memory_worker_failed", error=str(exc)[:300])
                        pending = True
                    delay = min((delay or 15) * 2, 300) if pending else None

            _worker = threading.Thread(target=work, name="anvil-memory", daemon=True)
            _worker.start()
        elif _stop.is_set():
            raise RuntimeError("记忆工作线程正在停止，请在退出后重启服务")
        _wake.set()


def stop_worker(timeout=2):
    with _worker_lock:
        _stop.set()
        _wake.set()
        worker = _worker
    if worker is not None:
        worker.join(timeout)
    return bool(worker and worker.is_alive())
