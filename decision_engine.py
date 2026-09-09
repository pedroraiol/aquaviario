#!/usr/bin/env python3
"""
decision_engine.py — roda no RASPBERRY PI, ao lado do agent_rpi.py.

Sonda as interfaces continuamente (reaproveita run_test() do agent_rpi.py —
mesmo protocolo, mesmo roteamento por interface), calcula um score por
interface com score.py, e troca a rota default do sistema pra sempre
apontar pro melhor link — COM histerese, pra não ficar trocando de rota
a cada rodada por causa de ruído.

A sondagem em si SEMPRE testa todas as interfaces, porque usa bind
explícito por socket (SO_BINDTODEVICE + bind(src_ip), como no
agent_rpi.py) — trocar a rota default só afeta o tráfego "normal" da
aplicação, que não faz esse bind. É assim que dá pra continuar
monitorando os links inativos enquanto um deles carrega o tráfego real.

Uso:
    sudo python3 decision_engine.py --server 10.99.0.1 \
        --ifaces eth0,wlan0,usb0 \
        --gateways eth0=10.0.1.2,wlan0=10.0.2.2,usb0=10.0.3.2 \
        --interval 5 --window 10 --margin 8 --hysteresis-rounds 3 \
        --log decisao.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone

from agent_rpi import run_test
from score import RTT_RUIM_MS, score as compute_score

# um link cuja sondagem barata (RTT/perda) já está nesse patamar é
# considerado degradado: não vale gastar um teste de vazão nele (caro, e
# que TRAVA até o timeout num link ruim) nem reaproveitar a última vazão
# boa que ficou na cache.
DEGRAD_PERDA_PCT = 20.0


def _link_degradado(rtt_p50, perda_total) -> bool:
    return (perda_total or 0.0) > DEGRAD_PERDA_PCT or (rtt_p50 or 0.0) > RTT_RUIM_MS


class _ArgsView:
    """Espelha o namespace de argumentos que run_test() espera, trocando
    só tcp_bytes por rodada (pra não medir vazão toda hora — é caro)."""

    def __init__(self, args: argparse.Namespace, tcp_bytes: int):
        self.__dict__.update(vars(args))
        self.tcp_bytes = tcp_bytes


def parse_gateways(s: str) -> dict:
    out = {}
    for par in s.split(","):
        par = par.strip()
        if not par:
            continue
        iface, gw = par.split("=", 1)
        out[iface.strip()] = gw.strip()
    return out


def resumo(result: dict, tput_cache: dict, iface: str,
           rodada: int, tcp_every: int) -> dict:
    """Extrai do resultado do run_test() o formato que score.py espera.

    tput_cache[iface] = (rodada_da_medicao, mbps). A vazão só é medida a
    cada `tcp_every` rodadas; entre medições reaproveita a última — MAS só
    se ela for recente (<= tcp_every rodadas) E o link não estiver
    degradado agora. Assim um link que acabou de piorar não fica
    "segurado" por um número de vazão velho e bom; passado o prazo a
    vazão vira None e score.py renormaliza sem ela.
    Uma medição tentada e falha (mbps_servidor=None) descarta o valor antigo.
    """
    if "erro" in result:
        return {"ok": False}
    u = result.get("udp", {})
    rtt_p50 = u.get("rtt_ms", {}).get("p50")
    perda_total = u.get("perda_total_pct")

    subida = result.get("tcp_subida")
    tput = None
    if subida is not None:
        medido = subida.get("mbps_servidor")
        if medido is not None:
            tput = medido
            tput_cache[iface] = (rodada, medido)
        else:
            tput_cache.pop(iface, None)      # mediu e falhou: não confia no valor antigo
    if tput is None and not _link_degradado(rtt_p50, perda_total):
        cache = tput_cache.get(iface)
        if cache and rodada - cache[0] <= max(1, tcp_every):
            tput = cache[1]
    return {
        "ok": True,
        "rtt_p50_ms": rtt_p50,
        "jitter_ms": u.get("jitter_descida_ms"),
        "perda_total_pct": perda_total,
        "tput_mbps": tput,
    }


def set_default_route(iface: str, gateway: str | None) -> bool:
    cmd = ["ip", "route", "replace", "default", "dev", iface]
    if gateway:
        cmd = ["ip", "route", "replace", "default", "via", gateway, "dev", iface]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        detalhe = (getattr(e, "stderr", "") or str(e)).strip()
        print(f"  ! falha ao trocar rota default para {iface}: {detalhe}",
              file=sys.stderr)
        return False


def log_line(fh, obj: dict) -> None:
    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fh.flush()


def main():
    ap = argparse.ArgumentParser(description="Engine de decisão / failover — Raspberry Pi")
    ap.add_argument("--server", required=True)
    ap.add_argument("--ifaces", required=True)
    ap.add_argument("--gateways", default="",
                    help="ex.: eth0=10.0.1.2,wlan0=10.0.2.2,usb0=10.0.3.2 "
                         "(interface sem gateway listado usa rota on-link, sem via)")
    ap.add_argument("--udp-port", type=int, default=5000)
    ap.add_argument("--tcp-port", type=int, default=5001)
    ap.add_argument("--count", type=int, default=200, help="pacotes UDP por rodada")
    ap.add_argument("--pps", type=float, default=100.0)
    ap.add_argument("--size", type=int, default=200)
    ap.add_argument("--resp-size", type=int, default=200)
    ap.add_argument("--drain", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--connect-timeout", type=float, default=4.0,
                    help="timeout só do connect() TCP de cada sondagem — curto de "
                         "propósito: um link caído é detectado em segundos em vez "
                         "de segurar o ciclo inteiro até o --timeout")
    ap.add_argument("--server-iface", default=None)
    ap.add_argument("--tcp-bytes", type=int, default=2 * 1024 * 1024,
                    help="bytes por sentido quando mede vazão (só a cada --tcp-every "
                         "rodadas). Menor que o do agent_rpi de propósito: aqui só "
                         "precisa estimar a ordem de grandeza pro score, e um valor "
                         "grande trava o ciclo até o timeout quando o link está ruim")
    ap.add_argument("--tcp-every", type=int, default=5,
                    help="mede vazão TCP a cada N rodadas por interface (0 desativa)")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="pausa entre ciclos completos (todas as interfaces)")
    ap.add_argument("--window", type=int, default=10,
                    help="quantas rodadas recentes entram no score")
    ap.add_argument("--margin", type=float, default=8.0,
                    help="quanto o candidato precisa superar o ativo pra virar candidato a troca")
    ap.add_argument("--hysteresis-rounds", type=int, default=3,
                    help="por quantos ciclos seguidos o candidato precisa se manter à frente pra trocar")
    ap.add_argument("--fail-fast-rounds", type=int, default=2,
                    help="se a interface ATIVA falhar (timeout/sem resposta) por N "
                         "ciclos seguidos, troca na hora pro melhor link que ainda "
                         "responde, sem esperar --margin/--hysteresis-rounds "
                         "(0 desativa; a histerese normal continua valendo pro caso "
                         "'outro link parece um pouco melhor')")
    ap.add_argument("--log", default="decisao.jsonl")
    ap.add_argument("--telemetry-url", default=None,
                    help="ex.: http://10.99.0.1:8080/telemetria — se informado, cada "
                         "rodada também é enfileirada e enviada pro servidor de "
                         "telemetria (store-and-forward)")
    ap.add_argument("--telemetry-db", default="fila_telemetria_engine.db")
    args = ap.parse_args()

    ifaces = [i.strip() for i in args.ifaces.split(",") if i.strip()]
    gateways = parse_gateways(args.gateways)
    if os.geteuid() != 0:
        print("aviso: sem root o SO_BINDTODEVICE e a troca de rota falham; use sudo.",
              file=sys.stderr)

    fila = None
    if args.telemetry_url:
        from telemetry_client import Fila
        fila = Fila(args.telemetry_db, args.telemetry_url)

    history = {i: deque(maxlen=args.window) for i in ifaces}
    tput_cache: dict = {}
    streak = {i: 0 for i in ifaces}
    ativo = None
    falhas_ativo = 0            # ciclos seguidos em que a interface ativa falhou
    rodada = 0

    with open(args.log, "a", buffering=1) as fh:
        print(f"engine de decisão — interfaces: {ifaces}", file=sys.stderr)
        while True:
            rodada += 1
            scores = {}
            for iface in ifaces:
                testa_tput = args.tcp_every > 0 and rodada % args.tcp_every == 0
                # se a sondagem barata anterior já mostrou o link degradado, não
                # gasta um teste de vazão nele: a nota já vai sair baixa por
                # RTT/perda e o teste só travaria o ciclo até o timeout.
                ult = history[iface][-1] if history[iface] else None
                if (testa_tput and ult and ult.get("ok")
                        and _link_degradado(ult.get("rtt_p50_ms"),
                                            ult.get("perda_total_pct"))):
                    testa_tput = False
                call_args = _ArgsView(args, args.tcp_bytes if testa_tput else 0)
                try:
                    r = run_test(call_args, iface, rodada)
                    resumo_r = resumo(r, tput_cache, iface, rodada, args.tcp_every)
                except Exception as e:
                    print(f"  ! {iface} falhou: {type(e).__name__}: {e}", file=sys.stderr)
                    r = {"ts_utc": datetime.now(timezone.utc).isoformat(),
                         "rodada": rodada, "iface": iface,
                         "erro": f"{type(e).__name__}: {e}"}
                    resumo_r = {"ok": False}
                if fila:
                    fila.enfileirar(r)
                    fila.esvaziar()
                history[iface].append(resumo_r)
                scores[iface] = compute_score(list(history[iface]))

            melhor = max(scores, key=lambda i: scores[i]["score"])
            respondeu = {i: bool(history[i]) and history[i][-1].get("ok") for i in ifaces}

            # a interface ativa falhou nesta rodada? (timeout / sem resposta)
            if ativo is not None and not respondeu[ativo]:
                falhas_ativo += 1
            else:
                falhas_ativo = 0

            if ativo is None:
                if set_default_route(melhor, gateways.get(melhor)):
                    ativo = melhor
                    log_line(fh, {"ts_utc": datetime.now(timezone.utc).isoformat(),
                                  "rodada": rodada, "evento": "ativacao_inicial",
                                  "iface": ativo, "scores": scores})
                else:
                    log_line(fh, {"ts_utc": datetime.now(timezone.utc).isoformat(),
                                  "rodada": rodada, "evento": "ativacao_inicial_falhou",
                                  "iface": melhor, "scores": scores})
            elif (args.fail_fast_rounds > 0
                  and falhas_ativo >= args.fail_fast_rounds
                  and melhor != ativo and respondeu[melhor]):
                # link ativo caiu de vez: troca já pro melhor link que ainda
                # responde, sem esperar a histerese (que é pra ruído, não pra queda).
                if set_default_route(melhor, gateways.get(melhor)):
                    anterior, ativo = ativo, melhor
                    streak = {i: 0 for i in ifaces}
                    falhas_ativo = 0
                    log_line(fh, {"ts_utc": datetime.now(timezone.utc).isoformat(),
                                  "rodada": rodada, "evento": "failover_rapido",
                                  "de": anterior, "para": ativo,
                                  "motivo": f"ativo falhou {args.fail_fast_rounds}x seguidas",
                                  "scores": scores})
                else:
                    log_line(fh, {"ts_utc": datetime.now(timezone.utc).isoformat(),
                                  "rodada": rodada, "evento": "failover_falhou",
                                  "para": melhor, "scores": scores})
            elif (melhor != ativo
                  and scores[melhor]["score"] - scores[ativo]["score"] >= args.margin):
                streak[melhor] += 1
                for i in ifaces:            # só um desafiante acumula streak por vez
                    if i != melhor:
                        streak[i] = 0
                if streak[melhor] >= args.hysteresis_rounds:
                    if set_default_route(melhor, gateways.get(melhor)):
                        anterior, ativo = ativo, melhor
                        streak = {i: 0 for i in ifaces}
                        log_line(fh, {"ts_utc": datetime.now(timezone.utc).isoformat(),
                                      "rodada": rodada, "evento": "failover",
                                      "de": anterior, "para": ativo, "scores": scores})
                    else:
                        log_line(fh, {"ts_utc": datetime.now(timezone.utc).isoformat(),
                                      "rodada": rodada, "evento": "failover_falhou",
                                      "para": melhor, "scores": scores})
            else:
                streak = {i: 0 for i in ifaces}

            print(f"[{rodada}] ativo={ativo}  " +
                  "  ".join(f"{i}={scores[i]['score']}" for i in ifaces))
            log_line(fh, {"ts_utc": datetime.now(timezone.utc).isoformat(),
                          "rodada": rodada, "evento": "status", "ativo": ativo, "scores": scores})

            time.sleep(args.interval)


if __name__ == "__main__":
    main()
