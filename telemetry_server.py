#!/usr/bin/env python3
"""
telemetry_server.py: endpoint HTTP + banco no servidor do laboratório.

Recebe os resultados que o Pi envia (mesmo formato JSON que o
agent_rpi.py/decision_engine.py já gravam localmente), grava num SQLite,
e serve um painel HTML somente-leitura. Roda ao lado do
reflector_server.py, mas são coisas diferentes: o reflector mede o enlace,
este aqui guarda o que foi medido.

    POST /telemetria   corpo = um registro JSON: uma sondagem (agente/engine)
                       ou, se tiver a chave "evento", um evento de decisão do
                       decision_engine.py (failover, interface caiu, status...)
    GET  /telemetria    últimos registros em JSON (?limit=&iface=)
    GET  /              dashboard HTML (recarrega sozinho só enquanto há dado novo)
    GET  /saude         health check pro Pi testar antes de esvaziar a fila

Uso:
    python3 telemetry_server.py --bind 0.0.0.0 --port 8080 --db telemetria.db
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_LOCK = threading.Lock()
ATIVO_JANELA_S = 10  # sem registro novo nesse intervalo, considera "parado"

# paleta categórica (claro, escuro) por slot; ordem fixa por interface
# (ordenada alfabeticamente), nunca ciclada por rank/score.
PALETA_SERIES = [
    ("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#008300"),
    ("#4a3aa7", "#9085e9"), ("#e34948", "#e66767"),
]


def init_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS telemetria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recebido_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            host TEXT, iface TEXT, rodada INTEGER,
            rtt_p50_ms REAL, jitter_ms REAL,
            perda_ida_pct REAL, perda_volta_pct REAL, perda_total_pct REAL,
            tcp_up_mbps REAL, tcp_down_mbps REAL,
            erro TEXT,
            payload TEXT NOT NULL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_iface_tempo ON telemetria(iface, id)")
    # migração: banco criado antes da coluna existir. IF NOT EXISTS em ADD
    # COLUMN só existe em SQLite recente o bastante, então confere na mão.
    colunas = {r[1] for r in con.execute("PRAGMA table_info(telemetria)")}
    if "iface_ativa" not in colunas:
        con.execute("ALTER TABLE telemetria ADD COLUMN iface_ativa INTEGER")
    # quando FOI MEDIDO (ts_utc do Pi). recebido_em é quando chegou: depois de
    # uma queda, o store-and-forward entrega o atraso todo de uma vez
    if "medido_em" not in colunas:
        con.execute("ALTER TABLE telemetria ADD COLUMN medido_em TEXT")
    con.execute("""
        CREATE TABLE IF NOT EXISTS eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recebido_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            medido_em TEXT, host TEXT, evento TEXT, payload TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


