#!/usr/bin/env python3
"""
protocol.py — formato de pacote e utilitários comuns ao agente (Raspberry Pi)
e ao refletor (servidor do laboratório).

colocar esse codigo nas duas maquinas
"""
from __future__ import annotations

import json
import socket
import struct
import time

MAGIC = b"NPRB"
VERSION = 1

T_TEST = 1      # agente  -> servidor
T_REFLECT = 2   # servidor -> agente

# magic | ver | tipo | sessao | seq | t1 | t2 | t3 | tam_payload
HDR_FMT = "!4sBBIIQQQH"
HDR_SIZE = struct.calcsize(HDR_FMT)   # 40 bytes


def now_ns() -> int:
    """CLOCK_REALTIME. Comparável entre máquinas SE houver NTP/PTP."""
    return time.time_ns()


def mono_ns() -> int:
    """Relógio monotônico local. Use para RTT (imune a ajustes de relógio)."""
    return time.perf_counter_ns()


def pack(msg_type: int, session: int, seq: int,
         t1: int, t2: int, t3: int, payload: bytes = b"") -> bytes:
    return struct.pack(HDR_FMT, MAGIC, VERSION, msg_type, session, seq,
                       t1, t2, t3, len(payload)) + payload


def unpack(data: bytes) -> dict:
    if len(data) < HDR_SIZE:
        raise ValueError("pacote menor que o cabeçalho")
    magic, ver, mtype, session, seq, t1, t2, t3, plen = struct.unpack(
        HDR_FMT, data[:HDR_SIZE])
    if magic != MAGIC:
        raise ValueError("magic inválido")
    if ver != VERSION:
        raise ValueError(f"versão {ver} não suportada")
    return {"type": mtype, "session": session, "seq": seq,
            "t1": t1, "t2": t2, "t3": t3,
            "payload_len": plen, "wire_size": len(data)}


def padding(total_size: int) -> bytes:
    """Enchimento para que o datagrama tenha `total_size` bytes no total."""
    return b"\x00" * max(0, total_size - HDR_SIZE)


# ----------------------------------------------------------------------------
# Canal de controle TCP: linhas JSON + blocos binários no mesmo socket
# ----------------------------------------------------------------------------
class Conn:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def send_json(self, obj: dict) -> None:
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def recv_json(self) -> dict:
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return json.loads(line.decode())
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("conexão de controle fechada")
            self.buf += chunk

    def recv_exact_timed(self, n: int) -> tuple[int, float]:
        """Consome exatamente n bytes e devolve (bytes, segundos desde o 1º byte)."""
        got = 0
        t_start = None
        if self.buf:
            take = min(n, len(self.buf))
            del self.buf[:take]
            got += take
            t_start = time.perf_counter()
        while got < n:
            chunk = self.sock.recv(min(262144, n - got))
            if not chunk:
                raise ConnectionError("conexão fechada durante transferência")
            if t_start is None:
                t_start = time.perf_counter()
            got += len(chunk)
        return got, time.perf_counter() - (t_start or time.perf_counter())

    def send_bulk(self, n: int, block: bytes) -> float:
        """Envia n bytes e devolve o tempo decorrido em segundos."""
        t0 = time.perf_counter()
        sent = 0
        while sent < n:
            m = min(len(block), n - sent)
            self.sock.sendall(block[:m])
            sent += m
        return time.perf_counter() - t0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------------
# Estatística
# ----------------------------------------------------------------------------
class Jitter:
    """Jitter interarrival da RFC 3550: J += (|D(i-1,i)| - J) / 16."""

    def __init__(self):
        self.j = 0.0
        self.prev_transit = None

    def update(self, transit_ns: float) -> None:
        if self.prev_transit is not None:
            d = abs(transit_ns - self.prev_transit)
            self.j += (d - self.j) / 16.0
        self.prev_transit = transit_ns

    @property
    def ms(self) -> float:
        return self.j / 1e6


def summarize(values, scale=1e6, nd=3) -> dict:
    """min/média/mediana/p95/p99/máx. `scale`=1e6 converte ns -> ms."""
    if not values:
        return {"n": 0}
    v = sorted(values)
    n = len(v)

    def pct(p):
        k = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
        return v[k]

    return {
        "n": n,
        "min": round(v[0] / scale, nd),
        "avg": round(sum(v) / n / scale, nd),
        "p50": round(pct(50) / scale, nd),
        "p95": round(pct(95) / scale, nd),
        "p99": round(pct(99) / scale, nd),
        "max": round(v[-1] / scale, nd),
    }
