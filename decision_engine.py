#!/usr/bin/env python3
"""
decision_engine.py: roda no RASPBERRY PI, ao lado do agent_rpi.py.

Mantém um ranking das interfaces pela PROBABILIDADE estimada de cada uma
oferecer uma conexão boa (estimador.py), com uma alternativa já escolhida
para quando a ativa cair, e troca a rota default do sistema.

Três fluxos independentes, pra nenhum travar o outro:

  principal (decisão)  a cada --hb-interval manda um heartbeat UDP de 40 bytes
                       por interface ao refletor (o refletor já devolve
                       qualquer T_TEST, não precisa de sessão) e lê a
                       portadora em /sys/class/net. Isso responde só "a
                       interface está VIVA?" em ~--hb-timeout segundos. Em
                       seguida decide e, se precisar, troca a rota.
  sondagem             o run_test() completo de sempre (RTT, jitter, perda,
                       vazão de subida a cada --tcp-every-s), interface por
                       interface. Cada
                       resultado vira uma observação boa/ruim no estimador.
                       Pode levar dezenas de segundos por ciclo; não importa,
                       a detecção de queda não depende dele.
  telemetria           esvazia a fila store-and-forward (sondagens + eventos
                       de decisão); com o servidor fora, o POST que espera o
                       timeout trava só esta thread.

Um erro inesperado numa thread vira log `erro_thread` e a thread segue no
próximo ciclo, em vez de morrer calada e deixar as estimativas envelhecendo.

Rotas: o kernel apaga as rotas de uma interface quando ela cai e volta
(down/up, modem reconectando) e a `ip rule from <ip>` fica velha quando o IP
muda. A thread principal confere a cada --route-check-s e recoloca a tabela
por interface (--route-table-base + posição em --ifaces) e a rota default
da ativa; cada correção vira log `rotas_reaplicadas`.

Política de troca:
  - ativa MORTA (sem portadora ou sem heartbeat há --hb-timeout): troca na
    hora pra alternativa pré-selecionada (a 1ª do ranking entre as vivas).
    Exceção: se a ativa era boa, a alternativa NÃO é boa e o gatilho foi o
    heartbeat (não a portadora), espera mais --bad-alt-grace segundos. Um
    soluço de poucos segundos (handover) jogaria o tráfego num link ruim, e
    voltar custa --confirm-s; se a ativa voltar nesse meio tempo, nada troca.
  - ativa DEGRADADA (perda>20% ou RTT ruim) em --fail-fast-rounds sondagens
    completas seguidas: troca na hora pra melhor viva cuja última sondagem
    não está degradada.
  - só MELHORIA de qualidade: o p_cons (estimativa pessimista) do desafiante
    precisa superar o p (estimativa média) da ativa por --margin, sem
    interrupção, durante --confirm-s segundos, e com medição atualizada.
    Pessimista contra média, não p_cons contra p_cons: as duas estimativas
    têm ruído, e comparar as duas pelo mesmo lado fazia duas interfaces
    iguais (85% boas) trocarem 2-5x em 2 h; assim fica em 0-1.
  - nenhuma viva: mantém a rota como está (não há pra onde ir; a telemetria
    fica na fila local) e troca pra primeira que voltar.

A sondagem continua testando todas as interfaces (bind explícito por
socket); só o tráfego comum, que não faz bind, segue a rota default.

Uso:
    sudo python3 decision_engine.py --server 10.99.0.1 \
        --ifaces eth0,wlan0,usb0 \
        --gateways eth0=10.0.1.2,wlan0=10.0.2.2,usb0=10.0.3.2 \
        --log decisao.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import select
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

from agent_rpi import bind_iface, iface_ipv4, run_test, travar_sondagem
from estimador import LIMITES_BOA, MEIA_VIDA_S, Estimativa, violacoes
from protocol import T_REFLECT, T_TEST, mono_ns, pack, unpack
from score import RTT_RUIM_MS

# um link cuja sondagem barata (RTT/perda) já está nesse patamar é
# considerado degradado: não vale gastar um teste de vazão nele (caro, e
# que TRAVA até o timeout num link ruim) nem reaproveitar a última vazão
# boa que ficou na cache. Também é o gatilho da troca por degradação.
DEGRAD_PERDA_PCT = 20.0
# p_cons a partir do qual a interface conta como "provavelmente boa" no log
P_BOA = 0.5
# depois de um `ip route replace` que falhou, não tenta a mesma interface
# de novo antes disso (senão é uma linha de log a cada heartbeat)
ESPERA_APOS_FALHA_S = 5.0


def _link_degradado(rtt_p50, perda_total) -> bool:
    return (perda_total or 0.0) > DEGRAD_PERDA_PCT or (rtt_p50 or 0.0) > RTT_RUIM_MS


def checar_roteamento_politica(server: str, iface: str, gateway: str | None) -> str | None:
    """Confere antes de sondar se a interface tem o roteamento por política
    da seção 3 do README (ip rule + tabela por interface). Sem isso a
    sondagem bindada não acha rota pro servidor e vira timeout genérico,
    difícil de rastrear até a causa. None se estiver tudo certo."""
    if gateway is None:
        return (f"sem gateway em --gateways para {iface}; a rota default pra ela "
                f"vai ser on-link (dev {iface}, sem via), só funciona se o "
                f"destino estiver na mesma sub-rede. Pra um uplink de verdade "
                f"(Ethernet/Wi-Fi/4G) isso normalmente está ERRADO; confira --gateways.")
    try:
        src_ip = iface_ipv4(iface)
    except OSError as e:
        return f"não consegui ler o IP de {iface} ({e}); ela está sem endereço?"
    try:
        out = subprocess.run(
            ["ip", "route", "get", server, "from", src_ip, "oif", iface],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"não consegui checar a rota pra {server} saindo por {iface}: {e}"
    if out.returncode != 0:
        detalhe = (out.stderr or out.stdout).strip()
        return (f"'ip route get {server} from {src_ip} oif {iface}' falhou: {detalhe}. "
                f"Falta a tabela/ip rule dessa interface (seção 3 do README)?")
    if f"dev {iface}" not in out.stdout:
        return (f"a rota pro servidor saindo de {src_ip} não usa {iface}: "
                f"{out.stdout.strip()}. Confira a prioridade das `ip rule`.")
    return None


class _ArgsView:
    """Espelha o namespace de argumentos que run_test() espera, trocando
    só tcp_bytes por rodada, pra não medir vazão toda hora (é caro)."""

    def __init__(self, args: argparse.Namespace, tcp_bytes: int):
        self.__dict__.update(vars(args))
        self.tcp_bytes = tcp_bytes
        self.tcp_so_subida = True     # só a subida entra no estimador


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
           agora: float, validade_s: float) -> dict:
    """Extrai do resultado do run_test() as métricas que entram no estimador.

    tput_cache[iface] = (instante_da_medicao, mbps). A vazão só é medida de
    tempos em tempos; entre medições reaproveita a última, mas só
    se ela for recente (<= validade_s) E o link não estiver
    degradado agora. Assim um link que acabou de piorar não fica
    "segurado" por um número de vazão velho e bom; passado o prazo a
    vazão vira None e o critério de vazão deixa de ser avaliado.
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
            tput_cache[iface] = (agora, medido)
        else:
            tput_cache.pop(iface, None)      # mediu e falhou: não confia no valor antigo
    if tput is None and not _link_degradado(rtt_p50, perda_total):
        cache = tput_cache.get(iface)
        if cache and agora - cache[0] <= validade_s:
            tput = cache[1]
    return {
        "ok": True,
        "rtt_p50_ms": rtt_p50,
        "jitter_ms": u.get("jitter_rtt_ms"),
        "perda_total_pct": perda_total,
        "tput_mbps": tput,
    }


