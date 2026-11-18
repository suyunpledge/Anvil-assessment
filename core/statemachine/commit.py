"""CLI 与服务共用的单文件提交：路径、双版本、进程内同目标互斥。"""

import hashlib
import os
import stat
import tempfile
import threading
import weakref
from pathlib import Path

from tools import ToolContext, VersionConflict, content_version

_locks = weakref.WeakValueDictionary()
_guard = threading.Lock()


def commit_verified(root, target, staged, real_version, staged_version):
    if not real_version or not staged_version:
        raise VersionConflict("缺少验证版本，拒绝落盘；请重新建单")
    ctx = ToolContext(root=os.path.abspath(root), target=target)
    real = ctx.resolve(target, for_write=True)
    key = os.path.normcase(os.path.realpath(real))
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        data = Path(staged).read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != staged_version:
            raise VersionConflict("暂存结果在验证后被修改，拒绝落盘")

        def check_target():
            ctx.resolve(target, for_write=True)
            try:
                current = content_version(real)
            except OSError as exc:
                raise VersionConflict("目标文件在工单开始后已被修改或删除，拒绝覆盖") from exc
            if current != real_version:
                raise VersionConflict("目标文件在工单开始后已被修改，拒绝覆盖；请重新建单")

        check_target()
        mode = stat.S_IMODE(os.stat(real).st_mode)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".anvil-commit-", dir=os.path.dirname(real), delete=False) as f:
                temp_path = f.name
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_path, mode)
            # 准备写入期间的变动也必须拒绝；替换的是已核验的字节，不再重读暂存文件。
            check_target()
            os.replace(temp_path, real)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
    return real
