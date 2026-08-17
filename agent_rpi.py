#!/usr/bin/env python3
"""
agent_rpi.py — roda no RASPBERRY PI.

Para cada interface, em rodízio entre as rodadas:
  1. abre o canal de controle TCP amarrado àquela interface;
  2. dispara N pacotes de teste UDP a uma taxa fixa e recebe as reflexões;
  3. pede ao servidor as métricas do lado dele;
  4. mede vazão TCP de subida e descida;
  5. grava uma linha JSON por teste.

Precisa de root (SO_BINDTODEVICE exige CAP_NET_RAW):
    sudo python3 agent_rpi.py --server 192.168.0.10 --ifaces eth0,wlan0,usb0
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

from protocol import (T_REFLECT, T_TEST, Conn, Jitter, mono_ns, now_ns,
                      pack, padding, summarize, unpack)

SIOCGIFADDR = 0x8915
BULK_BLOCK = os.urandom(262144)


# ----------------------------------------------------------------------------
# Amarração à interface
# ----------------------------------------------------------------------------
def iface_ipv4(iface: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR,
                             struct.pack("256s", iface.encode()[:15]))
        return socket.inet_ntoa(packed[20:24])
    finally:
        s.close()


def bind_iface(sock: socket.socket, iface: str, src_ip: str | None = None) -> None:
    """
    Duas travas, porque uma só não basta em máquina multi-homed:
      SO_BINDTODEVICE força a SAÍDA pelo dispositivo (ignora a rota default);
      bind(src_ip) fixa o IP de origem (necessário para o retorno voltar certo).
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        iface.encode() + b"\x00")
    except PermissionError:
        print(f"  ! sem permissão para SO_BINDTODEVICE em {iface} "
              f"(rode com sudo) — caindo para bind() por IP apenas",
              file=sys.stderr)
    if src_ip:
        sock.bind((src_ip, 0))


def link_info(iface: str) -> dict:
    """Contadores e, se for Wi-Fi, qualidade do sinal."""
    info: dict = {"iface": iface}
    base = f"/sys/class/net/{iface}"
    for k in ("rx_bytes", "tx_bytes", "rx_dropped", "tx_dropped",
              "rx_errors", "tx_errors"):
        try:
            with open(f"{base}/statistics/{k}") as f:
                info[k] = int(f.read().strip())
        except OSError:
            pass
    for k, path in (("mtu", f"{base}/mtu"), ("speed_mbps", f"{base}/speed"),
                    ("operstate", f"{base}/operstate")):
        try:
            with open(path) as f:
                v = f.read().strip()
                info[k] = int(v) if v.lstrip("-").isdigit() else v
        except OSError:
            pass
    try:  # /proc/net/wireless: link, level (dBm), noise
        with open("/proc/net/wireless") as f:
            for line in f:
                if line.strip().startswith(iface + ":"):
                    parts = line.split()
                    info["wifi_link"] = parts[2].rstrip(".")
                    info["wifi_level_dbm"] = parts[3].rstrip(".")
                    break
    except OSError:
        pass
    return info


# ----------------------------------------------------------------------------
# Teste UDP: pacote de teste -> reflexão com métricas
# ----------------------------------------------------------------------------
def udp_test(sock, session, count, pps, size, resp_size, drain_s):
    sent_at: dict[int, tuple[int, int]] = {}   # seq -> (t1_mono, t1_real)
    replies: list[dict] = []
    stop = threading.Event()
    pad = padding(size)

    def receiver():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                data = sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            t4_mono, t4_real = mono_ns(), now_ns()
            try:
                p = unpack(data)
            except ValueError:
                continue
            if p["type"] != T_REFLECT or p["session"] != session:
                continue
            origin = sent_at.get(p["seq"])
            if origin is None:
                continue
            t1_mono, t1_real = origin
            p.update(t1_mono=t1_mono, t1_real=t1_real,
                     t4_mono=t4_mono, t4_real=t4_real,
                     resp_size=len(data))
            replies.append(p)

    rx = threading.Thread(target=receiver, daemon=True)
    rx.start()

    interval = 1.0 / pps
    t0 = time.perf_counter()
    for seq in range(count):
        target = t0 + seq * interval
        delta = target - time.perf_counter()
        if delta > 0.0015:
            time.sleep(delta - 0.001)
        while time.perf_counter() < target:
            pass
        t1_mono, t1_real = mono_ns(), now_ns()
        sent_at[seq] = (t1_mono, t1_real)       # grave ANTES de enviar (corrida)
        try:
            sock.send(pack(T_TEST, session, seq, t1_real, 0, 0, pad))
        except OSError as e:
            print(f"  ! falha ao enviar seq={seq}: {e}", file=sys.stderr)
            del sent_at[seq]

    time.sleep(drain_s)                          # espera retardatários
    stop.set()
    rx.join(timeout=2)
    return sent_at, replies