def set_default_route(iface: str, gateway: str | None) -> bool:
    cmd = ["ip", "route", "replace", "default", "dev", iface]
    if gateway:
        cmd = ["ip", "route", "replace", "default", "via", gateway, "dev", iface]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        detalhe = (getattr(e, "stderr", "") or str(e)).strip()
        print(f"  ! falha ao trocar rota default para {iface}: {detalhe}",
              file=sys.stderr)
        return False


def _ip(*args: str) -> str:
    return subprocess.run(["ip", *args], check=True, capture_output=True,
                          text=True, timeout=2).stdout


def _ip_json(*args: str) -> list:
    try:
        return json.loads(_ip("-j", *args) or "[]")
    except subprocess.CalledProcessError:
        return []          # ex.: tabela que ainda não existe


def reconciliar_rotas(ifaces, gateways, tabela_base, ativo) -> list[str]:
    """Recoloca o que sumiu: por interface com IP, a rota default na tabela
    `tabela_base + posição` e a `ip rule from <ip atual>` pra ela (apagando
    regra de IP antigo); e a rota default (métrica 0) da ativa na tabela
    principal. Sem a tabela, heartbeat/sondagem daquela interface não acham
    caminho e ela nunca mais "volta"; sem a default, a aplicação fica sem
    rota com o engine achando que está tudo certo. Idempotente; devolve a
    lista do que corrigiu (vazia = estava tudo certo).

    Interface sem portadora é pulada: o kernel recusa rota por ela ("Nexthop
    has invalid gateway"), e ela é refeita na passada em que voltar. Erro
    numa interface não impede de conferir as outras."""
    feito = []
    erros = (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError)
    try:
        regras = _ip_json("rule", "show") if tabela_base else []
    except erros as e:
        regras, tabela_base = [], 0
        feito.append(f"erro lendo ip rule: {e}")
    for k, iface in enumerate(ifaces if tabela_base else []):
        if not carrier_ok(iface):
            continue
        try:
            try:
                ip = iface_ipv4(iface)
            except OSError:
                continue                     # sem IP ainda: nada pra apontar
            tabela, gw = str(tabela_base + k), gateways.get(iface)
            if not any(r.get("dst") == "default" and r.get("dev") == iface
                       and r.get("gateway") == gw for r in _ip_json("route", "show", "table", tabela)):
                _ip("route", "replace", "default", *(["via", gw] if gw else []),
                    "dev", iface, "table", tabela)
                feito.append(f"{iface}: default na tabela {tabela}")
            minhas = [r for r in regras if str(r.get("table")) == tabela]
            for r in minhas:
                if r.get("src") != ip:       # IP mudou (DHCP/modem reconectou)
                    _ip("rule", "del", "from", r["src"], "table", tabela)
                    feito.append(f"{iface}: removida regra do IP antigo {r['src']}")
            if not any(r.get("src") == ip for r in minhas):
                _ip("rule", "add", "from", ip, "table", tabela, "priority", tabela)
                feito.append(f"{iface}: regra from {ip} -> tabela {tabela}")
        except erros as e:
            feito.append(f"{iface}: erro: {(getattr(e, 'stderr', '') or str(e)).strip()}")
    if ativo is not None and carrier_ok(ativo):
        gw = gateways.get(ativo)
        try:
            if not any(r.get("dev") == ativo and r.get("gateway") == gw and not r.get("metric")
                       for r in _ip_json("route", "show", "default")):
                if set_default_route(ativo, gw):
                    feito.append(f"{ativo}: rota default da ativa recolocada")
        except erros as e:
            feito.append(f"{ativo}: erro: {(getattr(e, 'stderr', '') or str(e)).strip()}")
    return feito


