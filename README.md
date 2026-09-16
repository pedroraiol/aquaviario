# Aquaviário: comparação de interfaces de rede entre Raspberry Pi e servidor

Um Raspberry Pi embarcado tem Ethernet, Wi-Fi e um modem 4G ao mesmo tempo, e nenhum dos três é sempre o melhor caminho: a Ethernet só existe atracado, o Wi-Fi vai até a borda da marina e o 4G oscila conforme a embarcação se desloca.
O projeto se propõe a medir os três enlaces continuamente, separando ida de volta e latência de perda, para que a decisão de qual interface carrega o tráfego seja tomada com base na qualidade do sinal. A sondagem vem primeiro; em cima dela ficam o cálculo de nota (`score.py`) e o failover automático (`decision_engine.py`).

O agente roda no Pi e um refletor roda no servidor do laboratório:

```
Raspberry Pi (agente)                          Servidor do laboratório (refletor)
  eth0  ─┐                                       UDP 5000  reflete o pacote, carimba T2/T3
  wlan0 ─┼──►  pacote de teste (T1)  ────────►    TCP 5001  controle + métricas + vazão
  usb0  ─┘  ◄──── resposta (T1,T2,T3 + payload)
            T4 na chegada
```

### Como funciona uma rodada de sondagem

Toda rodada testa uma interface de cada vez, sempre na mesma sequência:

1. O Raspberry Pi abre uma conexão de controle TCP com o servidor (porta 5001) e avisa que vai começar uma rodada de UDP. Neste momento, o servidor passa a contar as estatísticas daquela sessão.
2. O Raspberry Pi dispara os pacotes de teste por UDP, um de cada vez numa taxa fixa.
   Cada pacote carrega o instante em que foi enviado (T1, medido no relógio
   monotônico do Raspberry Pi).
3. O servidor recebe cada pacote e carimba o instante de chegada (T2); logo
   antes de devolver a reflexão, carimba também o instante de envio da
   resposta (T3).
4. A resposta volta ao Raspberry Pi com T1 (o mesmo que ele mandou), T2 e T3 dentro do pacote. O Raspberry Pi marca o instante de chegada dessa resposta (T4) e já calcula
   o RTT daquele pacote na hora, como `(T4−T1) − (T3−T2)`: o tempo total de ida e volta, descontando quanto o pacote ficou parado sendo processado
   dentro do servidor.
5. Depois do último pacote (e de uma pequena espera pelos retardatários), o Raspberry Pi encerra o socket UDP e avisa o fim da rodada pelo canal de controle.
6. Só então o servidor calcula e devolve as métricas do lado dele: quantos pacotes recebeu, jitter e atraso de ida vistos por ele, entre outras. São
   números que o Raspberry Pi não teria como calcular por si próprio, porque dependem do que o servidor viu chegar, não do que ele recebeu de volta.
7. Se a rodada também mede vazão (o que não acontece em toda rodada), o Raspberry Pi e servidor trocam um bloco de bytes de subida e outro de descida pelo mesmo
   canal de controle, e só depois encerram a sessão.

Separar T1 a T4 dessa forma é o que permite calcular RTT usando só o relógio
do Raspberry Pi (imune a qualquer diferença entre os dois relógios) e, à parte, o
atraso de ida e de volta isoladamente, que já depende dos dois relógios
estarem sincronizados (ver seção 2 mais abaixo).

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
| `telemetry_server.py` | servidor do lab | endpoint HTTP + banco (SQLite) + dashboard com gráficos |
| `testbed.sh` | qualquer uma | bancada sem hardware, namespaces simulando as 3 interfaces |

Não há nada para instalar: tudo roda com a biblioteca padrão do Python 3.

---

## 1. Preparar o servidor do laboratório

Antes de mais nada, confira o básico: IP do servidor, rota até o Raspberry Pi, e se o
Python instalado é 3.10+ (o código usa `from __future__ import annotations`
e tipos como `str | None`).

```bash
ip -br addr
ip route
python3 --version
ls
python3 -m py_compile reflector_server.py protocol.py telemetry_server.py
```

