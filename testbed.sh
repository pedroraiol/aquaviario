#!/usr/bin/env bash
#
# testbed.sh — bancada sem hardware para o netprobe.
#
# Monta dois network namespaces ("rpi" e "lab") ligados por três pares veth,
# cada um com um perfil de atraso/perda diferente via netem, e exercita a
# MESMA política de rotas (ip rule / ip route por tabela + rp_filter=2) que o
# Raspberry Pi real vai precisar em produção.
#
#   netns "rpi"                                   netns "lab"
#     eth0  10.0.1.1/24 ── veth ── to-eth0  10.0.1.2/24 ┐
#     wlan0 10.0.2.1/24 ── veth ── to-wlan0 10.0.2.2/24 ├─ lo: 10.99.0.1/32
#     usb0  10.0.3.1/24 ── veth ── to-usb0  10.0.3.2/24 ┘
#   seu Linux real (root)  10.0.9.1/24 ── veth ── to-mgmt 10.0.9.2/24  (só admin/dashboard)
#
# Uso:
#   sudo ./testbed.sh up              # monta namespaces/veths/netem + sobe refletor e
#                                      # telemetria no lab (ficam no ar até o 'down')
#   sudo ./testbed.sh status          # endereços, rotas, regras e qdiscs
#   sudo ./testbed.sh check           # ping direto por link + lookup de rota por política
#   sudo ./testbed.sh run             # roda o agente (usa o refletor/telemetria do 'up')
#   sudo ./testbed.sh decide          # roda a engine de decisão (idem)
#   sudo ./testbed.sh telemetria      # consulta o banco de telemetria (status|dashboard)
#   sudo ./testbed.sh flap IF MODO    # simula IF piorando/melhorando (down|up|bad|good),
#                                      # pra ver a engine reagir enquanto "decide" está rodando
#   sudo ./testbed.sh down            # derruba refletor/telemetria e remove tudo
#
# O dashboard (http://10.99.0.1:8080/) fica no ar do 'up' até o 'down' —
# sobrevive a quantos 'run'/'decide' você quiser rodar no meio.
#
# Requisitos: iproute2 e o módulo sch_netem (pacote linux-modules-extra em
# alguns kernels Ubuntu).

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_IP=10.99.0.1
TELEMETRY_PORT=8080
# rota default inicial no rpi (existe num Pi real antes de qualquer ip rule
# por política; sem ela, tráfego "normal" — como a telemetria — não tem
# por onde sair. A decision_engine substitui isso pelo melhor link.
DEFAULT_DEV=eth0
DEFAULT_VIA=10.0.1.2
# link só de administração, ligando o SEU Linux real (fora de qualquer
# namespace) direto ao netns "lab" — só pra abrir o dashboard num navegador
# de verdade. Não é usado pela sondagem (que continua isolada em eth0/wlan0/usb0).
MGMT_HOST_IP=10.0.9.1
MGMT_LAB_IP=10.0.9.2

# nome da interface no rpi -> "peer subnet tabela delay jitter perda_subida perda_descida"
IFACE_CFG=(
    "eth0  to-eth0  10.0.1 100 2ms  0.3ms 0   0"
    "wlan0 to-wlan0 10.0.2 101 15ms 5ms   0.5 0.5"
    "usb0  to-usb0  10.0.3 102 60ms 20ms  4   0.5"
)

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "rode como root (sudo ./testbed.sh $1)" >&2
        exit 1
    fi
}

ensure_lab_services() {
    if ! pgrep -f "python3 $DIR/reflector_server\.py" >/dev/null; then
        echo "erro: o refletor não está no ar. Rode 'sudo ./testbed.sh up' primeiro." >&2
        exit 1
    fi
    if ! pgrep -f "python3 $DIR/telemetry_server\.py" >/dev/null; then
        echo "erro: o servidor de telemetria não está no ar. Rode 'sudo ./testbed.sh up' primeiro." >&2
        exit 1
    fi
}