def estimar_consumo(args, n_ifaces: int) -> dict:
    """Estimativa GROSSEIRA do tráfego que a própria engine gera em CADA
    interface, em MB/dia (sem cabeçalho TCP nem telemetria, que só sai pela
    ativa). Serve pra dimensionar contra a franquia do chip."""
    ip_udp = 28
    ciclo = n_ifaces * (args.count / args.pps + args.drain + 0.3) + args.interval
    por_dia = {
        "sondagem": args.count * (args.size + args.resp_size + 2 * ip_udp) / ciclo,
        "vazao": args.tcp_bytes / args.tcp_every_s if args.tcp_every_s > 0 else 0.0,
        "heartbeat": 2 * (40 + ip_udp) / args.hb_interval,
    }
    out = {k: round(v * 86400 / 1e6) for k, v in por_dia.items()}
    out["total"] = sum(out.values())
    out["ciclo_s"] = round(ciclo, 1)
    return out


def log_line(fh, obj: dict) -> None:
    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fh.flush()


def carrier_ok(iface: str) -> bool:
    """Leitura instantânea do kernel: sem portadora está morta, nem precisa
    esperar o heartbeat. 'unknown' (comum em modem/tun) e sysfs ilegível contam
    como ok, e aí quem decide é o heartbeat (interface que sumiu, ex. modem USB
    desplugado, já falha no bind do heartbeat). Ilegível NÃO pode ser morta:
    rodando num netns sem remontar o /sys (nsenter -n) o arquivo não existe
    e toda interface ficaria inelegível pra sempre."""
    try:
        with open(f"/sys/class/net/{iface}/operstate") as f:
            return f.read().strip() not in ("down", "lowerlayerdown", "notpresent")
    except OSError:
        return True


# ----------------------------------------------------------------------------
# Detecção rápida: está viva?
# ----------------------------------------------------------------------------
class Vida:
    """Liveness de uma interface pelo heartbeat. Não diz nada de qualidade
    (isso é do estimador); só se pacotes estão indo e voltando agora.

    Morta: nenhuma resposta há mais de `timeout_s`.
    Volta a ser elegível depois de `recuperar` respostas seguidas, cada uma a
    menos de `timeout_s` da anterior; um link que pisca não volta com uma
    resposta só."""

    def __init__(self, timeout_s: float, recuperar: int):
        self.timeout_s, self.recuperar = timeout_s, recuperar
        self.ultima = None
        self.seguidas = 0
        self.rtt_ms = None

    def resposta(self, agora: float, rtt_ms: float | None = None) -> None:
        recente = self.ultima is not None and agora - self.ultima <= self.timeout_s
        self.seguidas = self.seguidas + 1 if recente else 1
        self.ultima, self.rtt_ms = agora, rtt_ms

    def viva(self, agora: float) -> bool:
        return (self.ultima is not None and agora - self.ultima <= self.timeout_s
                and self.seguidas >= self.recuperar)


