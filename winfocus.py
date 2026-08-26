"""Windows 窗口置前：后台进程（HTTP 服务线程）启动的新窗口默认拿不到前台焦点，
会被 Windows 前台锁压在当前前台窗口（浏览器）后面。这里组合多种经典技术并
**立即验证 + 重试**，直到目标窗口真正成为前台：

    模拟 Alt 按下/抬起（keybd_event，尝试解除前台锁）
    + AttachThreadInput（把本线程输入队列挂到当前前台线程，共享其前台权）
    + BringWindowToTop / SetForegroundWindow（+ SwitchToThisWindow 兜底）
    → GetForegroundWindow() == 目标 才算成功

纯 ctypes 实现，无新依赖；非 Windows 平台导入后各函数退化为直接执行 launch 不置前。
"""

import ctypes
import logging
import time
from ctypes import wintypes

log = logging.getLogger("lan_share.winfocus")

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _VK_MENU = 0x12              # Alt 键
    _KEYEVENTF_KEYUP = 0x0002
    _SW_RESTORE = 9
except (OSError, AttributeError, ValueError):   # 非 Windows：整体降级
    _user32 = None
    _kernel32 = None


def visible_windows():
    """枚举可见、有标题的顶层窗口 → {hwnd: 标题}。"""
    if _user32 is None:
        return {}
    result = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if _user32.IsWindowVisible(hwnd):
                n = _user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _user32.GetWindowTextW(hwnd, buf, n + 1)
                    result[hwnd] = buf.value
        except Exception:   # 枚举中出错只跳过该窗口
            pass
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception as e:
        log.warning("EnumWindows 失败: %s", e)
    return result


def _focus_once(hwnd):
    """单次置前尝试（Alt + AttachThreadInput + 各 API 组合）。"""
    # Alt 模拟：部分场景可解除前台锁
    _user32.keybd_event(_VK_MENU, 0, 0, 0)
    _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    # 把本线程输入队列挂到当前前台线程上，共享其前台激活权
    fg = _user32.GetForegroundWindow()
    fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    my_tid = _kernel32.GetCurrentThreadId()
    attached = bool(fg_tid) and fg_tid != my_tid and _user32.AttachThreadInput(my_tid, fg_tid, True)
    try:
        if _user32.IsIconic(hwnd):            # 仅最小化时还原，避免把最大化窗口还原
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(my_tid, fg_tid, False)


def focus_window(hwnd, attempts=8):
    """把窗口带到前台，立即验证（GetForegroundWindow==目标），失败重试。

    单纯调用 SetForegroundWindow 常因前台锁静默失败（返回假成功或 0），
    因此以"验证到的真实状态"为准；多次重试覆盖窗口激活延迟等瞬时失败。
    """
    if _user32 is None or not hwnd:
        return False
    for _ in range(attempts):
        try:
            _focus_once(hwnd)
            if _user32.GetForegroundWindow() == hwnd:
                return True
            try:   # 老 API 兜底（未文档化但各版本 Windows 均可用）
                _user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
            if _user32.GetForegroundWindow() == hwnd:
                return True
        except Exception as e:
            log.warning("置前窗口失败(hwnd=%s): %s", hwnd, e)
            return False
        time.sleep(0.08)
    return False


def launch_and_focus(launch, title_hint=None, timeout=2.0, settle=0.25):
    """先快照窗口 → launch() 启动 → 轮询等新窗口出现并置前（带验证与重试）。

    - 出现新窗口后再等 settle 秒（启动画面 → 主窗口的常见序列）；
    - 新窗口置前失败（重试后仍未到前台）→ 按 title_hint 匹配已有窗口再试
      （explorer 常复用已有窗口，其标题 = 文件夹名）；
    - 均失败 → 返回 False（尽力而为，不影响启动本身）。
    """
    before = visible_windows()
    launch()
    if _user32 is None:
        return False
    candidates = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.1)
        now = visible_windows()
        if [h for h in now if h not in before]:
            time.sleep(settle)                # 等可能随后出现的主窗口
            now = visible_windows()
            candidates = [h for h in now if h not in before]
            break
    for h in candidates:
        if focus_window(h):
            return True
    if title_hint:                            # 兜底：explorer 复用已有窗口（标题 = 文件夹名）
        for h, t in visible_windows().items():
            if t == title_hint and focus_window(h):
                return True
    return False
