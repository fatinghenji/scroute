#!/usr/bin/env python3
"""plan_route 纯函数单元测试（零依赖：python3 -m unittest discover）。

覆盖: 名称/代号匹配、卖出端需求过滤、时薪模型、候选三关筛选、--refresh 缓存清理范围。
"""
import pathlib
import tempfile
import types
import unittest

import plan_route as pr
import scroute_net as net

try:  # pycryptodome 仅 dual 需要，未装时跳过 sctt 用例
    import sctt_routes
except ImportError:
    sctt_routes = None


class NormTest(unittest.TestCase):
    def test_strips_and_lowercases(self):
        self.assertEqual(pr.norm("Patch City"), "patchcity")
        self.assertEqual(pr.norm("Baijini-01"), "baijini01")

    def test_none_and_empty(self):
        self.assertEqual(pr.norm(None), "")
        self.assertEqual(pr.norm(""), "")


class NameMatchTest(unittest.TestCase):
    """prices 模式口径：目标在站名内，或与代号完全相等。"""

    def test_name_containment(self):
        self.assertTrue(pr.name_match("baijini", "Baijini Gateway", "BAIJI"))
        self.assertTrue(pr.name_match("gateway", "Baijini Gateway", "BAIJI"))
        self.assertFalse(pr.name_match("orem", "Baijini Gateway", "BAIJI"))

    def test_code_exact(self):
        self.assertTrue(pr.name_match("BAIJI", "Baijini Gateway", "BAIJI"))
        # 部分代号不匹配（"bjg" 既不在站名内，也不等于代号 "BJG1"）
        self.assertFalse(pr.name_match("bjg", "Baijini Gateway", "BJG1"))

    def test_empty_target(self):
        self.assertTrue(pr.name_match("", "Baijini Gateway", "BAIJI"))
        self.assertTrue(pr.name_match(None, None, None))


class RouteMatchTest(unittest.TestCase):
    """routes 模式口径：目标在「站名+代号」内即可（含部分代号）。"""

    def test_code_containment(self):
        # routes 模式口径：部分代号即可（"bjg" 是 "BJG1" 的前缀）
        self.assertTrue(pr.route_match("bjg", None, "BJG1"))

    def test_name_containment(self):
        self.assertTrue(pr.route_match("shubin", "Shubin SM0-22", "SM022"))


class DemandOkTest(unittest.TestCase):
    def test_enough_demand(self):
        self.assertEqual(pr.demand_ok(5000, 1000, 640), (True, 4000))

    def test_not_enough(self):
        self.assertEqual(pr.demand_ok(500, 1000, 640), (False, -500))

    def test_missing_fields_pass(self):
        self.assertEqual(pr.demand_ok(None, 1000, 640), (True, None))
        self.assertEqual(pr.demand_ok(5000, None, 640), (True, None))


class LegMinutesTest(unittest.TestCase):
    def test_no_distance(self):
        self.assertIsNone(pr.leg_minutes(None, 640, None, None, 262e6))

    def test_auto_terminals(self):
        auto = {"is_auto_load": True}
        m = pr.leg_minutes(68, 640, auto, auto, 262e6)
        qt = (68e9 / 262e6 + pr.SPOOL_S) / 60
        stop = pr.DOCK_MIN + max(2, 640 * pr.AUTO_MIN_PER_100SCU / 100)
        self.assertAlmostEqual(m, stop + qt + stop, places=6)

    def test_manual_terminals(self):
        manual = {"is_auto_load": False, "max_container_size": 8}
        m = pr.leg_minutes(68, 640, manual, manual, 262e6)
        qt = (68e9 / 262e6 + pr.SPOOL_S) / 60
        stop = pr.LAND_MIN + (640 / 8) * pr.MANUAL_BOX_S / 60
        self.assertAlmostEqual(m, stop + qt + stop, places=6)

    def test_unknown_terminal_auto_estimate(self):
        m = pr.leg_minutes(68, 640, None, None, 262e6)
        qt = (68e9 / 262e6 + pr.SPOOL_S) / 60
        stop = pr.DOCK_MIN + max(2, 640 * pr.AUTO_MIN_PER_100SCU / 100)
        self.assertAlmostEqual(m, stop + qt + stop, places=6)


class EstimateHourlyTest(unittest.TestCase):
    def test_roundtrip_conservative(self):
        cand = {"profit": 1_559_000, "dist": 68,
                "buy": {"_term": {"is_auto_load": True}},
                "sell": {"_term": {"is_auto_load": True}}}
        pr.estimate_hourly([cand], 640, 262e6)
        leg = pr.leg_minutes(68, 640, cand["buy"]["_term"], cand["sell"]["_term"], 262e6)
        back_qt = (68e9 / 262e6 + pr.SPOOL_S) / 60
        expected = cand["profit"] / (leg + back_qt + pr.DOCK_MIN) * 60
        self.assertAlmostEqual(cand["_hourly"], expected, places=6)

    def test_no_distance_no_hourly(self):
        cand = {"profit": 1_559_000, "dist": None,
                "buy": {"_term": None}, "sell": {"_term": None}}
        pr.estimate_hourly([cand], 640, 262e6)
        self.assertIsNone(cand["_hourly"])


