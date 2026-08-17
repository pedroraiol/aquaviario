# netprobe — comparação de interfaces de rede entre Raspberry Pi e servidor

```
Raspberry Pi (agente)                          Servidor do laboratório (refletor)
  eth0  ─┐                                       UDP 5000  reflete o pacote, carimba T2/T3
  wlan0 ─┼──►  pacote de teste (T1)  ────────►    TCP 5001  controle + métricas + vazão
  usb0  ─┘  ◄──── resposta (T1,T2,T3 + payload)
            T4 na chegada
```

## Arquivos

| arquivo | onde roda | função |
|---|---|---|
| `protocol.py` | **ambas** | formato do pacote, canal de controle, estatística |
| `reflector_server.py` | servidor do lab | refletor UDP + controle TCP |
| `agent_rpi.py` | Raspberry Pi | dispara os testes, faz o rodízio das interfaces |
| `analisar.py` | qualquer uma | consolida o `.jsonl` e ranqueia as interfaces |
| `score.py` | Raspberry Pi | transforma o histórico de uma interface numa nota 0-100 |
| `decision_engine.py` | Raspberry Pi | sonda continuamente, pontua e troca a rota default (failover) |
| `telemetry_client.py` | Raspberry Pi | fila local (SQLite) + envio store-and-forward pro laboratório |
| `telemetry_server.py` | servidor do lab | endpoint HTTP + banco (SQLite) + dashboard somente-leitura |
| `testbed.sh` | qualquer uma | bancada sem hardware — namespaces simulando as 3 interfaces |

nao precisa baixar nada, usa python padrao

---

## 1. Preparar o servidor do laboratório

```bash
sudo ufw allow 5000/udp
sudo ufw allow 5001/tcp
python3 reflector_server.py --bind 0.0.0.0
```

## 2. Sincronizar os relógios (obrigatório para atraso de ida e volta separados)

O RTT funciona sem sincronia. Já `T2−T1` (ida) e `T4−T3` (volta) só valem se os dois
relógios estiverem alinhados. Com `chrony` apontando o Pi para o próprio servidor do lab
chega-se em ~centenas de µs na LAN:

```bash
sudo apt install chrony
echo "server 192.168.0.10 iburst minpoll 4 maxpoll 4" | sudo tee -a /etc/chrony/chrony.conf
sudo systemctl restart chrony && chronyc tracking      # veja "System time" e "RMS offset"
```

O agente já estima o offset residual (`offset_relogios_ms`, filtrado pela amostra de
menor RTT) e corrige os valores de ida/volta. Se `chronyc` mostrar offset maior que
uns 20% do seu RTT típico, trate ida/volta como qualitativos e decida pelo RTT.

## 3. Roteamento por política no Raspberry Pi — a parte crítica

Com três interfaces ativas ao mesmo tempo, `bind()` no IP **não** faz o pacote sair
pela interface certa: a tabela de rotas usa a rota default para tudo. E as respostas
que chegam por uma interface "errada" são descartadas pelo filtro de caminho reverso.
O código usa `SO_BINDTODEVICE` (por isso precisa de root), mas você ainda precisa de
uma tabela de rotas por interface:

```bash
# /etc/iproute2/rt_tables — dê um nome a cada tabela
echo "100 t_eth0"  | sudo tee -a /etc/iproute2/rt_tables
echo "101 t_wlan0" | sudo tee -a /etc/iproute2/rt_tables
echo "102 t_usb0"  | sudo tee -a /etc/iproute2/rt_tables
```

Para cada interface (ajuste IPs/gateways):

```bash
sudo ip route add 192.168.0.0/24 dev eth0 src 192.168.0.50 table t_eth0
sudo ip route add default via 192.168.0.1 dev eth0 table t_eth0
sudo ip rule  add from 192.168.0.50 table t_eth0

sudo ip route add 192.168.1.0/24 dev wlan0 src 192.168.1.50 table t_wlan0
sudo ip route add default via 192.168.1.1 dev wlan0 table t_wlan0
sudo ip rule  add from 192.168.1.50 table t_wlan0

# ... idem para usb0/4G
```

Filtro de caminho reverso em modo frouxo (senão o retorno some):

```bash
sudo sysctl -w net.ipv4.conf.all.rp_filter=2
sudo sysctl -w net.ipv4.conf.default.rp_filter=2
# persistir: /etc/sysctl.d/99-multihoming.conf
```

Verificação: o resultado tem que citar a interface esperada:

```bash
ip route get 192.168.0.10 from 192.168.1.50
ping -I wlan0 -c3 192.168.0.10
```

