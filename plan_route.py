#!/usr/bin/env python3
"""UEX 货运路线规划器（纯 API，免浏览器）。

用法:
  plan_route.py --scu 640 --capital 7000000            # Railen + 700W
  plan_route.py --scu 288 --capital 2500000 --origin Baijini
  plan_route.py --scu 640 --capital 7000000 --commodity quartz --top 10

数据获取双通道（抗大端点卡死）:
  主: commodities_prices_all 一次全量（服务端偶发慢速滴水传输/卡死，实测 90s 传不完 1MB）
  降级: 官方 commodities_routes 全站引导（每商品站 1 请求；行自带 status/scu/roi/distance，
        本地过滤即出候选，免测距免交叉验证；替代旧逐商品 prices 降级——158 商品太慢）
流程: commodities + terminals + prices_all(带缓存) → 本地算价差候选
  或 routes 全站引导 → 候选 → 输出 Markdown 表（prices 模式附测距+交叉验证）

坑: urllib socket 超时对慢速滴水传输无效（字节一直来），必须 curl --max-time 硬杀。
"""
import argparse, json, os, subprocess, sys, tempfile, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = "https://api.uexcorp.uk/2.0"
UA = "HermesAgent/1.0 (sc-cargo-workflow)"
CACHE_DIR = Path(os.environ.get("UEX_CACHE_DIR", Path(tempfile.gettempdir()) / "uex_cache"))
CACHE_TTL = 25 * 60  # 25 分钟（服务端缓存30分钟，宁可略早刷新）
MAX_TIME = 15  # 默认 curl 硬超时；大端点单独放宽
RETRIES = 2
MAX_WORKERS = 8  # 并行度（2026-08-09 实测：0.15s 间隔≈400/分 连发 10 次全 200，120/分不强制）
MIN_GAP = 0.15  # 请求最小间隔（原 0.5s 自缚手脚）

# —— 时薪模型常量（2026-08-12 标定）——
# QD 巡航速度（m/s）：取 Erkul（erkul.games）quantumdrives 家族实测 driveSpeed，按船原装 QD 建表。
# 新船标定：在 erkul.games 查该船原装 QD 的 driveSpeed 实测值，填入下表后重跑即可。
SHIP_QD_SPEED = {
    "railen": 262e6,      # 原装 Bolon（工C），LIVE 4.9.0 实测；续航 318.75 Gm
    "hull b": 262e6,      # 同 S2 槽位假设同级 QD（未单独标定，使用时注明）
}
DEFAULT_QD_SPEED = 250e6  # 未知船兜底（S2 民用 QD 典型值 242-282 Mm/s）
SPOOL_S = 8        # 每次量子跳跃：点火 6s + 校准 ~2s（Bolon 实测 spoolUpTime=6）
DOCK_MIN = 4       # 空间站对接+起飞 min/次
LAND_MIN = 5       # 地面站降落+起飞 min/次
AUTO_MIN_PER_100SCU = 1.0   # 自动装/卸耗时 ~1 min/100 SCU（640 SCU ≈ 6.4 min）
MANUAL_BOX_S = 35  # 手动搬箱 s/箱（保守取值；25-45 区间内时薪结论排序基本不变）


def leg_minutes(dist_gm, scu, buy_term, sell_term, qd_speed):
    """单腿耗时模型：起点(起降+装货) + QT巡航 + 终点(起降+卸货)。"""
    def stop_min(t, scu_load):
        if t is None:
            return DOCK_MIN + max(2, scu_load * AUTO_MIN_PER_100SCU / 100)  # 未知按自动估
        if t.get("is_auto_load"):
            return DOCK_MIN + max(2, scu_load * AUTO_MIN_PER_100SCU / 100)
        box = t.get("max_container_size") or 8
        return LAND_MIN + (scu_load / box) * MANUAL_BOX_S / 60
    qt = (dist_gm * 1e9 / qd_speed + SPOOL_S) / 60 if dist_gm else None
    if qt is None:
        return None
    return stop_min(buy_term, scu) + qt + stop_min(sell_term, scu)

_lock = __import__("threading").Lock()
_last_req = [0.0]


def _get(url, timeout=MAX_TIME):
    with _lock:
        gap = MIN_GAP - (time.time() - _last_req[0])
        if gap > 0:
            time.sleep(gap)
        _last_req[0] = time.time()
    r = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), "--noproxy", "*",
         "-H", f"User-Agent: {UA}", url],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"curl exit {r.returncode}")
    return json.loads(r.stdout)