up() {
    need_root up
    ip netns add rpi
    ip netns add lab
    ip netns exec rpi ip link set lo up
    ip netns exec lab ip link set lo up

    while read -r IF PEER SUB TBL D J LOSS_UP LOSS_DOWN; do
        ip link add "$IF" type veth peer name "$PEER"
        ip link set "$IF" netns rpi
        ip link set "$PEER" netns lab

        ip netns exec rpi ip addr add "$SUB.1/24" dev "$IF"
        ip netns exec rpi ip link set "$IF" up
        ip netns exec lab ip addr add "$SUB.2/24" dev "$PEER"
        ip netns exec lab ip link set "$PEER" up

        # atraso/jitter/perda: cada lado molda o tráfego que ELE ENVIA (egress)
        ip netns exec rpi tc qdisc add dev "$IF" root netem delay "$D" "$J" loss "${LOSS_UP}%"
        ip netns exec lab tc qdisc add dev "$PEER" root netem delay "$D" "$J" loss "${LOSS_DOWN}%"

        # política de rotas no lado rpi: uma tabela por interface, igual ao Pi real
        ip netns exec rpi ip route add "$SUB.0/24" dev "$IF" src "$SUB.1" table "$TBL"
        ip netns exec rpi ip route add "$SERVER_IP/32" via "$SUB.2" dev "$IF" table "$TBL"
        ip netns exec rpi ip rule add from "$SUB.1" table "$TBL" priority "$TBL"

        echo "  $IF <-> $PEER   $SUB.1/24 <-> $SUB.2/24   delay=$D~$J  perda subida=${LOSS_UP}% descida=${LOSS_DOWN}%"
    done < <(printf '%s\n' "${IFACE_CFG[@]}")

    ip netns exec lab ip addr add "$SERVER_IP/32" dev lo

    # link de administração: root (seu Linux real) <-> netns "lab", só pra
    # dar pro navegador de verdade acesso ao dashboard em 10.99.0.1:8080
    ip link add mgmt0 type veth peer name to-mgmt
    ip link set to-mgmt netns lab
    ip addr add "$MGMT_HOST_IP/24" dev mgmt0
    ip link set mgmt0 up
    ip netns exec lab ip addr add "$MGMT_LAB_IP/24" dev to-mgmt
    ip netns exec lab ip link set to-mgmt up
    ip route add "$SERVER_IP/32" via "$MGMT_LAB_IP" dev mgmt0 2>/dev/null || true

    # filtro de caminho reverso frouxo no rpi — necessário com 3 rotas para o mesmo destino
    ip netns exec rpi sysctl -qw net.ipv4.conf.all.rp_filter=2
    ip netns exec rpi sysctl -qw net.ipv4.conf.default.rp_filter=2

    # rota default na tabela principal — existe num Pi real antes de qualquer
    # configuração de multi-homing; sem ela tráfego sem bind (telemetria, etc)
    # não tem por onde sair. A decision_engine substitui isso pelo melhor link.
    ip netns exec rpi ip route add default via "$DEFAULT_VIA" dev "$DEFAULT_DEV"

    # refletor + telemetria sobem AQUI, uma vez só, e ficam no ar até o
    # 'down' — igual ao servidor do laboratório de verdade, que não reinicia
    # a cada rodada de teste. 'run'/'decide' só usam o que já está no ar.
    rm -f /tmp/testbed_telemetria.db
    ip netns exec lab python3 "$DIR/reflector_server.py" --bind "$SERVER_IP" \
        > /tmp/testbed_reflector.log 2>&1 &
    ip netns exec lab python3 "$DIR/telemetry_server.py" \
        --bind "$SERVER_IP" --port "$TELEMETRY_PORT" --db /tmp/testbed_telemetria.db \
        > /tmp/testbed_telemetry.log 2>&1 &
    sleep 1

    echo "bancada no ar. servidor em $SERVER_IP, alcançável pelos 3 caminhos."
    echo "refletor e telemetria no ar (ficam até o 'down')."
    echo "dashboard: http://$SERVER_IP:$TELEMETRY_PORT/  — abra direto no seu navegador"
}

down() {
    need_root down
    # 'decide'/'run' de uma sessão anterior que não foi parado com Ctrl+C
    # deixa processos presos num namespace que "ip netns del" só torna
    # invisível (o kernel mantém vivo enquanto houver processo referenciando
    # ele) — mata isso antes, senão o namespace vira fantasma.
    local PRESOS
    PRESOS=$(pgrep -f "python3 $DIR/(reflector_server|telemetry_server|decision_engine|agent_rpi)\.py" 2>/dev/null || true)
    if [[ -n "$PRESOS" ]]; then
        echo "encerrando processos da bancada ainda vivos de uma sessão anterior:"
        ps -o pid,cmd --no-headers -p $PRESOS
        kill $PRESOS 2>/dev/null || true
        sleep 1
    fi
    ip netns del rpi 2>/dev/null || true
    ip netns del lab 2>/dev/null || true
    # defensivo: deletar "lab" já derruba o par mgmt0/to-mgmt e a rota junto,
    # mas garante limpeza mesmo se a ordem der errado
    ip route del "$SERVER_IP/32" dev mgmt0 2>/dev/null || true
    ip link del mgmt0 2>/dev/null || true
    echo "removido."
}

status() {
    need_root status
    for NS in rpi lab; do
        echo "== netns $NS =="
        ip netns exec "$NS" ip -br addr
        echo "--- rotas (todas as tabelas) ---"
        ip netns exec "$NS" ip route show table all
        echo "--- regras ---"
        ip netns exec "$NS" ip rule show
        echo "--- qdiscs ---"
        ip netns exec "$NS" tc qdisc show
        echo
    done
}

