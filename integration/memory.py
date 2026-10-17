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
from .trace import record, scope

_engines = {}
_lock = threading.RLock()
_worker_lock = threading.Lock()
_worker = None
_wake = threading.Event()


class Embedding:
    def __init__(self, cfg):
        self.config = cfg
        self.dimension = cfg.embedding_dim
        self.model_name = cfg.embedding_model
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def embed(self, text, strict=False):
        import numpy as np

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
            vec = np.asarray(data.get("embedding", []), dtype=np.float32)
            if vec.shape != (self.dimension,) or not np.isfinite(vec).all() or np.linalg.norm(vec) <= 1e-6:
                raise ValueError("嵌入向量为空、维度错误或含非有限值")
            vec /= np.linalg.norm(vec)
            self.cache[text] = vec
            if len(self.cache) > 256:
                self.cache.popitem(last=False)
            return vec.copy()

    def embed_batch(self, texts, strict=False):
        return [self.embed(text, strict=strict) for text in texts]


def project_id(project: str) -> str:
    canonical = os.path.normcase(os.path.realpath(project))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def engine(project: str):
    from hierarchical_retrieval import HConfig, HierarchicalRetrieval

    key = project_id(project)
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
    with _lock:
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


def flush() -> dict:
    folder = Path(runtime_path("memory-outbox"))
    result = {"written": 0, "pending": 0}
    with _lock:
        for path in sorted(folder.glob("*.json")):
            item = {}
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                with scope(wo_id=item["wo_id"], project=item["project"]):
                    engine(item["project"]).ingest(
                        item["text"], metadata={"wo_id": item["wo_id"], "source": "verified_workorder"}
                    )
                    path.unlink()
                    record("memory_ingested", ok=True, item=path.name)
                result["written"] += 1
            except Exception as exc:
                result["pending"] += 1
                record(
                    "memory_ingest_failed",
                    wo_id=item.get("wo_id"),
                    item=path.name,
                    error=str(exc)[:300],
                    retry="下次启动或 memory-flush",
                )
    return result


def start_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():

            def work():
                while True:
                    _wake.wait()
                    _wake.clear()
                    flush()

            _worker = threading.Thread(target=work, name="anvil-memory", daemon=True)
            _worker.start()
        _wake.set()
