# -*- coding: utf-8 -*-
"""hr_bridge.py —— HR 分级检索桥（考核版接入件，2026-10-17）。

它只回答一个问题：**这个任务的历史相关知识，HR 里存了什么？**

背景（考核叙事）：
  Anvil 的上下文层（builder.py）只看「目标文件邻域」——源码是静态的，
  但项目运行期沉淀的知识（为什么这么改、踩过什么坑、review 结论）不在源码里。
  HR（hierarchical-retrieval）是本机另一套服务（127.0.0.1:8001），
  三层结构：云端缓存 / 关键句库 / 话题界域（起源：用户 2026-05-24 手写引擎稿）。
  把它接进上下文层，模型改代码前能「带着项目记忆改」，不用每次从零理解项目。

设计纪律（与 builder.py 同一口径）：
  · **绝不阻断主流程**：HR 挂了/超时/返回垃圾 → 返回空结果，工单照常跑；
  · **结果进预算**：检索文本作为一个普通 section 参与 Budget 裁剪（优先级中低），
    预算紧张时先被扔，绝不挤占目标源码的位置；
  · **每次检索落日志**：`.runtime/integration-log/` 下按时间戳存 JSON，
    考核演示时可以打开看「模型实际用了哪些历史知识」。

接入点：builder.build() 里的 hr_section（见 build 的 hr_query 参数）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# HR 服务地址：默认本地副本（8001），可用环境变量覆盖（演示机/考场网络隔离时用）
HR_BASE = os.environ.get("ANVIL_HR_BASE", "http://127.0.0.1:8001")
HR_TIMEOUT_SEC = float(os.environ.get("ANVIL_HR_TIMEOUT", "15.0"))
#: 集成日志目录（相对 Anvil 根）
INTEGRATION_LOG_DIR = os.environ.get(
    "ANVIL_INTEGRATION_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 ".runtime", "integration-log"))


def _post(path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        HR_BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _log_integration(record: Dict[str, Any]) -> None:
    """落集成日志（失败不影响主流程；目录不可写就静默放弃）。"""
    try:
        os.makedirs(INTEGRATION_LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % int((time.time() % 1) * 1000)
        path = os.path.join(INTEGRATION_LOG_DIR, "hr-%s.json" % ts)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def make_query(task: str, target: str) -> str:
    """把「任务描述 + 目标文件」压成一个检索 query。

    口径：HR 的检索粒度是「项目知识」，不是代码行——所以 query 用自然语言任务
    描述为主，目标文件名（去扩展名）作为辅助信号拼在后面。任务描述里的代码
    标识符（函数名等）保留，它们正是 HR 关键句库里最常见的锚点。
    """
    target_stem = re.sub(r"\.(py|js|ts|md|json)$", "", os.path.basename(target))
    task_clean = re.sub(r"\s+", " ", (task or "").strip())[:300]
    if target_stem and target_stem.lower() not in task_clean.lower():
        return "%s %s" % (task_clean, target_stem)
    return task_clean


def retrieve(task: str, target: str = "",
             top_k_sentences: int = 4, top_k_context: int = 1,
             log_tag: str = "build") -> Dict[str, Any]:
    """调 HR /retrieve，返回 {text, ok, reason, hits}。失败时 text=""（绝不抛）。

    Parameters
    ----------
    task : 工单任务描述（检索 query 的主体）
    target : 目标文件路径（拼进 query 的辅助信号）
    top_k_sentences / top_k_context : HR 两个检索档的条数，刻意给小——
        检索段是「锦上添花」不是主粮，多条不如准。
    log_tag : 日志标记（build / recon / manual），便于区分调用来源。
    """
    query = make_query(task, target)
    record: Dict[str, Any] = {"kind": "hr_retrieve", "tag": log_tag, "ts": time.time(),
                              "query": query, "ok": False}
    t0 = time.time()
    if not query:
        record["reason"] = "空查询"
        _log_integration(record)
        return {"text": "", "ok": False, "reason": "空查询", "hits": []}
    try:
        res = _post("/retrieve", {
            "query": query,
            "top_k_sentences": int(top_k_sentences),
            "top_k_context": int(top_k_context),
        }, HR_TIMEOUT_SEC)
    except (urllib.error.URLError, OSError, ValueError) as e:
        record["reason"] = "HR 不可达：%s" % type(e).__name__
        record["error"] = str(e)[:200]
        record["sec"] = round(time.time() - t0, 3)
        _log_integration(record)
        return {"text": "", "ok": False, "reason": record["reason"], "hits": []}

    # ---- 解析（宽容口径：字段缺了不炸，取得到多少算多少）----
    sents: List[Dict[str, Any]] = list(res.get("key_sentences") or [])
    contexts: List[Dict[str, Any]] = list(res.get("full_contexts") or [])
    texts: List[str] = []
    hits_meta: List[Dict[str, Any]] = []
    seen: set = set()  # 去重：同句多路命中（域重叠/重复入库）只保留一份，重复只占预算
    for s in sents:
        t = str(s.get("text") or s.get("sentence") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        texts.append("- %s" % t)
        hits_meta.append({"source": "key_sentence", "text": t[:120],
                          "score": s.get("score")})
    for c in contexts:
        t = str(c.get("text") or c.get("context") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        # 完整上下文可能很长，只保留前 500 字（预算纪律）
        texts.append("· 背景：%s" % (t[:500] + ("…" if len(t) > 500 else "")))
        hits_meta.append({"source": "full_context", "text": t[:120],
                          "score": c.get("score")})

    record.update(ok=True, sec=round(time.time() - t0, 3),
                  hits=len(hits_meta), base=HR_BASE)
    _log_integration(record)
    if not texts:
        return {"text": "", "ok": True, "reason": "无命中", "hits": []}
    body = "\n".join(texts)
    return {"text": "【项目记忆（HR 分级检索）】\n%s" % body, "ok": True,
            "reason": "", "hits": hits_meta}


def enabled() -> bool:
    """HR 桥是否启用。环境变量 ANVIL_HR=0 可显式关（对照组/演示「不接 HR」用）。"""
    return os.environ.get("ANVIL_HR", "1") != "0"
