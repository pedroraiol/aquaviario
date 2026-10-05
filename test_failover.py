#!/usr/bin/env python3
"""
test_failover.py: cenários do decision_engine.Decisor com tempo SIMULADO
(sem rede, sem root). Roda com `python3 test_failover.py` ou pytest.

O tempo aqui é o do modelo de decisão: a entrada "viva" já vem pronta.
Quanto tempo o heartbeat real leva pra perceber a queda (~--hb-timeout) e
quanto a rota leva pra trocar de verdade só se mede na bancada; ver os
campos deteccao_s / troca_rota_ms do decisao.jsonl.
"""
from __future__ import annotations

import os
import random
import socket
import tempfile
import threading
import time
from types import SimpleNamespace

from decision_engine import Decisor, Vida, enviar_telemetria

BOA = {"ok": True, "rtt_p50_ms": 40, "jitter_ms": 5, "perda_total_pct": 0.3, "tput_mbps": None}
# ruim pelos limites da aplicação (RTT e perda), mas longe de "degradada"
MEDIOCRE = {"ok": True, "rtt_p50_ms": 200, "jitter_ms": 20, "perda_total_pct": 3, "tput_mbps": None}
# o `testbed.sh flap X bad`: 300ms±100ms, 60% de perda
DEGRADADA = {"ok": True, "rtt_p50_ms": 350, "jitter_ms": 90, "perda_total_pct": 60, "tput_mbps": None}


class Sim:
    """Heartbeat a cada `hb` s chama passo(); a cada `ciclo` s mede todas
    as interfaces, como a thread de sondagem (morta => sem_conexao)."""

    def __init__(self, ifaces, ciclo=15.0, hb=0.5, **kw):
        self.t, self.ciclo, self.hb = 0.0, ciclo, hb
        self.eventos, self.rotas = [], []
        self.viva = {i: True for i in ifaces}
        self.carrier = {i: True for i in ifaces}
        self.qual = {i: BOA for i in ifaces}
        self.medir = {i: True for i in ifaces}     # False = sondagem "travada"
        self.prox = ciclo
        self.d = Decisor(ifaces, self._rota, self._log, **kw)

    def _rota(self, i):
        self.rotas.append((self.t, i))
        return True

    def _log(self, ev, **c):
        self.eventos.append((self.t, ev, c))

    def rodar(self, segundos):
        fim = self.t + segundos
        while self.t < fim - 1e-9:
            self.t = round(self.t + self.hb, 3)
            if self.t >= self.prox:
                self.prox += self.ciclo
                for i, q in self.qual.items():
                    if not self.medir[i]:
                        continue
                    a = {"ok": False, "motivo": "sem_conexao"} if not self.viva[i] else (
                        q() if callable(q) else q)
                    self.d.medicao(i, a, self.t)
            self.d.passo({i: self.viva[i] and self.carrier[i] for i in self.viva}, self.t,
                         {i: {"carrier": c} for i, c in self.carrier.items()})

    def trocas(self, desde=0.0):
        return [(t, c["para"], c["motivo"]) for t, ev, c in self.eventos
                if ev == "failover" and t >= desde]


def test_queda_total_e_volta():
    s = Sim(["a", "b"])
    s.rodar(120)
    assert s.d.ativo == "a" and s.d.alternativa == "b"
    s.viva["a"] = False
    t0 = s.t
    s.rodar(1)
    # alternativa já escolhida: troca no primeiro passo após a queda
    assert s.trocas(t0) == [(t0 + 0.5, "b", "ativa_caida")], s.trocas(t0)
    s.viva["b"] = False
    t1, n_rotas = s.t, len(s.rotas)
    s.rodar(60)
    sem = [e for e in s.eventos if e[1] == "sem_interface_disponivel"]
    assert len(sem) == 1 and len(s.rotas) == n_rotas      # avisa uma vez, não mexe na rota
    assert s.d.ativo == "b" and s.d.alternativa is None
    s.viva["a"] = True
    t2 = s.t
    s.rodar(1)
    assert s.trocas(t2) == [(t2 + 0.5, "a", "recuperacao_apos_queda_total")]