def analyze_replies(replies, sent_count):
    rtts, owd_fwd, owd_rev, offsets = [], [], [], []
    jit = Jitter()
    seen, reordered, max_seq = set(), 0, -1

    for p in sorted(replies, key=lambda r: r["t4_mono"]):
        # RTT com relógio monotônico, descontando o tempo dentro do servidor
        rtt = (p["t4_mono"] - p["t1_mono"]) - (p["t3"] - p["t2"])
        rtts.append(rtt)
        fwd = p["t2"] - p["t1_real"]             # só vale com NTP/PTP
        rev = p["t4_real"] - p["t3"]
        owd_fwd.append(fwd)
        owd_rev.append(rev)
        offsets.append((fwd - rev) / 2.0)        # offset estimado entre relógios
        jit.update(p["t4_mono"] - p["t1_mono"])
        if p["seq"] < max_seq:
            reordered += 1
        max_seq = max(max_seq, p["seq"])
        seen.add(p["seq"])

    # offset mínimo-filtrado: a amostra de menor RTT é a menos contaminada
    offset_ns = 0.0
    if rtts:
        i = min(range(len(rtts)), key=lambda k: rtts[k])
        offset_ns = offsets[i]

    return {
        "respostas": len(replies),
        "unicas": len(seen),
        "fora_de_ordem": reordered,
        "rtt_ms": summarize(rtts),
        "jitter_descida_ms": round(jit.ms, 4),
        "offset_relogios_ms": round(offset_ns / 1e6, 3),
        "owd_ida_ms": summarize([v - offset_ns for v in owd_fwd]),
        "owd_volta_ms": summarize([v + offset_ns for v in owd_rev]),
    }


# ----------------------------------------------------------------------------
# Um teste completo em uma interface
# ----------------------------------------------------------------------------
def run_test(args, iface: str, rnd: int) -> dict:
    session = random.getrandbits(31)
    src_ip = iface_ipv4(iface)
    print(f"  [{iface}] ip={src_ip} sessão={session}")

    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.settimeout(args.timeout)
    bind_iface(tcp, iface, src_ip)
    tcp.connect((args.server, args.tcp_port))
    tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    ctl = Conn(tcp)

    result: dict = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "rodada": rnd, "iface": iface, "src_ip": src_ip, "session": session,
        "host": socket.gethostname(),
        "config": {"count": args.count, "pps": args.pps,
                   "req_size": args.size, "resp_size": args.resp_size,
                   "tcp_bytes": args.tcp_bytes},
        "link_antes": link_info(iface),
    }

    try:
        ctl.send_json({"cmd": "hello", "iface": iface,
                       "server_iface": args.server_iface})
        r = ctl.recv_json()
        result["servidor"] = r.get("servidor")
        udp_port = r.get("udp_port", args.udp_port)

        # --- fase 1: latência / jitter / perda -------------------------------
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        bind_iface(udp, iface, src_ip)
        udp.connect((args.server, udp_port))

        ctl.send_json({"cmd": "start_udp", "session": session, "iface": iface,
                       "resp_size": args.resp_size, "count": args.count})
        ctl.recv_json()

        sent_at, replies = udp_test(udp, session, args.count, args.pps,
                                    args.size, args.resp_size, args.drain)
        udp.close()

        ctl.send_json({"cmd": "stop_udp", "session": session})
        srv = ctl.recv_json()
        srv_stats = srv.get("stats", {})
        result["servidor_subida"] = srv_stats
        result["servidor_sistema"] = srv.get("sistema", {})

        enviados = len(sent_at)
        recebidos_srv = srv_stats.get("unicos", 0)
        recebidos_ag = len({p["seq"] for p in replies})
        result["udp"] = analyze_replies(replies, enviados)
        result["udp"].update({
            "enviados": enviados,
            "perda_ida_pct": round(100 * (enviados - recebidos_srv) / enviados, 3)
            if enviados else None,
            "perda_volta_pct": round(100 * (recebidos_srv - recebidos_ag) / recebidos_srv, 3)
            if recebidos_srv else None,
            "perda_total_pct": round(100 * (enviados - recebidos_ag) / enviados, 3)
            if enviados else None,
        })

        # --- fase 2: vazão TCP ----------------------------------------------
        if args.tcp_bytes > 0:
            ctl.send_json({"cmd": "tcp_up", "bytes": args.tcp_bytes})
            ctl.recv_json()
            t_up = ctl.send_bulk(args.tcp_bytes, BULK_BLOCK)
            up = ctl.recv_json()
            result["tcp_subida"] = {
                "mbps_servidor": up.get("mbps"),
                "mbps_agente": round(args.tcp_bytes * 8 / t_up / 1e6, 3) if t_up > 0 else None,
            }

            ctl.send_json({"cmd": "tcp_down", "bytes": args.tcp_bytes})
            ack = ctl.recv_json()
            n, secs = ctl.recv_exact_timed(int(ack["bytes"]))
            down = ctl.recv_json()
            result["tcp_descida"] = {
                "mbps_agente": round(n * 8 / secs / 1e6, 3) if secs > 0 else None,
                "segundos_servidor": down.get("segundos_servidor"),
            }

        ctl.send_json({"cmd": "bye"})
        ctl.recv_json()

    finally:
        ctl.close()

    result["link_depois"] = link_info(iface)
    return result