class BuildCandsPricesTest(unittest.TestCase):
    def test_three_gates(self):
        coms = {1: {"name": "Quartz", "is_available_live": True, "is_illegal": False}}
        buy_term = {"id": 10, "name": "Shubin SM0-22", "code": "SM022",
                    "is_available_live": True, "is_auto_load": True, "star_system_name": "Pyro"}
        sell_term = {"id": 11, "name": "Pyro Gateway", "code": "PYGW",
                     "is_available_live": True, "is_auto_load": True, "star_system_name": "Stanton"}
        rows = [
            {"id_commodity": 1, "id_terminal": 10, "terminal_name": "Shubin SM0-22", "terminal_code": "SM022",
             "price_buy": 2964, "status_buy": 6, "scu_buy": 1050, "price_sell": 0,
             "status_sell": None, "scu_sell": None, "scu_sell_stock": None},
            {"id_commodity": 1, "id_terminal": 11, "terminal_name": "Pyro Gateway", "terminal_code": "PYGW",
             "price_buy": 0, "status_buy": None, "scu_buy": 0, "price_sell": 5400,
             "status_sell": 1, "scu_sell": 5000, "scu_sell_stock": 800},
        ]
        args = types.SimpleNamespace(origin=None, dest=None, commodity=None,
                                     origin_system=None, dest_system=None,
                                     space_only=False, min_roi=25)
        cands = pr.build_cands_prices(rows, coms, {10: buy_term, 11: sell_term}, {}, args, 640, 10_000)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["commodity"], "Quartz")
        self.assertAlmostEqual(c["roi"], (5400 - 2964) / 2964 * 100)
        self.assertEqual(c["profit"], (5400 - 2964) * 640)
        self.assertEqual(c["sell"]["_demand_remaining"], 4200)  # 5000 − 800

    def test_buy_origin_filter(self):
        coms = {1: {"name": "Quartz", "is_available_live": True, "is_illegal": False}}
        term = {"id": 10, "name": "Shubin SM0-22", "code": "SM022",
                "is_available_live": True, "is_auto_load": True, "star_system_name": "Pyro"}
        rows = [
            {"id_commodity": 1, "id_terminal": 10, "terminal_name": "Shubin SM0-22", "terminal_code": "SM022",
             "price_buy": 2964, "status_buy": 6, "scu_buy": 1050, "price_sell": 0,
             "status_sell": None, "scu_sell": None, "scu_sell_stock": None},
            {"id_commodity": 1, "id_terminal": 11, "terminal_name": "Pyro Gateway", "terminal_code": "PYGW",
             "price_buy": 0, "status_buy": None, "scu_buy": 0, "price_sell": 5400,
             "status_sell": 1, "scu_sell": 5000, "scu_sell_stock": 800},
        ]
        args = types.SimpleNamespace(origin="Elsewhere", dest=None, commodity=None,
                                     origin_system=None, dest_system=None,
                                     space_only=False, min_roi=25)
        self.assertEqual(pr.build_cands_prices(rows, coms, {}, {}, args, 640, 10_000), [])


class BuildCandsRoutesTest(unittest.TestCase):
    def _args(self):
        return types.SimpleNamespace(origin=None, dest=None, commodity=None,
                                     origin_system=None, dest_system=None,
                                     space_only=False, min_roi=25)

    def _row(self, scu_dest=5000):
        return {"id_commodity": 1, "price_origin": 2964, "price_destination": 5400,
                "status_origin": 6, "status_destination": 1,
                "scu_origin": 1050, "scu_destination": scu_dest,
                "origin_terminal_name": "Shubin SM0-22", "origin_terminal_code": "SM022",
                "destination_terminal_name": "Pyro Gateway", "destination_terminal_code": "PYGW",
                "id_terminal_origin": 10, "id_terminal_destination": 11,
                "price_roi": 82.2, "distance": 68}

    def test_basic_filter(self):
        coms = {1: {"name": "Quartz", "is_available_live": True, "is_illegal": False}}
        cands = pr.build_cands_routes([self._row()], coms, {}, self._args(), 640, 10_000)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertTrue(c["_official"])
        self.assertEqual(c["profit"], (5400 - 2964) * 640)

    def test_sell_demand_gate(self):
        # 卖出端预测需求 < 一船舱容 → 排除（对齐 prices 模式的「需剩」过滤）
        coms = {1: {"name": "Quartz", "is_available_live": True, "is_illegal": False}}
        self.assertEqual(pr.build_cands_routes([self._row(scu_dest=500)], coms, {}, self._args(), 640, 10_000), [])


