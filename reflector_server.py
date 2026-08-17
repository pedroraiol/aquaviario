#!/usr/bin/env python3
"""
reflector_server.py — roda no SERVIDOR DO LABORATÓRIO.

Duas portas:
  UDP  5000  refletor: recebe o pacote de teste, carimba T2/T3, devolve com
             o pacote de teste de resposta (payload de tamanho configurável).
  TCP  5001  controle: abre/fecha sessão, entrega as métricas medidas pelo
             servidor e executa os testes de vazão TCP.

Uso:
    python3 reflector_server.py --bind 0.0.0.0 --udp-port 5000 --tcp-port 5001
"""
from __future__ import annotations

import argparse
import os
import socket
import socketserver
import threading
import time

from protocol import (T_REFLECT, T_TEST, Conn, Jitter, mono_ns, now_ns,
                      pack, padding, summarize, unpack)

SESSIONS: dict[int, "SessionState"] = {}
SESSIONS_LOCK = threading.Lock()
BULK_BLOCK = os.urandom(262144)   # dados aleatórios: evita compressão no caminho


class SessionState:
    """Métricas do enlace de SUBIDA (RPi -> servidor), medidas aqui."""

    def __init__(self, session: int, resp_size: int, iface: str = ""):
        self.session = session
        self.resp_size = resp_size
        self.iface = iface
        self.lock = threading.Lock()
        self.received = 0
        self.bytes = 0
        self.max_seq = -1
        self.reordered = 0
        self.duplicates = 0
        self.seen = set()
        self.first_arrival = None
        self.last_arrival = None
        self.jitter = Jitter()
        self.transits = []        # T2 - T1 (ns) — só é válido com relógios sincronizados
        self.proc_times = []      # T3 - T2 (ns) — custo interno do servidor

    def on_packet(self, seq: int, t1: int, t2: int, size: int) -> None:
        with self.lock:
            self.received += 1
            self.bytes += size
            if self.first_arrival is None:
                self.first_arrival = t2
            self.last_arrival = t2
            if seq in self.seen:
                self.duplicates += 1
            else:
                self.seen.add(seq)
            if seq < self.max_seq:
                self.reordered += 1
            self.max_seq = max(self.max_seq, seq)
            transit = t2 - t1
            self.transits.append(transit)
            self.jitter.update(transit)

    def snapshot(self) -> dict:
        with self.lock:
            dur = 0.0
            if self.first_arrival is not None and self.last_arrival is not None:
                dur = (self.last_arrival - self.first_arrival) / 1e9
            uplink_mbps = (self.bytes * 8 / dur / 1e6) if dur > 0 else None
            return {
                "recebidos": self.received,
                "unicos": len(self.seen),
                "maior_seq": self.max_seq,
                "fora_de_ordem": self.reordered,
                "duplicados": self.duplicates,
                "bytes": self.bytes,
                "duracao_s": round(dur, 4),
                "taxa_subida_mbps": round(uplink_mbps, 4) if uplink_mbps else None,
                "jitter_subida_ms": round(self.jitter.ms, 4),
                "owd_subida_ms": summarize(self.transits),
                "proc_servidor_us": summarize(self.proc_times, scale=1e3),
            }


def system_metrics(iface: str | None = None) -> dict:
    """'Métricas do servidor' — carga, memória e contadores da NIC."""
    out: dict = {"hostname": socket.gethostname(), "ts": now_ns()}
    try:
        out["loadavg"] = os.getloadavg()
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.split()[0])
        out["mem_kb"] = mem
    except OSError:
        pass
    if iface:
        base = f"/sys/class/net/{iface}/statistics"
        stats = {}
        for k in ("rx_bytes", "tx_bytes", "rx_dropped", "tx_dropped",
                  "rx_errors", "tx_errors"):
            try:
                with open(f"{base}/{k}") as f:
                    stats[k] = int(f.read().strip())
            except OSError:
                pass
        if stats:
            out["nic"] = {"iface": iface, **stats}
    return out


