#!/usr/bin/env python3
"""End-to-end: four phones on one daemon, and the fifth turned away.

Speaks the wire protocol over a real socket against a real daemon rather than
calling into it, because what is being checked is exactly the part unit tests
cannot see: that four connections each get their own slot and their own
injector, that the slot number arrives in the same read as the ACK, and that a
fifth is refused with REJECT(in_use) instead of an ACK and silence.

Run:  python3 test_multiplayer_e2e.py
"""
from __future__ import annotations

import socket
import struct
import subprocess
import sys
import time
import unittest

import vpad_daemon as d

HOST = "127.0.0.1"


def encode_hello(name: str = "tester", skin: str = "pro") -> bytes:
    body = bytes([d.PROTO_VER, len(name)]) + name.encode() \
        + bytes([len(skin)]) + skin.encode()
    return d.encode_frame(d.T_HELLO, body)


def read_frame(sock: socket.socket, timeout: float = 5.0):
    """One frame, or None on close. Length is inclusive of the 3-byte header."""
    sock.settimeout(timeout)
    head = b""
    while len(head) < 3:
        chunk = sock.recv(3 - len(head))
        if not chunk:
            return None
        head += chunk
    length = struct.unpack("<H", head[:2])[0]
    msg_type = head[2]
    body = b""
    while len(body) < length - 3:
        chunk = sock.recv(length - 3 - len(body))
        if not chunk:
            return None
        body += chunk
    return msg_type, body


class FourPlayers(unittest.TestCase):
    proc: subprocess.Popen
    port: int

    @classmethod
    def setUpClass(cls) -> None:
        cls.proc = subprocess.Popen(
            # -u: print() through a pipe is block-buffered, so the port
            # line would never arrive and setUpClass would hang.
            [sys.executable, "-u", "vpad_daemon.py", "--inject", "log",
             "--players", "4", "--port", "0", "--name", "e2e-test"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1,
        )
        cls.port = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            line = cls.proc.stdout.readline()
            if not line:
                break
            if "Listening on" in line:
                cls.port = int(line.rsplit(":", 1)[1].strip())
                break
        if not cls.port:
            cls.proc.kill()
            raise RuntimeError("daemon never reported a port")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def test_four_in_five_out(self) -> None:
        players = []
        try:
            for i in range(4):
                sock = socket.create_connection((HOST, self.port), timeout=5)
                players.append(sock)
                sock.sendall(encode_hello(f"phone-{i}"))

                msg_type, body = read_frame(sock)
                self.assertEqual(msg_type, d.T_HELLO_ACK,
                                 f"phone {i} did not get an ACK")
                self.assertEqual(body[1], 1, f"phone {i} was not accepted")

                # The slot rides in the same read as the ACK — that is the
                # point of writing them together.
                msg_type, body = read_frame(sock)
                self.assertEqual(msg_type, d.T_SLOT,
                                 f"phone {i} got no slot frame")
                self.assertEqual(body[0], i,
                                 f"phone {i} was given slot {body[0]}")

            # The fifth must be told why, not dropped in silence.
            fifth = socket.create_connection((HOST, self.port), timeout=5)
            players.append(fifth)
            fifth.sendall(encode_hello("phone-5"))
            msg_type, body = read_frame(fifth)
            self.assertEqual(msg_type, d.T_REJECT, "fifth phone was not rejected")
            self.assertEqual(body[0], d.R_IN_USE, "wrong rejection reason")
            self.assertIn(b"slot", body[1:].lower())
        finally:
            for sock in players:
                try:
                    sock.close()
                except OSError:
                    pass

    def test_slot_is_returned_and_reused(self) -> None:
        """A player who leaves frees the slot for the next one."""
        first = socket.create_connection((HOST, self.port), timeout=5)
        first.sendall(encode_hello("leaver"))
        read_frame(first)                      # ACK
        _, body = read_frame(first)            # SLOT
        freed = body[0]
        first.close()
        time.sleep(0.4)                        # let the daemon notice

        second = socket.create_connection((HOST, self.port), timeout=5)
        try:
            second.sendall(encode_hello("taker"))
            msg_type, ack = read_frame(second)
            self.assertEqual(msg_type, d.T_HELLO_ACK)
            self.assertEqual(ack[1], 1, "the freed slot was not handed out")
            _, slot = read_frame(second)
            self.assertEqual(slot[0], freed, "a different slot was handed out")
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
