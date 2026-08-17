#!/usr/bin/env python3
"""
telemetry_server.py — endpoint HTTP + banco no servidor do laboratório.

Recebe os resultados que o Pi envia (mesmo formato JSON que o
agent_rpi.py/decision_engine.py já gravam localmente), grava num SQLite,
e serve um painel HTML somente-leitura. Roda ao lado do
reflector_server.py — são coisas diferentes: o reflector mede o enlace,
este aqui guarda o que foi medido.

    POST /telemetria   corpo = um registro JSON (um round de um agente)
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_LOCK = threading.Lock()
ATIVO_JANELA_S = 10  # sem registro novo nesse intervalo, considera "parado"


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
    con.commit()
    con.close()


def inserir(db_path: str, registro: dict) -> None:
    u = registro.get("udp", {}) or {}
    with DB_LOCK, sqlite3.connect(db_path) as con:
        con.execute(
            """INSERT INTO telemetria
               (host, iface, rodada, rtt_p50_ms, jitter_ms,
                perda_ida_pct, perda_volta_pct, perda_total_pct,
                tcp_up_mbps, tcp_down_mbps, erro, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                registro.get("host"), registro.get("iface"), registro.get("rodada"),
                (u.get("rtt_ms") or {}).get("p50"), u.get("jitter_descida_ms"),
                u.get("perda_ida_pct"), u.get("perda_volta_pct"), u.get("perda_total_pct"),
                (registro.get("tcp_subida") or {}).get("mbps_servidor"),
                (registro.get("tcp_descida") or {}).get("mbps_agente"),
                registro.get("erro"),
                json.dumps(registro, ensure_ascii=False),
            ),
        )


def ultimos(db_path: str, limit: int, iface: str | None) -> list[dict]:
    with sqlite3.connect(db_path) as con:
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
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT recebido_em FROM telemetria ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    ts = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def dashboard_html(db_path: str) -> str:
    linhas = ultimos(db_path, 50, None)
    idade = idade_ultimo_registro_s(db_path)
    ativo = idade is not None and idade < ATIVO_JANELA_S

    def cel(v):
        return "" if v is None else html.escape(str(v))

    trs = "\n".join(
        f"<tr><td>{cel(r['recebido_em'])}</td><td>{cel(r['host'])}</td>"
        f"<td>{cel(r['iface'])}</td><td>{cel(r['rodada'])}</td>"
        f"<td>{cel(r['rtt_p50_ms'])}</td><td>{cel(r['jitter_ms'])}</td>"
        f"<td>{cel(r['perda_ida_pct'])}</td><td>{cel(r['perda_volta_pct'])}</td>"
        f"<td>{cel(r['tcp_up_mbps'])}</td><td>{cel(r['tcp_down_mbps'])}</td>"
        f"<td style=\"color:#b00\">{cel(r['erro'])}</td></tr>"
        for r in linhas
    )
    refresh_tag = '<meta http-equiv="refresh" content="5">' if ativo else ""
    if ativo:
        status = "🟢 recebendo dados ao vivo — atualiza sozinho a cada 5s"
    elif idade is None:
        status = "⏸ parado — nenhum registro ainda"
    else:
        status = f"⏸ parado — sem registro novo há {int(idade)}s"
    return f"""<!doctype html>
<html><head><meta charset="utf-8">{refresh_tag}
<title>netprobe — telemetria</title>
<style>
body {{ font-family: monospace; margin: 2rem; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #999; padding: 4px 8px; text-align: right; }}
th {{ background: #eee; }}
</style></head>
<body>
<h1>netprobe — últimos registros recebidos</h1>
<p>{status} — {len(linhas)} registros mostrados (mais recente primeiro)
<a href="/">atualizar</a></p>
<table>
<tr><th>recebido</th><th>host</th><th>iface</th><th>rodada</th>
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
            registro = json.loads(body)
        except json.JSONDecodeError:
            self._send(400, b"json invalido", "text/plain")
            return
        inserir(self.server.db_path, registro)
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
        description="Endpoint de telemetria + banco — servidor do laboratório")
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