Libere as portas e suba o refletor (mede o enlace) e o servidor de
telemetria (guarda o que foi medido e mostra o dashboard, são coisas
diferentes, ver seção sobre telemetria mais abaixo):

```bash
sudo ufw allow 5000/udp
sudo ufw allow 5001/tcp
sudo ufw allow 8080/tcp
sudo ufw status

python3 reflector_server.py --bind 0.0.0.0 --udp-port 5000 --tcp-port 5001

# outro terminal
python3 telemetry_server.py --bind 0.0.0.0 --port 8080 --db telemetria.db
```

### Teste rápido no raspberry, antes do setup completo

Vale confirmar que o caminho básico funciona antes de mexer em relógio ou
roteamento por política (próximas duas seções). Com uma única interface já
dá pra validar ponta a ponta:

```bash
ip -br addr
ip route
iw dev wlan0 link

ping -I wlan0 -c 10 IP_SERVIDOR
curl http://IP_SERVIDOR:8080/saude
nc -vz IP_SERVIDOR 5001

sudo python3 agent_rpi.py \
    --server IP_SERVIDOR \
    --ifaces wlan0 \
    --rounds 3 \
    --count 100 \
    --pps 10 \
    --tcp-bytes 0 \
    --out primeiro_teste.jsonl

python3 analisar.py primeiro_teste.jsonl
```

Se isso funcionar, o próximo passo é sincronizar os relógios e, quando
houver mais de uma interface, configurar o roteamento por política das
próximas duas seções antes de partir para o teste completo (seção 4).

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

## 3. Roteamento por política no Raspberry Pi:

Com três interfaces ativas ao mesmo tempo, `bind()` no IP **não** faz o pacote sair
pela interface certa: a tabela de rotas usa a rota default para tudo. E as respostas
que chegam por uma interface "errada" são descartadas pelo filtro de caminho reverso.
O código usa `SO_BINDTODEVICE` (por isso precisa de root), mas ainda é necessário uma tabela de rotas por interface:

```bash
# /etc/iproute2/rt_tables: dê um nome a cada tabela
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

- `--count/--pps`: 1000 pacotes a 100 pps = 10 s de teste. Para caçar perda rara,
  aumentar `--count`, não `--pps`.
- `--size/--resp-size`: vale testar em pelo menos dois tamanhos (ex.: 200 B e
  1400 B). O pacote pequeno mede a latência do caminho; o grande revela
  serialização e fragmentação. O tamanho precisa ficar **abaixo** do MTU menos
  28 B (IP+UDP) para não fragmentar.
- `--tcp-bytes 0` desativa a fase de vazão, quando você só quer latência.
- `--pause`: deixe pelo menos 5 s entre testes para as filas esvaziarem.

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
| `jitter_rtt_ms` | jitter interarrival da RFC 3550 sobre o RTT completo (ida+volta), `J += (\|D(i)−D(i−1)\| − J)/16` |
| `jitter_descida_ms` | o mesmo cálculo, mas só sobre a perna servidor→Pi (`T4_real−T3`); simétrico ao `jitter_subida_ms` que o servidor já calcula sobre `T2−T1` |
| `perda_ida_pct` | `(enviados − recebidos_pelo_servidor) / enviados` |
| `perda_volta_pct` | `(recebidos_pelo_servidor − respostas_recebidas) / recebidos_pelo_servidor` |
| `proc_servidor_us` | `T3−T2`, o custo interno do refletor, já descontado do RTT |
| `estab_rtt_iqr_ms` | dispersão do RTT entre rodadas: mede **previsibilidade**, não velocidade |

A separação ida/volta é o que essa arquitetura te dá de mais valioso: um enlace 4G com
5% de perda só na subida e um Wi-Fi com 5% distribuído são problemas completamente
diferentes, e um teste de RTT puro mostraria os dois como "5%".

## Rodar continuamente (systemd)

`/etc/systemd/system/aquaviario-agent.service` no Pi:

```ini
[Unit]
Description=Agente de medicao de rede
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/aquaviario
ExecStart=/usr/bin/python3 /opt/aquaviario/agent_rpi.py --server 192.168.0.10 \
    --ifaces eth0,wlan0,usb0 --rounds 1000000 --out /var/log/aquaviario/resultados.jsonl
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

