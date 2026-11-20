import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import serial
import serial.tools.list_ports
import streamlit as st

# ==============================================================================
# CONSTANTES ALINHADAS COM O FIRMWARE DO ESP32 (estacao.ino / secrets.h)
# ==============================================================================
LIMITE_DIA = 40.0
INTERVALO_ENVIO_SEG = 30
N_JANELA = 20
MIN_PONTOS_PREV = 3  # t=0s, t=30s, t=60s (1 minuto de leitura)
PASSOS_FRENTE = 20
PREV_MIN = -10.0
PREV_MAX = 50.0

MAPA_ESTADOS = {
    0: "Ideal (Verde)",
    1: "Atencao (Amarelo)",
    2: "Critico (Vermelho)",
}

ARQUIVO_SECRETS = Path(__file__).resolve().parent / "secrets.h"
ARQUIVO_HISTORICO_SERIAL = Path(__file__).resolve().parent / "dados_esp32_serial.csv"


def _carregar_config_mqtt() -> Tuple[str, int, str]:
    broker = "broker.hivemq.com"
    porta = 1883
    topico = "prof_thiago/unip/pd2/grupo_ambrosio/dados"
    if ARQUIVO_SECRETS.exists():
        try:
            conteudo = ARQUIVO_SECRETS.read_text(encoding="utf-8", errors="ignore")
            m_b = re.search(r'#define\s+MQTT_BROKER\s+"([^"]+)"', conteudo)
            m_p = re.search(r"#define\s+MQTT_PORT\s+(\d+)", conteudo)
            m_t = re.search(r'#define\s+MQTT_TOPIC\s+"([^"]+)"', conteudo)
            if m_b:
                broker = m_b.group(1).strip()
            if m_p:
                porta = int(m_p.group(1))
            if m_t:
                topico = m_t.group(1).strip()
        except Exception:
            pass
    broker = os.environ.get("MQTT_BROKER", broker)
    porta = int(os.environ.get("MQTT_PORT", porta))
    topico = os.environ.get("MQTT_TOPIC", topico)
    return broker, porta, topico


MQTT_BROKER, MQTT_PORT, MQTT_TOPIC = _carregar_config_mqtt()

_RE_LEITURA = re.compile(
    r"Luminosidade:\s*([-\d.]+)%\s*\|\s*Umidade:\s*([-\d.]+)%\s*\|\s*Temperatura:\s*([-\d.]+)"
)
_RE_PREVISAO = re.compile(r"Previsao 10 min:\s*([-\d.]+)\s*C")
_RE_COLETANDO = re.compile(r"Previsao:\s*coletando\s*(\d+)/(\d+)\s*pontos")
_RE_MQTT_SERIAL = re.compile(r"MQTT\s*\[([^\]]+)\]:\s*(\{.*\})")


# ==============================================================================
# 1. CARREGAMENTO DOS DADOS DA ESTACAO E REGRAS DO FIRMWARE
# ==============================================================================

def _prever_temperatura_esp32(y: np.ndarray, passos: int = PASSOS_FRENTE) -> float:
    """Replica exatamente a regressao linear embarcada no ESP32 (estacao.ino)."""
    n = len(y)
    if n < 2:
        return np.nan
    x = np.arange(n, dtype=float)
    soma_x = np.sum(x)
    soma_y = np.sum(y)
    soma_xy = np.sum(x * y)
    soma_xx = np.sum(x * x)

    b = (n * soma_xy - soma_x * soma_y) / (n * soma_xx - soma_x * soma_x)
    a = (soma_y - b * soma_x) / n
    x_alvo = (n - 1) + passos
    prev = a + b * x_alvo
    return float(np.clip(prev, PREV_MIN, PREV_MAX))


def _avaliar_penalidades_esp32(temp: float, umid: float, luz: float) -> Tuple[int, int, int, str]:
    """
    Replica a pontuacao de conforto termico e umidade do ESP32 (estacao.ino),
    retornando (pts_temp, pts_umid, estado_final, motivo_alerta).
    """
    dia = luz >= LIMITE_DIA
    if dia:
        min_v, max_v, min_a, max_a = 20.0, 26.0, 18.0, 28.0
    else:
        min_v, max_v, min_a, max_a = 18.0, 24.0, 16.0, 26.0

    if min_v <= temp <= max_v:
        pts_temp = 0
        motivo_t = ""
    elif min_a <= temp <= max_a:
        pts_temp = 1
        motivo_t = "Calor Moderado" if temp > max_v else "Frio Moderado"
    else:
        pts_temp = 2
        motivo_t = "Calor Extremo" if temp > max_a else "Frio Extremo"

    if 40.0 <= umid <= 60.0:
        pts_umid = 0
        motivo_u = ""
    else:
        pts_umid = 1
        motivo_u = "Ar Umido (>60%)" if umid > 60.0 else "Ar Seco (<40%)"

    soma = pts_temp + pts_umid
    if soma == 0:
        estado = 0
    elif soma == 1:
        estado = 1
    else:
        estado = 2

    if motivo_t and motivo_u:
        motivo = f"{motivo_t} + {motivo_u}"
    elif motivo_t:
        motivo = motivo_t
    elif motivo_u:
        motivo = motivo_u
    else:
        motivo = "Conforto Pleno"

    return pts_temp, pts_umid, estado, motivo


