"""OFFLINE unit tests for assigner.py — the draw, using a FAKE gate."""

import unittest

from ipshelf.core import assigner
from ipshelf.core.tests.base import TmpDataTestCase, make_exit


def fake_gate(results):
    """gate_fn factory: results = {"addr": True/False}. Unknown addr -> ok."""
    def gate_fn(addr, proto):
        if addr in results:
            return {"ok": results[addr]}
        return {"ok": True}
    return gate_fn


class TestDrawFresh(TmpDataTestCase):

    def setUp(self):
        super().setUp()
        self.write_pool([
            make_exit(addr="best:1", score=95),
            make_exit(addr="mid:1", score=60),
            make_exit(addr="worst:1", score=10),
        ])
        # region pre-locked for the channel (LAW 3: draw needs one)
        self.write_bindings({"@Chan": {"region": "US", "history": []}})

    def test_draw_picks_best_and_binds(self):
        res = assigner.draw_for_channel("@Chan")
        self.assertNotIn("error", res)
        self.assertEqual(res["addr"], "best:1")   # score sorted
        self.assertTrue(res["fresh"])
        self.assertEqual(res["proto"], "HTTP")
        self.assertEqual(res["proxy_url"], "http://best:1")
        self.assertEqual(res["proxy_variants"], ["http://best:1"])
        # persisted
        pool = self.read_json("shelf_pool.json")
        self.assertEqual(pool["exits"][0]["channels"], ["@Chan"])
        b = self.read_json("bindings.json")["@Chan"]
        self.assertEqual(b["region"], "US")
        self.assertEqual(b["sticky_ip"], "best:1")
        self.assertEqual(b["history"][-1]["reason"], "initial")
        self.assertIsNone(b["history"][-1]["to"])

    def test_draw_no_region_returns_error(self):
        res = assigner.draw_for_channel("@NewChan")  # no binding, no arg
        self.assertEqual(res["error"], "no_region")

    def test_draw_conflicting_region_locked(self):
        res = assigner.draw_for_channel("@Chan", region="KR")
        self.assertEqual(res["error"], "region_locked")
        self.assertEqual(res["current"], "US")

    def test_draw_matching_explicit_region_ok(self):
        res = assigner.draw_for_channel("@Chan", region="US")
        self.assertEqual(res["addr"], "best:1")

    def test_draw_uses_binding_region(self):
        self.write_bindings({"@Chan": {"region": "KR", "sticky_ip": None,
                                       "history": []}})
        self.write_pool(self.read_json("shelf_pool.json")["exits"] +
                        [make_exit(addr="kr:1", cc="KR", score=10)])
        res = assigner.draw_for_channel("@Chan")
        self.assertEqual(res["addr"], "kr:1")

    def test_channel_key_whitespace_stripped(self):
        res = assigner.draw_for_channel("  @Chan  ")
        self.assertEqual(res["addr"], "best:1")
        self.assertIn("@Chan", self.read_json("bindings.json"))

    def test_gate_skips_failing_candidates(self):
        gate = fake_gate({"best:1": False, "mid:1": False})
        res = assigner.draw_for_channel("@Chan", gate_fn=gate)
        self.assertEqual(res["addr"], "worst:1")
        self.assertTrue(res["fresh"])