No servidor, o mesmo padrão com `ExecStart=/usr/bin/python3 /opt/aquaviario/reflector_server.py`.

## Bancada de testes sem hardware (`testbed.sh`)

Dá para validar todo o sistema sem o Raspberry. O `testbed.sh` monta, numa única máquina Linux, dois *network namespaces* ligados por três pares `veth`, cada um com seu próprio perfil de atraso e perda:

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
ar até o `down`, sobrevivem a quantos `run`/`decide` forem rodados no meio
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
- Exercita o mesmo `SO_BINDTODEVICE` + `ip rule`/`ip route` que o raspberry vai precisar,
  então serve para depurar o roteamento antes de ir para o hardware real.

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

- **`score.py`**, puro, sem rede: transforma o histórico recente de uma
  interface (RTT, jitter, perda total, vazão de *subida*) em uma nota 0-100:
  qualidade da amostra mais recente, **escalada** pela estabilidade das
  últimas rodadas (0,7 a 1,0×), menos a penalidade por falhas recentes
  (decai com o tempo, uma falha agora pesa mais que uma de 5 rodadas
  atrás). A estabilidade multiplica em vez de somar de propósito: um link
  ruim porém constante não ganha pontos de graça por ser estável. Testável
  isolado, sem raspberry nem bancada nenhuma.
- **`decision_engine.py`** roda no Raspberry Pi ao lado do `agent_rpi.py`, reaproveita
  o mesmo `run_test()`. Cada ciclo sonda todas as interfaces, calcula o score
  de cada uma e, se o melhor link atual **não** é o ativo, só troca a rota
  default (`ip route replace default ... dev <iface>`) depois que o candidato
  ficou à frente por `--margin` pontos durante `--hysteresis-rounds` ciclos
  seguidos. Sem histerese o sistema fica trocando de link a cada rodada por
  ruído estatístico. Cada troca (e cada ciclo) fica registrada em
  `decisao.jsonl`. Se `ip route replace` falhar, a engine registra
  `failover_falhou`/`ativacao_inicial_falhou` e continua tentando no ciclo
  seguinte, em vez de morrer.
- A histerese (atraso) vale para o caso "outro link parece um pouco melhor". Quando a
  interface **ativa** simplesmente cai (timeout, sem resposta) por
  `--fail-fast-rounds` ciclos seguidos, a engine troca na hora pro melhor
  link que ainda responde, sem esperar `--margin`/`--hysteresis-rounds`;
  fica registrado como `failover_rapido`. O `connect()` de cada sondagem
  usa um timeout curto (`--connect-timeout`, 4 s) só pra detectar link
  morto rápido, sem segurar o ciclo inteiro no `--timeout`.
- O teste de vazão TCP da engine é **best-effort**: só roda a cada
  `--tcp-every` ciclos, é pulado quando a sondagem barata já mostrou o link
  degradado (RTT/perda altos), e se não terminar dentro do timeout as
  métricas de latência/jitter/perda daquele ciclo continuam valendo (a
  rodada não vira "falha"). Entre medições o score reaproveita a última
  vazão conhecida só enquanto ela é recente e o link não piorou; assim um
  link que acabou de degradar não fica com a nota "segurada" por um número
  de vazão velho.
- A sondagem continua testando **todas** as interfaces o tempo todo (bind
  explícito por socket, como sempre); só o tráfego comum da aplicação, que
  não faz esse bind, segue a rota default trocada pela engine. É assim que
  dá pra monitorar os links inativos sem tirá-los do ar.

Dá pra testar na bancada, sem raspberry: `sudo ./testbed.sh decide` sobe a engine
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

No raspberry real, `--gateways` leva os gateways de verdade de cada interface
(mesmos IPs usados na seção 3). Ao iniciar, a engine confere se cada
interface acha rota pro servidor (`ip route get <server> from <ip> oif
<iface>`) e se tem gateway configurado, avisando alto no `stderr` em vez
de deixar aparecer só como timeout depois. Interface sem gateway em
`--gateways` ainda sobe (a rota default vira on-link), mas só funciona se
o destino estiver na mesma sub-rede, pra Ethernet/Wi-Fi/4G de verdade,
sempre passe o gateway.

