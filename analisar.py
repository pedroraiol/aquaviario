#!/usr/bin/env python3
"""
analisar.py: consolida o resultados.jsonl e compara as interfaces.

    python3 analisar.py resultados.jsonl
    python3 analisar.py resultados.jsonl --csv resumo.csv

Só usa biblioteca padrão (roda no próprio Pi).
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics as st
from collections import defaultdict


def p(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    k = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[k]


def med(vals):
    return round(st.median(vals), 3) if vals else None


def assimetria(owd_ida_p95, owd_volta_p95):
    """>1 = descida (servidor->agente) pior que a subida na mesma rodada;
    é o padrão de contenção de airtime observado no teste longo (outro
    dispositivo puxando tráfego no mesmo canal/AP afeta mais o downlink)."""
    if not owd_ida_p95 or owd_volta_p95 is None:
        return None
    return round(owd_volta_p95 / owd_ida_p95, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arquivo")
    ap.add_argument("--csv", help="grava o resumo por interface em CSV")
    ap.add_argument("--por-teste-csv", help="grava uma linha por teste em CSV")
    args = ap.parse_args()

    linhas = []
    with open(args.arquivo) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "erro" in r:
                linhas.append({"iface": r["iface"], "erro": 1})
                continue
            u = r.get("udp", {})
            rtt = u.get("rtt_ms", {})
            owd_ida = u.get("owd_ida_ms", {})
            owd_volta = u.get("owd_volta_ms", {})
            linhas.append({
                "iface": r["iface"],
                "rodada": r.get("rodada"),
                "erro": 0,
                "rtt_p50": rtt.get("p50"),
                "rtt_p95": rtt.get("p95"),
                "rtt_max": rtt.get("max"),
                "jitter_rtt": u.get("jitter_rtt_ms"),
                "jitter_descida": u.get("jitter_descida_ms"),
                "perda_ida": u.get("perda_ida_pct"),
                "perda_volta": u.get("perda_volta_pct"),
                "perda_total": u.get("perda_total_pct"),
                "owd_ida_p95": owd_ida.get("p95"),
                "owd_volta_p95": owd_volta.get("p95"),
                "assimetria_owd": assimetria(owd_ida.get("p95"), owd_volta.get("p95")),
                "tcp_up": (r.get("tcp_subida") or {}).get("mbps_servidor"),
                "tcp_down": (r.get("tcp_descida") or {}).get("mbps_agente"),
            })

    grupos = defaultdict(list)
    for l in linhas:
        grupos[l["iface"]].append(l)

    resumo = []
    for iface, ls in grupos.items():
        ok = [l for l in ls if not l["erro"]]
        col = lambda k: [l[k] for l in ok if l.get(k) is not None]
        resumo.append({
            "iface": iface,
            "testes": len(ls),
            "falhas": sum(l["erro"] for l in ls),
            "rtt_p50_mediano_ms": med(col("rtt_p50")),
            "rtt_p95_mediano_ms": med(col("rtt_p95")),
            "rtt_pior_ms": max(col("rtt_max")) if col("rtt_max") else None,
            "rtt_p95_entre_rodadas_ms": p(col("rtt_max"), .95)
            if len(col("rtt_max")) > 3 else None,
            "jitter_rtt_mediano_ms": med(col("jitter_rtt")),
            "jitter_descida_mediano_ms": med(col("jitter_descida")),
            "perda_ida_media_pct": round(st.mean(col("perda_ida")), 3) if col("perda_ida") else None,
            "perda_volta_media_pct": round(st.mean(col("perda_volta")), 3) if col("perda_volta") else None,
            "assimetria_owd_mediana": med(col("assimetria_owd")),
            "tcp_up_mediano_mbps": med(col("tcp_up")),
            "tcp_down_mediano_mbps": med(col("tcp_down")),
            "estab_rtt_iqr_ms": round((p(col("rtt_p50"), .75) or 0) - (p(col("rtt_p50"), .25) or 0), 3)
            if len(col("rtt_p50")) > 3 else None,
        })

    # sem o `or 0.0`: quando 2+ interfaces não têm RTT válido, a chave vira
    # (True, None) nas duas e o sort compara None < None -> TypeError.
    resumo.sort(key=lambda r: (r["rtt_p50_mediano_ms"] is None,
                               r["rtt_p50_mediano_ms"] or 0.0))

    cols = list(resumo[0].keys()) if resumo else []
    w = {c: max(len(c), *(len(str(r[c])) for r in resumo)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    print("  ".join("-" * w[c] for c in cols))
    for r in resumo:
        print("  ".join(str(r[c]).ljust(w[c]) for c in cols))

    print("\nLeitura: RTT e jitter menores são melhores; vazão maior é melhor.")
    print("estab_rtt_iqr_ms = dispersão do RTT entre rodadas, quanto menor mais previsível o enlace.")
    print("rtt_p95_entre_rodadas_ms = p95 do rtt_max entre as rodadas; pior caso robusto a "
          "uma rodada isolada com blip (diferente de rtt_pior_ms, que é o max bruto).")
    print("assimetria_owd_mediana = mediana de owd_volta_p95/owd_ida_p95 por rodada; "
          ">1 = descida (servidor->agente) sofrendo mais que a subida, sinal de "
          "contenção de airtime/downlink e não de sinal fraco.")

    if args.csv and resumo:
        with open(args.csv, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=cols)
            wr.writeheader()
            wr.writerows(resumo)
        print(f"resumo -> {args.csv}")

    if args.por_teste_csv and linhas:
        campos = sorted({k for l in linhas for k in l})
        with open(args.por_teste_csv, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=campos)
            wr.writeheader()
            wr.writerows(linhas)
        print(f"por teste -> {args.por_teste_csv}")


if __name__ == "__main__":
    main()