def fetch(endpoint, params=None, cache_key=None, ttl=CACHE_TTL, attempts=RETRIES + 1, timeout=MAX_TIME):
    CACHE_DIR.mkdir(exist_ok=True)
    if cache_key:
        f = CACHE_DIR / f"{cache_key}.json"
        if f.exists() and time.time() - f.stat().st_mtime < ttl:
            return json.loads(f.read_text())
    url = f"{BASE}/{endpoint}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    last = None
    for attempt in range(attempts):
        try:
            data = _get(url, timeout)
            if cache_key:
                f.write_text(json.dumps(data))
            return data
        except Exception as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    print(f"[warn] {endpoint} 失败: {last}", file=sys.stderr)
    return None


def load_prices(coms, terms_by_id):
    """三级数据通道，返回 (rows, mode)；mode ∈ {"prices", "routes"}。

    1) prices_all 一次全量（服务端快时 1 请求搞定）
    2) 官方 commodities_routes 全站引导（每商品站 1 请求；行自带 status/scu/roi/
       distance，免测距免交叉验证）——需要 terminals 站列表
    3) 逐商品 prices 兜底（不依赖 terminals，任何网络状况都能出结果）
    """
    d = fetch("commodities_prices_all", cache_key="prices_all", attempts=1, timeout=12)  # 大端点只试1次
    if d and isinstance(d.get("data"), list):
        return d["data"], "prices"
    print("[warn] prices_all 不可用（服务端卡/慢）…", file=sys.stderr)
    if terms_by_id:
        rows = load_route_rows(terms_by_id)
        if rows:
            return rows, "routes"
        print("[warn] routes 引导无数据…", file=sys.stderr)
    print("[warn] 最后兜底：按商品并行拉 prices…", file=sys.stderr)
    return legacy_per_commodity(coms), "prices"


def legacy_per_commodity(coms):
    """最后兜底：按商品并行拉 prices（不依赖 terminals 站列表）。"""
    tradable = [c for c in coms.values()
                if c.get("is_available_live") and (c.get("is_buyable") or c.get("is_sellable"))]
    rows = []

    def one(c):
        r = fetch("commodities_prices", {"commodity_name": c["name"]},
                  cache_key=f"p_{c['id']}", attempts=1, timeout=12)
        return r["data"] if r and isinstance(r.get("data"), list) else []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(one, c) for c in tradable]
        for fu in as_completed(futs):
            rows.extend(fu.result())
    print(f"[info] 兜底完成，{len(rows)} 行", file=sys.stderr)
    return rows


def load_route_rows(terms_by_id):
    """routes 引导：对全部 commodity 站点拉官方路线（每站 1 请求）。

    行自带 status_origin/status_destination（库存码 1-7）、scu_origin/scu_destination、
    price_roi/price_origin/price_destination、distance → 本地过滤即出候选。
    """
    stations = [t for t in terms_by_id.values()
                if t.get("type") == "commodity" and t.get("is_available_live")]
    print(f"[info] 官方 routes 引导（{len(stations)} 个商品站）…", file=sys.stderr)
    rows = []

    def one(t):
        d = fetch("commodities_routes", {"id_terminal_origin": t["id"]},
                  cache_key=f"routes_{t['id']}", ttl=12 * 3600, attempts=1, timeout=20)
        return d["data"] if d and isinstance(d.get("data"), list) else []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(one, t) for t in stations]
        for fu in as_completed(futs):
            rows.extend(fu.result())
    print(f"[info] routes 引导完成，{len(rows)} 行", file=sys.stderr)
    return rows


def load_terms():
    """commodity 类型 terminals（货运相关全部 161 站，响应 ~214KB 远小于全量 826 站 1MB，
    大幅降低服务端慢时的超时率）；失败返回 None（触发按需懒加载）。"""
    d = fetch("terminals", {"type": "commodity"}, cache_key="terminals",
              ttl=12 * 3600, attempts=2, timeout=20)
    if not d or not isinstance(d.get("data"), list):
        print("[warn] terminals 全量失败，改为按需懒加载…", file=sys.stderr)
        return None, None
    return ({t["id"]: t for t in d["data"]},
            {t["code"]: t for t in d["data"] if t.get("code")})


