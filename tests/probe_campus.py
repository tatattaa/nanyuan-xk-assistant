"""判定本机是否在校园网内（决定 WebVPN 是否必要）。只读探测。"""
import json

import requests

SCHOOL_NET = "210.21.79.0/24"


def egress():
    """尽量取到本机对外的公网出口 IP。任一服务失败就换下一个。"""
    for url in ("https://api.ipify.org?format=json",
                "https://ipinfo.io/json",
                "https://ifconfig.me/all.json"):
        try:
            r = requests.get(url, timeout=8)
            if "json" in url:
                d = r.json()
                for k in ("ip", "query"):
                    if d.get(k):
                        return d[k], url
            return r.text.strip()[:64], url
        except Exception as e:
            last = f"{type(e).__name__}"
    return None, last


ip, src = egress()
print(f"探测来源 : {src}")
print(f"出口公网IP: {ip}")

# 校园网判断：出口 IP 落在学校网段内 → 人在校内，WebVPN 无意义
inside = False
if ip:
    try:
        import ipaddress

        inside = ipaddress.ip_address(ip) in ipaddress.ip_network(SCHOOL_NET)
    except ValueError:
        pass

print(f"是否属校园网段 {SCHOOL_NET}: {'是' if inside else '否/未知'}")
print()
print("结论要点：")
print("  · 只要「教务系统域名公网可达」+「抢课全程只访问这一个域名」，就不需要 WebVPN。")
print("  · 本机此前已完成真实查课与真实提交（校外直连），进一步佐证无需 WebVPN。")
print("  · 唯一无法在此刻证伪的场景：某些学校只在选课开放期对校内网开放教务端口。")
print(f"    （本次出口 IP {'疑在校内' if inside else '不在校内'}，该场景{'不成立' if inside else '无法当场排除'}）")