def test_ativa_volta_sozinha_apos_queda_total():
    s = Sim(["a", "b"])
    s.rodar(60)
    s.viva = {"a": False, "b": False}
    s.rodar(10)
    s.viva["a"] = True
    s.rodar(1)
    assert [e[1] for e in s.eventos if e[1] == "conexao_restabelecida"] == ["conexao_restabelecida"]
    assert s.d.ativo == "a" and len(s.rotas) == 1


def test_degradacao_gradual():
    s = Sim(["a", "b"])
    s.rodar(300)
    s.qual["a"] = MEDIOCRE
    t0 = s.t
    s.rodar(600)
    tr = s.trocas(t0)
    assert len(tr) == 1 and tr[0][1:] == ("b", "melhoria_qualidade"), tr
    atraso = tr[0][0] - t0
    # não troca na primeira sondagem ruim (confirmação de 30 s), mas não demora minutos
    assert 30 <= atraso <= 180, atraso
    print(f"  degradação gradual: troca {atraso:.0f} s após a 1ª sondagem ruim")


def test_degradacao_severa_confirmada():
    s = Sim(["a", "b"])
    s.rodar(300)
    s.qual["a"] = DEGRADADA
    t0 = s.t
    s.rodar(60)
    tr = s.trocas(t0)
    assert tr and tr[0][1:] == ("b", "degradacao_confirmada"), tr
    # 2 sondagens seguidas degradadas = 2 ciclos de 15 s, sem esperar margem/confirmação
    assert tr[0][0] - t0 <= 2 * s.ciclo + s.hb, tr


def test_recuperacao_volta_so_com_confirmacao():
    s = Sim(["a", "b"])
    s.qual["b"] = MEDIOCRE
    s.rodar(300)
    assert s.d.ativo == "a"
    s.viva["a"] = False
    s.rodar(120)
    assert s.d.ativo == "b"
    s.viva["a"] = True
    t0 = s.t
    s.rodar(5)
    assert s.d.ativo == "b"      # voltou a responder, mas não volta na hora
    s.rodar(600)
    tr = s.trocas(t0)
    assert tr and tr[0][1:] == ("a", "melhoria_qualidade"), tr
    print(f"  recuperação: volta pra 'a' {tr[0][0] - t0:.0f} s depois de ela voltar")


def test_recuperacao_sem_ganho_nao_volta():
    s = Sim(["a", "b"])
    s.rodar(300)
    s.viva["a"] = False
    s.rodar(120)
    s.viva["a"] = True
    t0 = s.t
    s.rodar(1200)
    assert s.trocas(t0) == []    # as duas boas: ficar onde está é o certo


def test_oscilacao_entre_duas_parecidas():
    rnd = random.Random(42)

    def ruidosa():               # 85% das sondagens boas, ruído puro
        return BOA if rnd.random() < 0.85 else MEDIOCRE
    s = Sim(["a", "b"])
    s.qual = {"a": ruidosa, "b": ruidosa}
    s.rodar(2 * 3600)
    assert len(s.trocas()) <= 2, s.trocas()


def test_interface_piscando_nao_atrai_rota():
    s = Sim(["a", "c"])
    s.qual["a"] = MEDIOCRE       # 'a' é ruim, mas estável
    s.viva["c"] = False
    s.rodar(120)
    for _ in range(180):         # 'c' liga/desliga a cada 10 s por 1 h
        s.viva["c"] = not s.viva["c"]
        s.rodar(10)
    assert s.d.ativo == "a" and s.trocas() == [], s.trocas()


def test_alternativa_desatualizada():
    s = Sim(["a", "b"], max_idade_s=90)
    s.rodar(300)
    s.qual["a"] = MEDIOCRE
    s.medir["b"] = False         # sondagem de 'b' parou (thread travada etc.)
    t0 = s.t
    s.rodar(600)
    assert s.trocas(t0) == []    # sem medição recente não justifica troca por melhoria
    st = s.d.status(s.t)["estimativas"]["b"]
    assert st["desatualizada"] and st["viva"]
    s.viva["a"] = False
    s.rodar(1)
    tr = s.trocas(t0)
    assert tr and tr[0][1:] == ("b", "ativa_caida")      # mas é pra onde ir se a ativa cair


