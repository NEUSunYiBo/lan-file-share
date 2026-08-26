"""局域网 IP 检测（纯标准库）。"""

import socket


def _usable(ip):
    """过滤回环与链路本地（APIPA 自动专用）地址。"""
    return not ip.startswith("127.") and not ip.startswith("169.254.")


def get_lan_ips():
    """返回本机可用的局域网 IPv4 列表，默认出口 IP 排最前。

    UDP connect 不会真的发包，仅用于让系统选出去往公网地址的出口网卡，
    从而拿到默认路由对应的本机 IP。
    """
    ips = []

    # 1) 默认出口 IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if _usable(ip):
                ips.append(ip)
    except OSError:
        pass  # 无网络时忽略

    # 2) 主机名解析出的所有本机地址（覆盖多网卡）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _usable(ip) and ip not in ips:
                ips.append(ip)
    except socket.gaierror:
        pass

    return ips
