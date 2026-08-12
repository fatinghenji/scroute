#!/usr/bin/env python3
"""SC Trade Tools 路线直查（免浏览器、免 LLM，双源核验用）。

复刻前端 UiTokenService：
  1. GET /api/system/epoch 多次采样取最小 RTT 校时
  2. token = base64(iv) + base64(AES-CBC(  "<op>@<server_ms>" ))，op=getTradeRoutes
  3. POST /api/tools/trades  body 同前端 trade-routes 页（2026-08-12 抓取）
AES key 内嵌于前端 main.js（站点改版会变，失效时重新抓 clientVersion/aes 字段）。

用法:
  sctt_routes.py [--ship Railen] [--investment 7000000] [--top 10] [--origin "Patch City"]
输出: Markdown 表（买站/商品/SCU/金额 → 卖站/金额/利润），供与 UEX plan_route.py 双源比对。
"""
import argparse, base64, json, os, subprocess, sys, time

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE = "https://sc-trade.tools"
AES_B64 = "YAQqYsrYmYIc9WxvCZF4W5yp6FaIb7h6"  # 前端内嵌（见 sctt_analytics_collect.py）
OP = "getTradeRoutes"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def curl(url, headers=None, data=None):
    cmd = ["curl", "-s", "--noproxy", "*", "-m", "45",
           "-H", f"User-Agent: {UA}",
           "-H", "Accept: application/json, text/plain, */*"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", data]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"curl exit {r.returncode}: {r.stderr[:200]}")
    return r.stdout


def sync_delay(samples=5):
    best = None
    for _ in range(samples):
        t0 = time.time_ns() // 10**6
        epoch = int(curl(f"{BASE}/api/system/epoch"))
        t1 = time.time_ns() // 10**6
        rtt = t1 - t0
        delay = (t0 + t1) // 2 - epoch
        if best is None or rtt < best[0]:
            best = (rtt, delay)
        time.sleep(0.15)
    return best[1]


def make_token(op, delay_ms):
    server_now = int(time.time() * 1000) - delay_ms
    key = base64.b64decode(AES_B64)
    iv = os.urandom(16)
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(f"{op}@{server_now}".encode(), 16))
    return base64.b64encode(iv).decode() + base64.b64encode(ct).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ship", default="Railen")
    ap.add_argument("--investment", type=int, default=7000000)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--origin", help="只保留从该站出发的路线（模糊匹配）")
    ap.add_argument("--dest", help="只保留卖到该站的路线（模糊匹配）")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON（调试用）")
    args = ap.parse_args()

    body = {"ship": args.ship, "investment": args.investment, "profitType": "time",
            "maxStops": 5, "allowWaitTimes": False, "useAutoLoading": False,
            "smartFilters": True, "expandedView": False,
            "commodityTypes": [], "commodityTypesType": "blacklist",
            "commodityNames": [], "commodityNamesType": "blacklist",
            "maxVolume": None, "locationTypes": [], "locationTypesType": "blacklist",
            "locationNames": [], "locationNamesType": "blacklist", "origin": None,
            "factionNames": [], "factionsNamesType": "blacklist",
            "minSecurityLevel": 1, "supportedBoxSizeInScu": 1, "avoidHiddenLocations": True}

    delay = sync_delay()
    raw = curl(f"{BASE}/api/tools/trades", {"token": make_token(OP, delay)},
               json.dumps(body))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(f"SCTT 拒绝: {raw[:200]}")  # 403 CAPTCHA / 站点改版
    if not isinstance(data, list):
        sys.exit(f"unexpected: {raw[:200]}")
    if args.json:
        print(json.dumps(data[:2], ensure_ascii=False, indent=1))
        return

    def unit(node):
        q = node.get("itemQuantityInScu") or 0
        return (node.get("price") or 0) / q if q else 0

    def norm(s):
        return "".join(c for c in (s or "").lower() if c.isalnum())

    routes = data
    if args.origin:
        routes = [r for r in routes if norm(args.origin) in norm(r.get("origin", {}).get("shop"))]
    if args.dest:
        routes = [r for r in routes if norm(args.dest) in norm(r.get("destination", {}).get("shop"))]

    print(f"## SCTT 路线（{args.ship} · {args.investment:,} aUEC"
          f"{' · 起点 ' + args.origin if args.origin else ''}）共 {len(routes)} 条\n")
    print("| # | 商品 | 买入站 @单价(库存) | 卖出站 @单价(需求) | 利润 | 耗时 |")
    print("|---|---|---|---|---|---|")
    for i, r in enumerate(routes[: args.top], 1):
        o, d = r.get("origin", {}), r.get("destination", {})
        roi = (unit(d) / unit(o) - 1) * 100 if unit(o) else 0
        print(f"| {i} | {o.get('itemName')} | {o.get('shop')} @{unit(o):,.0f}"
              f"({o.get('quantityInScu', 0):,.0f}) | {d.get('shop')} @{unit(d):,.0f}"
              f"({d.get('quantityInScu', 0):,.0f}) | {r.get('profit', 0)/1e4:.1f}W"
              f" (ROI {roi:.0f}%) | {(r.get('timeInSeconds') or 0)//60}min |")


if __name__ == "__main__":
    main()