(sem `iif`: com `iif` o kernel avalia como pacote *encaminhado*, o que exige
`net.ipv4.ip_forward=1`, pra gente é desnecessário pois o agente gera o pacote
localmente via socket, não encaminha nada.)

> Se as três interfaces desembocam na **mesma** rede/gateway, não estão sendo comparadas
> as interfaces, mas sim ARP. Tem que garantir caminhos distintos (switch, AP, operadora)
> ou o teste não mede o que você quer.

## 4. Rodar o agente

```bash
sudo python3 agent_rpi.py \
    --server 192.168.0.10 \
    --ifaces eth0,wlan0,usb0 \
    --rounds 20 \
    --count 1000 --pps 100 --size 200 --resp-size 200 \
    --tcp-bytes 26214400 \
    --out resultados.jsonl
```

Parâmetros que valem ajustar:

- `--count/--pps` — 1000 pacotes a 100 pps = 10 s de teste. Para caçar perda rara,
  aumentar `--count`, não `--pps`.
- `--size/--resp-size` — testar em pelo menos dois tamanhos (ex.: 200 B e 1400 B).
  Pacote pequeno mede latência do caminho; pacote grande revela serialização e
  fragmentação. deve ficar **abaixo** do MTU menos 28 B (IP+UDP) para não fragmentar.
- `--tcp-bytes 0` desativa a fase de vazão se você só quer latência.
- `--pause` — deixar pelo menos 5 s entre testes para as filas esvaziarem.

## 5. Analisar

```bash
python3 analisar.py resultados.jsonl --csv resumo.csv --por-teste-csv bruto.csv
```

---

## O que cada métrica significa

| campo | como é calculado |
|---|---|
| `rtt_ms` | `(T4−T1) − (T3−T2)`, com T1/T4 em relógio **monotônico**, imune a ajuste de NTP no meio do teste |
| `owd_ida_ms` / `owd_volta_ms` | `T2−T1` e `T4−T3` em relógio de parede, corrigidos pelo offset estimado |
| `jitter_descida_ms` | jitter interarrival da RFC 3550, `J += (|D(i)−D(i−1)| − J)/16` |
| `perda_ida_pct` | `(enviados − recebidos_pelo_servidor) / enviados` |
| `perda_volta_pct` | `(recebidos_pelo_servidor − respostas_recebidas) / recebidos_pelo_servidor` |
| `proc_servidor_us` | `T3−T2`, o custo interno do refletor, já descontado do RTT |
| `estab_rtt_iqr_ms` | dispersão do RTT entre rodadas: mede **previsibilidade**, não velocidade |

A separação ida/volta é o que essa arquitetura te dá de mais valioso: um enlace 4G com
5% de perda só na subida e um Wi-Fi com 5% distribuído são problemas completamente
diferentes, e um teste de RTT puro mostraria os dois como "5%".

## Rodar continuamente (systemd)

`/etc/systemd/system/netprobe-agent.service` no Pi:

```ini
[Unit]
Description=Agente de medicao de rede
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/netprobe
ExecStart=/usr/bin/python3 /opt/netprobe/agent_rpi.py --server 192.168.0.10 \
    --ifaces eth0,wlan0,usb0 --rounds 1000000 --out /var/log/netprobe/resultados.jsonl
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

No servidor, o mesmo padrão com `ExecStart=/usr/bin/python3 /opt/netprobe/reflector_server.py`.

## Metodologia — o que estraga o experimento

1. **Testar as interfaces em paralelo.** Elas competem por CPU e, no Pi, pelo mesmo
   barramento USB (em modelos anteriores ao Pi 4, a Ethernet é USB). O agente é
   sequencial de propósito.
2. **Testar em bloco** (todo o eth0, depois todo o wlan0). mediria a hora do dia.
   O rodízio já é feito, mas devemos rodar por 24 h para pegar horário de pico.
3. **Ignorar a saturação de CPU.** Em Python, acima de ~2000 pps o Pi vira o gargalo e
   não a rede. Acompanhar `loadavg` e o `proc_servidor_us`: se o processamento subir
   junto com o RTT, o número é nosso, não da rede.
4. **Só olhar a média.** Comparar p95/p99 e o IQR. para a maioria das aplicações,
   um enlace de 30 ms estável ganha de um de 12 ms que às vezes vai a 400 ms.

## Bancada de testes sem hardware (`testbed.sh`)

Validar o sistema sem o raspberry. O `testbed.sh` monta, em uma única máquina Linux, dois *network namespaces* ligados por três pares `veth`, cada um com um perfil de atraso/perda diferente:

```
netns "rpi"                                   netns "lab"
  eth0  10.0.1.1/24 ── veth ── to-eth0  10.0.1.2/24 ┐
  wlan0 10.0.2.1/24 ── veth ── to-wlan0 10.0.2.2/24 ├─ lo: 10.99.0.1/32
  usb0  10.0.3.1/24 ── veth ── to-usb0  10.0.3.2/24 ┘
