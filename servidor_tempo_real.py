from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from coleta import (
    ARQUIVO_HISTORICO_SERIAL,
    ColetorSerialESP32,
    INTERVALO_ENVIO_SEG,
    MIN_PONTOS_PREV,
    N_JANELA,
    _prever_temperatura_esp32,
    extrair_medicao_mqtt,
    listar_portas_seriais,
)

ARQUIVO_HTML = Path(__file__).resolve().parent / "dashboard.html"


def _num_ou_none(val) -> float | None:
    if val is None or val == "" or pd.isna(val):
        return None
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None


def normalizar_registro(reg: dict, idx_fallback: int = 0) -> dict | None:
    temp = _num_ou_none(reg.get("field1"))
    umid = _num_ou_none(reg.get("field2"))
    luz = _num_ou_none(reg.get("field3"))
    if temp is None or umid is None or luz is None:
        return None
    if not (-40.0 <= temp <= 80.0 and 0.0 <= umid <= 100.0 and 0.0 <= luz <= 100.0):
        return None

    eid = _num_ou_none(reg.get("entry_id"))
    estado = _num_ou_none(reg.get("field4"))
    return {
        "entry_id": int(eid) if eid is not None else idx_fallback,
        "timestamp": str(reg.get("created_at") or ""),
        "temperatura": temp,
        "umidade": umid,
        "luminosidade": luz,
        "estado": int(estado) if estado is not None else 0,
        "previsao": _num_ou_none(reg.get("field5")),
        "ts_captura_ms": _num_ou_none(reg.get("ts_captura_ms")),
    }


class GerenciadorTempoReal:
    def __init__(self) -> None:
        porta = listar_portas_seriais()[0]
        self.coletor = ColetorSerialESP32(porta=porta, baudrate=115200)
        self._recalcular_historico_1min()
        self._ultimo_mtime_csv = self._mtime_csv()

    def _mtime_csv(self) -> float:
        return ARQUIVO_HISTORICO_SERIAL.stat().st_mtime if ARQUIVO_HISTORICO_SERIAL.exists() else 0.0

    def _recalcular_historico_1min(self) -> None:
        with self.coletor._lock:
            if not self.coletor.registros:
                return
            ts_series = pd.to_datetime(
                [r.get("created_at") for r in self.coletor.registros], utc=True, errors="coerce"
            )
            janela_30s: list[float] = []
            ultimo_t = 0.0
            for r, ts_dt in zip(self.coletor.registros, ts_series):
                temp = _num_ou_none(r.get("field1"))
                if temp is None or pd.isna(ts_dt):
                    continue
                t_s = ts_dt.timestamp()
                if t_s - ultimo_t > 120.0:
                    janela_30s.clear()
                if t_s - ultimo_t >= INTERVALO_ENVIO_SEG or not janela_30s:
                    ultimo_t = t_s
                    janela_30s.append(temp)
                    if len(janela_30s) > N_JANELA:
                        janela_30s.pop(0)
                if len(janela_30s) >= MIN_PONTOS_PREV:
                    amostras = np.array(janela_30s[:-1] + [temp], dtype=float)
                    r["field5"] = round(_prever_temperatura_esp32(amostras), 1)
                else:
                    r["field5"] = np.nan

    def sincronizar_se_offline(self) -> None:
        # shortcut: recarrega o CSV apenas quando MQTT e serial estiverem desconectados, suficiente para testes locais
        if not self.coletor.conectado and not self.coletor.mqtt_conectado:
            mtime = self._mtime_csv()
            if mtime > self._ultimo_mtime_csv:
                self._ultimo_mtime_csv = mtime
                with self.coletor._lock:
                    self.coletor._carregar_historico_disco()
                self._recalcular_historico_1min()

    def obter_snapshot(self, limite: int = 200) -> tuple[list[dict], dict]:
        self.sincronizar_se_offline()
        with self.coletor._lock:
            brutos = list(self.coletor.registros[-limite:])
            status = {
                "porta": self.coletor.porta,
                "conectado": self.coletor.conectado,
                "mqtt_conectado": self.coletor.mqtt_conectado,
                "mqtt_broker": self.coletor.mqtt_broker,
                "mqtt_port": self.coletor.mqtt_port,
                "mqtt_topico": self.coletor.mqtt_topico,
                "ultimo_erro": self.coletor.ultimo_erro,
                "ultimo_erro_mqtt": self.coletor.ultimo_erro_mqtt,
                "progresso_previsao": self.coletor.progresso_previsao,
                "total_registros": len(self.coletor.registros),
            }
        limpos = []
        for i, r in enumerate(brutos, start=1):
            norm = normalizar_registro(r, idx_fallback=i)
            if norm is not None:
                limpos.append(norm)
        return limpos, status