check() {
    need_root check
    while read -r IF PEER SUB TBL _; do
        echo "-- $IF: ping direto ao peer $SUB.2 (valida o link + netem) --"
        ip netns exec rpi ping -I "$IF" -c3 -W1 "$SUB.2"
        echo "-- $IF: rota por política (from $SUB.1) até $SERVER_IP --"
        ip netns exec rpi ip route get "$SERVER_IP" from "$SUB.1"
        echo
    done < <(printf '%s\n' "${IFACE_CFG[@]}")
}

run() {
    need_root run
    ensure_lab_services
    local OUT=/tmp/testbed_resultados.jsonl
    rm -f "$OUT"

    echo "rodando agente em rpi (eth0,wlan0,usb0) — usando o refletor/telemetria do 'up'..."
    ip netns exec rpi python3 "$DIR/agent_rpi.py" \
        --server "$SERVER_IP" --ifaces eth0,wlan0,usb0 \
        --rounds "${ROUNDS:-3}" --count "${COUNT:-300}" --pps "${PPS:-150}" \
        --size 200 --resp-size 200 --tcp-bytes "${TCP_BYTES:-2097152}" \
        --pause 1 --out "$OUT" \
        --telemetry-url "http://$SERVER_IP:$TELEMETRY_PORT/telemetria" \
        --telemetry-db /tmp/testbed_fila.db

    echo
    python3 "$DIR/analisar.py" "$OUT"
    echo
    echo "bruto em $OUT"
    echo "dashboard (continua no ar): http://$SERVER_IP:$TELEMETRY_PORT/"
}

decide() {
    need_root decide
    ensure_lab_services
    local OUT=/tmp/testbed_decisao.jsonl
    rm -f "$OUT"

    local GW="eth0=10.0.1.2,wlan0=10.0.2.2,usb0=10.0.3.2"
    echo "rodando engine de decisão em rpi — Ctrl+C pra parar (refletor/telemetria do 'up' continuam no ar depois)."
    echo "em outro terminal: sudo ./testbed.sh flap wlan0 bad   (pra ver o failover reagir)"
    echo "                    abra http://$SERVER_IP:$TELEMETRY_PORT/  no seu navegador (dashboard ao vivo)"
    ip netns exec rpi python3 "$DIR/decision_engine.py" \
        --server "$SERVER_IP" --ifaces eth0,wlan0,usb0 --gateways "$GW" \
        --interval "${INTERVAL:-3}" --count "${COUNT:-150}" --pps "${PPS:-100}" \
        --tcp-every "${TCP_EVERY:-3}" --log "$OUT" \
        --telemetry-url "http://$SERVER_IP:$TELEMETRY_PORT/telemetria" \
        --telemetry-db /tmp/testbed_fila_engine.db

    echo "log em $OUT"
}

telemetria() {
    need_root telemetria
    local ACAO="${2:-status}"
    case "$ACAO" in
        status)
            ip netns exec rpi curl -s "http://$SERVER_IP:$TELEMETRY_PORT/telemetria?limit=${3:-10}" \
                | python3 -m json.tool
            ;;
        dashboard)
            echo "abra direto no navegador: http://$SERVER_IP:$TELEMETRY_PORT/" >&2
            echo "(isto aqui só imprime o HTML cru no terminal, é o navegador que renderiza)" >&2
            ip netns exec rpi curl -s "http://$SERVER_IP:$TELEMETRY_PORT/"
            ;;
        *)
            echo "uso: $0 telemetria [status|dashboard] [limit]" >&2
            exit 1
            ;;
    esac
}

flap() {
    need_root flap
    local IF="${2:?uso: $0 flap <eth0|wlan0|usb0> <down|up|bad|good>}"
    local MODE="${3:?uso: $0 flap <eth0|wlan0|usb0> <down|up|bad|good>}"
    case "$MODE" in
        down)
            ip netns exec rpi ip link set "$IF" down
            echo "$IF derrubada"
            ;;
        up)
            ip netns exec rpi ip link set "$IF" up
            echo "$IF religada"
            ;;
        bad)
            ip netns exec rpi tc qdisc change dev "$IF" root netem delay 300ms 100ms loss 60%
            echo "$IF degradada (300ms±100ms, 60% perda)"
            ;;
        good)
            while read -r CFG_IF _ _ _ D J LOSS_UP _; do
                if [[ "$CFG_IF" == "$IF" ]]; then
                    ip netns exec rpi tc qdisc change dev "$IF" root netem delay "$D" "$J" loss "${LOSS_UP}%"
                    echo "$IF restaurada ao perfil original ($D~$J, ${LOSS_UP}% perda)"
                fi
            done < <(printf '%s\n' "${IFACE_CFG[@]}")
            ;;
        *)
            echo "modo inválido: $MODE (use down|up|bad|good)" >&2
            exit 1
            ;;
    esac
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    status) status ;;
    check) check ;;
    run) run ;;
    decide) decide ;;
    telemetria) telemetria "$@" ;;
    flap) flap "$@" ;;
    *)
        echo "uso: $0 {up|status|check|run|decide|telemetria|flap|down}" >&2
        exit 1
        ;;
esac
