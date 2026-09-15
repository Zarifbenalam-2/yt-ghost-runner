"""Shared test fixture: point IPSHELF_DATA at a temp dir and build pools.

All tests in this package are OFFLINE — no network, no curl. The gate
is always a fake (dict of addr -> ok).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from ipshelf.core import shelf


def make_exit(addr="1.2.3.4:8080", proto="HTTP", cc="US", tier="T1",
              score=80.0, status="gold", channels=None, first_seen=None,
              ok_cycles=1):
    return {
        "addr": addr, "proto": proto,
        "egress_ip": addr.split(":")[0],
        "geo": {"country": "United States", "countryCode": cc,
                "region": "CA", "city": "LA", "isp": "x", "org": "x",
                "as": "AS1 x"},
        "cc": cc, "tier": tier, "type": "datacenter",
        "score": score, "latency_ms": 500, "bandwidth_mbps": 5.0,
        "playability": "OK", "page_ok": True, "github": "OK",
        "first_seen": first_seen or "2026-09-08T00:00:00Z",
        "last_gold_ok": "2026-09-08T00:00:00Z", "ok_cycles": ok_cycles,
        "channels": channels if channels is not None else [],
        "status": status,
    }


class TmpDataTestCase(unittest.TestCase):
    """Isolated data dir per test (IPSHELF_DATA env honored by shelf.py)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ipshelf-test-")
        self.data = Path(self._tmp.name)
        self._old = os.environ.get("IPSHELF_DATA")
        os.environ["IPSHELF_DATA"] = str(self.data)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("IPSHELF_DATA", None)
        else:
            os.environ["IPSHELF_DATA"] = self._old
        self._tmp.cleanup()

    # helpers -------------------------------------------------------------
    def write_pool(self, exits):
        pool = {"updated": shelf.now_ts(), "exits": exits}
        shelf.save_pool(pool)
        return pool

    def write_bindings(self, bindings):
        shelf.save_bindings(bindings)
        return bindings

    def read_json(self, name):
        with open(self.data / name, "r", encoding="utf-8") as f:
            return json.load(f)
