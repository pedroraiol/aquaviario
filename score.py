#!/usr/bin/env python3
"""
score.py — combina as métricas de um enlace num único número (0-100).

Usado pelo decision_engine.py para decidir qual interface deve ser o
caminho ativo. Não depende de rede nem dos outros módulos — só matemática
em cima do histórico de rodadas, então é testável isolado.

Três componentes, cada um 0-100 (100 = melhor):
  qualidade    — RTT, jitter, perda e vazão de SUBIDA da amostra mais recente
                 (vazão de subida, não a média com descida: o caso de uso é
                 telemetria do Pi PARA o servidor, então é a subida que
                 importa se o link está fraco num sentido só)
  estabilidade — o quanto a qualidade oscilou nas últimas N amostras
  penalidade   — falhas recentes (timeout, exceção, sem resposta); uma
                 falha AGORA pesa mais que uma falha há 5 rodadas
"""
from __future__ import annotations

import statistics as st

# thresholds "bom"/"ruim" por métrica — em `bom` a nota já é 100, em
# `ruim` a nota já é 0, linear entre os dois. Calibrados para um cenário
# de telemetria embarcada (não para um datacenter): 20ms de RTT já é ótimo.
RTT_BOM_MS, RTT_RUIM_MS = 20.0, 300.0
JITTER_BOM_MS, JITTER_RUIM_MS = 5.0, 80.0
PERDA_BOM_PCT, PERDA_RUIM_PCT = 0.0, 10.0
TPUT_BOM_MBPS, TPUT_RUIM_MBPS = 5.0, 0.1

W_RTT, W_JITTER, W_PERDA, W_TPUT = 0.30, 0.15, 0.40, 0.15   # soma 1.0
W_QUALIDADE, W_ESTABILIDADE = 0.7, 0.3

PENALTY_POR_FALHA = 40.0    # pontos tirados por uma falha "agora"
PENALTY_DECAY = 0.6         # cada rodada mais antiga pesa 60% da anterior
JANELA_PENALIDADE = 10      # não olha falha de mais de 10 rodadas atrás


def _linear(v: float | None, bom: float, ruim: float) -> float:
    """100 em `bom`, 0 em `ruim`, linear entre os dois (funciona para
    métricas onde menor é melhor OU maior é melhor, dependendo da ordem)."""
    if v is None:
        return 0.0
    if bom <= ruim:          # menor é melhor (RTT, jitter, perda)
        if v <= bom:
            return 100.0
        if v >= ruim:
            return 0.0
        return 100.0 * (ruim - v) / (ruim - bom)
    else:                    # maior é melhor (vazão)
        if v >= bom:
            return 100.0
        if v <= ruim:
            return 0.0
        return 100.0 * (v - ruim) / (bom - ruim)


def qualidade_amostra(amostra: dict) -> float:
    """amostra: {"rtt_p50_ms", "jitter_ms", "perda_total_pct", "tput_mbps"}"""
    q_rtt = _linear(amostra.get("rtt_p50_ms"), RTT_BOM_MS, RTT_RUIM_MS)
    q_jit = _linear(amostra.get("jitter_ms"), JITTER_BOM_MS, JITTER_RUIM_MS)
    q_perda = _linear(amostra.get("perda_total_pct"), PERDA_BOM_PCT, PERDA_RUIM_PCT)
    q_tput = _linear(amostra.get("tput_mbps"), TPUT_BOM_MBPS, TPUT_RUIM_MBPS)
    return W_RTT * q_rtt + W_JITTER * q_jit + W_PERDA * q_perda + W_TPUT * q_tput


def estabilidade(qualidades: list[float]) -> float:
    """Quanto menos a qualidade oscilou nas últimas rodadas, maior a nota.
    1 ponto de nota perdido por 1 ponto de desvio-padrão."""
    if len(qualidades) < 2:
        return 100.0
    return max(0.0, 100.0 - st.pstdev(qualidades))


def penalidade_falhas(janela_ok: list[bool]) -> float:
    """janela_ok: True/False por rodada, mais recente por último."""
    penal = 0.0
    peso = 1.0
    for ok in reversed(janela_ok[-JANELA_PENALIDADE:]):
        if not ok:
            penal += PENALTY_POR_FALHA * peso
        peso *= PENALTY_DECAY
    return penal


def score(historico: list[dict]) -> dict:
    """
    historico: lista de rodadas, da mais antiga pra mais recente, cada uma
      {"ok": bool, "rtt_p50_ms":..., "jitter_ms":..., "perda_total_pct":..., "tput_mbps":...}
    (quando "ok" é False as outras chaves podem faltar — foi timeout/erro.)
    """
    if not historico:
        return {"score": 0.0, "qualidade": 0.0, "estabilidade": 100.0, "penalidade": 0.0}

    oks = [h["ok"] for h in historico]
    qualidades = [qualidade_amostra(h) for h in historico if h["ok"]]
    qualidade_atual = qualidades[-1] if qualidades else 0.0
    estab = estabilidade(qualidades[-10:]) if qualidades else 0.0
    penal = penalidade_falhas(oks)

    final = W_QUALIDADE * qualidade_atual + W_ESTABILIDADE * estab - penal
    return {
        "score": round(max(0.0, min(100.0, final)), 2),
        "qualidade": round(qualidade_atual, 2),
        "estabilidade": round(estab, 2),
        "penalidade": round(penal, 2),
    }
