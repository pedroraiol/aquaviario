#!/usr/bin/env python3
"""
telemetry_client.py — fila local (SQLite) + envio pro telemetry_server.py
do laboratório. Store-and-forward: se o servidor estiver inalcançável, o
registro fica na fila e é reenviado nos próximos ciclos, na ordem em que
chegou — pra não perder justamente os dados do momento em que o link caiu.

Usado pelo agent_rpi.py e pelo decision_engine.py; não depende de nada
além da stdlib.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request

TIMEOUT_S = 3.0


class Fila:
    def __init__(self, db_path: str, server_url: str):
        self.server_url = server_url
        self.con = sqlite3.connect(db_path)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS fila (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_enfileirado TEXT NOT NULL,
                payload TEXT NOT NULL,
                enviado INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_pendentes ON fila(enviado)")
        self.con.commit()

    def enfileirar(self, registro: dict) -> None:
        self.con.execute(
            "INSERT INTO fila (ts_enfileirado, payload, enviado) VALUES (?, ?, 0)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             json.dumps(registro, ensure_ascii=False)),
        )
        self.con.commit()

    def pendentes(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM fila WHERE enviado = 0").fetchone()[0]

    def esvaziar(self, max_itens: int = 50) -> int:
        """Envia os pendentes em ordem. Para no primeiro erro — o servidor
        provavelmente ainda está fora, tenta de novo no próximo ciclo em
        vez de martelar o resto da fila. Retorna quantos foram enviados."""
        linhas = self.con.execute(
            "SELECT id, payload FROM fila WHERE enviado = 0 ORDER BY id ASC LIMIT ?",
            (max_itens,),
        ).fetchall()
        enviados = 0
        for row_id, payload in linhas:
            if not self._post(payload):
                break
            self.con.execute("DELETE FROM fila WHERE id = ?", (row_id,))
            self.con.commit()
            enviados += 1
        return enviados

    def _post(self, payload_json: str) -> bool:
        req = urllib.request.Request(
            self.server_url,
            data=payload_json.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def close(self) -> None:
        self.con.close()
