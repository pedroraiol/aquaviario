#!/usr/bin/env python3
"""
estimador.py: probabilidade de cada interface oferecer uma conexão "boa",
a partir do histórico de sondagens completas. Puro, sem rede, como o score.py.

1) O que é "boa". Uma sondagem completa é BOA se cumpre TODOS os limites
   abaixo; senão é RUIM (e diz quais violou). É um critério absoluto: se
   todas as interfaces estiverem ruins, nenhuma é "boa", mesmo a melhor.
   Defaults pensados pra telemetria/store-and-forward do Gateway (HTTP/MQTT
   saindo do barco), todos ajustáveis por CLI no decision_engine.py:
     RTT p50 <= 150 ms   acima disso cada POST/handshake (vários RTTs) fica
                         lento e normalmente é fila cheia (bufferbloat) no
                         modem/operadora, não distância; 4G/5G saudável dá 20-80 ms.
     perda   <= 2 %      a vazão do TCP cai com ~1/sqrt(perda) (Mathis et al.);
                         acima de uns 2% as retransmissões dominam.
     jitter  <= 30 ms    variação de RTT (RFC 3550); acima disso retransmissão
                         espúria do TCP e qualquer tráfego interativo sofrem.
     vazão de subida >= 1 Mbps, SÓ quando houve medição (ela é feita a cada
                         N ciclos); sem medição recente o critério é ignorado,
                         não reprovado. 1 Mbps esvazia a fila acumulada de
                         telemetria depois de uma queda em tempo razoável.
   Sondagem que falhou (timeout, sem resposta, interface sem conexão) é RUIM.

2) O modelo. Cada sondagem é um lançamento de Bernoulli: boa com
   probabilidade p desconhecida. A crença sobre p é uma Beta(a, b)
   (a = boas + 1, b = ruins + 1; o +1 é a priori uniforme, "não sei nada").
   A estimativa é a média a/(a+b): no fundo, a fração de sondagens boas,
   puxada para 0,5 quando há poucos dados.

   Recência: antes de cada uso, a e b decaem para a priori com meia-vida
   MEIA_VIDA_S (no tempo, não em número de sondagens). Uma observação de
   2 minutos atrás vale metade de uma de agora. Isso resolve duas coisas
   de uma vez:
     - o barco anda e a cobertura muda: o histórico antigo não segura a nota;
     - medição velha perde peso SOZINHA: sem sondagem nova, a estimativa
       volta para 0,5 e a incerteza cresce. Ninguém é "excelente" por dados
       de 10 minutos atrás.
   n_eff = a + b - 2 é o número efetivo de sondagens que ainda pesam.

   Pouca evidência: o ranking usa p_cons = média - K_DESVIOS * desvio-padrão
   da Beta (aprox. o percentil 16 com K=1). Com 3 sondagens boas p=0,80 mas
   p_cons=0,64; em regime (n_eff~12, todas boas) p=0,93 e p_cons=0,86.
   Interface com pouco histórico ou com histórico velho não passa na frente
   de uma comprovada.

   Por que não score/100: o score mistura magnitudes (RTT 25 vs 40 ms muda a
   nota) e não tem relação com frequência de acerto. Aqui p tem leitura
   direta: "fração recente (ponderada) de sondagens que cumpriram os limites
   da aplicação", e o desvio diz quanto confiar nela.

Custo: duas multiplicações e uma exponencial por atualização, nada de numpy.
"""
from __future__ import annotations

import math

LIMITES_BOA = {"rtt_ms": 150.0, "perda_pct": 2.0, "jitter_ms": 30.0, "tput_mbps": 1.0}

# meia-vida da evidência. Com uma sondagem a cada ~15 s por interface, isso
# dá n_eff de regime ~12: o bastante pra p_cons passar de 0,8, pouco o
# bastante pra uma piora consistente derrubar p abaixo de 0,5 em ~2 min.
MEIA_VIDA_S = 120.0
K_DESVIOS = 1.0
A0 = B0 = 1.0        # priori Beta(1,1): uniforme


def violacoes(amostra: dict, limites: dict = LIMITES_BOA) -> list[str]:
    """Lista dos critérios de "boa" que a amostra viola; vazia = boa.
    amostra: o resumo do decision_engine ({"ok", "rtt_p50_ms", "jitter_ms",
    "perda_total_pct", "tput_mbps"})."""
    if not amostra.get("ok"):
        return [amostra.get("motivo", "sem_resposta")]
    v = []
    rtt, perda, jit = (amostra.get("rtt_p50_ms"), amostra.get("perda_total_pct"),
                       amostra.get("jitter_ms"))
    # métrica ausente numa sondagem ok = sem resposta UDP nenhuma
    if rtt is None or rtt > limites["rtt_ms"]:
        v.append("rtt")
    if perda is None or perda > limites["perda_pct"]:
        v.append("perda")
    if jit is None or jit > limites["jitter_ms"]:
        v.append("jitter")
    tput = amostra.get("tput_mbps")
    if tput is not None and tput < limites["tput_mbps"]:
        v.append("vazao")
    return v


class Estimativa:
    """Beta(a, b) com esquecimento exponencial no tempo (ver docstring do módulo)."""

    def __init__(self, meia_vida_s: float = MEIA_VIDA_S):
        self.meia_vida_s = meia_vida_s
        self.a, self.b = A0, B0
        self.t = None            # instante de referência de a, b
        self.t_obs = None        # instante da última observação

    def _em(self, agora: float) -> tuple[float, float]:
        if self.t is None:
            return self.a, self.b
        f = 0.5 ** (max(0.0, agora - self.t) / self.meia_vida_s)
        return A0 + (self.a - A0) * f, B0 + (self.b - B0) * f

    def observar(self, boa: bool, agora: float) -> None:
        self.a, self.b = self._em(agora)
        if boa:
            self.a += 1.0
        else:
            self.b += 1.0
        self.t = self.t_obs = agora

    def estado(self, agora: float) -> dict:
        a, b = self._em(agora)
        n = a + b
        p = a / n
        dp = math.sqrt(a * b / (n * n * (n + 1.0)))
        return {"p": round(p, 3),
                "p_cons": round(max(0.0, p - K_DESVIOS * dp), 3),
                "n_eff": round(n - A0 - B0, 1),
                "idade_s": None if self.t_obs is None else round(agora - self.t_obs, 1)}


if __name__ == "__main__":
    e = Estimativa()
    assert e.estado(0)["p"] == 0.5
    for k in range(12):
        e.observar(True, k * 15.0)
    s = e.estado(165.0)
    assert s["p"] > 0.85 and s["p_cons"] > 0.75, s
    # 20 min sem medir: volta perto da priori, sem confiança
    velho = e.estado(165.0 + 1200)
    assert abs(velho["p"] - 0.5) < 0.05 and velho["p_cons"] < 0.3, velho
    assert violacoes({"ok": True, "rtt_p50_ms": 40, "perda_total_pct": 0.5,
                      "jitter_ms": 4, "tput_mbps": None}) == []
    assert violacoes({"ok": True, "rtt_p50_ms": 40, "perda_total_pct": 5,
                      "jitter_ms": 4, "tput_mbps": 0.3}) == ["perda", "vazao"]
    assert violacoes({"ok": False}) == ["sem_resposta"]
    print("ok", s, velho)
