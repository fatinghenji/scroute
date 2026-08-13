#!/usr/bin/env python3
"""直连→代理回退 curl 封装（plan_route.py 与 sctt_routes.py 共用）。

背景: UEX 国际链路直连常被限速到 ~13KB/s（curl --max-time 超时退出 28），
      本机代理 3s 下完（2026-08-12 实测）。

策略:
  1. 直连优先（--noproxy * 强制不走环境代理），失败且代理可达 → 代理重试
  2. 代理成功后记忆「直连已死」，后续请求直接走代理，不再每次白付直连超时
  3. 代理随后失败 → 撤销记忆退回直连（自愈，防代理中途挂掉锁死）

代理地址优先级: UEX_PROXY > https_proxy/HTTPS_PROXY > http_proxy/HTTP_PROXY
               > 默认 http://127.0.0.1:43010
"""
import os, socket, subprocess, sys, urllib.parse

PROXY = (os.environ.get("UEX_PROXY")
         or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
         or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
         or "http://127.0.0.1:43010")
_direct_dead = [False]  # 可变闭包：直连已确认不可用（代理成功顶替）
_proxy_ok = [None]      # 可变闭包：None=未探测；True/False=代理可达性


def _proxy_available():
    """代理可达性探测（TCP 连接，1s 超时，记忆化）。失败路径由调用方兜底。"""
    if _proxy_ok[0] is None:
        try:
            u = urllib.parse.urlparse(PROXY)
            socket.create_connection((u.hostname, u.port or 80), timeout=1).close()
            _proxy_ok[0] = True
        except OSError:
            _proxy_ok[0] = False
    return _proxy_ok[0]


def run_curl(url, timeout, ua, headers=None, data=None):
    """跑一条 curl，带直连→代理回退；返回 (returncode, stdout)。

    headers: 额外请求头 dict；data: 非 None 时按 JSON POST 发送。
    注意: 代理重试不在调用方的限速锁内（只在直连首次失败时发生一次，影响有限）。
    """
    cmd = ["curl", "-s", "--max-time", str(timeout), "-H", f"User-Agent: {ua}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", data]
    if _direct_dead[0]:
        # 直连已知不可用：直接走代理；代理失败即撤销记忆退回直连（自愈）
        r = subprocess.run(cmd + ["-x", PROXY, url], capture_output=True, text=True)
        if r.returncode != 0:
            _direct_dead[0] = False
        return r.returncode, r.stdout
    r = subprocess.run(cmd + ["--noproxy", "*", url], capture_output=True, text=True)
    if r.returncode == 0 or not _proxy_available():
        return r.returncode, r.stdout
    print(f"[info] 直连失败(curl {r.returncode})，代理 {PROXY} 重试（本次运行后续直连代理）",
          file=sys.stderr)
    r = subprocess.run(cmd + ["-x", PROXY, url], capture_output=True, text=True)
    if r.returncode == 0:
        _direct_dead[0] = True
    return r.returncode, r.stdout


def reset():
    """测试用：清空直连已死/代理探测记忆。"""
    _direct_dead[0] = False
    _proxy_ok[0] = None


if __name__ == "__main__":
    print(f"PROXY={PROXY}")
