# -*- coding: utf-8 -*-
"""runner.py —— 工单运行器（交付件 B 的内核，与 HTTP 解耦）。

把「跑一个工单」这件事从 HTTP 里拆出来，是为了三件事：

  1. 能离线单测（用假适配器跑，不调模型、不花额度）；
  2. 能换入口（CLI / HTTP / 将来的其它前端都调同一份逻辑）；
  3. **落盘必须经人确认**——这是 IDE 一期五条验收里唯一涉及"不可逆"的一条，
     所以它必须是运行器层面的机制，不能只靠前端自觉。
     做法：状态机以「不落盘」模式跑（code 类 `commit=False`），
     结果留在暂存目录里；收到 `confirm` 才把暂存内容复制回真实文件。

两类工单：
  · code  —— 五节点链（读 → 改 → 语法 → 类型 → 测试 gate），改动在暂存区
  · tidy  —— 整理链（扫 → 规划 → 方案闸 → 执行 → 复核闸），默认 dry_run
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.paths import ensure_core_on_path, runtime_path  # noqa: E402
from integration.scheduler import ModelCancelled, model_scope  # noqa: E402
from integration.config import default_adapter  # noqa: E402
from service.events import EventBuffer  # noqa: E402

ensure_core_on_path()

import engine as _engine  # noqa: E402  (core/statemachine)
import tidy as TD  # noqa: E402
import tools as TOOLS  # noqa: E402
from contract import WorkOrder  # noqa: E402
from tidy import TidyOrder  # noqa: E402

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_AWAITING_CONFIRM = "awaiting_confirm"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

#: 终态/停等态：取消这些单的含义是"用户放弃这单"，状态应转 cancelled。
#: 依据（2026-09-30 实测）：模型反问 needs_input 后用户点"取消"，状态纹丝不动，
#: 面板上永远挂着一条 needs_input——用户看不到自己的操作产生了任何效果。
STATUS_NEEDS_INPUT_LIKE = ("needs_input", "escalated", "awaiting_confirm")

#: 常见整理目录的语义描述（与 CLI 的 _DEFAULT_HINTS 保持同一口径）
_DEFAULT_HINTS: Dict[str, str] = {
    "文档": "合同、报告、说明书、笔记、会议纪要等文字性文档",
    "票据": "发票、报销单、记账凭证、税额、开票与报销资料",
    "合同": "合同、协议、甲方乙方、租期、违约与保密条款",
    "照片": "照片、图像、摄影、光圈快门像素等拍摄参数",
    "图片": "照片、截图、图像、设计稿、插画",
    "压缩包": "压缩归档文件、备份包、资源包",
    "表格": "表格、数据表、清单、统计与预算数据",
    "视频": "视频、录像、影片、剪辑素材",
    "音频": "音频、录音、音乐、语音素材",
    "代码": "源代码、脚本、程序文件、项目源码",
    "安装包": "安装程序、可执行文件、软件安装包",
}


class Cancelled(Exception):
    """协作式取消：适配器在每次调用前检查标记，命中就抛。"""


class CancellableAdapter:
    """包一层适配器，提供「在两个节点之间可被打断」的能力。

    模型请求前后均检查取消；服务层在实际落盘处再次检查并串行处理确认和取消。
    """

    def __init__(self, inner: Any, flag: threading.Event) -> None:
        self._inner = inner
        self._flag = flag
        self.spec = getattr(inner, "spec", None)
        self.model = getattr(inner, "model", None)
        self.calls_used = 0

    @property
    def profile(self):
        return getattr(self._inner, "profile", None)

    @property
    def runtime_toolsets(self):
        return self._inner.runtime_toolsets

    @runtime_toolsets.setter
    def runtime_toolsets(self, value):
        self._inner.runtime_toolsets = value

    def _check(self) -> None:
        if self._flag.is_set():
            raise Cancelled("已被取消")

    def fill_slot(self, *a, **kw):
        self._check()
        from integration.config import settings
        from integration.trace import record
        if self.calls_used >= settings()["limits"]["model_calls"]:
            raise RuntimeError("工单模型调用预算已用尽")
        self.calls_used += 1
        kw.setdefault("timeout", settings()["limits"]["model_timeout"])
        node = kw.get("node") or (a[0] if a else "")
        messages = kw.get("messages") or (a[1] if len(a) > 1 else [])
        registry = kw.get("registry") or (a[2] if len(a) > 2 else {})
        tools_for = getattr(self._inner, "tools_for", None)
        schemas = tools_for(node, registry) if tools_for else []
        record("context", node=node, model=self.model, call=self.calls_used,
               messages=messages, tools=schemas)
        result = self._inner.fill_slot(*a, **kw)
        self._check()
        record("adapter_result", node=node, model=self.model, call=self.calls_used,
               result_kind=result.kind, ok=result.ok, calls=result.calls, error=result.error)
        return result

    def ask(self, *a, **kw):
        self._check()
        from integration.config import settings
        from integration.trace import record
        if self.calls_used >= settings()["limits"]["model_calls"]:
            raise RuntimeError("工单模型调用预算已用尽")
        self.calls_used += 1
        kw.setdefault("timeout", settings()["limits"]["model_timeout"])
        record("context", node="plan", model=self.model, call=self.calls_used,
               messages=kw.get("messages") or (a[0] if a else []), tools=[])
        result = self._inner.ask(*a, **kw)
        self._check()
        record("adapter_result", node="plan", model=self.model, call=self.calls_used,
               result_kind=result.kind, ok=result.ok, error=result.error)
        return result

    def record(self, *a, **kw):
        try:
            return self._inner.record(*a, **kw)
        except Exception:
            return None

    def health(self):
        return self._inner.health()


@dataclass
class RunnerOpts:
    """建单参数。``kind`` 决定走哪条链。"""
    kind: str = "code"                     # code | tidy
    task: str = ""
    workdir: str = ""                      # code：沙箱根；tidy：要整理的目录
    target: str = ""                       # code：目标文件（相对 workdir）
    test_path: str = ""                    # code：测试文件
    adapter: str = field(default_factory=default_adapter)
    model: str = ""                        # 覆盖模型名（画像不变）
    mode: str = "dry_run"                  # tidy：dry_run | execute
    dirs: List[str] = field(default_factory=list)     # tidy：允许归入的目录
    semantic: bool = False                 # tidy：用本地嵌入给建议
    max_repair_rounds: int = 3
    allow_delete: bool = False             # ★ tidy：开启删除（会强制人确认 + 自动备份）
    require_confirm: bool = True           # ★ 落盘必须经人确认
    visual_tasks: bool = False             # ★ code：装载 visual_query（工单级，默认不暴露）
    wo_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        return d


def build_adapter(opts: RunnerOpts):
    """构造真实适配器，并把它的 transport 路由到本地网关。

    为什么必须过网关：画像（think 兼容、ctx 夹取）、审计、出网熔断都挂在那里。
    直连 Ollama 会让这三条承诺静默失效——2026-09-28 端到端跑时就撞上了（工单跑完、审计空）。
    ``LOCAL_LLM_VIA=direct`` 可显式指定直连（仅用于排障）。
    """
    from adapter_factory import build_adapter as _ba

    from integration.config import settings
    cfg = settings()
    kind = opts.adapter or default_adapter()
    model = opts.model or (cfg["models"]["code"] if kind == default_adapter() else None)
    # 工具成功率和节点覆盖按模型隔离，避免切模型时继承另一模型的运行画像。
    profile_id = hashlib.sha256((kind + "\0" + (model or "")).encode("utf-8")).hexdigest()[:20]
    ad = _ba(kind=kind, model=model, profile_path=runtime_path("model-profiles", profile_id + ".json"))
    ad.max_output_tokens = cfg["limits"]["output_tokens"]
    ad.transport_retries = 0
    ad._retries_by_node = {}
    via = (os.environ.get("LOCAL_LLM_VIA") or "gateway").lower()
    if via == "native":
        from integration.models import AgentTransport
        ad.transport = AgentTransport()
        return ad
    if via == "direct":
        return ad
    gw = os.environ.get("LOCAL_LLM_GATEWAY", "http://127.0.0.1:8080")
    try:
        from gateway.client import route_adapter_through_gateway
        route_adapter_through_gateway(ad, gw, strict=os.environ.get("LOCAL_LLM_VIA") != "direct")
    except Exception as e:
        # 不静默降级：要么过网关，要么把问题说清楚（可一键切 direct 排障）
        raise RuntimeError(
            "无法把模型调用路由到网关：%s\n"
            "请先起网关（python gateway/app.py），或临时用 LOCAL_LLM_VIA=direct 直连排障。"
            % e)
    return ad


class WorkOrderRunner:
    """一次工单运行。线程安全的事件队列，供 SSE 消费。"""

    def __init__(self, opts: RunnerOpts, adapter: Any = None) -> None:
        self.opts = opts
        self.wo_id = opts.wo_id or ("wo-%s" % uuid.uuid4().hex[:10])
        self.adapter = adapter
        self.events = EventBuffer()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status = "pending"
        self.result: Dict[str, Any] = {}
        self.created = time.time()
        self.error = ""
        self._lock = threading.RLock()
        self._confirm_lock = threading.RLock()
        self._stage_dir = ""
        self._staged_target = ""
        self._staged_content = ""
        self._tidy_plan: Dict[str, Any] = {}
        self._tidy_scan: Dict[str, Any] = {}
        self._tidy_order = None
        self.timeline: List[Dict[str, Any]] = []

    # ---------------- 事件 ----------------
    def emit(self, kind: str, **kw: Any) -> None:
        # 注意：kw 里**不能**再出现 ``kind``（会与位置参数撞车：
        # `emit() got multiple values for argument 'kind'`）。工单类别请用 ``wo_kind``。
        ev = {"ts": time.time(), "kind": kind, "wo": self.wo_id}
        ev.update(kw)
        from integration.trace import record
        record("event", wo_id=self.wo_id, event=ev)
        with self._lock:
            ev = self.events.put(ev)
            if kind in ("node", "gate", "violation", "status", "error", "done", "progress"):
                self.timeline.append(ev)
                del self.timeline[:-80]

    def drain(self, timeout: float = 0.0) -> List[Dict[str, Any]]:
        out = []
        while True:
            try:
                out.append(self.events.get(timeout=timeout if not out else 0))
            except queue.Empty:
                break
        return out

    # ---------------- 生命周期 ----------------
    def start(self) -> str:
        self._thread = threading.Thread(target=self._run_guarded, daemon=True,
                                        name="wo-%s" % self.wo_id)
        self._thread.start()
        return self.wo_id

    def cancel(self) -> bool:
        """取消。对**已终态但未确认**的单（如 needs_input：模型在反问、等用户答复），
        取消的含义是"用户放弃这单"，状态应转为 cancelled——否则面板上它会永远
        停在 needs_input，用户点丢弃却看不到任何变化（2026-09-30 实测暴露）。
        """
        with self._confirm_lock, self._lock:
            if self.status == STATUS_CANCELLED:
                return True
            if self.status in (STATUS_DONE, STATUS_FAILED, "security_abort"):
                return False
            self._cancel.set()
            if self.status in STATUS_NEEDS_INPUT_LIKE or not self.alive:
                self.status = STATUS_CANCELLED
                self.emit("status", status=STATUS_CANCELLED, reason="用户放弃")
            else:
                self.emit("status", status="cancelling")
            return True

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def request_stop(self) -> None:
        """关闭服务时只发出协作停止信号，不等待提交锁或磁盘留痕。

        已进入的提交仍完成；其余节点在现有取消检查点退出。
        用户主动取消继续使用 cancel()，保留其状态与返回值语义。
        """
        self._cancel.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------- 执行 ----------------
    def _run_guarded(self) -> None:
        from integration.trace import scope
        try:
            with scope(wo_id=self.wo_id, project=os.path.realpath(self.opts.workdir)), \
                    model_scope(cancel=self._cancel):
                self._run_scoped()
        finally:
            self.events.finish()

    def _run_scoped(self) -> None:
        try:
            if self._cancel.is_set():
                raise Cancelled("已被取消")
            if self.opts.kind == "tidy":
                self._run_tidy()
            else:
                self._run_code()
        except (Cancelled, ModelCancelled):
            with self._lock:
                self.status = STATUS_CANCELLED
            self.emit("status", status=STATUS_CANCELLED, reason="cancelled")
        except Exception as e:                       # 兜底：任何异常都不许让服务静默
            with self._lock:
                self.status = STATUS_FAILED
                self.error = "%s: %s" % (type(e).__name__, e)
            self.emit("error", message=self.error)
            self.emit("status", status=STATUS_FAILED)

    # ---- code 链 ----
    def _run_code(self) -> None:
        with self._lock:
            self.status = STATUS_RUNNING
        self.emit("status", status=STATUS_RUNNING, wo_kind="code")
        base = self.adapter if self.adapter is not None else build_adapter(self.opts)
        self.adapter = base
        ad = CancellableAdapter(base, self._cancel)
        workdir = os.path.abspath(self.opts.workdir)
        real_path = os.path.join(workdir, self.opts.target.replace("\\", "/"))
        try:
            version_at_start = TOOLS.content_version(real_path) if os.path.exists(real_path) else ""
        except Exception:
            version_at_start = ""
        initial_version = version_at_start
        wo = WorkOrder(wo_id=self.wo_id, task=self.opts.task,
                       workdir=workdir,
                       target=self.opts.target,
                       test_path=self.opts.test_path or None)
        wo.constraints["max_repair_rounds"] = int(self.opts.max_repair_rounds)
        # 考核接入（2026-10-17）：视觉工单开关透传（引擎按它装载 visual_query）
        wo.constraints["visual_tasks"] = bool(self.opts.visual_tasks)
        # ★ 外部审查 #6：把上下文层接进真实工单链。
        #   注意它工作在**暂存区**（src 是 staging 目录），所以要拿 staging 路径。
        def _ctx_provider(task: str, target: str):
            """把上下文层接进真实工单链（外部审查 #6）。

            注意：状态机改的是**暂存区里的副本**（self.staging 在 _stage 之后才有值），
            所以这里从状态机实例上取 staging 路径，保证上下文描述的是「将要被改的那份文件」。
            """
            try:
                from context import builder as _ctxb
                spec = getattr(ad, "spec", None)
                staging = getattr(sm_ref, "staging", "") or None
                return _ctxb.build_workorder_context(workdir=workdir, target=target,
                                                     task=task, spec=spec,
                                                     staging_root=staging)
            except Exception as e:
                raise RuntimeError("上下文层调用失败：%s" % e)

        sm = _engine.WorkOrderStateMachine(
            ad, wo,
            staging_root=runtime_path("staging"),
            runlog_dir=runtime_path("runs"),
            # 所有服务写入都在运行器提交锁内执行，免确认也不能绕过取消检查。
            commit=False,
            initial_version=initial_version or None,
            context_provider=_ctx_provider,
            verbose=False,
            on_event=lambda m: self.emit("progress", node=wo.node, message=m))
        sm_ref = sm          # 让上面的闭包能拿到 staging 路径

        # 取消：包成 Terminal 让引擎能正常收尾（它的 run() 只捕获 Terminal）
        try:
            res = sm.run()
        except Cancelled:
            raise
        except _engine.Terminal as t:
            with self._lock:
                self.status = t.status
            self.emit("status", status=t.status, error=t.message)
            return

        # 把关键信息推成事件
        for g in wo.gate_results:
            self.emit("gate", gate=g["gate"], signal=g["signal"],
                      issues=[i.get("message", "") for i in g.get("issues", [])][:3])
        for v in wo.violations:
            self.emit("violation", rule=v.get("rule"), detail=v.get("detail", "")[:200])

        self._stage_dir = res.staging
        self.error = res.error or ""
        self._staged_target = os.path.join(res.staging, self.opts.target.replace("\\", "/"))
        try:
            with open(self._staged_target, "r", encoding="utf-8") as f:
                self._staged_content = f.read()
        except OSError:
            self._staged_content = ""

        # 暂存版本（用于比对）与真实版本（用于确认时再校验）
        try:
            staged_version = TOOLS.content_version(self._staged_target) \
                if os.path.isfile(self._staged_target) else ""
        except Exception:
            staged_version = ""

        self.result = {
            # ★ 字段名故意叫 run_status：快照顶层的 status 是**运行器状态**
            #   （running / awaiting_confirm / done …），不能被结果集里的同名键覆盖。
            #   这个撞名 2026-09-28 端到端跑时暴露：工单实际在等确认，快照却报 done。
            "run_status": res.status,
            "diff": res.diff,
            "staged_content": self._staged_content,
            "real_path": real_path,
            "real_version": version_at_start,
            "staged_version": staged_version,
            "gates": wo.gate_results,
            "violations": wo.violations,
            "history": [s.to_dict() for s in wo.history],
            "runlog": res.runlog_path,
            "elapsed": wo.artifacts.get("elapsed_sec"),
            "questions": wo.questions,
            "context": wo.artifacts.get("context") or {},
        }
        with self._confirm_lock:
            ad._check()
            if res.status == "done":
                with self._lock:
                    self.status = STATUS_AWAITING_CONFIRM
                if self.opts.require_confirm:
                    self.emit("status", status=STATUS_AWAITING_CONFIRM,
                              diff=res.diff, needs_confirm=True)
                else:
                    committed = self._confirm_code()
                    if not committed["ok"]:
                        raise RuntimeError(committed["error"])
            else:
                with self._lock:
                    self.status = res.status
                self.emit("status", status=res.status, diff=res.diff)
            self.emit("done", status=self.status)

    def _remember_success(self) -> None:
        from context.hr_bridge import enabled
        if not enabled():
            return
        from integration.memory import enqueue
        try:
            text = "已确认并通过验证的工单 %s。任务：%s。目标：%s。验证：%s。修改摘要：%s" % (
                self.wo_id, self.opts.task[:1000], self.opts.target,
                json.dumps(self.result.get("gates", []), ensure_ascii=False)[:1200],
                str(self.result.get("diff", ""))[:2500])
            item = enqueue(self.opts.workdir, self.wo_id, text)
            with self._lock:
                self.result["memory_outbox"] = item
        except Exception as exc:
            self.emit("error", message="修改已保存，但记忆排队失败：%s" % exc)

    # ---- tidy 链 ----
    def _run_tidy(self) -> None:
        with self._lock:
            self.status = STATUS_RUNNING
        self.emit("status", status=STATUS_RUNNING, wo_kind="tidy")
        base = self.adapter if self.adapter is not None else build_adapter(self.opts)
        self.adapter = base
        ad = CancellableAdapter(base, self._cancel)
        hints = {}
        if self.opts.semantic and self.opts.dirs:
            # 与 CLI 保持同一套描述表；写在这里是避免服务依赖 gateway 模块
            hints = {d: _DEFAULT_HINTS.get(d, "%s 类文件" % d) for d in self.opts.dirs}
        # 人确认模式下先只出方案（dry_run），确认时再执行
        mode = "dry_run"
        # ★ 2026-10-03：删除能力由工单显式声明。allow_delete 为真时，方案闸才放行
        #   delete 动作；真正的「同意」由服务端 require_confirm + /confirm 接口保证，
        #   「备份」由 safeops 在 apply 阶段保证（写-校验-删）。
        order = TidyOrder(wo_id=self.wo_id, task=self.opts.task,
                          root=os.path.abspath(self.opts.workdir), mode=mode,
                          target_dirs=list(self.opts.dirs),
                          constraints={"allow_delete": bool(self.opts.allow_delete),
                                       "overwrite": False,
                                       "max_repair_rounds": self.opts.max_repair_rounds})
        self._tidy_order = order
        from tidy_engine import TidyStateMachine
        sm = TidyStateMachine(ad, order, runlog_dir=runtime_path("runs"), verbose=False,
                              on_event=lambda m: self.emit("progress", message=m),
                              semantic=bool(self.opts.semantic), category_hints=hints)
        try:
            res = sm.run()
        except Cancelled:
            raise
        for g in res.gates:
            self.emit("gate", gate=g["gate"], signal=g["signal"],
                      issues=[i.get("message", "") for i in g.get("issues", [])][:3])
        self._tidy_plan = res.plan
        # ★ 外部审查 #5：确认前必须让人看到**具体要移动什么**。
        #   原来只发了个数量（moves: len(...)），扩展拿不到清单，等于盲确认。
        plan_moves = [{"src": m.get("src"), "dst": m.get("dst"),
                       "reason": m.get("reason", "")}
                      for m in (res.plan or {}).get("moves", [])]
        self.result = {
            "run_status": res.status, "kind": "tidy", "plan": res.plan,
            "applied": res.applied, "gates": res.gates, "history": res.history,
            "runlog": res.runlog_path, "elapsed": res.elapsed,
            "semantic": getattr(sm, "semantic_notes", {}),
            "moves": plan_moves,
        }
        with self._confirm_lock:
            ad._check()
            if res.status == "done" and res.plan.get("moves"):
                self._tidy_scan = TD.scan_root(os.path.abspath(self.opts.workdir), order.filters)
                with self._lock:
                    self.status = STATUS_AWAITING_CONFIRM
                if not self.opts.require_confirm and self.opts.mode == "execute":
                    self._confirm_tidy()
                else:
                    self.emit("status", status=STATUS_AWAITING_CONFIRM, needs_confirm=True,
                              moves=len(plan_moves), plan=plan_moves)
            else:
                with self._lock:
                    self.status = res.status
                self.emit("status", status=res.status)
            self.emit("done", status=self.status)

    # ---------------- 确认落盘 ----------------
    def confirm(self) -> Dict[str, Any]:
        """人确认后落盘。code 类复制暂存文件；tidy 类执行方案并复核。"""
        with self._confirm_lock:
            if self._cancel.is_set():
                return {"ok": False, "error": "工单已取消，不能确认"}
            if self.status != STATUS_AWAITING_CONFIRM:
                return {"ok": False, "error": "当前状态不需要确认：%s" % self.status}
            if self.opts.kind == "tidy":
                return self._confirm_tidy()
            return self._confirm_code()

    def _confirm_code(self) -> Dict[str, Any]:
        real = self.result.get("real_path") or ""
        if not real or not os.path.isfile(self._staged_target):
            return {"ok": False, "error": "找不到暂存结果"}
        from commit import commit_verified
        try:
            real = commit_verified(self.opts.workdir, self.opts.target, self._staged_target,
                                   self.result.get("real_version"), self.result.get("staged_version"))
        except (OSError, TOOLS.ToolError) as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            self.status = STATUS_DONE
        self.emit("applied", path=real)
        self.emit("status", status=STATUS_DONE)
        self._remember_success()
        return {"ok": True, "path": real}

    def _confirm_tidy(self) -> Dict[str, Any]:
        root = os.path.abspath(self.opts.workdir)
        # 等待确认期间，目录可能被改动，授权及路径门必须重新检查。
        order = self._tidy_order or TidyOrder(wo_id=self.wo_id, task=self.opts.task, root=root,
                                             target_dirs=list(self.opts.dirs),
                                             constraints={"allow_delete": self.opts.allow_delete, "overwrite": False})
        gate = TD.check_plan(root, self._tidy_plan, order)
        if not gate.ok:
            return {"ok": False, "error": "整理方案在确认前已失效，请重新建单",
                    "verify": gate.to_dict()}
        scan = self._tidy_scan or TD.scan_root(root)
        applied = TD.apply_plan(root, self._tidy_plan, log=[])
        vg = TD.verify_after(root, self._tidy_plan, scan, applied)
        self.emit("gate", gate="verify_gate", signal=vg.signal,
                  issues=[i.message for i in vg.issues][:3])
        self.result["applied"] = applied
        self.result["verify"] = vg.to_dict()
        with self._lock:
            self.status = "done" if vg.ok else "escalated"
        self.emit("applied", moved=len(applied.get("moved", [])), ok=vg.ok)
        self.emit("status", status=self.status)
        return {"ok": vg.ok, "moved": len(applied.get("moved", [])),
                "deleted": len(applied.get("deleted", [])),
                "backups": [d.get("backup") for d in applied.get("deleted", [])],
                "verify": vg.to_dict()}

    # ---------------- 对外快照 ----------------
    def snapshot(self) -> Dict[str, Any]:
        """对外快照。**顶层 status 永远是运行器状态**，不可被结果集覆盖。"""
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> Dict[str, Any]:
        d = {
            "wo_id": self.wo_id, "status": self.status, "kind": self.opts.kind,
            "target": self.opts.target, "adapter": self.opts.adapter,
            "model": self.opts.model or getattr(self.adapter, "model", ""),
            "task": self.opts.task, "created": self.created, "error": self.error,
            "elapsed": round(time.time() - self.created, 2),
            "timeline": self.timeline[-80:],
        }
        for k, v in self.result.items():
            if k in ("real_path", "status"):
                continue
            d[k] = v
        return copy.deepcopy(d)


# ============================================================
# 注册表（按 wo_id 管理）
# ============================================================
class RegistryBusy(RuntimeError):
    pass


class RunnerRegistry:
    def __init__(self, max_active=None, max_retained=None) -> None:
        self._items: Dict[str, WorkOrderRunner] = {}
        self._lock = threading.Lock()
        from integration.config import settings
        limits = settings()["service"]
        self.max_active = max_active or limits["max_active"]
        self.max_retained = max_retained or limits["max_retained"]
        self._closing = False

    def add(self, runner: WorkOrderRunner) -> str:
        with self._lock:
            self._admit(runner)
            self._items[runner.wo_id] = runner
        return runner.wo_id

    def _admit(self, runner, launching=False):
        if self._closing:
            raise RegistryBusy("服务正在停止，暂不接收新工单")
        if runner.wo_id in self._items:
            raise ValueError("工单标识已存在")
        active = sum(r.alive or r.status in ("pending", STATUS_RUNNING) for r in self._items.values())
        if launching and active >= self.max_active:
            raise RegistryBusy("运行工单达到上限 %d，请稍后重试" % self.max_active)
        while len(self._items) >= self.max_retained:
            removable = next((key for key, r in self._items.items()
                              if not r.alive and r.status in
                              (STATUS_DONE, STATUS_CANCELLED, STATUS_FAILED, "security_abort")), None)
            if removable is None:
                raise RegistryBusy("待处理工单达到保留上限，请先确认或取消已有工单")
            del self._items[removable]

    def get(self, wo_id: str) -> Optional[WorkOrderRunner]:
        with self._lock:
            return self._items.get(wo_id)

    def all(self) -> List[WorkOrderRunner]:
        with self._lock:
            return list(self._items.values())

    def create(self, opts: RunnerOpts, adapter: Any = None) -> WorkOrderRunner:
        r = WorkOrderRunner(opts, adapter=adapter)
        with self._lock:
            self._admit(r, launching=True)
            self._items[r.wo_id] = r
            try:
                r.start()
            except Exception:
                del self._items[r.wo_id]
                raise
        return r

    def shutdown(self, timeout=2):
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            if self._closing:
                return sum(r.alive for r in self._items.values())
            self._closing = True
            runners = list(self._items.values())
        for runner in runners:
            if runner.alive:
                runner.request_stop()
        for runner in runners:
            runner.join(max(0, deadline - time.monotonic()))
        return sum(r.alive for r in runners)

    def open(self):
        with self._lock:
            if self._closing and any(r.alive for r in self._items.values()):
                raise RegistryBusy("上一轮后台工单尚未退出，不能重新启动服务")
            self._closing = False


def list_presets() -> Dict[str, Any]:
    """列出可用适配层与模型（供扩展的下拉框用）。"""
    from mythos_core.profiles import ALL_PROFILES, UNUSABLE_TOOL_MODELS, load_generated_profiles
    load_generated_profiles()
    from integration.config import settings
    default = default_adapter()
    return {
        "profiles": [{"key": k, "model": settings()["models"]["code"] if k == default else p.model, "label": p.label,
                      "sends_think": p.sends_think, "ctx_max": p.ctx_max,
                      "notes": (p.notes or "")[:120]}
                     for k, p in sorted(ALL_PROFILES.items())],
        "unusable": UNUSABLE_TOOL_MODELS,
        "default": default,
    }