class Heartbeat:
    def __init__(self, ifaces, server, port, timeout_s, recuperar):
        self.ifaces, self.server, self.port = ifaces, server, port
        self.vida = {i: Vida(timeout_s, recuperar) for i in ifaces}
        self.sock = {i: None for i in ifaces}
        self.aberto_em = {i: 0.0 for i in ifaces}
        self.sessao = {i: random.getrandbits(31) for i in ifaces}
        self.seq = 0

    def _abrir(self, iface):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # "minimize delay": fura a fila do bulk TCP no pfifo_fast local
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)
            bind_iface(s, iface, iface_ipv4(iface))
            s.connect((self.server, self.port))
            s.setblocking(False)
            return s
        except OSError:
            s.close()
            return None

    def enviar(self, agora: float) -> None:
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        for i in self.ifaces:
            v, s = self.vida[i], self.sock[i]
            # sem resposta desde que abriu/respondeu: reabre, o IP pode ter
            # mudado (DHCP, reconexão do modem) e o bind antigo não serve mais
            if s is not None and agora - max(v.ultima or 0.0, self.aberto_em[i]) > v.timeout_s:
                s.close()
                s = self.sock[i] = None
            if s is None:
                s = self.sock[i] = self._abrir(i)
                self.aberto_em[i] = agora
                if s is None:
                    continue
            try:
                s.send(pack(T_TEST, self.sessao[i], self.seq, mono_ns(), 0, 0))
            except OSError:
                s.close()
                self.sock[i] = None

    def receber(self, ate: float) -> None:
        """Lê respostas até o instante `ate` (relógio monotonic)."""
        while (resta := ate - time.monotonic()) > 0:
            por_sock = {s: i for i, s in self.sock.items() if s is not None}
            if not por_sock:
                time.sleep(resta)
                return
            prontos, _, _ = select.select(list(por_sock), [], [], resta)
            for s in prontos:
                i = por_sock[s]
                try:
                    p = unpack(s.recv(2048))
                except (OSError, ValueError):
                    continue
                if p["type"] == T_REFLECT and p["session"] == self.sessao[i]:
                    self.vida[i].resposta(time.monotonic(),
                                          round((mono_ns() - p["t1"]) / 1e6, 2))


