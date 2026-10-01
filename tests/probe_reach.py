"""探测：教务系统是否公网直达（决定要不要做 WebVPN 适配）。只读，不发任何业务请求。"""
import ipaddress
import socket
import sys

import requests

HOSTS = [
    "jwxt.nfu.edu.cn",          # 教务系统（正方）
    "jw.nfu.edu.cn",            # 教务处
    "webvpn.nfu.edu.cn",        # 猜测的 WebVPN
    "vpn.nfu.edu.cn",           # 猜测的 VPN
]

print(f"{'主机':<32}{'DNS':<22}{'HTTP':<12}{'Date'}")
print("-" * 84)
for h in HOSTS:
    try:
        ip = socket.gethostbyname(h)
        dns = ip
    except Exception as e:
        ip, dns = None, f"解析失败({type(e).__name__})"
    http = "-"
    date = "-"
    if ip:
        try:
            r = requests.get(f"https://{h}/", timeout=10, allow_redirects=False)
            http = str(r.status_code)
            date = (r.headers.get("Date") or "-")[:29]
        except requests.RequestException as e:
            http = f"ERR:{type(e).__name__}"
    print(f"{h:<32}{dns:<22}{http:<12}{date}")

print()
print("判定：只要 jwxt 能拿到 HTTP 响应（无论 200/302/403），就说明它公网可达，")
print("      本工具无需 WebVPN —— 因为抢课全程只需访问这一个域名。")