Ainda não implementado: persistir o estado entre reinícios do processo.
Ele sempre começa sem link ativo e escolhe o melhor da primeira rodada.

## Banco de telemetria e store-and-forward

`telemetry_server.py` (laboratório) + `telemetry_client.py` (raspberry pi) resolvem o
que faltava depois da sondagem: hoje cada resultado só existe como arquivo
local (`resultados.jsonl`/`decisao.jsonl`) na máquina que rodou o teste,
nada centraliza isso. Esse par manda cada resultado pro laboratório, sem
perder dado quando a conexão cai no meio do caminho.

- **`telemetry_server.py`**, servidor HTTP + SQLite, roda ao lado do
  `reflector_server.py` (é outra coisa: o reflector *mede* o enlace, este
  *guarda* o que foi medido).
  - `POST /telemetria` recebe um registro (mesmo JSON que o agente já grava
    localmente) e insere no banco.
  - `GET /telemetria?limit=&iface=` devolve os últimos registros em JSON.
  - `GET /` é o dashboard: gráficos de RTT, jitter, perda e vazão por
    interface, cada uma com sua cor fixa e o valor de cada ponto disponível
    ao passar o mouse, além da tabela detalhada com os números exatos. Os
    gráficos são SVG gerado em Python, sem nenhuma biblioteca externa nem
    JavaScript, então funcionam mesmo sem internet no laboratório. Quando
    uma métrica não foi medida naquela rodada (a vazão, por exemplo, só roda
    a cada `--tcp-every` ciclos), o gráfico mostra a lacuna em vez de traçar
    uma linha enganosa ligando os dois lados. A página recarrega sozinha a
    cada 5s enquanto há dado novo chegando.
  - `GET /saude` é o health check que o raspberry usa antes de tentar esvaziar a fila.
- **`telemetry_client.py`**, fila local em SQLite (`Fila`), usada pelo
  `agent_rpi.py` e pelo `decision_engine.py` via `--telemetry-url`. Cada
  resultado é **sempre** enfileirado antes de tentar enviar; se o POST falhar
  (servidor fora, link caído), o registro fica na fila e é reenviado no
  próximo ciclo, na ordem em que chegou. É o "store-and-forward": você não
  perde justamente os dados do momento em que o enlace estava ruim.

```bash
# laboratório
python3 telemetry_server.py --bind 0.0.0.0 --port 8080 --db telemetria.db

# Pi: basta acrescentar --telemetry-url ao agent_rpi.py ou ao decision_engine.py
sudo python3 agent_rpi.py --server 192.168.0.10 --ifaces eth0,wlan0,usb0 \
    --telemetry-url http://192.168.0.10:8080/telemetria \
    --telemetry-db fila_telemetria.db
```

Dá pra testar só na bancada: `sudo ./testbed.sh up` já sobe o `telemetry_server.py`
(fica no ar até o `down`), e `run`/`decide` passam `--telemetry-url` sozinhos
apontando pra ele. O `testbed.sh` também cria um link só de administração (`mgmt0` no
seu Linux real ↔ `to-mgmt` no netns `lab`), então o dashboard em
**`http://10.99.0.1:8080/`** abre direto no navegador, com os
gráficos e o auto-refresh de 5s funcionando. Esse link não participa da sondagem, é só pra ver a página;
`sudo ./testbed.sh telemetria status` continua útil se quiser o JSON sem
navegador.

Pra ver o store-and-forward de verdade: derrube a interface ativa com
`flap <iface> down` durante um `decide`, espere alguns ciclos (a fila local
acumula, sem travar o resto do sistema) e religue com `flap <iface> up`.
Os registros atrasados aparecem no banco e no dashboard na sequência certa.

Ainda não implementado: um dashboard que combine telemetria de vários Pis
(hoje os gráficos e a tabela mostram um único servidor) e HTTPS/autenticação
no endpoint. O slide 8 pede domínio institucional e HTTPS, que fazem sentido
quando o servidor estiver exposto além do laboratório.