# ----------------------------------------------------------------------------
# Decisão
# ----------------------------------------------------------------------------
class Decisor:
    """Junta as duas fontes: viva/morta (heartbeat, rápido) e probabilidade
    de estar boa (sondagens completas, lento). Sem rede e sem relógio
    próprio (`agora` vem de fora), pra dar pra testar com tempo simulado;
    `trocar_rota(iface) -> bool` e `log(evento, **campos)` são injetados."""

    def __init__(self, ifaces, trocar_rota, log, *, limites=LIMITES_BOA,
                 meia_vida_s=MEIA_VIDA_S, margem=0.15, confirmacao_s=30.0,
                 degrad_seguidas=2, max_idade_s=90.0, espera_alt_ruim_s=3.0):
        self.ifaces = list(ifaces)
        self.trocar_rota, self.log = trocar_rota, log
        self.limites, self.margem, self.confirmacao_s = limites, margem, confirmacao_s
        self.degrad_seguidas, self.max_idade_s = degrad_seguidas, max_idade_s
        self.espera_alt_ruim_s = espera_alt_ruim_s
        self.est = {i: Estimativa(meia_vida_s) for i in ifaces}
        self.degrad = {i: 0 for i in ifaces}     # sondagens completas seguidas degradadas
        self.ultima = {i: None for i in ifaces}  # último resumo + violações
        self.viva = {i: False for i in ifaces}
        self.ativo = None
        self.alternativa = None
        self.desafiante, self.desafiante_desde = None, None
        self.sem_link = False                    # ninguém vivo
        self.ativa_caiu_em = None                # quando a ativa foi declarada morta
        self.adiada = False                      # troca segurada por alternativa ruim
        self.alguma_boa = None
        self.vazao_em = None                     # teste de vazão rodando agora (só pro log)
        self.falha_troca: dict = {}
        self.lock = threading.Lock()

    def medicao(self, iface: str, amostra: dict, agora: float) -> None:
        viol = violacoes(amostra, self.limites)
        ruim = not amostra.get("ok") or _link_degradado(amostra.get("rtt_p50_ms"),
                                                        amostra.get("perda_total_pct"))
        with self.lock:
            self.est[iface].observar(not viol, agora)
            self.ultima[iface] = {**amostra, "violacoes": viol}
            self.degrad[iface] = self.degrad[iface] + 1 if ruim else 0

    def _estados(self, agora: float) -> dict:
        out = {}
        for i in self.ifaces:
            e = self.est[i].estado(agora)
            e["viva"] = self.viva[i]
            e["desatualizada"] = e["idade_s"] is None or e["idade_s"] > self.max_idade_s
            out[i] = e
        return out

    def status(self, agora: float) -> dict:
        with self.lock:
            return {"ativo": self.ativo, "alternativa": self.alternativa,
                    "estimativas": self._estados(agora),
                    "degradadas_seguidas": dict(self.degrad),
                    # mesmo formato de antes: o calibrar_pesos.py lê isto
                    "entradas": {i: u for i, u in self.ultima.items() if u}}

    def passo(self, viva: dict, agora: float, vida: dict | None = None) -> None:
        with self.lock:
            for i in self.ifaces:
                if viva[i] != self.viva[i]:
                    self.log("interface_voltou" if viva[i] else "interface_caiu",
                             iface=i, vida=(vida or {}).get(i))
            self.viva = dict(viva)
            est = self._estados(agora)
            ranking = sorted((i for i in self.ifaces if viva[i]),
                             key=lambda i: (-est[i]["p_cons"], self.ifaces.index(i)))
            alt = [i for i in ranking if i != self.ativo]
            ctx = {"estimativas": est, "vida": vida}

            if self.sem_link and self.ativo is not None and viva[self.ativo]:
                self.sem_link = False
                self.log("conexao_restabelecida", iface=self.ativo, **ctx)
            if self.ativo is not None and viva[self.ativo] and self.ativa_caiu_em is not None:
                if self.adiada:
                    # dado pra calibrar --hb-timeout/--bad-alt-grace no campo
                    self.log("queda_curta_absorvida", iface=self.ativo,
                             duracao_s=round(agora - self.ativa_caiu_em, 1), **ctx)
                self.ativa_caiu_em, self.adiada = None, False

            if self.ativo is None:
                # partida: empate (ninguém medido ainda) fica com a ordem de --ifaces
                if ranking:
                    self._trocar(ranking[0], "ativacao_inicial", agora, ctx)
            elif not viva[self.ativo]:
                if self.ativa_caiu_em is None:
                    self.ativa_caiu_em = agora
                v = (vida or {}).get(self.ativo) or {}
                portadora = v.get("carrier") is False
                # ativa boa caiu e a alternativa é ruim: segura um pouco (ver docstring).
                # Sem portadora a queda é certa, não espera.
                esperar = (alt and not portadora and not self.sem_link
                           and est[alt[0]]["p_cons"] < P_BOA <= est[self.ativo]["p_cons"]
                           and agora - self.ativa_caiu_em < self.espera_alt_ruim_s)
                if esperar:
                    if not self.adiada:
                        self.adiada = True
                        self.log("troca_adiada", de=self.ativo, para=alt[0],
                                 espera_s=self.espera_alt_ruim_s, **ctx)
                elif alt:
                    motivo = "recuperacao_apos_queda_total" if self.sem_link else "ativa_caida"
                    self._trocar(alt[0], motivo, agora, ctx,
                                 deteccao_s=v.get("sem_resposta_ha_s"),
                                 gatilho="portadora" if portadora else "heartbeat",
                                 adiada_s=round(agora - self.ativa_caiu_em, 1) if self.adiada else None)
                elif not self.sem_link:
                    self.sem_link = True
                    self.log("sem_interface_disponivel", ativo=self.ativo, **ctx)
            elif self.degrad_seguidas and self.degrad[self.ativo] >= self.degrad_seguidas:
                ok = [i for i in alt if self.ultima[i] and self.degrad[i] == 0
                      and not est[i]["desatualizada"]]
                if ok:
                    self._trocar(ok[0], "degradacao_confirmada", agora, ctx,
                                 sondagens_degradadas=self.degrad[self.ativo])
            else:
                # pessimista do desafiante contra a média da ativa (ver docstring)
                c = alt[0] if alt else None
                if (c is not None and not est[c]["desatualizada"]
                        and est[c]["p_cons"] - est[self.ativo]["p"] >= self.margem):
                    if self.desafiante != c:
                        self.desafiante, self.desafiante_desde = c, agora
                    elif agora - self.desafiante_desde >= self.confirmacao_s:
                        self._trocar(c, "melhoria_qualidade", agora, ctx,
                                     confirmado_por_s=round(agora - self.desafiante_desde, 1))
                else:
                    self.desafiante = None

            alt = [i for i in ranking if i != self.ativo]
            self.alternativa = alt[0] if alt else None

            # maior p_cons não quer dizer "boa": avisa quando ninguém é
            boa = any(est[i]["p_cons"] >= P_BOA for i in ranking)
            if boa != self.alguma_boa:
                self.alguma_boa = boa
                self.log("interface_boa_disponivel" if boa else "nenhuma_interface_boa",
                         ativo=self.ativo, **ctx)

    def _trocar(self, para, motivo, agora, ctx, **extra) -> None:
        if agora - self.falha_troca.get(para, -1e9) < ESPERA_APOS_FALHA_S:
            return
        t0 = time.monotonic()
        ok = self.trocar_rota(para)
        campos = {"de": self.ativo, "para": para, "motivo": motivo,
                  "troca_rota_ms": round((time.monotonic() - t0) * 1000, 1),
                  "teste_vazao_em": self.vazao_em, **extra, **ctx}
        if not ok:
            self.falha_troca[para] = agora
            self.log("failover_falhou", **campos)
            return
        self.ativo = para
        self.desafiante = None
        self.sem_link = False
        self.ativa_caiu_em, self.adiada = None, False
        self.log("ativacao_inicial" if motivo == "ativacao_inicial" else "failover",
                 para_boa=ctx["estimativas"][para]["p_cons"] >= P_BOA, **campos)