class TestDrawSticky(TmpDataTestCase):

    def setUp(self):
        super().setUp()
        self.write_pool([
            make_exit(addr="sticky:1", score=95, channels=["@Chan"]),
            make_exit(addr="other:1", score=50),
        ])
        self.write_bindings({"@Chan": {
            "region": "US", "sticky_ip": "sticky:1",
            "bound_since": "2026-09-01T00:00:00Z",
            "history": [{"ip": "sticky:1", "from": "2026-09-01T00:00:00Z",
                         "to": None, "reason": "initial"}]}})

    def test_sticky_reuse_without_gate(self):
        res = assigner.draw_for_channel("@Chan")
        self.assertEqual(res["addr"], "sticky:1")
        self.assertFalse(res["fresh"])  # reused, not re-bound

    def test_sticky_reuse_on_gate_pass(self):
        res = assigner.draw_for_channel("@Chan", gate_fn=fake_gate({}))
        self.assertEqual(res["addr"], "sticky:1")
        self.assertFalse(res["fresh"])

    def test_sticky_gate_fail_burns_and_redraws(self):
        gate = fake_gate({"sticky:1": False, "other:1": True})
        res = assigner.draw_for_channel("@Chan", gate_fn=gate)
        # old IP burned and DELETED from the pool (burn-and-delete)
        self.assertEqual(res["addr"], "other:1")
        self.assertTrue(res["fresh"])
        pool = self.read_json("shelf_pool.json")
        addrs = [e["addr"] for e in pool["exits"]]
        self.assertNotIn("sticky:1", addrs)
        self.assertIn("other:1", addrs)
        # binding updated to new IP, history has both, old closed w/ gate_fail
        b = self.read_json("bindings.json")["@Chan"]
        self.assertEqual(b["sticky_ip"], "other:1")
        self.assertEqual(len(b["history"]), 2)
        self.assertEqual(b["history"][0]["reason"], "gate_fail")
        self.assertIsNotNone(b["history"][0]["to"])
        self.assertEqual(b["history"][1]["reason"], "replace")
        # new entry now serves the channel
        other = [e for e in pool["exits"] if e["addr"] == "other:1"][0]
        self.assertEqual(other["channels"], ["@Chan"])

    def test_stale_sticky_redraws(self):
        # binding points at an IP that no longer exists in the pool
        self.write_bindings({"@Chan": {
            "region": "US", "sticky_ip": "ghost:1",
            "history": [{"ip": "ghost:1", "from": "t", "to": None,
                         "reason": "initial"}]}})
        res = assigner.draw_for_channel("@Chan")
        self.assertEqual(res["addr"], "other:1")  # best free = sticky gone


class TestShelfEmpty(TmpDataTestCase):

    def test_all_candidates_fail_gate(self):
        self.write_pool([make_exit(addr="a:1"), make_exit(addr="b:1")])
        self.write_bindings({"@Chan": {"region": "US", "history": []}})
        gate = fake_gate({"a:1": False, "b:1": False})
        res = assigner.draw_for_channel("@Chan", gate_fn=gate)
        self.assertEqual(res["error"], "shelf_empty")
        self.assertEqual(res["region"], "US")
        # nothing was bound
        self.assertIsNone(self.read_json("bindings.json")["@Chan"].get("sticky_ip"))

    def test_empty_shelf(self):
        self.write_pool([])
        self.write_bindings({"@Chan": {"region": "US", "history": []}})
        res = assigner.draw_for_channel("@Chan")
        self.assertEqual(res["error"], "shelf_empty")


class TestRegionLock(TmpDataTestCase):

    def test_assign_region_creates_stub(self):
        res = assigner.assign_region("@Chan", "US")
        self.assertEqual(res, {"channel": "@Chan", "region": "US",
                              "locked": True})
        b = self.read_json("bindings.json")["@Chan"]
        self.assertEqual(b["region"], "US")
        self.assertEqual(b["history"], [])

    def test_region_locked_error(self):
        self.write_bindings({"@Chan": {"region": "US", "history": []}})
        res = assigner.assign_region("@Chan", "KR")
        self.assertEqual(res["error"], "region_locked")
        self.assertEqual(res["current"], "US")

    def test_same_region_idempotent(self):
        self.write_bindings({"@Chan": {"region": "US", "history": []}})
        res = assigner.assign_region("@Chan", "US")
        self.assertTrue(res["locked"])
        self.assertTrue(res["already"])

    def test_assign_region_then_draw(self):
        assigner.assign_region("@Chan", "US")
        self.write_pool([make_exit(addr="x:1")])
        res = assigner.draw_for_channel("@Chan")
        self.assertEqual(res["addr"], "x:1")
        self.assertEqual(self.read_json("bindings.json")["@Chan"]["region"],
                         "US")