def resolve_terms_for(cands, terms_by_id, terms_by_code):
    """为候选路线涉及的站点补元数据（懒加载，小端点逐个拉）。"""
    def match(t, name, code):
        if code and t.get("code") == code:
            return True
        return bool(name) and (norm(name) in norm(t.get("name")) or norm(name) in norm(t.get("nickname") or ""))

    need = {}
    for c in cands:
        for r in (c["buy"], c["sell"]):
            if r.get("_term") is None:
                need.setdefault((r.get("terminal_name"), r.get("terminal_code")), r)

    def one(item):
        name, code = item
        r = need[item]
        if code and code in (terms_by_code or {}):
            return r, (terms_by_code or {})[code]
        d = fetch("terminals", {"name": name}, cache_key=f"t_{norm(name)}", ttl=12 * 3600)
        if not d or not isinstance(d.get("data"), list):
            return r, None
        cand = [t for t in d["data"] if match(t, name, code)]
        cand = [t for t in cand if t.get("type") == "commodity"] or cand  # 优先 commodity 终端
        return r, (cand[0] if cand else None)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(one, k) for k in need]
        for fu in as_completed(futs):
            r, t = fu.result()
            if t is not None:
                r["_term"] = t


def norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def main():
    ap = argparse.ArgumentParser(description="UEX 货运路线规划")
    ap.add_argument("--scu", type=float, required=False, help="船舱容 (SCU)；与 --ship 二选一")
    ap.add_argument("--ship", help="船名模糊匹配 UEX vehicles 自动取舱容（如 \"Hull B\"），优先于 --scu")
    ap.add_argument("--capital", type=float, required=True, help="本金 (aUEC)")
    ap.add_argument("--full", action="store_true", help="允许满仓押全部本金（默认只用一半）")
    ap.add_argument("--origin", help="限定买入站（名字/代号模糊匹配，如 Baijini / BAIJI）")
    ap.add_argument("--dest", help="限定卖出站（返程腿规划：找便宜的买入站 → 卖回该站，如 \"Patch City\"）")
    ap.add_argument("--origin-system", help="限定出发星系（模糊匹配，如 Pyro）")
    ap.add_argument("--dest-system", help="限定目的星系（模糊匹配，如 Stanton）")
    ap.add_argument("--space-only", action="store_true", help="仅限空间站终端（出发+到达，对齐 UEX-Trader Space Only）")
    ap.add_argument("--commodity", help="限定商品（模糊匹配，如 quartz）")
    ap.add_argument("--min-roi", type=float, default=30, help="最低 ROI%%（默认 30）")
    ap.add_argument("--top", type=int, default=15, help="输出条数（默认 15）")
    ap.add_argument("--dist-top", type=int, default=5, help="测距候选数（默认 5）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存强制刷新")
    ap.add_argument("--qd-speed", type=float, default=None,
                    help="量子巡航速度 Mm/s（Erkul driveSpeed，如 262）。默认按 --ship 查内置标定表")
    args = ap.parse_args()
    t_start = time.time()

    if args.refresh:
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()

    scu = args.scu
    if args.ship:
        print(f"[info] 从 UEX vehicles 解析船名 '{args.ship}' 舱容…", file=sys.stderr)
        vd = fetch("vehicles", cache_key="vehicles", ttl=24 * 3600, attempts=2, timeout=20)
        if not vd or not isinstance(vd.get("data"), list):
            sys.exit("vehicles 拉取失败，无法解析船名 → 舱容")
        hits = [v for v in vd["data"] if norm(args.ship) in norm(v.get("name")) and v.get("is_spaceship")]
        if not hits:
            print("UEX vehicles 中无匹配。相近船名：",
                  ", ".join(sorted({v.get("name") for v in vd["data"] if v.get("is_spaceship")})[:40]), file=sys.stderr)
            sys.exit(f"未找到船名匹配 '{args.ship}'")
        exact = [v for v in hits if norm(v.get("name")) == norm(args.ship)]
        pick = (exact or hits)[0]
        scu = float(pick["scu"])
        print(f"[info] 匹配: {pick['name']} → 舱容 {scu:.0f} SCU", file=sys.stderr)
    if not scu:
        sys.exit("必须提供 --scu 或 --ship")

    # 量子巡航速度：--qd-speed 显式指定 > 船名查标定表 > 兜底 250
    qd_speed = args.qd_speed * 1e6 if args.qd_speed else None
    if qd_speed is None and args.ship:
        qd_speed = SHIP_QD_SPEED.get(norm(args.ship))
    if qd_speed is None:
        qd_speed = DEFAULT_QD_SPEED
        print(f"[info] QD 速度未标定，用兜底 {DEFAULT_QD_SPEED/1e6:.0f} Mm/s（--qd-speed 可指定 Erkul 实测值）", file=sys.stderr)

    budget = args.capital if args.full else args.capital / 2
    max_price = budget / scu

    _t = time.time()
    print("[info] 拉取 commodities…", file=sys.stderr)
    coms_d = fetch("commodities", cache_key="commodities", ttl=3600)
    if not coms_d:
        sys.exit("commodities 拉取失败")
    coms = {c["id"]: c for c in coms_d["data"]}
    print(f"[timing] commodities {time.time()-_t:.1f}s", file=sys.stderr)

    _t = time.time()
    print("[info] 拉取 terminals（装卸/箱型元数据）…", file=sys.stderr)
    terms_by_id, terms_by_code = load_terms()
    print(f"[timing] terminals {time.time()-_t:.1f}s", file=sys.stderr)

    _t = time.time()
    print("[info] 拉取价格数据…", file=sys.stderr)
    price_rows, mode = load_prices(coms, terms_by_id)
    cands = []

    def term_sys_ok(t, sysname):
        return not sysname or (t is not None and norm(sysname) in norm(t.get("star_system_name")))

    def term_space_ok(t):
        return not args.space_only or (t is not None and bool(t.get("space_station_name")))

    if mode == "prices":
        # —— prices 模式：逐站商品价差（prices_all 成功时）——
        buys, sells = {}, {}
        for r in price_rows:
            c = coms.get(r["id_commodity"])
            if not c or not c.get("is_available_live"):
                continue
            t = (terms_by_id or {}).get(r["id_terminal"]) or (terms_by_code or {}).get(r.get("terminal_code"))
            r["_term"] = t  # 全量失败时先置 None，候选阶段懒加载补
            if t is not None and not t.get("is_available_live"):
                continue
            pb, ps = r.get("price_buy") or 0, r.get("price_sell") or 0
            sb, ss = r.get("status_buy"), r.get("status_sell")
            if pb > 0 and sb and sb >= 5 and (r.get("scu_buy") or 0) >= scu and pb <= max_price:
                if not term_sys_ok(t, args.origin_system) or not term_space_ok(t):
                    continue
                oname = norm(r.get("terminal_name")) + (norm(t["name"]) if t else "")
                if not args.origin or norm(args.origin) in oname or norm(args.origin) == norm(r.get("terminal_code")):
                    buys.setdefault(r["id_commodity"], []).append(r)
            if ps > 0 and ss and ss <= 2:
                if not term_sys_ok(t, args.dest_system) or not term_space_ok(t):
                    continue
                # 剩余需求 = scu_sell(官方:预测需求) − scu_sell_stock(官方:站点报告库存)
                # 语义来源: uexcorp.space/api/documentation/id/get_commodities_prices
                # 注意: scu_sell_users 官方定义为「近15天用户成交均量」而非在途量，不可当在途减
                demand, stock = r.get("scu_sell"), r.get("scu_sell_stock")
                if demand and stock is not None:
                    remain = demand - stock
                    if remain < scu:
                        continue  # 剩余需求装不满一船，飞到也卖不动
                    r["_demand_remaining"] = remain
                else:
                    r["_demand_remaining"] = None
                sells.setdefault(r["id_commodity"], []).append(r)
        for cid, bl in buys.items():
            if cid not in sells:
                continue
            c = coms[cid]
            if args.commodity and norm(args.commodity) not in norm(c["name"]):
                continue
            for b in bl:
                for s in sells[cid]:
                    if s["id_terminal"] == b["id_terminal"]:
                        continue
                    if args.dest:
                        sname = norm(s.get("terminal_name")) + (norm(s["_term"]["name"]) if s.get("_term") else "")
                        if norm(args.dest) not in sname and norm(args.dest) != norm(s.get("terminal_code")):
                            continue
                    roi = (s["price_sell"] - b["price_buy"]) / b["price_buy"] * 100
                    if roi < args.min_roi:
                        continue
                    cands.append({"commodity": c["name"], "illegal": bool(c.get("is_illegal")),
                                  "buy": b, "sell": s, "roi": roi,
                                  "profit": (s["price_sell"] - b["price_buy"]) * scu,
                                  "cost": b["price_buy"] * scu})
    else:
        # —— routes 模式：官方路线直接过滤（prices_all 失败时）——
        route_rows = price_rows
        seen = set()
        for r in route_rows:
            c = coms.get(r.get("id_commodity"))
            if not c or not c.get("is_available_live"):
                continue
            po, pd = r.get("price_origin") or 0, r.get("price_destination") or 0
            so, sd = r.get("status_origin"), r.get("status_destination")
            if po <= 0 or pd <= 0 or po > max_price or not so or so < 5 or not sd or sd > 2:
                continue
            if (r.get("scu_origin") or 0) < scu:
                continue
            if args.origin and norm(args.origin) not in norm(r.get("origin_terminal_name") or "") + norm(r.get("origin_terminal_code") or ""):
                continue
            if args.origin_system and norm(args.origin_system) not in norm(r.get("origin_star_system_name") or ""):
                continue
            if args.dest_system and norm(args.dest_system) not in norm(r.get("destination_star_system_name") or ""):
                continue
            if args.space_only and not (r.get("is_space_station_origin") and r.get("is_space_station_destination")):
                continue
            if args.dest and norm(args.dest) not in norm(r.get("destination_terminal_name") or "") + norm(r.get("destination_terminal_code") or ""):
                continue
            if args.commodity and norm(args.commodity) not in norm(c["name"]):
                continue
            roi = r.get("price_roi") or ((pd - po) / po * 100)
            if roi < args.min_roi:
                continue
            key = (r.get("id_terminal_origin"), r.get("id_terminal_destination"), r.get("id_commodity"))
            if key in seen:
                continue
            seen.add(key)
            buy = {"terminal_name": r.get("origin_terminal_name"), "terminal_code": r.get("origin_terminal_code"),
                   "price_buy": po, "scu_buy": r.get("scu_origin"), "status_buy": so,
                   "id_terminal": r.get("id_terminal_origin"), "_term": terms_by_id.get(r.get("id_terminal_origin"))}
            sell = {"terminal_name": r.get("destination_terminal_name"), "terminal_code": r.get("destination_terminal_code"),
                    "price_sell": pd, "scu_sell": r.get("scu_destination"), "status_sell": sd,
                    "id_terminal": r.get("id_terminal_destination"), "_term": terms_by_id.get(r.get("id_terminal_destination"))}
            cands.append({"commodity": c["name"], "illegal": bool(c.get("is_illegal")),
                          "buy": buy, "sell": sell, "roi": roi,
                          "profit": (pd - po) * scu, "cost": po * scu,
                          "dist": r.get("distance"), "_official": True})
    print(f"[timing] prices/routes {time.time()-_t:.1f}s", file=sys.stderr)
    cands.sort(key=lambda x: -x["profit"])
    cands = cands[: args.top]

    if not terms_by_id:
        print(f"[info] 懒加载站点元数据（候选涉及 {len(set(r['terminal_name'] for c in cands for r in (c['buy'], c['sell'])))} 个站）…", file=sys.stderr)
        resolve_terms_for(cands, terms_by_id, terms_by_code)

    def add_dist(c):
        if c.get("dist"):
            return
        bt, st = c["buy"].get("_term"), c["sell"].get("_term")
        c["dist"] = None
        if bt and st:
            d = fetch("terminals_distances",
                      {"id_terminal_origin": bt["id"], "id_terminal_destination": st["id"]},
                      cache_key=f"dist_{bt['id']}_{st['id']}", ttl=12 * 3600, attempts=2)
            if d and isinstance(d.get("data"), dict):
                c["dist"] = d["data"].get("distance")

    if cands and cands[0].get("_official"):
        # routes 模式：官方数据即验证（无需测距/交叉验证）
        verified = {(norm(c["commodity"]), c["buy"].get("id_terminal"), c["sell"].get("id_terminal")): c["roi"]
                    for c in cands}
    else:
        _t = time.time()
        print(f"[info] 测距 {min(args.dist_top, len(cands))} 条…", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(add_dist, cands[: args.dist_top]))
        print(f"[timing] 测距 {time.time()-_t:.1f}s", file=sys.stderr)

        def verify(c):
            bt = c["buy"].get("_term")
            if not bt:
                return []
            d = fetch("commodities_routes", {"id_terminal_origin": bt["id"]},
                      cache_key=f"routes_{bt['id']}", attempts=1, timeout=8)
            return [(norm(r.get("commodity_name")), r.get("id_terminal_origin"), r.get("id_terminal_destination"),
                     r.get("price_roi")) for r in (d or {}).get("data", [])]

        uniq = {}
        for c in cands:
            bt = c["buy"].get("_term")
            uniq.setdefault(bt["id"] if bt else -1, c)
        verified = {}
        print(f"[info] 官方路线交叉验证 {min(3, len(uniq))} 个买入站…", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(verify, c) for c in list(uniq.values())[:3]]
            for fu in as_completed(futs):
                for cn, oid, did, roi in fu.result():
                    verified[(cn, oid, did)] = roi
        print(f"[timing] 交叉验证 {time.time()-_t:.1f}s", file=sys.stderr)

    def load_mark(r):
        t = r.get("_term")
        if t is None:
            return "?"
        return "自动" if t.get("is_auto_load") else f"手动(≤{t.get('max_container_size')}SCU箱)"

    # —— 时薪估算：单腿耗时（含装卸）+ 保守口径（含空驶返程 QT）——
    for c in cands:
        leg = leg_minutes(float(c["dist"]) if c.get("dist") else None, scu,
                          c["buy"].get("_term"), c["sell"].get("_term"), qd_speed)
        if leg is None:
            c["_hourly"] = None
            continue
        # 空驶返程：QT 时间 + 一次起降（无装卸）
        back_qt = (float(c["dist"]) * 1e9 / qd_speed + SPOOL_S) / 60
        roundtrip = leg + back_qt + DOCK_MIN
        c["_hourly"] = c["profit"] / roundtrip * 60  # aUEC/h，保守口径

    def sys_of(r):
        t = r.get("_term")
        return (t or {}).get("star_system_name") or "?"

    print(f"\n# 规划参数: {scu:.0f} SCU{(' (' + args.ship + ')') if args.ship else ''} · 本金 {args.capital/1e4:.0f}W"
          f"{'（满仓）' if args.full else '（半仓红线 ' + format(budget/1e4, '.0f') + 'W）'}"
          f" · 买价上限 {max_price:,.0f}/SCU · ROI≥{args.min_roi}%\n")
    print("| # | 商品 | 买入站 @价(库存) | 卖出站 @价 | 星系 | 距离 | 装/卸 | ROI | 利润 | 成本 | 时薪 | 验证 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(cands, 1):
        b, s = c["buy"], c["sell"]
        v = verified.get((norm(c["commodity"]), b.get("id_terminal"), s.get("id_terminal")))
        vtxt = f"✓{v:.0f}%" if v is not None else "—"
        dist = f"{float(c['dist']):.0f}Gm" if c.get("dist") else "?"
        hourly = f"{c['_hourly']/1e4:.0f}W/h" if c.get("_hourly") else "?"
        flag = "⚠️" if c["illegal"] else ""
        rem = s.get("_demand_remaining")
        remtxt = f"(需剩{rem:,.0f})" if rem is not None else ""
        route = sys_of(b)
        if sys_of(b) != sys_of(s):
            route += f"→{sys_of(s)}⚠️跨星系"
        print(f"| {i} | {c['commodity']}{flag} | {b['terminal_name']} @{b['price_buy']:,.0f}({b.get('scu_buy',0):,.0f}) "
              f"| {s['terminal_name']} @{s['price_sell']:,.0f}{remtxt} | {route} | {dist} "
              f"| {load_mark(b)}/{load_mark(s)} | {c['roi']:.1f}% | {c['profit']/1e4:.1f}W | {c['cost']/1e4:.0f}W | {hourly} | {vtxt} |")
    print(f"\n时薪口径: QD {qd_speed/1e6:.0f} Mm/s · 含单腿装卸 + 空驶返程（保守；找到返程货再上浮）")
    print("需剩 = 卖出端剩余需求（官方口径: scu_sell 预测需求 − scu_sell_stock 站点库存），< 一船舱容的路线已过滤")
    print(f"数据: UEX API 2.0 · 缓存 ≤30 分钟 · 出发前请用 --refresh 复核库存 · 总耗时 {time.time()-t_start:.0f}s")


if __name__ == "__main__":
    main()