def test_falha_ao_trocar_rota_nao_vira_spam():
    s = Sim(["a", "b"])
    s.rodar(60)
    s.d.trocar_rota = lambda i: False
    s.viva["a"] = False
    s.rodar(20)
    falhas = [e for e in s.eventos if e[1] == "failover_falhou"]
    assert 3 <= len(falhas) <= 5, len(falhas)            # 1 tentativa a cada 5 s


def test_soluco_com_alternativa_ruim_nao_troca():
    s = Sim(["a", "b"])
    s.qual["b"] = MEDIOCRE
    s.rodar(300)
    s.viva["a"] = False          # handover: 2 s mudo (já descontado o --hb-timeout)
    s.rodar(2)
    s.viva["a"] = True
    s.rodar(60)
    assert s.trocas() == [] and s.d.ativo == "a", s.trocas()
    ev = [e[1] for e in s.eventos]
    assert "troca_adiada" in ev and "queda_curta_absorvida" in ev


def test_queda_longa_com_alternativa_ruim_troca_apos_espera():
    s = Sim(["a", "b"])
    s.qual["b"] = MEDIOCRE
    s.rodar(300)
    s.viva["a"] = False
    t0 = s.t
    s.rodar(10)
    tr = s.trocas(t0)
    # declarada morta no 1º passo (t0+0,5), troca 3 s (--bad-alt-grace) depois
    assert tr == [(t0 + 0.5 + 3.0, "b", "ativa_caida")], tr


def test_queda_de_portadora_nao_espera():
    s = Sim(["a", "b"])
    s.qual["b"] = MEDIOCRE
    s.rodar(300)
    s.carrier["a"] = False
    t0 = s.t
    s.rodar(1)
    assert s.trocas(t0) == [(t0 + 0.5, "b", "ativa_caida")], s.trocas(t0)
    assert "troca_adiada" not in [e[1] for e in s.eventos]


def test_vida_heartbeat():
    v = Vida(timeout_s=2.0, recuperar=3)
    assert not v.viva(0)
    for t in (0.0, 0.5, 1.0):
        v.resposta(t)
    assert v.viva(1.0) and v.viva(3.0) and not v.viva(3.1)
    v.resposta(10.0)             # depois de um buraco, uma resposta só não basta
    assert not v.viva(10.0)
    v.resposta(10.5)
    v.resposta(11.0)
    assert v.viva(11.0)


def test_telemetria_indisponivel_nao_bloqueia():
    """Servidor que aceita a conexão e nunca responde (pior caso: o POST
    espera o timeout inteiro). A sondagem continua enfileirando sem atraso,
    a decisão continua no ritmo, e nada se perde."""
    from telemetry_client import Fila
    buraco = socket.socket()
    buraco.bind(("127.0.0.1", 0))
    buraco.listen(64)            # nunca dá accept()
    url = f"http://127.0.0.1:{buraco.getsockname()[1]}/telemetria"
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "fila.db")
        args = SimpleNamespace(telemetry_db=db, telemetry_url=url)
        fila = Fila(db, url)     # a conexão da "thread de sondagem"
        threading.Thread(target=enviar_telemetria, args=(args,), daemon=True).start()
        time.sleep(0.2)
        s = Sim(["a", "b"])
        pior_enfileirar, pior_passo = 0.0, 0.0
        for k in range(40):
            t = time.monotonic()
            fila.enfileirar({"k": k})
            pior_enfileirar = max(pior_enfileirar, time.monotonic() - t)
            t = time.monotonic()
            s.rodar(0.5)
            pior_passo = max(pior_passo, time.monotonic() - t)
            time.sleep(0.1)
        assert pior_enfileirar < 0.5 and pior_passo < 0.05, (pior_enfileirar, pior_passo)
        assert fila.pendentes() == 40
        print(f"  telemetria fora: enfileirar pior {pior_enfileirar * 1000:.1f} ms, "
              f"passo pior {pior_passo * 1000:.2f} ms, 40/40 na fila")
    buraco.close()


if __name__ == "__main__":
    for nome, f in list(globals().items()):
        if nome.startswith("test_"):
            f()
            print(f"ok {nome}")