GERENCIADOR: GerenciadorTempoReal | None = None


class HandlerDashboard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _enviar_json(self, payload: dict) -> None:
        corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            conteudo = ARQUIVO_HTML.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(conteudo)))
            self.end_headers()
            self.wfile.write(conteudo)
            return

        if parsed.path == "/api/historico":
            qs = parse_qs(parsed.query)
            try:
                limite = max(10, min(5000, int(qs.get("limit", ["200"])[0])))
            except ValueError:
                limite = 200
            registros, status = GERENCIADOR.obter_snapshot(limite=limite)
            self._enviar_json({"registros": registros, "status": status})
            return

        if parsed.path == "/api/stream":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            registros_iniciais, _ = GERENCIADOR.obter_snapshot(limite=1)
            ultimo_id = registros_iniciais[-1]["entry_id"] if registros_iniciais else 0
            ultimo_ping = time.monotonic()

            try:
                while True:
                    time.sleep(0.05)
                    recentes, status = GERENCIADOR.obter_snapshot(limite=20)
                    novos = [r for r in recentes if r["entry_id"] > ultimo_id]
                    if novos:
                        ultimo_id = novos[-1]["entry_id"]
                        evento = json.dumps(
                            {
                                "novos": novos,
                                "status": status,
                                "ts_envio_sse_ms": round(time.time() * 1000, 2),
                            },
                            ensure_ascii=False,
                        )
                        self.wfile.write(f"data: {evento}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    elif time.monotonic() - ultimo_ping >= 5.0:
                        ultimo_ping = time.monotonic()
                        evento_status = json.dumps({"novos": [], "status": status}, ensure_ascii=False)
                        self.wfile.write(f"data: {evento_status}\n\n".encode("utf-8"))
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        self.send_error(HTTPStatus.NOT_FOUND, "Rota nao encontrada")


def _self_check() -> None:
    amostra_valida = {
        "created_at": "2026-11-01T23:34:33Z",
        "entry_id": "42",
        "field1": "24.3",
        "field2": "83.7",
        "field3": "15.0",
        "field4": "2",
        "field5": "24.2",
    }
    norm = normalizar_registro(amostra_valida)
    assert norm is not None and norm["entry_id"] == 42
    assert norm["temperatura"] == 24.3 and norm["umidade"] == 83.7
    assert norm["luminosidade"] == 15.0 and norm["previsao"] == 24.2

    amostra_sem_prev = {**amostra_valida, "field5": float("nan")}
    norm_sem_prev = normalizar_registro(amostra_sem_prev)
    assert norm_sem_prev is not None and norm_sem_prev["previsao"] is None
    assert json.loads(json.dumps(norm_sem_prev))["previsao"] is None

    amostra_outlier = {**amostra_valida, "field1": "150.0"}
    assert normalizar_registro(amostra_outlier) is None

    med_json = extrair_medicao_mqtt(
        '{"temperatura":25.4,"umidade":58.2,"luminosidade":45.0,"estado":0,"previsao":25.9}'
    )
    assert med_json == (25.4, 58.2, 45.0, 0, 25.9)
    med_null = extrair_medicao_mqtt(
        '{"temperatura":25.4,"umidade":58.2,"luminosidade":45.0,"estado":0,"previsao":null}'
    )
    assert med_null == (25.4, 58.2, 45.0, 0, None)

    if ARQUIVO_HISTORICO_SERIAL.exists():
        df_csv = pd.read_csv(ARQUIVO_HISTORICO_SERIAL).tail(5)
        normalizados = [normalizar_registro(r) for r in df_csv.to_dict(orient="records")]
        assert any(r is not None for r in normalizados)
        json.dumps(normalizados)

    # Benchmark real de latencia ponta a ponta (captura -> fila SSE -> entrega cliente)
    import http.client
    import threading

    global GERENCIADOR
    GERENCIADOR = GerenciadorTempoReal()
    GERENCIADOR.coletor.parar()
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), HandlerDashboard)
    porta_teste = servidor.server_address[1]
    t_srv = threading.Thread(target=servidor.serve_forever, daemon=True)
    t_srv.start()

    conn = http.client.HTTPConnection("127.0.0.1", porta_teste, timeout=5)
    conn.request("GET", "/api/stream")
    resp = conn.getresponse()
    assert resp.status == 200

    # Aguarda o stream inicializar o ultimo_id antes de injetar amostras
    time.sleep(0.15)
    latencias_fila_ms = []
    latencias_rede_ms = []
    latencias_totais_ms = []

    with GERENCIADOR.coletor._lock:
        base_id = len(GERENCIADOR.coletor.registros) + 10000

    for i in range(1, 9):
        time.sleep(0.07 * i)  # injeta em fases variadas dentro do ciclo de 500 ms
        ts_cap = round(time.time() * 1000, 2)
        with GERENCIADOR.coletor._lock:
            GERENCIADOR.coletor.registros.append(
                {
                    "created_at": "2026-11-03T14:00:00Z",
                    "entry_id": base_id + i,
                    "field1": 24.5,
                    "field2": 60.0,
                    "field3": 50.0,
                    "field4": 0,
                    "field5": 24.8,
                    "ts_captura_ms": ts_cap,
                }
            )
        while True:
            linha = resp.fp.readline().decode("utf-8").strip()
            if linha.startswith("data: "):
                ts_rec = round(time.time() * 1000, 2)
                payload = json.loads(linha[6:])
                if payload.get("novos"):
                    item = payload["novos"][-1]
                    ts_sse = payload["ts_envio_sse_ms"]
                    latencias_fila_ms.append(round(ts_sse - item["ts_captura_ms"], 2))
                    latencias_rede_ms.append(round(ts_rec - ts_sse, 2))
                    latencias_totais_ms.append(round(ts_rec - item["ts_captura_ms"], 2))
                    break

    conn.close()
    servidor.shutdown()
    servidor.server_close()

    print(
        f"Benchmark SSE (8 amostras) | "
        f"Fila servidor: min={min(latencias_fila_ms):.1f}ms, media={sum(latencias_fila_ms)/len(latencias_fila_ms):.1f}ms, max={max(latencias_fila_ms):.1f}ms | "
        f"Transporte HTTP: media={sum(latencias_rede_ms)/len(latencias_rede_ms):.2f}ms | "
        f"Total (captura -> cliente): min={min(latencias_totais_ms):.1f}ms, media={sum(latencias_totais_ms)/len(latencias_totais_ms):.1f}ms, max={max(latencias_totais_ms):.1f}ms"
    )
    print("Self-check OK")


def main() -> None:
    if "--self-check" in sys.argv:
        _self_check()
        return

    global GERENCIADOR
    GERENCIADOR = GerenciadorTempoReal()
    porta_http = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
    servidor = ThreadingHTTPServer(("0.0.0.0", porta_http), HandlerDashboard)
    print(f"Dashboard em tempo real disponivel em: http://localhost:{porta_http}")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
        GERENCIADOR.coletor.parar()


if __name__ == "__main__":
    main()