# ----------------------------------------------------------------------------
# Threads
# ----------------------------------------------------------------------------
def _erro_thread(log, nome: str, e: Exception) -> None:
    """Thread não pode morrer calada: o engine seguiria trocando rota com
    estimativas cada vez mais velhas (ou sem telemetria) sem ninguém saber."""
    traceback.print_exc()
    log("erro_thread", thread=nome, erro=f"{type(e).__name__}: {e}")


def sondar(args, ifaces, decisor: Decisor, log) -> None:
    fila = None
    if args.telemetry_url:
        # conexão sqlite própria desta thread; quem envia é a thread de telemetria
        from telemetry_client import Fila
        fila = Fila(args.telemetry_db, args.telemetry_url)
    host = socket.gethostname()
    tput_cache: dict = {}
    ultimo_tput: dict = {}
    rodada = 0
    time.sleep(args.hb_timeout)      # dá tempo do heartbeat dizer quem está vivo
    while True:
        rodada += 1
        try:
            for iface in ifaces:
                agora = time.monotonic()
                if not decisor.viva[iface]:
                    # sem conexão também é observação: o link NÃO estava bom agora.
                    # Não gasta o connect-timeout tentando sondar, mas registra
                    # na telemetria, senão a queda nem aparece no dashboard.
                    decisor.medicao(iface, {"ok": False, "motivo": "sem_conexao"}, agora)
                    if fila:
                        fila.enfileirar({"ts_utc": datetime.now(timezone.utc).isoformat(),
                                         "rodada": rodada, "iface": iface, "host": host,
                                         "erro": "sem_conexao",
                                         "iface_ativa": iface == decisor.ativo})
                    continue
                # vazão é o que mais gasta dado: só a cada --tcp-every-s, e nunca
                # num link que a sondagem anterior já mostrou degradado (só
                # travaria esta thread até o timeout)
                testa_tput = (args.tcp_every_s > 0 and not decisor.degrad[iface]
                              and agora - ultimo_tput.get(iface, -1e12) >= args.tcp_every_s)
                if testa_tput:
                    ultimo_tput[iface] = agora
                call_args = _ArgsView(args, args.tcp_bytes if testa_tput else 0)
                decisor.vazao_em = iface if testa_tput else None
                try:
                    r = run_test(call_args, iface, rodada)
                    resumo_r = resumo(r, tput_cache, iface, time.monotonic(),
                                      1.5 * args.tcp_every_s)
                except Exception as e:
                    print(f"  ! {iface} falhou: {type(e).__name__}: {e}", file=sys.stderr)
                    r = {"ts_utc": datetime.now(timezone.utc).isoformat(),
                         "rodada": rodada, "iface": iface, "host": host,
                         "erro": f"{type(e).__name__}: {e}"}
                    resumo_r = {"ok": False}
                finally:
                    decisor.vazao_em = None
                # qual interface carregava o tráfego enquanto essa sondagem rodou;
                # é o que o dashboard do telemetry_server.py mostra como "em uso"
                r["iface_ativa"] = iface == decisor.ativo
                if fila:
                    fila.enfileirar(r)
                decisor.medicao(iface, resumo_r, time.monotonic())

            st = decisor.status(time.monotonic())
            print(f"[{rodada}] ativo={st['ativo']} alternativa={st['alternativa']}  " +
                  "  ".join(f"{i}=p{e['p']}/{e['p_cons']} n{e['n_eff']} {e['idade_s']}s"
                            f"{'' if e['viva'] else ' MORTA'}"
                            for i, e in st["estimativas"].items()))
            log("status", rodada=rodada, **st)
        except Exception as e:
            _erro_thread(log, "sondagem", e)
        time.sleep(args.interval)


def enviar_telemetria(args, eventos: queue.Queue, log) -> None:
    """Grava os eventos de decisão na fila (conexão sqlite própria desta
    thread) e esvazia a fila; acorda assim que chega evento novo, senão a
    cada 2 s."""
    from telemetry_client import Fila
    fila = Fila(args.telemetry_db, args.telemetry_url)
    while True:
        try:
            try:
                fila.enfileirar(eventos.get(timeout=2.0))
                while not eventos.empty():
                    fila.enfileirar(eventos.get_nowait())
            except queue.Empty:
                pass
            fila.esvaziar()
        except Exception as e:
            _erro_thread(log, "telemetria", e)
            time.sleep(2.0)