class TestReleaseAndBurn(TmpDataTestCase):

    def setUp(self):
        super().setUp()
        self.write_pool([
            make_exit(addr="ip:1", channels=["@A", "@B"]),
            make_exit(addr="solo:1", channels=["@C"]),
        ])
        self.write_bindings({
            "@A": {"region": "US", "sticky_ip": "ip:1",
                   "bound_since": "t",
                   "history": [{"ip": "ip:1", "from": "t", "to": None,
                                "reason": "initial"}]},
            "@B": {"region": "US", "sticky_ip": "ip:1",
                   "bound_since": "t",
                   "history": [{"ip": "ip:1", "from": "t", "to": None,
                                "reason": "initial"}]},
            "@C": {"region": "KR", "sticky_ip": "solo:1",
                   "bound_since": "t",
                   "history": [{"ip": "solo:1", "from": "t", "to": None,
                                "reason": "initial"}]},
        })

    def test_release_keeps_region(self):
        res = assigner.release("@A", "manual")
        self.assertTrue(res["released"])
        pool = self.read_json("shelf_pool.json")
        ip1 = [e for e in pool["exits"] if e["addr"] == "ip:1"][0]
        self.assertEqual(ip1["channels"], ["@B"])  # only @A removed
        b = self.read_json("bindings.json")["@A"]
        self.assertIsNone(b["sticky_ip"])
        self.assertEqual(b["region"], "US")  # LAW 3: region survives
        self.assertIsNotNone(b["history"][-1]["to"])
        self.assertEqual(b["history"][-1]["reason"], "manual")

    def test_release_no_binding(self):
        res = assigner.release("@Ghost", "manual")
        self.assertFalse(res["released"])

    def test_burn_ip_deletes_and_releases(self):
        res = assigner.burn_ip("ip:1", "gate_fail")
        self.assertEqual(sorted(res["released"]), ["@A", "@B"])
        pool = self.read_json("shelf_pool.json")
        addrs = [e["addr"] for e in pool["exits"]]
        self.assertNotIn("ip:1", addrs)  # deleted entirely
        self.assertIn("solo:1", addrs)
        b = self.read_json("bindings.json")
        self.assertIsNone(b["@A"]["sticky_ip"])
        self.assertIsNone(b["@B"]["sticky_ip"])
        self.assertEqual(b["@A"]["region"], "US")   # region kept
        self.assertEqual(b["@A"]["history"][-1]["reason"], "gate_fail")
        self.assertIsNotNone(b["@A"]["history"][-1]["to"])

    def test_burn_unknown_addr(self):
        res = assigner.burn_ip("nope:1", "x")
        self.assertEqual(res["released"], [])


class TestNeedsHarvest(TmpDataTestCase):

    def test_free_capacity_below_demand(self):
        # 1 free exit with 0 used -> 5 free slots
        self.write_pool([make_exit(addr="a:1", channels=[])])
        self.assertTrue(assigner.needs_harvest("US", demand=10))
        self.assertFalse(assigner.needs_harvest("US", demand=5))
        self.assertFalse(assigner.needs_harvest("US", demand=4))

    def test_partial_slots_count(self):
        # 2 free exits, one with 4 channels (1 slot left) -> 6 free slots
        self.write_pool([
            make_exit(addr="a:1", channels=["@1", "@2", "@3", "@4"]),
            make_exit(addr="b:1"),
        ])
        self.assertTrue(assigner.needs_harvest("US", demand=7))
        self.assertFalse(assigner.needs_harvest("US", demand=6))

    def test_empty_region_needs_harvest(self):
        self.write_pool([])
        self.assertTrue(assigner.needs_harvest("US", demand=1))


class TestCapAcrossChannels(TmpDataTestCase):

    def test_ip_never_serves_more_than_cap(self):
        self.write_pool([make_exit(addr="only:1", score=99)])
        self.write_bindings({f"@ch{i}": {"region": "US", "history": []}
                             for i in range(assigner.shelf.CAP + 1)})
        for i in range(assigner.shelf.CAP):
            res = assigner.draw_for_channel(f"@ch{i}")
            self.assertEqual(res["addr"], "only:1")
        # CAP-th draw must fail: shelf now empty for everyone else
        res = assigner.draw_for_channel(f"@ch{assigner.shelf.CAP}")
        self.assertEqual(res["error"], "shelf_empty")
        pool = self.read_json("shelf_pool.json")
        self.assertEqual(len(pool["exits"][0]["channels"]),
                         assigner.shelf.CAP)


if __name__ == "__main__":
    unittest.main()
