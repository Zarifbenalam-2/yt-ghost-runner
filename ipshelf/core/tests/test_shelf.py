"""OFFLINE unit tests for shelf.py — store, CAP, free_ips, merge_exits."""

import unittest

from ipshelf.core import shelf
from ipshelf.core.tests.base import TmpDataTestCase, make_exit


class TestLoadSave(TmpDataTestCase):

    def test_empty_when_missing(self):
        self.assertEqual(shelf.load_pool()["exits"], [])
        self.assertEqual(shelf.load_bindings(), {})

    def test_roundtrip_pool_and_bindings(self):
        pool = self.write_pool([make_exit(addr="9.9.9.9:80")])
        b = self.write_bindings({"@Ch": {"region": "US",
                                         "sticky_ip": "9.9.9.9:80",
                                         "bound_since": "x",
                                         "history": []}})
        loaded = shelf.load_pool()
        self.assertEqual(loaded["exits"][0]["addr"], "9.9.9.9:80")
        self.assertIn("updated", loaded)
        self.assertEqual(shelf.load_bindings(), b)
        # atomic write leaves no .tmp files behind
        leftovers = [p.name for p in self.data.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_get_exit(self):
        self.write_pool([make_exit(addr="1.1.1.1:1"),
                         make_exit(addr="2.2.2.2:2")])
        pool = shelf.load_pool()
        self.assertEqual(shelf.get_exit(pool, "2.2.2.2:2")["addr"],
                         "2.2.2.2:2")
        self.assertIsNone(shelf.get_exit(pool, "3.3.3.3:3"))


class TestCapEnforcement(TmpDataTestCase):

    def test_free_ips_respects_cap(self):
        exits = [
            make_exit(addr="1.1.1.1:1", score=90,
                      channels=["@a", "@b", "@c", "@d", "@e"]),  # full (5)
            make_exit(addr="2.2.2.2:2", score=80, channels=["@a"]),  # free
            make_exit(addr="3.3.3.3:3", score=70),                  # free
        ]
        pool = self.write_pool(exits)
        free = shelf.free_ips(pool, "US")
        addrs = [e["addr"] for e in free]
        self.assertNotIn("1.1.1.1:1", addrs)  # CAP=5 blocks it
        self.assertIn("2.2.2.2:2", addrs)
        self.assertIn("3.3.3.3:3", addrs)

    def test_free_ips_excludes_non_gold(self):
        pool = self.write_pool([
            make_exit(addr="1.1.1.1:1", status="burned"),
            make_exit(addr="2.2.2.2:2"),
        ])
        free = shelf.free_ips(pool, "US")
        self.assertEqual([e["addr"] for e in free], ["2.2.2.2:2"])

    def test_free_ips_sorted_score_desc(self):
        pool = self.write_pool([
            make_exit(addr="low:1", score=10),
            make_exit(addr="high:1", score=99),
            make_exit(addr="mid:1", score=50),
        ])
        free = shelf.free_ips(pool, "US")
        self.assertEqual([e["addr"] for e in free],
                         ["high:1", "mid:1", "low:1"])

    def test_cap_constant(self):
        self.assertEqual(shelf.CAP, 5)


class TestFreeIpsRegions(TmpDataTestCase):

    def test_country_region_filter(self):
        pool = self.write_pool([
            make_exit(addr="us:1", cc="US"),
            make_exit(addr="kr:1", cc="KR"),
            make_exit(addr="mx:1", cc="MX"),
        ])
        free = shelf.free_ips(pool, "US")
        self.assertEqual([e["addr"] for e in free], ["us:1"])

    def test_t1_pseudo_region(self):
        pool = self.write_pool([
            make_exit(addr="us:1", cc="US", tier="T1"),
            make_exit(addr="kr:1", cc="KR", tier="T1"),
            make_exit(addr="mx:1", cc="MX", tier="T2"),
            make_exit(addr="pk:1", cc="PK", tier="T3"),
        ])
        free = shelf.free_ips(pool, "T1")
        addrs = {e["addr"] for e in free}
        self.assertEqual(addrs, {"us:1", "kr:1"})

    def test_shelf_regions_rows(self):
        pool = self.write_pool([
            make_exit(addr="us1:1", cc="US", tier="T1", channels=["@a"]),
            make_exit(addr="us2:1", cc="US", tier="T1"),
            make_exit(addr="kr1:1", cc="KR", tier="T1"),
            make_exit(addr="mx1:1", cc="MX", tier="T2"),
        ])
        rows = shelf.shelf_regions(pool)
        by_region = {r["region"]: r for r in rows}
        self.assertIn("T1", by_region)
        self.assertIn("US", by_region)
        self.assertIn("KR", by_region)
        self.assertIn("MX", by_region)
        # T1 row first
        self.assertEqual(rows[0]["region"], "T1")
        # US row (count 2) before KR/MX (count 1)
        self.assertEqual(rows[1]["region"], "US")
        us = by_region["US"]
        self.assertEqual(us["count"], 2)
        self.assertEqual(us["assigned"], 1)
        self.assertEqual(us["capacity"], 2 * shelf.CAP)
        self.assertEqual(us["free"], 2 * shelf.CAP - 1)  # 9 free slots
        self.assertTrue(us["needs_attention"] is False)  # 9 >= 2
        # T1 aggregation across US+KR: 3 exits, 1 channel bound
        t1 = by_region["T1"]
        self.assertEqual(t1["count"], 3)
        self.assertEqual(t1["assigned"], 1)
        self.assertEqual(t1["capacity"], 3 * shelf.CAP)
        self.assertEqual(t1["free"], 3 * shelf.CAP - 1)
        self.assertFalse(t1["needs_attention"])
        # a shelf with zero free slots flags attention
        pool2 = {"exits": [make_exit(addr="full:1", cc="MX",
                                     channels=["@1", "@2", "@3", "@4", "@5"])]}
        rows2 = shelf.shelf_regions(pool2)
        mx = [r for r in rows2 if r["region"] == "MX"][0]
        self.assertEqual(mx["free"], 0)
        self.assertTrue(mx["needs_attention"])


class TestMergeExits(TmpDataTestCase):

    def test_merge_preserves_channels_and_first_seen(self):
        old = make_exit(addr="1.1.1.1:1", channels=["@Zarif"],
                        first_seen="2026-01-01T00:00:00Z", score=50,
                        ok_cycles=2)
        pool = self.write_pool([old])
        refreshed_grade = make_exit(addr="1.1.1.1:1", score=91.5,
                                    channels=[],  # hunt machine has no channel data
                                    first_seen="2026-12-31T00:00:00Z")
        res = shelf.merge_exits(pool, [refreshed_grade])
        self.assertEqual(res, {"added": 0, "refreshed": 1})
        e = shelf.get_exit(pool, "1.1.1.1:1")
        self.assertEqual(e["channels"], ["@Zarif"])          # kept
        self.assertEqual(e["first_seen"], "2026-01-01T00:00:00Z")  # kept
        self.assertEqual(e["score"], 91.5)                   # refreshed
        self.assertEqual(e["status"], "gold")
        self.assertEqual(e["ok_cycles"], 3)                  # bumped
        self.assertTrue(e["last_gold_ok"])                   # stamped

    def test_merge_appends_new_with_empty_channels(self):
        pool = self.write_pool([])
        res = shelf.merge_exits(pool, [make_exit(addr="5.5.5.5:5")])
        self.assertEqual(res, {"added": 1, "refreshed": 0})
        self.assertEqual(len(pool["exits"]), 1)
        self.assertEqual(pool["exits"][0]["channels"], [])
        self.assertEqual(pool["exits"][0]["status"], "gold")

    def test_merge_mixed(self):
        pool = self.write_pool([make_exit(addr="1.1.1.1:1")])
        res = shelf.merge_exits(pool, [make_exit(addr="1.1.1.1:1"),
                                       make_exit(addr="2.2.2.2:2")])
        self.assertEqual(res, {"added": 1, "refreshed": 1})
        self.assertEqual(len(pool["exits"]), 2)


class TestConstants(unittest.TestCase):

    def test_t1_set(self):
        self.assertEqual(shelf.T1_SET,
                         {"US", "GB", "CA", "DE", "FR", "NL", "SE", "CH",
                          "AT", "DK", "NO", "FI", "IE", "BE", "LU", "JP",
                          "SG", "AU", "NZ", "KR", "HK", "TW"})

    def test_now_ts_format(self):
        ts = shelf.now_ts()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


if __name__ == "__main__":
    unittest.main()
