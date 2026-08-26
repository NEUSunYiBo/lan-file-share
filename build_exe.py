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
    r = subprocess.run(args, cwd=BASE)
    if r.returncode != 0:
        sys.exit("打包失败，退出码 %s" % r.returncode)

    exe = os.path.join(BASE, "dist", NAME + ".exe")
    size_mb = os.path.getsize(exe) / 1024 / 1024
    print()
    print("打包完成: %s（%.1f MB）" % (exe, size_mb))


if __name__ == "__main__":
    main()