def inserir(db_path: str, registro: dict) -> None:
    if not isinstance(registro, dict):
        raise ValueError("registro precisa ser um objeto JSON")
    if "evento" in registro:
        with DB_LOCK, closing(sqlite3.connect(db_path)) as con, con:
            con.execute("INSERT INTO eventos (medido_em, host, evento, payload) "
                        "VALUES (?, ?, ?, ?)",
                        (registro.get("ts_utc"), registro.get("host"),
                         str(registro["evento"]), json.dumps(registro, ensure_ascii=False)))
        return
    u = registro.get("udp", {}) or {}
    iface_ativa = registro.get("iface_ativa")  # só existe vindo do decision_engine.py
    with DB_LOCK, closing(sqlite3.connect(db_path)) as con, con:
        con.execute(
            """INSERT INTO telemetria
               (medido_em, host, iface, rodada, rtt_p50_ms, jitter_ms,
                perda_ida_pct, perda_volta_pct, perda_total_pct,
                tcp_up_mbps, tcp_down_mbps, iface_ativa, erro, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                registro.get("ts_utc"),
                registro.get("host"), registro.get("iface"), registro.get("rodada"),
                (u.get("rtt_ms") or {}).get("p50"), u.get("jitter_rtt_ms"),
                u.get("perda_ida_pct"), u.get("perda_volta_pct"), u.get("perda_total_pct"),
                (registro.get("tcp_subida") or {}).get("mbps_servidor"),
                (registro.get("tcp_descida") or {}).get("mbps_agente"),
                None if iface_ativa is None else int(iface_ativa),
                registro.get("erro"),
                json.dumps(registro, ensure_ascii=False),
            ),
        )


def ultimos(db_path: str, limit: int, iface: str | None) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        if iface:
            rows = con.execute(
                "SELECT * FROM telemetria WHERE iface = ? ORDER BY id DESC LIMIT ?",
                (iface, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM telemetria ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def idade_ultimo_registro_s(db_path: str) -> float | None:
    """Segundos desde o registro mais recente, ou None se o banco está vazio."""
    with closing(sqlite3.connect(db_path)) as con:
        row = con.execute(
            "SELECT recebido_em FROM telemetria ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    ts = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def ultimos_eventos(db_path: str, limit: int) -> list[dict]:
    """Eventos de decisão mais recentes, sem os `status` periódicos."""
    with closing(sqlite3.connect(db_path)) as con:
        rows = con.execute(
            "SELECT medido_em, host, payload FROM eventos WHERE evento != 'status' "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"medido_em": m, "host": h, **json.loads(p)} for m, h, p in rows]


def ultimo_status(db_path: str) -> dict | None:
    """Último `status` do engine: ativa, alternativa e estimativas."""
    with closing(sqlite3.connect(db_path)) as con:
        row = con.execute("SELECT payload FROM eventos WHERE evento = 'status' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _hora(ts: str | None) -> str:
    """'2026-10-05T14:03:07.123+00:00' -> '2026-10-05 14:03:07' (UTC)."""
    e = _epoch(ts)
    return "" if e is None else datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def interface_ativa(db_path: str) -> dict | None:
    """A interface marcada como ativa no registro mais recente que veio com
    essa informação (só o decision_engine.py manda `iface_ativa`; rodando o
    agent_rpi.py sozinho, sem failover, não existe "ativa" e isso fica None).
    """
    with closing(sqlite3.connect(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT iface, host, recebido_em FROM telemetria "
            "WHERE iface_ativa = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _cores_por_iface(ifaces: list[str]) -> dict[str, str]:
    """ordem fixa (alfabética), não por score/rank: um iface não pode trocar
    de cor entre uma atualização e outra só porque outro ficou melhor."""
    return {iface: i for i, iface in enumerate(sorted(ifaces))}


def _serie_por_iface(linhas_cron: list[dict], campo: str) -> dict[str, list[tuple[float, float]]]:
    """linhas_cron: mais antiga primeiro. Agrupa por iface, pulando None (erro,
    interface sem conexão, ou métrica que só é medida de vez em quando, como vazão).

    Eixo x = instante da MEDIÇÃO (ts_utc do Pi; recebido_em só pra registro
    antigo, sem medido_em). Não a rodada: ela recomeça em 1 quando o engine
    reinicia e embaralhava execuções diferentes. E não o horário de chegada:
    o atraso de uma queda chega todo junto. Assim uma queda aparece como
    lacuna no gráfico, que é o que ela foi."""
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in linhas_cron:
        v = r.get(campo)
        x = _epoch(r.get("medido_em") or r.get("recebido_em"))
        if v is not None and x is not None and r.get("iface"):
            out[r["iface"]].append((x, v))
    for pts in out.values():
        pts.sort(key=lambda p: p[0])
    return dict(out)


def _num(v: float) -> str:
    return f"{v:.3g}"


def _segmentos_continuos(pts: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Quebra em blocos só onde o intervalo entre medições foge do ritmo normal
    dessa série. Vazão é medida bem mais espaçada que RTT, então o "normal"
    é por série: a MEDIANA dos intervalos (a duração de um ciclo varia, ex.
    quando tem teste de vazão; o mínimo cortava à toa). O corte é pra pulo
    INESPERADO (queda, sondagem que falhou), não pro ritmo da própria métrica."""
    if not pts:
        return []
    if len(pts) == 1:
        return [pts]
    passos = [b[0] - a[0] for a, b in zip(pts, pts[1:])]
    passo_normal = sorted(passos)[len(passos) // 2]
    limite = passo_normal * 2.5
    blocos = [[pts[0]]]
    for prox, passo in zip(pts[1:], passos):
        if passo > limite:
            blocos.append([])
        blocos[-1].append(prox)
    return blocos


def _grafico_svg(titulo: str, unidade: str, series: dict[str, list[tuple[int, float]]],
                  slot_por_iface: dict[str, int], largura: int = 760, altura: int = 240) -> str:
    """Gráfico de linha, um eixo só, cor fixa por iface (nunca por rank).
    Sem JS: o hover funciona via <title> nativo do SVG em cada ponto."""
    if not series:
        return (f'<div class="grafico"><h3>{html.escape(titulo)} '
                f'<span class="unidade">({html.escape(unidade)})</span></h3>'
                f'<p class="sem-dado">sem dados ainda</p></div>')

    pad_l, pad_r, pad_t, pad_b = 44, 16, 12, 12
    plot_w, plot_h = largura - pad_l - pad_r, altura - pad_t - pad_b

    todos_x = [x for pts in series.values() for x, _ in pts]
    todos_y = [y for pts in series.values() for _, y in pts]
    xmin, xmax = min(todos_x), max(todos_x)
    ymin, ymax = min(todos_y), max(todos_y)
    if ymin == ymax:
        ymin, ymax = ymin - 1, ymax + 1
    folga = (ymax - ymin) * 0.1
    ymin, ymax = ymin - folga, ymax + folga
    xspan = max(1, xmax - xmin)

    def sx(x):
        return pad_l + (x - xmin) / xspan * plot_w

    def sy(y):
        return pad_t + plot_h - (y - ymin) / (ymax - ymin) * plot_h

    partes = []
    for k in range(5):
        val = ymin + (ymax - ymin) * k / 4
        y = sy(val)
        partes.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{largura - pad_r}" y2="{y:.1f}" class="grade"/>'
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" class="rotulo-eixo" text-anchor="end">{_num(val)}</text>'
        )

    for iface, pts in sorted(series.items()):
        cor = f"var(--series-{slot_por_iface[iface] % 8 + 1})"
        for bloco in _segmentos_continuos(pts):
            if len(bloco) == 1:
                # ponto isolado (sem vizinho na mesma rodada+1 pra conectar):
                # um "M" sozinho não desenha nada em SVG, então marca com um
                # ponto sólido pra não sumir da vista.
                x, y = bloco[0]
                partes.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3.5" fill="{cor}"/>')
                continue
            d = " ".join(f'{"M" if i == 0 else "L"}{sx(x):.1f},{sy(y):.1f}' for i, (x, y) in enumerate(bloco))
            partes.append(f'<path d="{d}" class="linha" stroke="{cor}"/>')
        for x, y in pts:
            partes.append(
                f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="9" class="alvo-hover">'
                f'<title>{html.escape(iface)}: {_num(y)} {html.escape(unidade)} '
                f'({datetime.fromtimestamp(x, timezone.utc).strftime("%H:%M:%S")} UTC)</title></circle>'
            )
        ux, uy = pts[-1]
        partes.append(
            f'<circle cx="{sx(ux):.1f}" cy="{sy(uy):.1f}" r="7" class="anel-fim"/>'
            f'<circle cx="{sx(ux):.1f}" cy="{sy(uy):.1f}" r="5" fill="{cor}"/>'
            f'<text x="{sx(ux) + 9:.1f}" y="{sy(uy) + 4:.1f}" class="rotulo-fim">{_num(uy)}</text>'
        )

    legenda = ""
    if len(series) > 1:
        itens = "".join(
            f'<span class="legenda-item"><span class="legenda-cor" '
            f'style="background:var(--series-{slot_por_iface[iface] % 8 + 1})"></span>{html.escape(iface)}</span>'
            for iface in sorted(series)
        )
        legenda = f'<div class="legenda">{itens}</div>'

    return f"""<div class="grafico">
<h3>{html.escape(titulo)} <span class="unidade">({html.escape(unidade)})</span></h3>
{legenda}
<svg viewBox="0 0 {largura} {altura}" class="svg-grafico" role="img" aria-label="{html.escape(titulo)} no tempo">
{"".join(partes)}
</svg>
</div>"""


def dashboard_html(db_path: str) -> str:
    linhas = ultimos(db_path, 50, None)
    idade = idade_ultimo_registro_s(db_path)
    recebendo_dados = idade is not None and idade < ATIVO_JANELA_S
    iface_ativa_info = interface_ativa(db_path)
    status_engine = ultimo_status(db_path)
    eventos = ultimos_eventos(db_path, 15)

    linhas_cron = list(reversed(linhas))          # mais antiga primeiro, pros gráficos
    slot_por_iface = _cores_por_iface(
        {r["iface"] for r in linhas_cron if r.get("iface")}
        | set((status_engine or {}).get("estimativas", {})))

    def cor_de(iface):
        slot = slot_por_iface.get(iface)
        return f"var(--series-{slot % 8 + 1})" if slot is not None else "var(--texto)"

    if status_engine and status_engine.get("ativo"):
        # o status do engine é a fonte certa: diz a ativa E a alternativa
        alt = status_engine.get("alternativa")
        banner_ativa = (
            '<div class="banner-ativa">interface em uso: '
            f'<strong style="color:{cor_de(status_engine["ativo"])}">'
            f'{html.escape(status_engine["ativo"])}</strong>, alternativa: '
            + (f'<strong style="color:{cor_de(alt)}">{html.escape(alt)}</strong>' if alt else "nenhuma")
            + f' <span class="banner-ativa-detalhe">(host {html.escape(status_engine.get("host") or "?")}, '
            f'medido em {html.escape(_hora(status_engine.get("ts_utc")))} UTC)</span></div>'
        )
    elif iface_ativa_info:
        slot = slot_por_iface.get(iface_ativa_info["iface"])
        cor = f"var(--series-{slot % 8 + 1})" if slot is not None else "var(--texto)"
        banner_ativa = (
            '<div class="banner-ativa">interface em uso agora: '
            f'<strong style="color:{cor}">{html.escape(iface_ativa_info["iface"])}</strong>'
            f' <span class="banner-ativa-detalhe">(host {html.escape(iface_ativa_info["host"] or "?")}, '
            f'desde {html.escape(iface_ativa_info["recebido_em"])})</span></div>'
        )
    else:
        banner_ativa = (
            '<div class="banner-ativa banner-ativa-vazio">nenhuma interface ativa '
            'registrada ainda. Isso só aparece quando quem está mandando telemetria é '
            'o decision_engine.py (com --telemetry-url); rodando o agent_rpi.py sozinho '
            'não existe failover, então não existe "ativa".</div>'
        )

    graficos = "".join(
        _grafico_svg(titulo, unidade, _serie_por_iface(linhas_cron, campo), slot_por_iface)
        for titulo, unidade, campo in [
            ("RTT p50", "ms", "rtt_p50_ms"),
            ("Jitter", "ms", "jitter_ms"),
            ("Perda total", "%", "perda_total_pct"),
            ("Vazão subida", "Mbps", "tcp_up_mbps"),
            ("Vazão descida", "Mbps", "tcp_down_mbps"),
        ]
    )

    def cel(v):
        return "" if v is None else html.escape(str(v))

    estimativas = ""
    if status_engine and status_engine.get("estimativas"):
        linhas_est = "".join(
            f'<tr><td style="color:{cor_de(i)}">{html.escape(i)}</td>'
            f"<td>{cel(e.get('p'))}</td><td>{cel(e.get('p_cons'))}</td>"
            f"<td>{cel(e.get('n_eff'))}</td><td>{cel(e.get('idade_s'))}</td>"
            f"<td>{'sim' if e.get('viva') else '<b>NÃO</b>'}</td>"
            f"<td>{'sim' if e.get('desatualizada') else ''}</td></tr>"
            for i, e in sorted(status_engine["estimativas"].items()))
        estimativas = (
            "<h3>Estimativas do engine</h3>"
            "<p class=\"nota\">p = probabilidade estimada de a interface estar boa; "
            "p_cons = estimativa pessimista (é a que ordena); n_eff = sondagens que "
            "ainda pesam; idade = segundos desde a última medição.</p>"
            "<table><tr><th>iface</th><th>p</th><th>p_cons</th><th>n_eff</th>"
            f"<th>idade s</th><th>viva</th><th>desatualizada</th></tr>{linhas_est}</table>")

    def resumo_evento(ev):
        partes = []
        if ev.get("de") or ev.get("para"):
            partes.append(f"{ev.get('de')} → {ev.get('para')}")
        elif ev.get("iface"):
            partes.append(str(ev["iface"]))
        for k in ("motivo", "gatilho", "deteccao_s", "adiada_s", "troca_rota_ms",
                  "duracao_s", "thread", "erro"):
            if ev.get(k) is not None:
                partes.append(f"{k}={ev[k]}")
        if ev.get("itens"):
            partes.append("; ".join(map(str, ev["itens"])))
        return html.escape("  ".join(partes))

    lista_eventos = ""
    if eventos:
        lista_eventos = (
            "<h3>Eventos de decisão recentes</h3><table>"
            "<tr><th>medido (UTC)</th><th>host</th><th>evento</th><th>detalhe</th></tr>"
            + "".join(f"<tr><td>{cel(_hora(ev.get('medido_em')))}</td><td>{cel(ev.get('host'))}</td>"
                      f"<td>{cel(ev.get('evento'))}</td>"
                      f"<td style=\"text-align:left\">{resumo_evento(ev)}</td></tr>"
                      for ev in eventos)
            + "</table>")

    trs = "\n".join(
        f"<tr><td>{cel(_hora(r.get('medido_em')))}</td><td>{cel(r['recebido_em'])}</td>"
        f"<td>{cel(r['host'])}</td>"
        f"<td>{cel(r['iface'])}</td>"
        f"<td class=\"marca-ativa\">{'X' if r.get('iface_ativa') == 1 else ''}</td>"
        f"<td>{cel(r['rodada'])}</td>"
        f"<td>{cel(r['rtt_p50_ms'])}</td><td>{cel(r['jitter_ms'])}</td>"
        f"<td>{cel(r['perda_ida_pct'])}</td><td>{cel(r['perda_volta_pct'])}</td>"
        f"<td>{cel(r['tcp_up_mbps'])}</td><td>{cel(r['tcp_down_mbps'])}</td>"
        f"<td style=\"color:#b00\">{cel(r['erro'])}</td></tr>"
        for r in linhas
    )
    refresh_tag = '<meta http-equiv="refresh" content="5">' if recebendo_dados else ""
    if recebendo_dados:
        status = " Recebendo dados ao vivo, atualiza sozinho a cada 5s"
    elif idade is None:
        status = "⏸ Parado, nenhum registro ainda"
    else:
        status = f"⏸ Parado, sem registro novo há {int(idade)}s"
    return f"""<!doctype html>
<html><head><meta charset="utf-8">{refresh_tag}
<title>aquaviario: telemetria</title>
<style>
:root {{
  color-scheme: light;
  --pagina: #f9f9f7; --superficie: #fcfcfb;
  --texto: #0b0b0b; --texto-2: #52514e; --texto-mudo: #898781;
  --grade: #e1e0d9; --eixo: #c3c2b7;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
  --series-5: #e87ba4; --series-6: #008300; --series-7: #4a3aa7; --series-8: #e34948;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --pagina: #0d0d0d; --superficie: #1a1a19;
    --texto: #ffffff; --texto-2: #c3c2b7; --texto-mudo: #898781;
    --grade: #2c2c2a; --eixo: #383835;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
    --series-5: #d55181; --series-6: #008300; --series-7: #9085e9; --series-8: #e66767;
  }}
}}
body {{ font-family: monospace; margin: 2rem; background: var(--pagina); color: var(--texto); }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid var(--eixo); padding: 4px 8px; text-align: right; }}
th {{ background: var(--superficie); }}
h1, h3 {{ color: var(--texto); }}

.grade-graficos {{ display: flex; flex-wrap: wrap; gap: 1.5rem; margin-bottom: 2rem; }}
.grafico {{ background: var(--superficie); border: 1px solid var(--grade); border-radius: 6px;
            padding: 1rem 1.25rem; flex: 1 1 560px; max-width: 720px; }}
.grafico h3 {{ margin: 0 0 0.5rem; font-size: 1.1rem; }}
.grafico .unidade {{ color: var(--texto-mudo); font-weight: normal; }}
.grafico .sem-dado {{ color: var(--texto-mudo); font-size: 0.85rem; }}
.svg-grafico {{ width: 100%; height: auto; overflow: visible; }}
.linha {{ fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }}
.grade {{ stroke: var(--grade); stroke-width: 1; }}
.rotulo-eixo, .rotulo-fim {{ fill: var(--texto-mudo); font-size: 11px; font-family: sans-serif; }}
.anel-fim {{ fill: var(--superficie); }}
.alvo-hover {{ fill: transparent; }}
.alvo-hover:hover {{ fill: var(--texto-mudo); opacity: 0.3; }}
.legenda {{ display: flex; flex-wrap: wrap; gap: 0.6rem; font-size: 0.8rem; color: var(--texto-2);
            font-family: sans-serif; margin-bottom: 0.3rem; }}
.legenda-item {{ display: inline-flex; align-items: center; gap: 0.3rem; }}
.legenda-cor {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
.banner-ativa {{ background: var(--superficie); border: 1px solid var(--grade);
                 border-radius: 6px; padding: 0.6rem 1rem; margin-bottom: 1rem;
                 font-size: 1rem; }}
.banner-ativa-detalhe {{ color: var(--texto-mudo); font-size: 0.85rem; }}
.banner-ativa-vazio {{ color: var(--texto-mudo); font-size: 0.85rem; }}
.marca-ativa {{ text-align: center; }}
.nota {{ color: var(--texto-mudo); font-size: 0.85rem; }}
.secao {{ margin-bottom: 2rem; }}
</style></head>
<body>
<h1>aquaviario: últimos registros recebidos</h1>
{banner_ativa}
<p>{status}, {len(linhas)} registros mostrados (gráficos em ordem cronológica, tabela mais recente primeiro)
<a href="/">atualizar</a></p>
<div class="secao">{estimativas}</div>
<div class="secao">{lista_eventos}</div>
<div class="grade-graficos">
{graficos}
</div>
<table>
<tr><th>medido (UTC)</th><th>recebido</th><th>host</th><th>iface</th><th>ativa</th><th>rodada</th>
<th>rtt p50 ms</th><th>jitter ms</th><th>perda ida %</th><th>perda volta %</th>
<th>tcp up mbps</th><th>tcp down mbps</th><th>erro</th></tr>
{trs}
</table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/saude":
            self._send(200, b"ok", "text/plain")
        elif parsed.path == "/telemetria":
            limit = int(qs.get("limit", ["50"])[0])
            iface = qs.get("iface", [None])[0]
            body = json.dumps(ultimos(self.server.db_path, limit, iface),
                              ensure_ascii=False).encode()
            self._send(200, body, "application/json")
        elif parsed.path == "/":
            self._send(200, dashboard_html(self.server.db_path).encode(),
                      "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/telemetria":
            self._send(404, b"not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            inserir(self.server.db_path, json.loads(body))
        except ValueError:            # JSON inválido (JSONDecodeError é ValueError) ou não-objeto
            self._send(400, b"json invalido", "text/plain")
            return
        self._send(200, b"ok", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(
        description="Endpoint de telemetria + banco, servidor do laboratório")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="telemetria.db")
    args = ap.parse_args()

    init_db(args.db)
    srv = Server((args.bind, args.port), Handler)
    srv.db_path = args.db
    print(f"[telemetria] escutando em {args.bind}:{args.port}, banco em {args.db}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando.")


if __name__ == "__main__":
    main()
