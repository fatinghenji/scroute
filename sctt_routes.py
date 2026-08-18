#!/usr/bin/env python3
"""SC Trade Tools 路线直查（免浏览器、免 LLM，双源核验用）。

复刻前端 UiTokenService：
  1. GET /api/system/epoch 多次采样取最小 RTT 校时
  2. token = base64(iv) + base64(AES-CBC(  "<op>@<server_ms>" ))，op=getTradeRoutes
  3. POST /api/tools/trades  body 同前端 trade-routes 页（2026-08-12 抓取）
  4. --circuit N：取第 N 条路线的 id，POST /api/tools/circuits/{id}（body 同上，
     op=getCircuit），返回环形路线各腿（2026-08-14 依官方 OpenAPI /v3/api-docs
     + 前端 chunk 734.js 复核：itineraryUrl=/api/tools/itinerary op=getItinerary，
     circuitUrl=/api/tools/circuits/ op=getCircuit，body 均为对应 FormDto）
AES key 内嵌于前端 main.js（站点改版会变，失效时重新抓前端 main.js 的 clientVersion/aes 字段）。

结果按 (ship, investment) 缓存 25 分钟（与 plan_route 同目录 /tmp/uex_cache，
UEX_CACHE_DIR 环境变量可改），避免 dual 每次重复拉；--refresh 强制刷新。
网络: 直连失败自动回退本机代理（scroute_net.py，与 plan_route 共用同一套逻辑）。

用法:
  sctt_routes.py [--ship Railen] [--investment 7000000] [--top 10]
                 [--origin "Patch City"] [--refresh]
  sctt_routes.py --circuit 1 [--origin X]   # 展开第 N 条（过滤后序号）为环形路线
输出: 对齐表格（买站/商品/SCU/金额 → 卖站/金额/利润），供与 UEX plan_route.py 双源比对。
      --circuit 模式输出该路线闭环各腿（含返程腿，全程不空载）。
"""
import argparse, base64, hashlib, json, os, sys, tempfile, time
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from tablefmt import render_table

from scroute_net import run_curl

BASE = "https://sc-trade.tools"
AES_B64 = "YAQqYsrYmYIc9WxvCZF4W5yp6FaIb7h6"  # 前端 main.js 内嵌
OP = "getTradeRoutes"
OP_CIRCUIT = "getCircuit"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CACHE_DIR = Path(os.environ.get("UEX_CACHE_DIR", Path(tempfile.gettempdir()) / "uex_cache"))
CACHE_TTL = 25 * 60  # 25 分钟（对齐 plan_route 价格缓存）


def curl(url, headers=None, data=None):
    hdrs = {"Accept": "application/json, text/plain, */*"}
    hdrs.update(headers or {})
    code, out = run_curl(url, 45, UA, hdrs, data)
    if code != 0:
        raise RuntimeError(f"curl exit {code}: {out[:200]}")
    return out


def sync_delay(samples=3):
    """校时：多次采样取最小 RTT 的那次时钟偏移（3 次足够，兼顾请求量）。"""
    best = None
    for _ in range(samples):
        t0 = time.time_ns() // 10**6
        epoch = int(curl(f"{BASE}/api/system/epoch"))
        t1 = time.time_ns() // 10**6
        rtt = t1 - t0
        delay = (t0 + t1) // 2 - epoch
        if best is None or rtt < best[0]:
            best = (rtt, delay)
        time.sleep(0.1)
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
    ap.add_argument("--refresh", action="store_true", help="忽略缓存强制刷新")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON（调试用，绕过缓存）")
    ap.add_argument("--circuit", type=int, metavar="N",
                    help="展开过滤后第 N 条路线为环形路线（POST /api/tools/circuits/{id}）")
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

    # 响应体与 origin/dest 无关（过滤在本地做），按 (ship, investment) 缓存即可
    cache_f = CACHE_DIR / f"sctt_{hashlib.md5(f'{args.ship}|{args.investment}'.encode()).hexdigest()}.json"
    data = None
    if not args.json and not args.refresh:
        if cache_f.exists() and time.time() - cache_f.stat().st_mtime < CACHE_TTL:
            data = json.loads(cache_f.read_text())
    if data is None:
        delay = sync_delay()
        raw = curl(f"{BASE}/api/tools/trades", {"token": make_token(OP, delay)},
                   json.dumps(body))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            sys.exit(f"SCTT 拒绝: {raw[:200]}")  # 403 CAPTCHA / 站点改版
        if not isinstance(data, list):
            sys.exit(f"unexpected: {raw[:200]}")
        if not args.json:
            CACHE_DIR.mkdir(exist_ok=True)
            cache_f.write_text(json.dumps(data))
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

    if args.circuit:
        if not (1 <= args.circuit <= len(routes)):
            sys.exit(f"--circuit 超出范围（过滤后共 {len(routes)} 条）")
        pick = routes[args.circuit - 1]
        delay = sync_delay()
        raw = curl(f"{BASE}/api/tools/circuits/{pick['id']}",
                   {"token": make_token(OP_CIRCUIT, delay)}, json.dumps(body))
        try:
            legs = json.loads(raw)
        except json.JSONDecodeError:
            sys.exit(f"SCTT 拒绝: {raw[:200]}")
        if not isinstance(legs, list):
            sys.exit(f"unexpected: {raw[:200]}")
        o0, d0 = pick.get("origin", {}), pick.get("destination", {})
        print(f"## SCTT 环形路线 #{args.circuit}（{args.ship} · {args.investment:,} aUEC · "
              f"{o0.get('itemName')}：{o0.get('shop')} → {d0.get('shop')}）共 {len(legs)} 腿\n")
        tp = tt = 0
        rows = [["腿", "商品", "买入站 @单价", "卖出站 @单价", "利润", "耗时"]]
        for i, r in enumerate(legs, 1):
            o, d = r.get("origin", {}), r.get("destination", {})
            tp += r.get("profit") or 0
            tt += r.get("timeInSeconds") or 0
            rows.append([f"{i}", o.get("itemName") or "?",
                         f"{o.get('shop') or '?'} @{unit(o):,.0f}",
                         f"{d.get('shop') or '?'} @{unit(d):,.0f}",
                         f"{(r.get('profit') or 0)/1e4:.1f}W",
                         f"{(r.get('timeInSeconds') or 0)//60}min"])
        print(render_table(rows, "llllrr"))
        print(f"\n全程：利润 {tp/1e4:.1f}W · 耗时 {tt//60}min（闭环不空载）")
        return

    print(f"## SCTT 路线（{args.ship} · {args.investment:,} aUEC"
          f"{' · 起点 ' + args.origin if args.origin else ''}）共 {len(routes)} 条\n")
    rows = [["#", "商品", "买入站 @单价(库存)", "卖出站 @单价(需求)", "利润", "耗时"]]
    for i, r in enumerate(routes[: args.top], 1):
        o, d = r.get("origin", {}), r.get("destination", {})
        roi = (unit(d) / unit(o) - 1) * 100 if unit(o) else 0
        rows.append([f"{i}", o.get("itemName") or "?",
                     f"{o.get('shop') or '?'} @{unit(o):,.0f}({o.get('quantityInScu', 0):,.0f})",
                     f"{d.get('shop') or '?'} @{unit(d):,.0f}({d.get('quantityInScu', 0):,.0f})",
                     f"{r.get('profit', 0)/1e4:.1f}W (ROI {roi:.0f}%)",
                     f"{(r.get('timeInSeconds') or 0)//60}min"])
    print(render_table(rows, "llllrr"))


if __name__ == "__main__":
    main()