def main():
    ap = argparse.ArgumentParser(description="Engine de decisão / failover, Raspberry Pi")
    ap.add_argument("--server", required=True)
    ap.add_argument("--ifaces", required=True,
                    help="em ordem de preferência: desempata quando não há medição ainda")
    ap.add_argument("--gateways", default="",
                    help="ex.: eth0=10.0.1.2,wlan0=10.0.2.2,usb0=10.0.3.2 "
                         "(interface sem gateway listado usa rota on-link, sem via)")
    ap.add_argument("--udp-port", type=int, default=5000)
    ap.add_argument("--tcp-port", type=int, default=5001)
    # padrões de sondagem pensados pro custo de dado no celular; o engine
    # imprime a estimativa de consumo ao iniciar (ver estimar_consumo)
    ap.add_argument("--count", type=int, default=100, help="pacotes UDP por sondagem")
    ap.add_argument("--pps", type=float, default=100.0)
    ap.add_argument("--size", type=int, default=200)
    ap.add_argument("--resp-size", type=int, default=200)
    ap.add_argument("--drain", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--connect-timeout", type=float, default=4.0,
                    help="timeout só do connect() TCP de cada sondagem completa")
    ap.add_argument("--server-iface", default=None)
    ap.add_argument("--tcp-bytes", type=int, default=2 * 1024 * 1024,
                    help="bytes enviados (só subida) quando mede vazão. Menor que o "
                         "do agent_rpi de propósito: aqui só precisa estimar a ordem "
                         "de grandeza, e um valor grande segura a thread de sondagem "
                         "quando o link está ruim")
    ap.add_argument("--tcp-every-s", type=float, default=1800.0,
                    help="mede vazão de subida no máximo a cada N segundos por "
                         "interface (0 desativa). É o que mais gasta dado")
    ap.add_argument("--interval", type=float, default=15.0,
                    help="pausa entre ciclos completos de sondagem (todas as interfaces)")
    # detecção rápida
    ap.add_argument("--hb-interval", type=float, default=0.5,
                    help="período do heartbeat e de cada passo de decisão")
    ap.add_argument("--hb-timeout", type=float, default=2.0,
                    help="sem resposta do heartbeat por isso => interface morta. "
                         "Calibrar no campo: handover de 4G/5G pode calar o link por "
                         "~1 s sem ele ter caído")
    ap.add_argument("--bad-alt-grace", type=float, default=3.0,
                    help="segundos a mais que a ativa (boa) pode ficar sem heartbeat "
                         "antes de trocar, quando a melhor alternativa NÃO é boa "
                         "(p_cons<0,5). Não vale pra queda de portadora. 0 desativa")
    ap.add_argument("--hb-recover", type=int, default=3,
                    help="respostas seguidas pra uma interface morta voltar a ser elegível")
    # modelo de qualidade
    ap.add_argument("--half-life", type=float, default=MEIA_VIDA_S,
                    help="meia-vida (s) do peso de cada sondagem no estimador")
    ap.add_argument("--max-age", type=float, default=90.0,
                    help="medição mais velha que isso (s) é 'desatualizada': não "
                         "serve pra justificar troca por melhoria (ainda serve de "
                         "alternativa se a ativa cair)")
    ap.add_argument("--boa-rtt-ms", type=float, default=LIMITES_BOA["rtt_ms"])
    ap.add_argument("--boa-perda-pct", type=float, default=LIMITES_BOA["perda_pct"])
    ap.add_argument("--boa-jitter-ms", type=float, default=LIMITES_BOA["jitter_ms"])
    ap.add_argument("--boa-tput-mbps", type=float, default=LIMITES_BOA["tput_mbps"])
    # política de troca
    ap.add_argument("--margin", type=float, default=0.15,
                    help="quanto o p_cons do candidato precisa superar o p da ativa "
                         "pra uma troca só por melhoria")
    ap.add_argument("--confirm-s", type=float, default=30.0,
                    help="por quanto tempo seguido a margem precisa se manter")
    ap.add_argument("--fail-fast-rounds", type=int, default=2,
                    help="ativa DEGRADADA (perda>20%% ou RTT ruim) ou com sondagem "
                         "falha em N sondagens completas seguidas => troca na hora, "
                         "sem margem/confirmação (0 desativa). Queda total é "
                         "detectada pelo heartbeat, independente disto")
    ap.add_argument("--route-table-base", type=int, default=200,
                    help="tabela de rota por política da i-ésima interface de "
                         "--ifaces = base+i; o engine mantém essas tabelas e as "
                         "`ip rule from <ip>` sozinho (0 desativa: aí é com você)")
    ap.add_argument("--route-check-s", type=float, default=5.0,
                    help="de quanto em quanto tempo confere/recoloca as rotas")
    ap.add_argument("--log", default="decisao.jsonl")
    ap.add_argument("--telemetry-url", default=None,
                    help="ex.: http://10.99.0.1:8080/telemetria; se informado, cada "
                         "sondagem também é enfileirada e enviada pro servidor de "
                         "telemetria (store-and-forward)")
    ap.add_argument("--telemetry-db", default="fila_telemetria_engine.db")
    args = ap.parse_args()

    ifaces = [i.strip() for i in args.ifaces.split(",") if i.strip()]
    gateways = parse_gateways(args.gateways)
    if os.geteuid() != 0:
        print("aviso: sem root o SO_BINDTODEVICE e a troca de rota falham; use sudo.",
              file=sys.stderr)
    travar_sondagem("decision_engine.py")

    # monta as tabelas por interface e depois confere: um erro de ip rule /
    # tabela ou gateway ausente só ia aparecer depois como timeout genérico.
    for item in reconciliar_rotas(ifaces, gateways, args.route_table_base, None):
        print(f"rotas: {item}", file=sys.stderr)
    for iface in ifaces:
        problema = checar_roteamento_politica(args.server, iface, gateways.get(iface))
        if problema:
            print(f"aviso: {iface}: {problema}", file=sys.stderr)

    consumo = estimar_consumo(args, len(ifaces))
    print(f"consumo estimado do engine POR interface: ~{consumo['total']} MB/dia "
          f"(~{consumo['total'] * 30 / 1000:.1f} GB/mês): sondagem {consumo['sondagem']}, "
          f"vazão {consumo['vazao']}, heartbeat {consumo['heartbeat']} MB/dia; "
          f"ciclo ~{consumo['ciclo_s']} s", file=sys.stderr)

    eventos = queue.Queue() if args.telemetry_url else None
    host = socket.gethostname()

    with open(args.log, "a", buffering=1) as fh:
        lock_log = threading.Lock()

        def log(evento, **campos):
            reg = {"ts_utc": datetime.now(timezone.utc).isoformat(),
                   "evento": evento, **campos}
            try:
                with lock_log:
                    log_line(fh, reg)
            except (OSError, ValueError) as e:
                # disco cheio etc.: o failover continua, só sem log local
                print(f"  ! falha gravando {args.log}: {e}", file=sys.stderr)
            if eventos is not None:
                # pro dashboard; as métricas cruas já vão como registro de sondagem
                eventos.put({**{k: v for k, v in reg.items() if k != "entradas"},
                             "host": host})
            if evento != "status":
                print(f"  * {evento} " + " ".join(
                    f"{k}={campos[k]}" for k in ("iface", "de", "para", "motivo",
                                                 "deteccao_s", "adiada_s", "troca_rota_ms",
                                                 "itens")
                    if k in campos), file=sys.stderr)

        decisor = Decisor(
            ifaces, lambda i: set_default_route(i, gateways.get(i)), log,
            limites={"rtt_ms": args.boa_rtt_ms, "perda_pct": args.boa_perda_pct,
                     "jitter_ms": args.boa_jitter_ms, "tput_mbps": args.boa_tput_mbps},
            meia_vida_s=args.half_life, margem=args.margin,
            confirmacao_s=args.confirm_s, degrad_seguidas=args.fail_fast_rounds,
            max_idade_s=args.max_age, espera_alt_ruim_s=args.bad_alt_grace)
        hb = Heartbeat(ifaces, args.server, args.udp_port, args.hb_timeout, args.hb_recover)

        log("inicio", ifaces=ifaces, consumo_estimado_mb_dia=consumo,
            config={k: v for k, v in vars(args).items() if k != "telemetry_url"})
        threading.Thread(target=sondar, args=(args, ifaces, decisor, log),
                         daemon=True, name="sondagem").start()
        if args.telemetry_url:
            threading.Thread(target=enviar_telemetria, args=(args, eventos, log),
                             daemon=True, name="telemetria").start()

        print(f"engine de decisão, interfaces: {ifaces}", file=sys.stderr)
        prox_rotas, viva_ant, erros_rotas_ant = 0.0, None, None
        while True:
            fim = time.monotonic() + args.hb_interval
            hb.enviar(time.monotonic())
            hb.receber(fim)
            agora = time.monotonic()
            vida = {}
            for i in ifaces:
                v = hb.vida[i]
                vida[i] = {"carrier": carrier_ok(i), "hb_rtt_ms": v.rtt_ms,
                           "sem_resposta_ha_s": None if v.ultima is None
                           else round(agora - v.ultima, 3)}
            viva = {i: vida[i]["carrier"] and hb.vida[i].viva(agora) for i in ifaces}
            decisor.passo(viva, agora, vida)
            # periódico, e na hora em que alguma interface cai/volta (é quando
            # o kernel costuma ter apagado rota). Roda nesta thread, a mesma que
            # troca a rota: não tem como reaplicar uma ativa velha por corrida.
            if agora >= prox_rotas or viva != viva_ant:
                prox_rotas, viva_ant = agora + args.route_check_s, viva
                corrigido = reconciliar_rotas(ifaces, gateways, args.route_table_base,
                                              decisor.ativo)
                # correção feita sai sempre (repetida = algo, tipo NetworkManager,
                # brigando pelas rotas); a MESMA lista só de erros, uma vez só
                so_erros = bool(corrigido) and all("erro" in c for c in corrigido)
                if corrigido and not (so_erros and corrigido == erros_rotas_ant):
                    log("rotas_reaplicadas", ativo=decisor.ativo, itens=corrigido)
                erros_rotas_ant = corrigido if so_erros else None


if __name__ == "__main__":
    main()