def print_line(r: dict) -> None:
    u = r.get("udp", {})
    rtt = u.get("rtt_ms", {})
    print(f"    RTT p50={rtt.get('p50')}ms p95={rtt.get('p95')}ms  "
          f"jitter={u.get('jitter_descida_ms')}ms  "
          f"perda ida/volta={u.get('perda_ida_pct')}/{u.get('perda_volta_pct')}%  "
          f"TCP ↑{(r.get('tcp_subida') or {}).get('mbps_servidor')} "
          f"↓{(r.get('tcp_descida') or {}).get('mbps_agente')} Mbps")


def main():
    ap = argparse.ArgumentParser(description="Agente de medição — Raspberry Pi")
    ap.add_argument("--server", required=True, help="IP/host do servidor do laboratório")
    ap.add_argument("--ifaces", required=True,
                    help="lista separada por vírgula, ex.: eth0,wlan0,usb0")
    ap.add_argument("--udp-port", type=int, default=5000)
    ap.add_argument("--tcp-port", type=int, default=5001)
    ap.add_argument("--rounds", type=int, default=10, help="rodadas por interface")
    ap.add_argument("--count", type=int, default=1000, help="pacotes UDP por teste")
    ap.add_argument("--pps", type=float, default=100.0, help="taxa de envio (pacotes/s)")
    ap.add_argument("--size", type=int, default=200, help="bytes do pacote de teste")
    ap.add_argument("--resp-size", type=int, default=200, help="bytes do pacote de resposta")
    ap.add_argument("--tcp-bytes", type=int, default=25 * 1024 * 1024,
                    help="bytes por sentido no teste de vazão (0 desativa)")
    ap.add_argument("--drain", type=float, default=2.0,
                    help="segundos aguardando respostas atrasadas")
    ap.add_argument("--pause", type=float, default=5.0,
                    help="pausa entre testes (deixa a rede assentar)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--server-iface", default=None,
                    help="interface do servidor, só para coletar contadores dela")
    ap.add_argument("--out", default="resultados.jsonl")
    ap.add_argument("--telemetry-url", default=None,
                    help="ex.: http://10.99.0.1:8080/telemetria — se informado, cada "
                         "resultado também é enfileirado e enviado pro servidor de "
                         "telemetria (store-and-forward, não bloqueia se ele estiver fora)")
    ap.add_argument("--telemetry-db", default="fila_telemetria.db",
                    help="arquivo SQLite da fila local de envio")
    args = ap.parse_args()

    ifaces = [i.strip() for i in args.ifaces.split(",") if i.strip()]
    if os.geteuid() != 0:
        print("aviso: sem root o SO_BINDTODEVICE falha; use sudo.", file=sys.stderr)

    fila = None
    if args.telemetry_url:
        from telemetry_client import Fila
        fila = Fila(args.telemetry_db, args.telemetry_url)

    with open(args.out, "a", buffering=1) as fh:
        for rnd in range(1, args.rounds + 1):
            # rodízio: a ordem gira a cada rodada, para nenhuma interface ficar
            # sempre no mesmo instante do ciclo
            ordem = ifaces[rnd % len(ifaces):] + ifaces[:rnd % len(ifaces)]
            print(f"\n== rodada {rnd}/{args.rounds}  ordem={ordem}")
            for iface in ordem:
                try:
                    r = run_test(args, iface, rnd)
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    print_line(r)
                except Exception as e:
                    print(f"  ! {iface} falhou: {type(e).__name__}: {e}", file=sys.stderr)
                    r = {"ts_utc": datetime.now(timezone.utc).isoformat(),
                         "rodada": rnd, "iface": iface,
                         "erro": f"{type(e).__name__}: {e}"}
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                if fila:
                    fila.enfileirar(r)
                    enviados = fila.esvaziar()
                    pendentes = fila.pendentes()
                    if pendentes:
                        print(f"    [telemetria] {enviados} enviados agora, "
                              f"{pendentes} ainda na fila", file=sys.stderr)
                time.sleep(args.pause)

    print(f"\nresultados em {args.out}")


if __name__ == "__main__":
    main()