```

As interfaces têm os mesmos nomes do raspberry real, então a linha de comando do agente é
idêntica nos dois cenários. O servidor escuta num IP único (`10.99.0.1`) alcançável
pelos três caminhos.

```bash
sudo ./testbed.sh up            # monta + sobe refletor e telemetria (ficam no ar até o down)
sudo ./testbed.sh status        # endereços, rotas, regras e qdiscs
sudo ./testbed.sh check         # ping pelos 3 caminhos
sudo ./testbed.sh run           # roda o agente (usa o refletor/telemetria do 'up')
sudo ./testbed.sh decide        # roda a engine de decisão (score/failover)
sudo ./testbed.sh flap IF MODO  # simula IF piorando/melhorando: down|up|bad|good
sudo ./testbed.sh down          # derruba refletor/telemetria e remove tudo
```

O refletor e o servidor de telemetria sobem uma única vez, no `up`, e ficam no
ar até o `down` — sobrevivem a quantos `run`/`decide` você rodar no meio
(exatamente como o servidor do laboratório de verdade, que não reinicia a
cada teste). O dashboard em `http://10.99.0.1:8080/` continua acessível
mesmo depois que um `run` termina.

Requisitos: `iproute2` e o módulo `sch_netem` (em Ubuntu, às vezes está em
`linux-modules-extra-$(uname -r)`). Em WSL2 o `netem` pode faltar dependendo do
kernel, nesse caso usar uma VM Linux de verdade.

### Motivo de fazer o testbed

- **O perfil do `usb0` é assimétrico de propósito**: 4% de perda na subida contra
  0,5% na descida. Se o agente reportar isso corretamente em `perda_ida_pct` e
  `perda_volta_pct`, a instrumentação está certa. Nenhum teste de RTT puro
  distingue esses dois casos.
- **Os dois lados compartilham o mesmo relógio**, então `owd_ida_ms` e
  `owd_volta_ms` podem ser conferidos contra o valor exato que você pôs no `netem`.
  É a única situação em que você tem a resposta certa na mão.
- Exercita o mesmo `SO_BINDTODEVICE` + `ip rule`/`ip route` que o Pi vai precisar. depura o roteamento antes de usar o raspberry.

### O que a bancada NÃO reproduz

Rádio Wi-Fi (retransmissão da camada MAC, interferência, perda de associação),
modem 4G (variação de RTT por handover, políticas da operadora), contenção do
barramento USB do Pi, limite de CPU do ARM e timestamps de hardware. Ou seja: a
bancada valida a **corretude do código**; ela não decide qual interface é melhor.
Essa resposta só vem do Pi conectado nos enlaces reais.

## Score e failover (protótipo)

`score.py` e `decision_engine.py` são a próxima camada, em cima da sondagem:
decidir sozinho qual interface deve carregar o tráfego real, e trocar
automaticamente quando um link piora.

- **`score.py`** — puro, sem rede: transforma o histórico recente de uma
  interface (RTT, jitter, perda total, vazão de *subida*) em uma nota 0-100,
  combinando qualidade da amostra mais recente + estabilidade nas últimas
  rodadas − penalidade por falhas recentes (decai com o tempo — uma falha
  agora pesa mais que uma de 5 rodadas atrás). Testável isolado, sem Pi nem
  bancada nenhuma.
- **`decision_engine.py`** — roda no Pi ao lado do `agent_rpi.py`, reaproveita
  o mesmo `run_test()`. Cada ciclo sonda todas as interfaces, calcula o score
  de cada uma e, se o melhor link atual **não** é o ativo, só troca a rota
  default (`ip route replace default ... dev <iface>`) depois que o candidato
  ficou à frente por `--margin` pontos durante `--hysteresis-rounds` ciclos
  seguidos — sem histerese o sistema fica trocando de link a cada rodada por
  ruído estatístico. Cada troca (e cada ciclo) fica registrada em
  `decisao.jsonl`.
- A sondagem continua testando **todas** as interfaces o tempo todo (bind
  explícito por socket, como sempre); só o tráfego comum da aplicação — que
  não faz esse bind — segue a rota default trocada pela engine. É assim que
  dá pra monitorar os links inativos sem tirá-los do ar.

Testável 100% na bancada, sem Pi: `sudo ./testbed.sh decide` sobe a engine
dentro do netns `rpi`; em outro terminal, `sudo ./testbed.sh flap wlan0 bad`
degrada o `wlan0` na hora (300ms±100ms, 60% de perda) e dá pra ver a engine
detectar e trocar pra `eth0` no log. `sudo ./testbed.sh flap wlan0 good`
devolve o `wlan0` ao perfil original.

