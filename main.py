"""入口：单实例检查 → 加载配置 → 启动 HTTP 服务 → 运行系统托盘。"""

import logging
import os
import sys
import threading

import config as config_mod
import server

MUTEX_NAME = r"Local\LanShare.FileShare.SingleInstance"
# 须与 tray.py 的 RELAUNCH_EVENT_NAME 一致（第二实例 → 运行中实例 的通知通道）
RELAUNCH_EVENT_NAME = r"Local\LanShare.FileShare.Relaunch"


def _notify_running_instance():
    """通知已运行的实例“有人重复启动了”（由它弹 toast 提醒），成功返回 True。"""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 0x2 = EVENT_MODIFY_STATE（允许 SetEvent）
    h = kernel32.OpenEventW(0x2, False, RELAUNCH_EVENT_NAME)
    if not h:
        return False
    kernel32.SetEvent(h)
    kernel32.CloseHandle(h)
    return True


def _ensure_single_instance():
    r"""命名互斥体保证单实例：已有实例在运行时通知它弹 toast，随即退出。

    互斥体是内核对象，按名字全局定位（与 exe 所在路径无关，python/exe
    双开也能互斥）；进程退出时内核自动销毁，异常退出也不会残留。
    重复启动的处理：第二实例通过命名事件通知第一实例（由它弹 toast，
    应用身份与图标正确），自身立即退出——不弹窗、不需要用户点确认。
    """
    if os.name != "nt":
        return
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS：已有实例
        if not _notify_running_instance():
            # 通知不了（如首实例处于无托盘降级模式）→ 控制台提示兜底
            print("局域网文件共享已在运行中（请查看系统托盘图标）。", file=sys.stderr)
        sys.exit(0)


def _setup_file_log():
    r"""日志落文件：windowed exe 无控制台，stderr 里的报错全部不可见；
    落到 %APPDATA%\LanShare\server.log（滚动，上限 1MB x1）便于排障。"""
    try:
        from logging.handlers import RotatingFileHandler
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LanShare")
        os.makedirs(base, exist_ok=True)
        h = RotatingFileHandler(os.path.join(base, "server.log"),
                                maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(h)
    except Exception as e:
        logging.getLogger("lan_share").warning("文件日志初始化失败: %s", e)


def main():
    _ensure_single_instance()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _setup_file_log()
    log = logging.getLogger("lan_share")

    cfg = config_mod.load()
    state = server.AppState(cfg)

    # 组装：先建服务管理器，再把 app 绑定进去（避免循环依赖）
    service = server.ServerManager()
    app = server.create_app(state, service=service)
    service.app = app

    try:
        service.start(cfg["port"])
    except OSError as e:
        log.error("HTTP 服务启动失败（端口 %s 可能被占用）: %s", cfg["port"], e)
        print(f"[启动失败] 端口 {cfg['port']} 可能被占用：{e}", file=sys.stderr)
        print("程序仍将以托盘模式运行，可在管理页修改端口后恢复服务。", file=sys.stderr)

    # 托盘需要主线程跑消息循环；不可用时退化为前台阻塞模式
    try:
        import tray
    except ImportError as e:
        log.error("托盘模块不可用（%s），服务以前台模式运行，Ctrl+C 退出", e)
        print("", file=sys.stderr)
        print("[警告] 托盘模块不可用（%s）" % e, file=sys.stderr)
        print("服务已以前台模式运行：关闭本窗口或按 Ctrl+C 即停止。", file=sys.stderr)
        print("如需系统托盘图标，请安装依赖：pip install pystray pillow", file=sys.stderr)
        print("", file=sys.stderr)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            service.stop()
        return

    try:
        tray.run_tray(state, service)  # 阻塞直至托盘退出
    except Exception as e:
        log.error("托盘运行异常（%s），服务以前台模式继续运行，Ctrl+C 退出", e)
        print("", file=sys.stderr)
        print("[警告] 托盘运行异常（%s）" % e, file=sys.stderr)
        print("服务已以前台模式运行：关闭本窗口或按 Ctrl+C 即停止。", file=sys.stderr)
        print("", file=sys.stderr)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            service.stop()


if __name__ == "__main__":
    main()