class ClearCacheTest(unittest.TestCase):
    def test_only_price_caches_cleared(self):
        d = pathlib.Path(tempfile.mkdtemp())
        for name in ("prices_all", "p_5", "routes_10", "terminals", "vehicles",
                     "dist_1_2", "t_shubinsm022", "commodities"):
            (d / f"{name}.json").write_text("{}")
        pr.clear_price_caches(d)
        remaining = sorted(p.stem for p in d.glob("*.json"))
        self.assertEqual(remaining, ["commodities", "dist_1_2", "t_shubinsm022", "terminals", "vehicles"])


class NetProxyTest(unittest.TestCase):
    """scroute_net：直连→代理回退 + 直连已死记忆 + 代理失败自愈。"""

    def setUp(self):
        net.reset()
        self.orig_run = net.subprocess.run

    def tearDown(self):
        net.subprocess.run = self.orig_run
        net.reset()

    @staticmethod
    def _fake_run(seq):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            code, out = seq[len(calls) - 1]
            return types.SimpleNamespace(returncode=code, stdout=out)

        return fake_run, calls

    def test_direct_fail_then_proxy_success_marks_dead(self):
        net._proxy_ok[0] = True  # 跳过 TCP 探测
        fake_run, calls = self._fake_run([(28, ""), (0, '{"ok":1}')])
        net.subprocess.run = fake_run
        code, out = net.run_curl("http://x/api", 9, "UA")
        self.assertEqual((code, out), (0, '{"ok":1}'))
        self.assertIn("--noproxy", calls[0]) and self.assertIn("*", calls[0])
        self.assertIn("-x", calls[1]) and self.assertIn(net.PROXY, calls[1])
        self.assertTrue(net._direct_dead[0])  # 代理成功 → 记忆直连已死

    def test_direct_ok_uses_direct_only(self):
        fake_run, calls = self._fake_run([(0, "data")])
        net.subprocess.run = fake_run
        code, _ = net.run_curl("http://x/api", 9, "UA")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertFalse(net._direct_dead[0])

    def test_direct_dead_goes_straight_to_proxy(self):
        net._direct_dead[0] = True
        fake_run, calls = self._fake_run([(0, "data")])
        net.subprocess.run = fake_run
        code, _ = net.run_curl("http://x/api", 9, "UA")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("-x", calls[0]) and self.assertNotIn("--noproxy", calls[0])

    def test_proxy_dies_resets_dead(self):
        net._direct_dead[0] = True
        fake_run, calls = self._fake_run([(56, "")])
        net.subprocess.run = fake_run
        code, _ = net.run_curl("http://x/api", 9, "UA")
        self.assertEqual(code, 56)
        self.assertFalse(net._direct_dead[0])  # 自愈：退回直连再试

    def test_no_proxy_no_retry(self):
        net._proxy_ok[0] = False
        fake_run, calls = self._fake_run([(28, "")])
        net.subprocess.run = fake_run
        code, _ = net.run_curl("http://x/api", 9, "UA")
        self.assertEqual(code, 28)
        self.assertEqual(len(calls), 1)  # 不重试

    def test_post_body_and_headers(self):
        fake_run, calls = self._fake_run([(0, "{}")])
        net.subprocess.run = fake_run
        code, _ = net.run_curl("http://x/api", 9, "UA", {"X-T": "1"}, '{"a":1}')
        self.assertEqual(code, 0)
        self.assertIn("-d", calls[0]) and self.assertIn('{"a":1}', calls[0])
        self.assertIn("X-T: 1", calls[0])


class GetDelegationTest(unittest.TestCase):
    """plan_route._get 委托 scroute_net.run_curl（限速 + 解析）。"""

    def tearDown(self):
        net.reset()

    def test_get_success_parses_json(self):
        fake_run, calls = [], []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            return types.SimpleNamespace(returncode=0, stdout='{"ok": 1}')

        net.subprocess.run = fake_run
        self.assertEqual(pr._get("http://x/api", timeout=7), {"ok": 1})
        self.assertIn("--max-time", calls[0]) and self.assertIn("7", calls[0])
        self.assertIn("--noproxy", calls[0])  # 直连优先

    def test_get_failure_raises(self):
        net._proxy_ok[0] = False

        def fake_run(cmd, **kw):
            return types.SimpleNamespace(returncode=28, stdout="")

        net.subprocess.run = fake_run
        with self.assertRaises(RuntimeError):
            pr._get("http://x/api", timeout=7)


@unittest.skipIf(sctt_routes is None, "pycryptodome 未安装")
class ScttCurlTest(unittest.TestCase):
    """sctt_routes.curl 是 run_curl 的薄封装（带 Accept 头 + JSON POST）。"""

    def tearDown(self):
        net.reset()

    def test_curl_passes_headers_and_data(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            return types.SimpleNamespace(returncode=0, stdout='[]')

        net.subprocess.run = fake_run
        out = sctt_routes.curl("http://x/api", {"token": "T"}, '{"ship":"Railen"}')
        self.assertEqual(out, "[]")
        self.assertIn("Accept: application/json, text/plain, */*", calls[0])
        self.assertIn("token: T", calls[0])
        self.assertIn('{"ship":"Railen"}', calls[0])


if __name__ == "__main__":
    unittest.main()