```bash
sudo python3 decision_engine.py --server 10.99.0.1 \
    --ifaces eth0,wlan0,usb0 \
    --gateways eth0=10.0.1.2,wlan0=10.0.2.2,usb0=10.0.3.2 \
    --interval 5 --window 10 --margin 8 --hysteresis-rounds 3 \
    --log decisao.jsonl
```

No Pi real, `--gateways` leva os gateways de verdade de cada interface
(mesmos IPs usados na seção 3). Ainda não implementado: persistir o estado
entre reinícios do processo — ele sempre começa sem link ativo e escolhe o
melhor da primeira rodada.

## Banco de telemetria e store-and-forward

`telemetry_server.py` (laboratório) + `telemetry_client.py` (Pi) resolvem o
que faltava depois da sondagem: hoje cada resultado só existe como arquivo
local (`resultados.jsonl`/`decisao.jsonl`) na máquina que rodou o teste —
nada centraliza isso. Esse par manda cada resultado pro laboratório, sem
perder dado quando a conexão cai no meio do caminho.

- **`telemetry_server.py`** — servidor HTTP + SQLite, roda ao lado do
  `reflector_server.py` (é outra coisa: o reflector *mede* o enlace, este
  *guarda* o que foi medido).
  - `POST /telemetria` recebe um registro (mesmo JSON que o agente já grava
    localmente) e insere no banco.
  - `GET /telemetria?limit=&iface=` devolve os últimos registros em JSON.
  - `GET /` é um dashboard HTML somente-leitura, recarrega sozinho a cada 5s.
  - `GET /saude` é o health check que o Pi usa antes de tentar esvaziar a fila.
- **`telemetry_client.py`** — fila local em SQLite (`Fila`), usada pelo
  `agent_rpi.py` e pelo `decision_engine.py` via `--telemetry-url`. Cada
  resultado é **sempre** enfileirado antes de tentar enviar; se o POST falhar
  (servidor fora, link caído), o registro fica na fila e é reenviado no
  próximo ciclo, na ordem em que chegou — é o "store-and-forward": você não
  perde justamente os dados do momento em que o enlace estava ruim.

```bash
# laboratório
python3 telemetry_server.py --bind 0.0.0.0 --port 8080 --db telemetria.db

# Pi — basta acrescentar --telemetry-url ao agent_rpi.py ou ao decision_engine.py
sudo python3 agent_rpi.py --server 192.168.0.10 --ifaces eth0,wlan0,usb0 \
    --telemetry-url http://192.168.0.10:8080/telemetria \
    --telemetry-db fila_telemetria.db
```

Testável 100% na bancada: `sudo ./testbed.sh up` já sobe o `telemetry_server.py`
(fica no ar até o `down`), e `run`/`decide` passam `--telemetry-url` sozinhos
apontando pra ele. O `testbed.sh` também cria um link só de administração (`mgmt0` no
seu Linux real ↔ `to-mgmt` no netns `lab`) — então o dashboard em
**`http://10.99.0.1:8080/`** abre direto no seu navegador de verdade, com o
auto-refresh de 5s funcionando (nada de HTML cru no terminal). Esse link
não participa da sondagem, é só pra você ver a página; `sudo ./testbed.sh
telemetria status` continua útil se quiser o JSON sem navegador.

Pra ver o store-and-forward de verdade: derrube a interface ativa com
`flap <iface> down` durante um `decide`, espere alguns ciclos (a fila local
acumula, sem travar o resto do sistema) e religue com `flap <iface> up` —
os registros atrasados aparecem no banco e no dashboard na sequência certa.

Ainda não implementado: um dashboard que combine telemetria de vários Pis
(hoje é uma tabela simples por servidor) e HTTPS/autenticação no endpoint —
o slide 8 pede domínio institucional e HTTPS, que fazem sentido quando o
servidor estiver exposto além do laboratório.

## Se precisar de mais precisão

- **Timestamps de hardware:** `SO_TIMESTAMPING` com `SOF_TIMESTAMPING_RX_HARDWARE`
  tira o jitter do escalonador da conta. Cheque suporte com `ethtool -T eth0`.
  A NIC do Pi não faz timestamp em hardware; a do servidor talvez sim.
- **Validação cruzada:** rode `irtt` (mede exatamente isso, em Go, com muito menos
  overhead) e `iperf3 -B <ip> --bind-dev eth0` uma vez em cada interface e confira se
  a ordem das interfaces bate com a sua. Se divergir muito, o gargalo é o agente.
- **Reescrever a fase UDP em C** só vale a pena acima de ~5000 pps.
