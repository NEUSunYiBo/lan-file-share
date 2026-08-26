# 局域网文件共享（LAN File Share）

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![Tests](https://img.shields.io/badge/tests-98%20passed-brightgreen.svg)

一个**零配置、开箱即用**的 Windows 局域网文件共享工具。双击运行后自动驻留系统托盘并启动本地 Web 服务，局域网内的手机、平板、电脑**无需安装任何客户端**，用浏览器即可访问——浏览、下载、上传文件与整个文件夹，并对传输流水做可视化统计分析。

A zero-configuration, out-of-the-box file sharing tool for Windows LANs. Double-click to run, and it automatically stays in the system tray and starts a local web server. Phones, tablets, and computers on the same network can access it from a browser with no client installation — browse, download, and upload files and entire folders, with visual statistical analysis of all transfer activity.

典型场景：

- 电脑上的资料想传到手机 / 平板（反向亦可），不想插数据线
- 需要长期在局域网内共享若干文件夹给家人 / 同事
- 想要一个带传输日志和统计面板的「私人网盘」

## 截图

用户端（手机 / 电脑浏览器直接访问）：

![用户端](assets/screenshot-browse.png)

管理端（共享管理 / 上传设置 / 日志仪表板）：

![管理端](assets/screenshot-admin.png)

## 功能特性

### 共享与浏览

| 特性 | 说明 |
|---|---|
| 即开即用 | 单个 exe（或 `python main.py`）启动，无安装向导、无注册表依赖 |
| 挂载共享 | 任意本地文件夹或单个文件挂载为共享点，随时增删 |
| 精细控制 | 取消共享（排除指定子目录/文件）、暂时隐藏（用户端不可见、随时恢复，避免临时删除挂载） |
| 目录浏览 | 目录/文件彩色类型图标（图片/视频/音频/PDF/文本分色）、面包屑导航、多种排序（名称/大小/修改时间 × 升降序，目录置顶） |
| 搜索 | 三种范围：本目录（即时过滤）/ 本共享（递归）/ 全部共享（跨挂载全局搜索） |
| 在线预览 | 图片、视频（支持进度条拖动，基于 HTTP Range）、音频、PDF、文本 |
| 侧键导航 | 完整支持浏览器历史（鼠标侧键前进/后退、移动端返回手势） |
| 传输体验 | 下载走流式传输；列表底部有当前页文件夹/文件分类统计角标 |

### 文件上传

| 特性 | 说明 |
|---|---|
| 文件夹上传 | 客户端可选整个文件夹（含拖拽），服务端按原目录结构完整保存 |
| 进度反馈 | 单文件进度 + 整体进度（按总文件数/总字节数折算），实时刷新 |
| 同名处理 | 与已有文件重名时自动重命名（`a.txt` → `a (1).txt`），不覆盖、不报错 |
| 独立存放 | 上传内容进入专属 uploads 目录，与主动共享的文件完全隔离，且在列表中固定置顶 |
| 共享开关 | 管理端一键控制上传目录是否对外共享：关闭时内容仅本机可见 |

### 管理与可观测性

| 特性 | 说明 |
|---|---|
| 日志仪表板 | SQLite 记录每次上传/下载流水（文件名、大小、来源 IP、时间） |
| 可视化统计 | 文件类型占比饼图、类型大小分布柱状图（名称/大小双排序）、上传/下载趋势、最近记录 |
| 卡片定制 | 仪表板卡片可开关、可拖拽排序，偏好持久化 |
| 扫码直达 | 管理页展示访问地址二维码（多网卡可点击切换 IP），手机扫码即达 |
| 右键定位 | 管理端对文件/文件夹/挂载点右键：复制完整路径、在文件夹中显示（资源管理器选中）、直接打开 |
| 托盘常驻 | 系统托盘图标 + 菜单（打开管理页/退出），Windows 原生通知（正确的应用名与图标） |
| 单实例保护 | 重复启动自动通知已运行实例（toast 提示）后退出，不弹窗打扰 |

### 安全

| 特性 | 说明 |
|---|---|
| 可选访问密码 | SHA-256 加盐哈希存储，登录换取令牌；未设置则免密直访 |
| 管理面隔离 | 全部管理接口仅接受本机（loopback）请求，局域网设备无法触达 |
| 路径越界防护 | 所有文件操作做路径归一化校验，`../` 穿越类攻击直接拒绝 |
| 排除项防枚举 | 被取消共享的路径对用户端返回 404 而非 403，避免目录结构探测 |

## 快速开始

### 方式一：直接下载 exe（推荐）

从 [Releases](https://github.com/NEUSunYiBo/lan-file-share/releases) 下载 `LanShare.exe`（GitHub 资产名仅支持 ASCII，下载后可自行改名），放到任意目录双击运行：

1. 托盘出现程序图标，服务在本机启动
2. 浏览器打开管理页 `http://localhost:8000/admin`
3. 在「共享挂载」中添加要共享的文件夹
4. 手机扫管理页的二维码（或访问 `http://<你的IP>:8000`）即可开始传输

> exe 旁无需任何附加文件；首次运行自动生成 `config.json` 与 `uploads/`。卸载 = 删除整个文件夹。

### 方式二：从源码运行

```bash
git clone https://github.com/NEUSunYiBo/lan-file-share.git
cd lan-file-share
pip install -r requirements.txt
python main.py
```

要求 Python 3.9+，Windows 系统（托盘与通知依赖 Win32 API）。

## 使用指南

### 服务端（管理者）

启动后打开 `http://localhost:<port>/admin`：

- **共享挂载**：点「＋ 文件夹 / ＋ 文件」挂载共享点；每个条目可切换「共享中/已隐藏」状态，或删除挂载；悬停显示完整本地路径；**右键任意文件/文件夹/挂载**：复制完整路径、在文件夹中显示、直接打开
- **上传文件夹**：修改接收目录（系统目录选择弹窗，无需手输路径）、控制上传内容是否对外共享
- **访问地址**：列出本机所有可用 IP，附二维码；点击任一 IP 可切换二维码指向（多网卡场景）
- **访问密码 / 服务端口**：按需设置；密码设置后所有用户端访问需先登录
- **日志**：进入仪表板查看传输流水与统计图表

### 用户端（局域网设备）

浏览器访问 `http://<服务端IP>:<port>`（或扫管理页二维码）：

- 点击共享挂载进入浏览，面包屑 + 侧键返回上级
- 搜索框选范围（本目录 / 本共享 / 全部共享），输入关键词过滤或全局搜索
- 点击文件在线预览，或下载；点「上传」按钮选择文件/文件夹发送到服务端
- 支持深色模式（跟随系统，可手动切换）

## 配置说明

运行时在程序旁自动生成 `config.json`（完整格式见 [config.example.json](config.example.json)）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `port` | int | 服务端口，默认 `8000` |
| `password_hash` | string | 访问密码哈希（`salt$hex` 格式，管理页设置，留空 = 无密码） |
| `mounts[].id` | string | 挂载点唯一 ID（自动生成） |
| `mounts[].name` | string | 对用户端显示的名称 |
| `mounts[].path` | string | 本地真实路径（绝对路径） |
| `mounts[].hidden` | bool | 暂时隐藏：用户端不可见，管理端仍显示 |
| `mounts[].excluded` | list | 排除规则（取消共享的子路径，`/` 分隔） |
| `upload_dir` | string | 设备上传的接收目录，默认程序旁 `uploads/` |
| `auto_share_uploads` | bool | 上传内容是否对外共享，默认 `true` |
| `dashboard.cards` | list | 仪表板卡片开关与排序（管理页内直接调整即可） |

配置读写均有校验与原子写入保护（先写临时文件再替换），损坏时自动回退默认值，不会导致程序无法启动。

## 架构

```
main.py            # 入口：单实例互斥 → 加载配置 → 启动 HTTP 服务 → 系统托盘
server.py          # Flask 应用组装与 AppState
api_user.py        # 用户端接口（浏览/下载/预览/搜索/鉴权）
api_admin.py       # 管理端接口（仅本机：挂载/配置/二维码/状态）
api_upload.py      # 上传接口（文件与文件夹、重名处理、相对路径净化）
mounts.py          # 挂载注册表（排除/隐藏/系统挂载置顶/路径安全）
config.py          # 配置持久化（校验 + 原子写）
auth.py            # 密码哈希（SHA-256 加盐）与访问令牌
network.py         # 局域网 IP 探测
transfer_log.py    # SQLite 传输日志（WAL 模式）
tray.py            # 托盘图标/菜单/Windows 原生通知（AUMID 注册）
build_exe.py       # 一键打包脚本（PyInstaller onefile，页面内嵌）
pages/             # 前端：browse.html 用户端 / admin.html 管理端（零构建、原生 JS）
assets/            # README 截图
tests/             # pytest 测试（98 个用例）
```

技术栈：Python + Flask + Waitress（生产级 WSGI 服务器）+ PyStray/Pillow（托盘与图标）+ qrcode；前端为无构建依赖的单文件 HTML（原生 JS + ECharts），随程序打包分发，不依赖任何 CDN。

## 从源码打包 exe

```bash
pip install pyinstaller
python build_exe.py            # 产物：dist/局域网文件共享.exe（约 24 MB，页面已内嵌）
python build_exe.py --console  # 带控制台窗口的调试版
```

打包细节：

- **单文件模式**（onefile）：前端页面与图标全部嵌入 exe 内部资源，分发只需这一个文件
- **自定义图标**：项目根放置自己的 `app.ico` 后打包即可生效（不会被打包脚本覆盖）
- 建议在干净的虚拟环境中打包，避免把本机多余的库（如 Anaconda 全家桶）打进去

## 测试

```bash
pip install pytest
pytest tests/ -q
```

98 个用例覆盖：鉴权流程、目录浏览与排序、下载与 Range 分片、路径越界防护、挂载管理（含排除/隐藏/系统挂载）、上传（含文件夹结构、重名、路径净化）、传输日志、配置读写与回退。

## 常见问题

**Q：手机打不开？**
先确认手机与服务端在同一局域网（连同一个路由器/Wi-Fi）；管理页「访问地址」里选手机能到达的 IP（多网卡时默认 IP 不一定可达，点其他 IP 切换二维码试试）；检查 Windows 防火墙是否放行了该端口。

**Q：重复双击 exe 没反应？**
程序已在运行（看系统托盘），第二次启动会通知运行中实例弹 toast 后自动退出。

**Q：上传的文件去哪了？**
管理页「上传文件夹」模块显示的接收目录（默认 exe 旁 `uploads/`），该目录在共享列表中固定置顶。

**Q：忘记访问密码？**
关闭程序后删除（或编辑）exe 旁的 `config.json` 中的 `password_hash` 字段即可重置（挂载等其他配置不受影响）。

**Q：想换端口 / 换机器保留配置？**
端口在管理页直接改；迁移配置只需把 `config.json`（可连同 `uploads/`）复制到新位置即可。

## 许可证

[MIT](LICENSE) — 可自由使用、修改与分发，请保留原始许可证声明。

## 致谢

- [Flask](https://flask.palletsprojects.com/) / [Waitress](https://docs.pylonsproject.org/projects/waitress/)
- [PyStray](https://github.com/moses-palmer/pystray) / [Pillow](https://python-pillow.org/)
- [ECharts](https://echarts.apache.org/)（Apache-2.0）
- [qrcode](https://github.com/lincolnloop/python-qrcode)
