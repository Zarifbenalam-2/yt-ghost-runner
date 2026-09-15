"""OFFLINE unit tests for sweeper.py — burn pass with a fake gate."""

import json
import os
import unittest
from pathlib import Path

from ipshelf.core import sweeper
from ipshelf.core.tests.base import TmpDataTestCase, make_exit


class TestSweep(TmpDataTestCase):

    def setUp(self):
        super().setUp()
        # 3 gold exits: two OK, one dead; one bound channel on a dead one
        self.write_pool([
            make_exit(addr="ok1:1", score=90, channels=["@A"]),
            make_exit(addr="ok2:1", score=80),
            make_exit(addr="dead:1", score=70, channels=["@B"]),
            make_exit(addr="burned:1", score=60, status="burned"),
        ])
        self.write_bindings({
            "@A": {"region": "US", "sticky_ip": "ok1:1",
                   "bound_since": "t",
                   "history": [{"ip": "ok1:1", "from": "t", "to": None,
                                "reason": "initial"}]},
            "@B": {"region": "US", "sticky_ip": "dead:1",
                   "bound_since": "t",
                   "history": [{"ip": "dead:1", "from": "t", "to": None,
                                "reason": "initial"}]},
        })

    def test_sweep_burns_non_ok(self):
        def gate(addr, proto):
            return {"ok": addr != "dead:1", "playability":
                    "OK" if addr != "dead:1" else "DEAD"}
        report = sweeper.sweep(gate_fn=gate)

        self.assertEqual(report["checked"], 3)  # only gold exits
        self.assertEqual(report["alive"], 2)
        self.assertEqual(report["burned"], 1)
        self.assertEqual(report["released_channels"], ["@B"])

        pool = self.read_json("shelf_pool.json")
        addrs = {e["addr"] for e in pool["exits"]}
        self.assertNotIn("dead:1", addrs)   # deleted (burn-and-delete)
        self.assertIn("ok1:1", addrs)       # survivors kept
        self.assertIn("ok2:1", addrs)
        self.assertIn("burned:1", addrs)   # non-gold wasn't even checked

    def test_sweep_keeps_ok_and_stamps(self):
        def gate(addr, proto):
            return {"ok": True, "playability": "OK"}
        report = sweeper.sweep(gate_fn=gate)
        self.assertEqual(report["alive"], 3)
        self.assertEqual(report["burned"], 0)
        self.assertEqual(report["released_channels"], [])
        pool = self.read_json("shelf_pool.json")
        for e in pool["exits"]:
            if e["status"] == "gold":
                self.assertTrue(e["last_gold_ok"])
                self.assertGreaterEqual(e["ok_cycles"], 2)

    def test_sweep_releases_channels_on_burn(self):
        def gate(addr, proto):
            return {"ok": addr != "dead:1"}
        sweeper.sweep(gate_fn=gate)
        b = self.read_json("bindings.json")
        # @A keeps its sticky, @B released but keeps region
        self.assertEqual(b["@A"]["sticky_ip"], "ok1:1")
        self.assertIsNone(b["@B"]["sticky_ip"])
        self.assertEqual(b["@B"]["region"], "US")
        self.assertIsNotNone(b["@B"]["history"][-1]["to"])

    def test_sweep_report_file_written(self):
        def gate(addr, proto):
            return {"ok": True, "playability": "OK"}
        report = sweeper.sweep(gate_fn=gate)
        sweeps_dir = Path(str(self.data)) / "sweeps"
        files = list(sweeps_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        with open(files[0], "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["checked"], report["checked"])
        self.assertEqual(saved["alive"], report["alive"])
    def test_sweep_report_path_shape(self):
        p = sweeper.sweep_report_path("2026-09-10T12:00:00Z")
        self.assertTrue(str(p).endswith(".json"))
        self.assertIn("sweeps", str(p))
        # no colons (Windows-hostile) in the filename
        name = Path(p).name
        self.assertNotIn(":", name)

    def test_sweep_gate_error_counts_as_fail(self):
        def gate(addr, proto):
            raise RuntimeError("gate exploded")
        report = sweeper.sweep(gate_fn=gate)
        self.assertEqual(report["burned"], 3)
        self.assertEqual(report["alive"], 0)


if __name__ == "__main__":
    unittest.main()