# ----------------------------------------------------------------------------
# Refletor UDP
# ----------------------------------------------------------------------------
def udp_reflector(bind: str, port: int, default_resp_size: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sock.bind((bind, port))
    print(f"[udp] refletor escutando em {bind}:{port}")

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            t2 = now_ns()                      # carimbe ANTES de qualquer parsing
        except OSError:
            continue
        try:
            p = unpack(data)
        except ValueError:
            continue
        if p["type"] != T_TEST:
            continue

        with SESSIONS_LOCK:
            st = SESSIONS.get(p["session"])
        resp_size = st.resp_size if st else len(data)
        if st:
            st.on_packet(p["seq"], p["t1"], t2, len(data))

        t3 = now_ns()                          # carimbe LOGO ANTES de enviar
        resp = pack(T_REFLECT, p["session"], p["seq"],
                    p["t1"], t2, t3, padding(resp_size))
        try:
            sock.sendto(resp, addr)
        except OSError:
            pass
        if st:
            with st.lock:
                st.proc_times.append(t3 - t2)


# ----------------------------------------------------------------------------
# Controle TCP
# ----------------------------------------------------------------------------
class ControlHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = Conn(self.request)
        session = None
        iface_hint = None
        print(f"[tcp] conexão de {self.client_address[0]}")
        try:
            while True:
                msg = conn.recv_json()
                cmd = msg.get("cmd")

                if cmd == "hello":
                    iface_hint = msg.get("server_iface")
                    conn.send_json({"ok": True,
                                    "servidor": socket.gethostname(),
                                    "udp_port": self.server.udp_port})

                elif cmd == "start_udp":
                    session = int(msg["session"])
                    st = SessionState(session, int(msg.get("resp_size", 200)),
                                      msg.get("iface", ""))
                    with SESSIONS_LOCK:
                        SESSIONS[session] = st
                    conn.send_json({"ok": True})

                elif cmd == "stop_udp":
                    with SESSIONS_LOCK:
                        st = SESSIONS.pop(int(msg["session"]), None)
                    conn.send_json({"ok": st is not None,
                                    "stats": st.snapshot() if st else {},
                                    "sistema": system_metrics(iface_hint)})

                elif cmd == "tcp_up":                # RPi envia, servidor mede
                    n = int(msg["bytes"])
                    conn.send_json({"ok": True})
                    got, secs = conn.recv_exact_timed(n)
                    conn.send_json({"ok": True, "bytes": got,
                                    "segundos": round(secs, 6),
                                    "mbps": round(got * 8 / secs / 1e6, 4) if secs > 0 else None})

                elif cmd == "tcp_down":              # servidor envia, RPi mede
                    n = int(msg["bytes"])
                    conn.send_json({"ok": True, "bytes": n})
                    secs = conn.send_bulk(n, BULK_BLOCK)
                    conn.send_json({"ok": True, "segundos_servidor": round(secs, 6)})

                elif cmd == "sysinfo":
                    conn.send_json({"ok": True, "sistema": system_metrics(iface_hint)})

                elif cmd == "bye":
                    conn.send_json({"ok": True})
                    return

                else:
                    conn.send_json({"ok": False, "erro": f"comando desconhecido: {cmd}"})

        except (ConnectionError, OSError, ValueError) as e:
            print(f"[tcp] {self.client_address[0]} encerrou: {e}")
        finally:
            if session is not None:
                with SESSIONS_LOCK:
                    SESSIONS.pop(session, None)


class ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="Refletor de medição — servidor do laboratório")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--udp-port", type=int, default=5000)
    ap.add_argument("--tcp-port", type=int, default=5001)
    ap.add_argument("--resp-size", type=int, default=200,
                    help="tamanho padrão do pacote de resposta quando não há sessão")
    args = ap.parse_args()

    t = threading.Thread(target=udp_reflector,
                         args=(args.bind, args.udp_port, args.resp_size),
                         daemon=True)
    t.start()

    srv = ControlServer((args.bind, args.tcp_port), ControlHandler)
    srv.udp_port = args.udp_port
    print(f"[tcp] controle escutando em {args.bind}:{args.tcp_port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando.")


if __name__ == "__main__":
    main()
