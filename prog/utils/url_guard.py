"""
SSRF 防护工具（S9）
===================
文件用途：
    校验 URL 是否指向内网/保留地址，防止服务端请求伪造（SSRF）攻击。
    适用于 LLM base_url 变更、Webhook 回调等场景。

校验范围：
    - 127.0.0.0/8        回环地址
    - 10.0.0.0/8         A 类私有
    - 172.16.0.0/12      B 类私有
    - 192.168.0.0/16     C 类私有
    - 169.254.0.0/16     链路本地（含 AWS 元数据 169.254.169.254）
    - 0.0.0.0/8          本机网络
    - ::1/128            IPv6 回环
    - fc00::/7           IPv6 唯一本地地址
    - fe80::/10          IPv6 链路本地

接口：
    is_safe_url(url) -> bool          -- URL 是否安全（非内网）
    validate_url(url) -> None         -- 不安全抛 ValueError
"""

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse


def _resolve_host(host: str) -> list:
    """解析主机名为 IP 地址列表（含 IPv4/IPv6）。"""
    try:
        # getaddrinfo 返回 (family, type, proto, canonname, sockaddr)
        results = socket.getaddrinfo(host, None)
        return [sockaddr[0] for _, _, _, _, sockaddr in results]
    except socket.gaierror:
        return []


def is_ip_private(ip_str: str) -> bool:
    """判断 IP 地址是否为内网/保留地址。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 无法解析的 IP 视为不安全
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_safe_url(url: str) -> bool:
    """校验 URL 是否安全（不指向内网/保留地址）。

    参数：
        url: 完整 URL 字符串（如 "https://api.openai.com/v1"）

    返回：
        True = 安全（公网地址）；False = 不安全（内网/保留/无法解析）
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    # 仅允许 http/https 协议
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    # 直接是 IP 地址
    try:
        ipaddress.ip_address(host)
        return not is_ip_private(host)
    except ValueError:
        pass
    # 域名：解析后逐个 IP 校验（任一为内网即不安全）
    ips = _resolve_host(host)
    if not ips:
        return False  # 无法解析域名
    return all(not is_ip_private(ip) for ip in ips)


def validate_url(url: str, context: Optional[str] = None) -> None:
    """校验 URL 安全性，不安全时抛 ValueError。

    参数：
        url: 完整 URL 字符串
        context: 校验场景描述（如 "LLM base_url"），用于错误消息
    """
    if not is_safe_url(url):
        label = f"（{context}）" if context else ""
        raise ValueError(
            f"URL 不安全{label}：禁止指向内网/保留地址或非 HTTP(S) 协议"
        )