def _avaliar_semaforo_esp32(temp: float, umid: float, luz: float) -> int:
    """Replica a pontuacao de conforto termico e umidade do ESP32."""
    return _avaliar_penalidades_esp32(temp, umid, luz)[2]


def listar_portas_seriais() -> List[str]:
    """Lista portas USB/ACM reais do ESP32 priorizando /dev/ttyUSB0 e /dev/ttyACM0."""
    usb_acm = [
        p.device
        for p in serial.tools.list_ports.comports()
        if "ttyUSB" in p.device or "ttyACM" in p.device or p.vid is not None
    ]
    ordenadas: List[str] = []
    for padrao in ("/dev/ttyUSB0", "/dev/ttyACM0"):
        if os.path.exists(padrao) and padrao not in ordenadas:
            ordenadas.append(padrao)
    for p in usb_acm:
        if p not in ordenadas:
            ordenadas.append(p)
    return ordenadas or ["/dev/ttyUSB0"]


def _mqtt_varint_encode(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def _mqtt_varint_decode(sock: socket.socket) -> int:
    mult = 1
    val = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("Conexao MQTT encerrada")
        val += (b[0] & 0x7F) * mult
        if not (b[0] & 0x80):
            return val
        mult *= 128


def _mqtt_recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Conexao MQTT encerrada")
        buf.extend(chunk)
    return bytes(buf)


def _mqtt_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def extrair_medicao_mqtt(texto: str) -> Optional[Tuple[float, float, float, Optional[int], Optional[float]]]:
    """Extrai (temp, umid, luz, estado, prev) de um payload JSON ou linha textual MQTT."""
    texto = texto.strip()
    if not texto:
        return None
    if texto.startswith("{"):
        try:
            dados = json.loads(texto)
            if not isinstance(dados, dict):
                return None
            temp = dados.get("temperatura", dados.get("temp", dados.get("field1", dados.get("t"))))
            umid = dados.get("umidade", dados.get("umid", dados.get("field2", dados.get("h"))))
            luz = dados.get("luminosidade", dados.get("luz", dados.get("field3", dados.get("l"))))
            if temp is None or umid is None or luz is None:
                return None
            estado = dados.get("estado", dados.get("estado_semaforo", dados.get("field4")))
            prev = dados.get("previsao", dados.get("previsao_10min", dados.get("prev", dados.get("field5"))))
            est_int = int(estado) if estado is not None and not pd.isna(estado) else None
            prev_flt = float(prev) if prev is not None and not pd.isna(prev) else None
            return round(float(temp), 1), round(float(umid), 1), round(float(luz), 1), est_int, prev_flt
        except Exception:
            return None

    m_leit = _RE_LEITURA.search(texto)
    if m_leit:
        luz = round(float(m_leit.group(1)), 1)
        umid = round(float(m_leit.group(2)), 1)
        temp = round(float(m_leit.group(3)), 1)
        return temp, umid, luz, None, None
    return None


class ColetorSerialESP32:
    """Coletor em tempo real via MQTT (broker.hivemq.com) e USB Serial com persistencia em CSV."""

    def __init__(
        self,
        porta: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        mqtt_broker: str = MQTT_BROKER,
        mqtt_port: int = MQTT_PORT,
        mqtt_topico: str = MQTT_TOPIC,
    ):
        self.porta = porta
        self.baudrate = baudrate
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = int(mqtt_port)
        self.mqtt_topico = mqtt_topico
        self._lock = threading.Lock()
        self._mqtt_send_lock = threading.Lock()
        self._ativo = False
        self._thread: Optional[threading.Thread] = None
        self._thread_mqtt: Optional[threading.Thread] = None
        self._ser: Optional[serial.Serial] = None
        self._mqtt_sock: Optional[socket.socket] = None
        self.conectado = False
        self.mqtt_conectado = False
        self.ultimo_erro: Optional[str] = None
        self.ultimo_erro_mqtt: Optional[str] = None
        self.ultima_linha: str = ""
        self.ultima_previsao: float = np.nan
        self.progresso_previsao: str = "0/20"
        self._janela_30s: List[float] = []
        self._ultimo_ponto_30s: float = 0.0
        self._ultimo_registro_ts: float = 0.0
        self._ultimo_tx_esp32_mqtt: float = 0.0
        self.registros: List[Dict] = []
        self._carregar_historico_disco()
        self.iniciar(porta, baudrate)

    def _carregar_historico_disco(self) -> None:
        if not ARQUIVO_HISTORICO_SERIAL.exists():
            return
        try:
            df_hist = pd.read_csv(ARQUIVO_HISTORICO_SERIAL)
            if not df_hist.empty:
                self.registros = df_hist.to_dict(orient="records")
                ts_col = pd.to_datetime(df_hist["created_at"], utc=True, errors="coerce")
                ts_ultimo = ts_col.iloc[-1]
                agora_utc = datetime.now(timezone.utc)
                if pd.notna(ts_ultimo) and (agora_utc - ts_ultimo).total_seconds() <= 120:
                    prevs = df_hist["field5"].dropna()
                    if not prevs.empty:
                        self.ultima_previsao = float(prevs.iloc[-1])
                    # Reconstroi a janela deslizante de 30 s da sessao atual
                    recentes = df_hist[ts_col >= (ts_ultimo - pd.Timedelta(minutes=15))]
                    ultimo_t = 0.0
                    for _, row in recentes.iterrows():
                        t_s = pd.to_datetime(row["created_at"], utc=True).timestamp()
                        if t_s - ultimo_t >= INTERVALO_ENVIO_SEG:
                            ultimo_t = t_s
                            self._janela_30s.append(float(row["field1"]))
                    self._janela_30s = self._janela_30s[-N_JANELA:]
                    self._ultimo_ponto_30s = ultimo_t
                else:
                    self.ultima_previsao = np.nan
        except Exception:
            self.registros = []

    def iniciar(self, porta: str, baudrate: int = 115200) -> None:
        broker, port_cfg, topico = _carregar_config_mqtt()
        mudou_topico = (topico != self.mqtt_topico) or (broker != self.mqtt_broker) or (port_cfg != self.mqtt_port)
        self.mqtt_broker, self.mqtt_port, self.mqtt_topico = broker, port_cfg, topico
        if (
            self._ativo
            and not mudou_topico
            and self.porta == porta
            and self.baudrate == baudrate
            and self._thread
            and self._thread.is_alive()
            and self._thread_mqtt
            and self._thread_mqtt.is_alive()
        ):
            return
        self.parar()
        self.porta = porta
        self.baudrate = baudrate
        self._ativo = True
        self._thread = threading.Thread(target=self._loop_leitura, daemon=True)
        self._thread_mqtt = threading.Thread(target=self._loop_mqtt, daemon=True)
        self._thread.start()
        self._thread_mqtt.start()

    def parar(self) -> None:
        self._ativo = False
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        if self._mqtt_sock is not None:
            try:
                self._mqtt_sock.close()
            except Exception:
                pass
            self._mqtt_sock = None
        self.conectado = False
        self.mqtt_conectado = False

    def _registrar_medicao(
        self,
        temp: float,
        umid: float,
        luz: float,
        estado_in: Optional[int] = None,
        prev_in: Optional[float] = None,
    ) -> Optional[Dict]:
        """Registra uma medicao validada (de MQTT ou Serial) com deduplicacao de 1s."""
        agora_ts = time.time()
        if agora_ts - self._ultimo_registro_ts < 1.0:
            return None
        self._ultimo_registro_ts = agora_ts

        estado = (
            estado_in
            if estado_in in (0, 1, 2)
            else _avaliar_semaforo_esp32(temp, umid, luz)
        )

        if agora_ts - self._ultimo_ponto_30s >= INTERVALO_ENVIO_SEG:
            self._ultimo_ponto_30s = agora_ts
            self._janela_30s.append(temp)
            if len(self._janela_30s) > N_JANELA:
                self._janela_30s.pop(0)

        amostras_reg = (
            self._janela_30s[:-1] + [temp]
            if self._janela_30s
            else [temp]
        )
        if prev_in is not None and not np.isnan(prev_in):
            self.ultima_previsao = round(float(prev_in), 1)
            self.progresso_previsao = f"{max(len(self._janela_30s), MIN_PONTOS_PREV)}/{N_JANELA}"
        elif len(self._janela_30s) >= MIN_PONTOS_PREV:
            self.ultima_previsao = round(
                _prever_temperatura_esp32(np.array(amostras_reg)), 1
            )
            self.progresso_previsao = f"{len(self._janela_30s)}/{N_JANELA}"
        else:
            self.progresso_previsao = f"{len(self._janela_30s)}/{MIN_PONTOS_PREV}"

        entry_id = len(self.registros) + 1
        registro = {
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entry_id": entry_id,
            "field1": temp,
            "field2": umid,
            "field3": luz,
            "field4": estado,
            "field5": self.ultima_previsao,
        }
        self.registros.append({**registro, "ts_captura_ms": round(agora_ts * 1000, 2)})
        if len(self.registros) > 10000:
            self.registros = self.registros[-10000:]

        escrever_cabecalho = not ARQUIVO_HISTORICO_SERIAL.exists()
        pd.DataFrame([registro]).to_csv(
            ARQUIVO_HISTORICO_SERIAL,
            mode="a",
            header=escrever_cabecalho,
            index=False,
        )
        return registro

    def publicar_mqtt(self, temp: float, umid: float, luz: float, estado: int, prev: float) -> bool:
        """Publica os dados no topico MQTT caso o broker esteja conectado."""
        sock = self._mqtt_sock
        if not self.mqtt_conectado or sock is None:
            return False
        payload = json.dumps(
            {
                "temperatura": round(temp, 1),
                "umidade": round(umid, 1),
                "luminosidade": round(luz, 1),
                "estado": int(estado),
                "previsao": None if pd.isna(prev) else round(float(prev), 1),
            }
        )
        corpo = _mqtt_str(self.mqtt_topico) + payload.encode("utf-8")
        pacote = b"\x30" + _mqtt_varint_encode(len(corpo)) + corpo
        try:
            with self._mqtt_send_lock:
                sock.sendall(pacote)
            return True
        except Exception:
            return False

    def _loop_mqtt(self) -> None:
        while self._ativo:
            sock: Optional[socket.socket] = None
            try:
                sock = socket.create_connection((self.mqtt_broker, self.mqtt_port), timeout=8.0)
                cid = f"unip_pd2_dash_{os.getpid()}_{int(time.time() * 1000) % 100000}"
                var_hdr = b"\x00\x04MQTT\x04\x02\x00\x3c"
                payload_conn = _mqtt_str(cid)
                pkt_conn = b"\x10" + _mqtt_varint_encode(len(var_hdr) + len(payload_conn)) + var_hdr + payload_conn
                sock.sendall(pkt_conn)

                tipo_ack = _mqtt_recv_exact(sock, 1)
                rem_ack = _mqtt_varint_decode(sock)
                connack = _mqtt_recv_exact(sock, rem_ack)
                if tipo_ack[0] != 0x20 or len(connack) < 2 or connack[1] != 0:
                    raise ConnectionError(f"CONNACK invalido: {connack!r}")

                corpo_sub = (1).to_bytes(2, "big") + _mqtt_str(self.mqtt_topico) + b"\x00"
                pkt_sub = b"\x82" + _mqtt_varint_encode(len(corpo_sub)) + corpo_sub
                sock.sendall(pkt_sub)

                sock.settimeout(1.0)
                self._mqtt_sock = sock
                self.mqtt_conectado = True
                self.ultimo_erro_mqtt = None
                ultimo_ping = time.monotonic()

                while self._ativo:
                    if time.monotonic() - ultimo_ping >= 25.0:
                        with self._mqtt_send_lock:
                            sock.sendall(b"\xc0\x00")
                        ultimo_ping = time.monotonic()

                    try:
                        b0 = sock.recv(1)
                    except socket.timeout:
                        continue
                    if not b0:
                        raise ConnectionError("Broker MQTT fechou a conexao")

                    tipo_flags = b0[0]
                    rem_len = _mqtt_varint_decode(sock)
                    corpo = _mqtt_recv_exact(sock, rem_len) if rem_len > 0 else b""

                    if (tipo_flags & 0xF0) == 0x30:
                        t_len = int.from_bytes(corpo[0:2], "big")
                        pos = 2 + t_len
                        qos = (tipo_flags >> 1) & 0x03
                        if qos > 0:
                            pkt_id = corpo[pos : pos + 2]
                            pos += 2
                            if qos == 1:
                                with self._mqtt_send_lock:
                                    sock.sendall(b"\x40\x02" + pkt_id)
                        msg_str = corpo[pos:].decode("utf-8", errors="replace").strip()
                        extraido = extrair_medicao_mqtt(msg_str)
                        if extraido is not None:
                            temp, umid, luz, est_in, prev_in = extraido
                            with self._lock:
                                self.ultima_linha = f"MQTT [{self.mqtt_topico}]: {msg_str}"
                                self._registrar_medicao(temp, umid, luz, est_in, prev_in)
            except Exception as exc:
                self.mqtt_conectado = False
                self.ultimo_erro_mqtt = f"Erro MQTT ({self.mqtt_broker}:{self.mqtt_port}): {exc}"
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                self._mqtt_sock = None
                time.sleep(2.5)

    def _abrir_serial(self) -> serial.Serial:
        try:
            return serial.Serial(self.porta, self.baudrate, timeout=1.0)
        except serial.SerialException as exc:
            # Libera o monitor serial do Arduino IDE caso esteja bloqueando /dev/ttyUSB0 (Errno 16)
            if getattr(exc, "errno", None) == 16 or "Device or resource busy" in str(exc):
                subprocess.run(["pkill", "-f", "serial-monitor"], check=False)
                time.sleep(0.6)
                return serial.Serial(self.porta, self.baudrate, timeout=1.0)
            raise

    def _loop_leitura(self) -> None:
        while self._ativo:
            if self._ser is None or not self._ser.is_open:
                try:
                    self._ser = self._abrir_serial()
                    self.conectado = True
                    self.ultimo_erro = None
                except Exception as exc:
                    self.conectado = False
                    self.ultimo_erro = f"Nao foi possivel abrir {self.porta}: {exc}"
                    time.sleep(2.0)
                    continue

            try:
                raw = self._ser.readline()
                if not raw:
                    continue
                linha = raw.decode("utf-8", errors="replace").strip()
                if not linha:
                    continue

                registro_para_ponte = None
                with self._lock:
                    self.ultima_linha = linha

                    if _RE_MQTT_SERIAL.search(linha):
                        self._ultimo_tx_esp32_mqtt = time.time()
                        continue

                    m_prev = _RE_PREVISAO.search(linha)
                    if m_prev:
                        self.ultima_previsao = float(m_prev.group(1))
                        self.progresso_previsao = f"{N_JANELA}/{N_JANELA}"
                        continue

                    m_col = _RE_COLETANDO.search(linha)
                    if m_col:
                        self.progresso_previsao = f"{m_col.group(1)}/{m_col.group(2)}"
                        continue

                    m_leit = _RE_LEITURA.search(linha)
                    if m_leit:
                        luz = round(float(m_leit.group(1)), 1)
                        umid = round(float(m_leit.group(2)), 1)
                        temp = round(float(m_leit.group(3)), 1)
                        reg = self._registrar_medicao(temp, umid, luz)
                        if reg is not None and (time.time() - self._ultimo_tx_esp32_mqtt > 6.0):
                            registro_para_ponte = reg

                if registro_para_ponte is not None:
                    self.publicar_mqtt(
                        registro_para_ponte["field1"],
                        registro_para_ponte["field2"],
                        registro_para_ponte["field3"],
                        registro_para_ponte["field4"],
                        registro_para_ponte["field5"],
                    )
            except Exception as exc:
                self.conectado = False
                self.ultimo_erro = f"Erro na leitura serial ({self.porta}): {exc}"
                try:
                    if self._ser is not None:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                time.sleep(2.0)

    def obter_dados(self, max_registros: int = 1000) -> Tuple[pd.DataFrame, Dict]:
        with self._lock:
            fatia = list(self.registros[-max_registros:])
            info = {
                "id": self.porta,
                "name": f"ESP32 via MQTT ({self.mqtt_topico})",
                "conectado": self.conectado,
                "mqtt_conectado": self.mqtt_conectado,
                "mqtt_broker": self.mqtt_broker,
                "mqtt_port": self.mqtt_port,
                "mqtt_topico": self.mqtt_topico,
                "ultimo_erro": self.ultimo_erro if not self.mqtt_conectado else None,
                "ultimo_erro_mqtt": self.ultimo_erro_mqtt,
                "ultima_linha": self.ultima_linha,
                "progresso_previsao": self.progresso_previsao,
            }
        return pd.DataFrame(fatia), info


@st.cache_resource(show_spinner=False)
def obter_coletor_serial(porta: str = "/dev/ttyUSB0", baudrate: int = 115200) -> ColetorSerialESP32:
    """Mantem uma unica conexao serial aberta em background durante toda a execucao do Streamlit."""
    return ColetorSerialESP32(porta=porta, baudrate=baudrate)
