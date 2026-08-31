"""一键打包脚本：把本项目打包成单个 exe 文件。

用法：
    python build_exe.py                     # 图形模式（无控制台窗口，日常使用推荐）
    python build_exe.py --console           # 控制台模式（显示日志，便于排错）
    python build_exe.py --icon myapp.ico    # 用自定义图标打包（任意路径）
    python build_exe.py --regen-icon        # 强制重新生成默认图标（覆盖 app.ico）

图标逻辑：项目根目录已有 app.ico 时直接使用（放你自己的图标即可，不会被覆盖）；
不存在时才自动生成默认图标（蓝底白色双向箭头）。图标同时嵌入 exe 内部资源，
exe 文件图标、托盘图标、网页 favicon、页面品牌图标四处共用同一份，分发只需单个 exe。

首次打包前需安装 PyInstaller：
    pip install pyinstaller

产物：dist/局域网文件共享.exe（单文件，pages 页面已内嵌）
注意：exe 首次运行会在自己旁边生成 config.json（端口 / 密码 / 挂载配置）。
"""

import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
NAME = "局域网文件共享"
ICON = os.path.join(BASE, "app.ico")


def _python_tcl_dir():
    r"""返回构建 Python 实际加载的 tcl86t.dll 所在目录（与本 Python 配套的正确版本）。

    import _tkinter 会让 Windows 按本 Python 的 DLL 搜索规则加载 tcl86t.dll，
    GetModuleHandle 取其真实路径——无论 PATH 被怎样污染，这个目录永远是配套版本。
    """
    if os.name != "nt":
        return None
    try:
        import _tkinter  # noqa: F401  仅为确保 tcl86t.dll 已加载
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # HMODULE 是指针宽度，必须显式声明 restype，否则被截断成 32 位导致句柄无效
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        h = k32.GetModuleHandleW("tcl86t.dll")
        if not h:
            return None
        buf = ctypes.create_unicode_buffer(260)
        if not k32.GetModuleFileNameW(ctypes.c_void_p(h), buf, 260):
            return None
        return os.path.dirname(buf.value)
    except Exception:
        return None


def _sanitize_build_path():
    r"""构建期 PATH 净化：剔除携带"别家 Tcl/Tk DLL"的目录。

    PyInstaller 沿 PATH 解析 _tkinter.pyd 的依赖。若 PATH 里混入其他来源的
    tcl86t.dll（如 conda 的 pkgs 缓存、其他环境），且排在配套目录之前，
    会打进与 _tcl_data 脚本库版本不匹配的 DLL，运行时 tkinter 报
    "version conflict for package Tcl"，管理端「添加文件夹/文件」的
    选择窗口直接 500。

    注意：只剔除 tcl86t.dll/tk86t.dll 携带者（配套目录除外），不动其他目录
    ——sqlite3/libcrypto 等仍需从 PATH 上的 conda Library\bin 正常解析，
    剔除整个 conda 根会连正确 DLL 一起丢掉。
    """
    right = _python_tcl_dir()
    if not right:
        # 定位不到配套目录时不动 PATH：宁可维持原状，也不冒误伤风险
        print("警告：未能定位配套 Tcl/Tk 目录，本次不净化 PATH")
        return os.environ.get("PATH", "")
    right_n = os.path.normcase(os.path.normpath(right))
    print("Tcl/Tk 配套目录（保留）: %s" % right)
    kept, dropped = [], []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        has_tk = (os.path.isfile(os.path.join(d, "tcl86t.dll"))
                  or os.path.isfile(os.path.join(d, "tk86t.dll")))
        is_right = os.path.normcase(os.path.normpath(d)) == right_n
        (dropped if has_tk and not is_right else kept).append(d)
    for d in dropped:
        print("PATH 净化（剔除 Tcl/Tk 污染源）: -%s" % d)
    return os.pathsep.join(kept)


def make_icon(force=False):
    """生成默认 exe 图标（蓝底白色双向箭头）。

    已存在 app.ico 且未强制重新生成时直接使用——这样放入自己的 app.ico
    就能打进 exe，不会被自动生成的默认图标覆盖。
    """
    if os.path.exists(ICON) and not force:
        print("使用现有图标: app.ico（换图标可直接替换此文件，或用 --icon 指定其他文件）")
        return True
    try:
        import tray
        tray._icon_image().save(ICON, sizes=[(16, 16), (24, 24), (32, 32),
                                            (48, 48), (64, 64), (128, 128), (256, 256)])
        print("已生成默认图标: app.ico")
        return True
    except Exception as e:
        print("图标生成失败（使用默认图标）:", e)
        return False


def main():
    icon = ICON
    if "--icon" in sys.argv:
        i = sys.argv.index("--icon")
        if i + 1 >= len(sys.argv):
            sys.exit("--icon 需要跟图标文件路径，例如: --icon myapp.ico")
        custom = os.path.abspath(sys.argv[i + 1])
        if not os.path.isfile(custom):
            sys.exit("图标文件不存在: %s" % custom)
        # 统一规范化到根目录 app.ico：--add-data 内嵌时保持文件名为 app.ico，
        # 与 tray.py / server.py 运行时的读取路径（_MEIPASS/app.ico）一致
        if os.path.abspath(custom) != os.path.abspath(ICON):
            shutil.copyfile(custom, ICON)
            print("已把 %s 复制为 %s（项目统一图标位置）" % (custom, ICON))

    # 用项目内的 app.ico：尊重已有文件，仅缺失或 --regen-icon 时生成
    make_icon(force="--regen-icon" in sys.argv)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", NAME,
        # 用户端 / 管理端页面打进去（运行时解压到临时目录）
        "--add-data", os.path.join("pages") + os.pathsep + "pages",
        # 图标嵌入内部资源（_MEIPASS）：托盘图标和 /favicon.ico 运行时从内部读取，
        # 分发只需单个 exe，无需在 exe 旁边放置 ico
        "--add-data", icon + os.pathsep + ".",
        # pystray 的 Windows 后端是按平台动态导入的，显式声明防止漏打包
        "--hidden-import", "pystray._win32",
        "main.py",
    ]
    if os.path.isfile(icon):
        args += ["--icon", icon]
    args.append("--windowed" if "--console" not in sys.argv else "--console")

    print("开始打包（可能需要一两分钟）…")
    env = dict(os.environ)
    env["PATH"] = _sanitize_build_path()
    r = subprocess.run(args, cwd=BASE, env=env)
    if r.returncode != 0:
        sys.exit("打包失败，退出码 %s" % r.returncode)

    exe = os.path.join(BASE, "dist", NAME + ".exe")
    size_mb = os.path.getsize(exe) / 1024 / 1024
    print()
    print("打包完成: %s（%.1f MB）" % (exe, size_mb))


if __name__ == "__main__":
    main()
