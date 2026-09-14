#!/usr/bin/env python3
"""
calibrar_pesos.py: testa os pesos de score.py contra o gabarito do
testbed.sh, em vez de confiar nos valores "calibrados a olho" que já
estavam lá.

Os perfis do testbed.sh (IFACE_CFG) são fixos: sem flap, eth0 é sempre
melhor que wlan0, que é sempre melhor que usb0. Isso dá um gabarito pra
testar peso. Deixa o decision_engine.py rodando sem flap por uns
minutos, recalcula o score com um monte de combinações de peso diferentes
em cima do decisao.jsonl gravado (ele já traz as métricas cruas de cada
rodada em "entradas") e mostra qual combinação acerta o ranking com mais
folga.

Uso:
    sudo ./testbed.sh decide &      # sem flap, uns 1-2 min bastam
    kill %1
    python3 calibrar_pesos.py /tmp/testbed_decisao.jsonl \
        --esperado eth0,wlan0,usb0

Isso é só um filtro inicial pra descartar combinação ruim de peso. A
bancada não reproduz rádio Wi-Fi, handover de 4G nem contenção de USB
(ver README). Não troca peso de verdade sem confirmar com dado de campo.
"""
from __future__ import annotations

import argparse
import itertools
import json

from score import (W_ESTABILIDADE, W_JITTER, W_PERDA, W_QUALIDADE, W_RTT,
                   W_TPUT, score as compute_score)


def carregar_entradas(caminho: str) -> list[dict]:
    """Cada item: {iface: resumo_r} de uma rodada 'status' do decision_engine."""
    rodadas = []
    with open(caminho) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("evento") == "status" and "entradas" in r:
                rodadas.append(r["entradas"])
    return rodadas


def historico_ate(rodadas: list[dict], indice: int, iface: str, janela: int) -> list[dict]:
    ini = max(0, indice - janela + 1)
    return [rodadas[k][iface] for k in range(ini, indice + 1) if iface in rodadas[k]]


def concordancia(rodadas: list[dict], esperado: list[str], janela: int,
                 pesos: dict) -> tuple[float, float]:
    """Fração de rodadas em que o ranking do score bate com `esperado`
    (melhor->pior) e a margem média entre 1º e 2º colocado (separação:
    quanto maior, mais robusto o ranking fica a ruído estatístico)."""
    acertos, margens, total = 0, [], 0
    for idx in range(len(rodadas)):
        notas = {}
        for iface in esperado:
            hist = historico_ate(rodadas, idx, iface, janela)
            if not hist:
                continue
            notas[iface] = compute_score(hist, **pesos)["score"]
        if len(notas) < len(esperado):
            continue
        total += 1
        ordem = sorted(notas, key=lambda i: -notas[i])
        if ordem == esperado:
            acertos += 1
        ordenadas = sorted(notas.values(), reverse=True)
        margens.append(ordenadas[0] - ordenadas[1])
    if total == 0:
        return 0.0, 0.0
    return acertos / total, sum(margens) / len(margens)


def grade_pesos(passo: float):
    """Todas as combinações w_rtt+w_jitter+w_perda+w_tput=1 (múltiplos de
    `passo`) x w_qualidade em [0,1] (múltiplo de `passo`, w_estabilidade
    complementar)."""
    n = int(round(1 / passo))
    niveis = [round(i * passo, 4) for i in range(n + 1)]
    for w_rtt, w_jitter, w_perda in itertools.product(niveis, repeat=3):
        w_tput = round(1.0 - w_rtt - w_jitter - w_perda, 4)
        if w_tput < -1e-9 or w_tput > 1 + 1e-9:
            continue
        w_tput = max(0.0, w_tput)
        for w_qual in niveis:
            yield {
                "w_rtt": w_rtt, "w_jitter": w_jitter, "w_perda": w_perda,
                "w_tput": w_tput, "w_qualidade": w_qual,
                "w_estabilidade": round(1.0 - w_qual, 4),
            }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="decisao.jsonl gravado SEM flap (baseline conhecido)")
    ap.add_argument("--esperado", required=True,
                    help="ordem melhor->pior conhecida, ex.: eth0,wlan0,usb0")
    ap.add_argument("--janela", type=int, default=10,
                    help="tamanho da janela de histórico usada no score "
                         "(bata com o --window do decision_engine que gerou o log)")
    ap.add_argument("--passo", type=float, default=0.1,
                    help="granularidade da busca em grade "
                         "(0.1 = 121 combinações; 0.05 = ~1.9 mil; fica mais lento)")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    esperado = [i.strip() for i in args.esperado.split(",")]
    rodadas = carregar_entradas(args.log)
    if not rodadas:
        raise SystemExit(
            f"nenhuma rodada com 'entradas' em {args.log}. Grave o log de "
            f"novo com a versão atual do decision_engine.py.")
    print(f"{len(rodadas)} rodadas carregadas de {args.log}")

    atuais = {"w_rtt": W_RTT, "w_jitter": W_JITTER, "w_perda": W_PERDA,
              "w_tput": W_TPUT, "w_qualidade": W_QUALIDADE,
              "w_estabilidade": W_ESTABILIDADE}
    ac0, mg0 = concordancia(rodadas, esperado, args.janela, atuais)
    print(f"\npesos atuais (score.py): acerto={ac0:.1%}  margem_media={mg0:.2f}")
    print(f"  {atuais}")

    resultados = [(ac, mg, pesos) for pesos in grade_pesos(args.passo)
                  for ac, mg in [concordancia(rodadas, esperado, args.janela, pesos)]]
    resultados.sort(key=lambda t: (-t[0], -t[1]))

    print(f"\ntop {args.top} combinações "
          f"(empate em acerto desempata pela maior margem média):")
    for ac, mg, pesos in resultados[:args.top]:
        print(f"  acerto={ac:.1%}  margem_media={mg:.2f}  {pesos}")

    print("\nisso só vale contra o gabarito fixo da bancada. Confirme com "
          "dado de campo do Pi antes de mudar peso de verdade.")


if __name__ == "__main__":
    main()
