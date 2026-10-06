"""进程级单实例锁：防止同一个后台服务被起多份。

为什么需要它（2026-10-02 实测）：桌面 BAT 用 `start "" pythonw ...` 启动守护进程，
父 cmd 立刻退出，进程随即成为孤儿；而清理逻辑只认 pidfile 里的**一个** PID。一旦
pidfile 被后一次启动覆盖（或服务自身重写），先前那份就再也没人管——本次就漏了两份
已运行 190 分钟的轮询进程，两份都在读同一个群消息并各自回复。

pidfile 的根本缺陷：**它记录的是一个可能失真的快照**（文件还在但进程已死、或 PID 被
系统回收给了别的程序）。内核对象没有这个问题——持有者是内核里的句柄，进程无论正常
退出、崩溃还是被杀，资源都由内核立即释放。

实现按平台分两条路（都经过实测）：
  * Windows：命名互斥体 `CreateMutexW`。**不要用 msvcrt.locking 做这件事**——本机
    实测它对同一段的第二次加锁会成功，即根本没有跨进程互斥：第二个进程 `read` 被锁
    区域返回了 '0'、`locking` 直接成功（详见 2026-10-02 记录）。区域锁在这里不可信。
  * POSIX：`fcntl.flock`，作用于整个文件，语义明确。

用法：
    from app.single_instance import SingleInstance

    lock = SingleInstance("potato-feishu-poll")
    if not lock.acquire():
        log.warning("已有实例在运行（%s），本次退出", lock.holder_pid())
        return

不要给 run_server 用全局单例——它按端口区分实例（18080/18081/...），应当
`SingleInstance(f"potato-server-{port}")`，即"同端口唯一、不同端口可共存"。
"""

from __future__ import annotations

import atexit
import os
import tempfile

__all__ = ["SingleInstance"]

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """独占一个以 name 命名的内核级锁。acquire() 返回 False 表示已有实例持锁。"""

    def __init__(self, name: str, *, lock_dir: str | None = None) -> None:
        self.name = name
        directory = lock_dir or os.environ.get("POTATO_LOCK_DIR") or tempfile.gettempdir()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            directory = tempfile.gettempdir()
        # 仅 POSIX 用得到；Windows 走互斥体。保留一个可读的 owner 文件供日志展示。
        self.path = os.path.join(directory, f"{name}.lock")
        self.owner_path = os.path.join(directory, f"{name}.owner")
        self._fh = None
        self._handle = None

    # -- public ---------------------------------------------------------------
    def acquire(self) -> bool:
        """True = 拿到锁（或锁机制不可用时的放行）；False = 已被别的进程持有。

        机制本身不可用时**放行而不是阻塞**：一个诊断/清理工具不该因为平台限制把生产
        服务挡在门外。但"已被占用"是明确信号，绝不放过。
        """
        if self._fh is not None or self._handle is not None:
            return True
        ok = self._acquire_windows() if os.name == "nt" else self._acquire_posix()
        if ok:
            self._write_owner()
            atexit.register(self.release)
        return ok

    def release(self) -> None:
        if self._handle is not None:
            try:
                import ctypes

                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            except Exception:  # noqa: BLE001 — 释放失败不该让退出流程崩掉
                pass
            self._handle = None
        if self._fh is not None:
            try:
                self._unlock_posix(self._fh)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        try:
            os.remove(self.owner_path)
        except OSError:
            pass

    def holder_pid(self) -> int | None:
        """记录在 .owner 文件里的持锁者 PID，仅供日志展示。拿不到就返回 None。"""
        try:
            with open(self.owner_path, encoding="utf-8") as fh:
                raw = fh.read().strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    # -- windows --------------------------------------------------------------
    def _acquire_windows(self) -> bool:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool

        ctypes.set_last_error(0)
        # Local\ 前缀 = 当前登录会话内唯一，足够覆盖"同一用户重复点启动脚本"的场景，
        # 且不像 Global\ 那样需要额外权限。
        handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
        err = ctypes.get_last_error()
        if not handle:
            # 创建失败（极端情况）→ 放行，避免因锁机制问题让服务起不来。
            return True
        if err == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    # -- posix ----------------------------------------------------------------
    def _acquire_posix(self) -> bool:
        try:
            fh = open(self.path, "a+b")
        except OSError:
            return True
        try:
            self._lock_posix(fh)
        except OSError:
            try:
                fh.close()
            except OSError:
                pass
            return False
        self._fh = fh
        return True

    @staticmethod
    def _lock_posix(fh) -> None:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_posix(fh) -> None:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # -- shared ---------------------------------------------------------------
    def _write_owner(self) -> None:
        try:
            with open(self.owner_path, "w", encoding="utf-8") as of:
                of.write(str(os.getpid()))
        except OSError:
            pass
