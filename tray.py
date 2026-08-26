"""系统托盘（pystray）：服务启停、打开页面、退出。"""

import logging
import os
import shutil
import sys
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw

import network

log = logging.getLogger("lan_share.tray")

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _icon_image():
    """托盘图标：exe 运行读内嵌资源（_MEIPASS/app.ico），源码运行读项目根 app.ico。"""
    if getattr(sys, "frozen", False):
        ico = os.path.join(sys._MEIPASS, "app.ico")
    else:
        ico = os.path.join(BASE_DIR, "app.ico")
    if os.path.isfile(ico):
        try:
            return Image.open(ico).convert("RGBA")
        except Exception as e:
            log.warning("读取 app.ico 失败，使用默认托盘图标: %s", e)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 12, 58, 52), radius=12, fill=(37, 99, 235, 255))
    w = (255, 255, 255, 255)
    # 左向箭头（上）
    # d.line((26, 26, 44, 26), fill=w, width=5)
    # d.polygon([(18, 26), (28, 20), (28, 32)], fill=w)
    # 右向箭头（下）
    d.line((20, 38, 38, 38), fill=w, width=5)
    d.polygon([(46, 38), (36, 32), (36, 44)], fill=w)
    return img


AUMID = "LanShare.FileShare"


def setup_app_identity():
    r"""设置通知应用身份（AUMID + HKCU 注册表登记 DisplayName/IconUri）。

    实测结论（notify_test 四模式对照实验）：
    - 不设置：通知头部显示 exe 文件名 + exe 图标（源码运行显示“Python”）
    - 仅设 AUMID 不注册：头部退化为 AUMID 原文且无图标（更糟，务必避开）
    - AUMID + 注册表登记：头部稳定显示自定义名称与图标（采用）
    """
    import ctypes
    import winreg

    # IconUri 必须指向固定路径的文件：_MEIPASS 每次运行解压到不同临时目录，
    # 因此把图标复制到 %APPDATA%\LanShare\app.ico（源码/exe 两种模式统一）
    src = (os.path.join(sys._MEIPASS, "app.ico")
           if getattr(sys, "frozen", False)
           else os.path.join(BASE_DIR, "app.ico"))
    icon_dst = None
    if os.path.isfile(src):
        dst_dir = os.path.join(os.environ.get("APPDATA") or ".", "LanShare")
        dst = os.path.join(dst_dir, "app.ico")
        try:
            os.makedirs(dst_dir, exist_ok=True)
            same = False
            if os.path.isfile(dst):
                with open(src, "rb") as f1, open(dst, "rb") as f2:
                    same = f1.read() == f2.read()
            if not same:
                shutil.copyfile(src, dst)
            icon_dst = dst
        except OSError as e:
            log.warning("通知身份图标复制失败: %s", e)

    # 没有图标就整体跳过：退回默认行为（exe 名 + exe 图标），
    # 好过“仅 AUMID 不注册”的退化状态
    if not icon_dst:
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(AUMID))
    except Exception as e:
        log.warning("设置进程 AUMID 失败: %s", e)
        return

    try:
        key = r"Software\Classes\AppUserModelId" + "\\" + AUMID
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "局域网文件共享")
            winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, icon_dst)
    except OSError as e:
        log.warning("通知身份注册表登记失败: %s", e)


# 须与 main.py 的 RELAUNCH_EVENT_NAME 一致（第二实例 → 运行中实例 的通知通道）
RELAUNCH_EVENT_NAME = r"Local\LanShare.FileShare.Relaunch"


def _start_relaunch_watchdog(notify):
    """启动“重复启动”看门狗线程；返回 ready 事件（托盘就绪后应 set）。

    第二个实例检测到互斥体已存在后会设置 RELAUNCH_EVENT_NAME 事件；
    本线程收到后弹 toast 提醒。由已运行的实例发通知，应用身份与图标
    正确，且第二实例可以立即退出——不弹窗、不需用户确认。
    """
    if os.name != "nt":
        return None
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = kernel32.CreateEventW(None, False, False, RELAUNCH_EVENT_NAME)
        if not h:
            log.warning("创建重复启动通知事件失败: %s", ctypes.get_last_error())
            return None
    except Exception as e:
        log.warning("重复启动看门狗不可用: %s", e)
        return None

    ready = threading.Event()

    def _watch():
        while True:
            # -1 = INFINITE；返回 0(WAIT_OBJECT_0) 表示事件被触发
            if kernel32.WaitForSingleObject(h, -1) != 0:
                return
            ready.wait(timeout=5)  # 等托盘就绪，避免通知发得太早而丢失
            notify("本次重复启动已忽略，请查看系统托盘图标。", "程序已在运行中")
            log.info("已忽略一次重复启动")

    threading.Thread(target=_watch, name="relaunch-watchdog", daemon=True).start()
    return ready


def run_tray(state, service):
    """运行托盘主循环（阻塞，应从主线程调用）。"""
    setup_app_identity()

    def _port():
        return state.config["port"]

    def open_admin(_item):
        webbrowser.open(f"http://localhost:{_port()}/admin")

    def open_browse(_item):
        webbrowser.open(f"http://localhost:{_port()}/")

    def toggle_service(_item):
        if service.running:
            service.stop()
            _notify("服务已暂停，局域网将无法访问", "局域网文件共享")
        else:
            try:
                service.start(_port())
            except OSError as e:
                _notify(f"恢复失败：{e}", "局域网文件共享")
            else:
                _notify(f"服务已恢复：http://localhost:{_port()}/", "局域网文件共享")

    def quit_app(_item):
        service.stop()
        icon.stop()
        log.info("程序退出")

    def _toggle_title(_item):
        return "暂停服务" if service.running else "恢复服务"

    menu = pystray.Menu(
        pystray.MenuItem("打开管理页", open_admin, default=True),
        pystray.MenuItem("打开浏览页", open_browse),
        pystray.MenuItem(_toggle_title, toggle_service),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("lan_share", _icon_image(), "局域网文件共享", menu)

    def _notify(message, title):
        try:
            icon.notify(message, title)
        except Exception as e:  # 某些环境气泡不可用时仅记日志
            log.warning("托盘通知失败: %s", e)

    # 重复启动看门狗：第二实例设置 RELAUNCH 事件时，由本实例弹 toast
    relaunch_ready = _start_relaunch_watchdog(_notify)

    def _on_ready(icon):
        # 关键：pystray 传入自定义 setup 时必须显式设置 visible，
        # 否则图标默认隐藏（这正是之前看不到图标的原因）
        icon.visible = True
        if relaunch_ready:
            relaunch_ready.set()
        ips = network.get_lan_ips()
        if ips:
            _notify(f"局域网访问地址：http://{ips[0]}:{_port()}", "共享服务已启动")
        else:
            _notify("未检测到局域网 IP", "共享服务已启动")

    icon.run(setup=_on_ready)
