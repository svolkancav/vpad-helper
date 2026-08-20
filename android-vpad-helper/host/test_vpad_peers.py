#!/usr/bin/env python3
"""`@status peers=` — the line the tray reads to show who is connected.

    python -m unittest discover -s host -v

Why a test at all: the tray's strip is derived from this ONE line, and the
line is assembled from the slot pool rather than from the phone that just
said HELLO. That distinction is the whole point (a peer leaving must not
blank the others), and it is invisible to the eye — the window looks right
either way until a second phone joins.
"""
from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_host as host  # noqa: E402
import vpad_slots as slots  # noqa: E402


def _peers_line(pool: slots.SlotPool) -> str:
    """Run `announce_peers` and return just the value it printed."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        host.announce_peers(pool)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("@status peers=")]
    assert len(lines) == 1, buf.getvalue()
    return lines[0][len("@status peers="):]


class AnnouncePeersTest(unittest.TestCase):
    def test_empty_pool_says_nobody(self):
        self.assertEqual(_peers_line(slots.SlotPool(4)), "")

    def test_one_occupant_carries_slot_and_label(self):
        pool = slots.SlotPool(4)
        pool.acquire("V-Pad @ 192.168.1.134")
        self.assertEqual(_peers_line(pool), "1:V-Pad @ 192.168.1.134")

    def test_slots_are_listed_in_order(self):
        pool = slots.SlotPool(4)
        pool.acquire("A @ 10.0.0.1")
        pool.acquire("B @ 10.0.0.2")
        pool.acquire("C @ 10.0.0.3")
        self.assertEqual(
            _peers_line(pool),
            "1:A @ 10.0.0.1;2:B @ 10.0.0.2;3:C @ 10.0.0.3",
        )

    def test_a_peer_leaving_leaves_the_others_listed(self):
        """The reason this line exists instead of the old single `peer=`."""
        pool = slots.SlotPool(4)
        pool.acquire("A @ 10.0.0.1")
        second = pool.acquire("B @ 10.0.0.2")
        pool.release(second)
        self.assertEqual(_peers_line(pool), "1:A @ 10.0.0.1")

    def test_separators_inside_a_phone_name_cannot_break_the_line(self):
        """A phone named by its owner is untrusted input for this format."""
        pool = slots.SlotPool(2)
        pool.acquire("we;ird:name @ 10.0.0.9")
        line = _peers_line(pool)
        # One record: exactly one ';'-free entry after the slot number.
        self.assertEqual(len(line.split(";")), 1)
        self.assertTrue(line.startswith("1:"))
        self.assertNotIn(";", line[2:])
        self.assertNotIn(":", line[2:])


if __name__ == "__main__":
    unittest.main()
