import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import serial
import serial.tools.list_ports
import streamlit as st

# ==============================================================================
# CONSTANTES ALINHADAS COM O FIRMWARE DO ESP32 (estacao.ino)
# ==============================================================================
LIMITE_DIA = 40.0
INTERVALO_ENVIO_SEG = 30
N_JANELA = 20
PASSOS_FRENTE = 20
PREV_MIN = -10.0
PREV_MAX = 50.0

MAPA_ESTADOS = {
    0: "Ideal (Verde)",
    1: "Atencao (Amarelo)",
    2: "Critico (Vermelho)",
}

ARQUIVO_HISTORICO_SERIAL = Path(__file__).resolve().parent / "dados_esp32_serial.csv"
_RE_LEITURA = re.compile(
    r"Luminosidade:\s*([-\d.]+)%\s*\|\s*Umidade:\s*([-\d.]+)%\s*\|\s*Temperatura:\s*([-\d.]+)"
)
_RE_PREVISAO = re.compile(r"Previsao 10 min:\s*([-\d.]+)\s*C")
_RE_COLETANDO = re.compile(r"Previsao:\s*coletando\s*(\d+)/(\d+)\s*pontos")
_RE_THINGSPEAK = re.compile(r"ThingSpeak:\s*status\s*(\d+),\s*entrada\s*(\S+)")


# ==============================================================================
# 1. CARREGAMENTO DOS DADOS DA ESTACAO E REGRAS DO FIRMWARE
# ==============================================================================

@st.cache_data(ttl=25, show_spinner=False)
def carregar_dados_thingspeak(
    channel_id: str,
    read_api_key: str = "",
    results: int = 800,
) -> Tuple[pd.DataFrame, Dict]:
    """Busca as medicoes mais recentes diretamente da API REST do ThingSpeak."""
    url = f"https://api.thingspeak.com/channels/{channel_id.strip()}/feeds.json"
    params = {"results": results}
    if read_api_key and read_api_key.strip():
        params["api_key"] = read_api_key.strip()

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    payload = response.json()

    channel_info = payload.get("channel", {})
    feeds = payload.get("feeds", [])
    df_bruto = pd.DataFrame(feeds)
    return df_bruto, channel_info


def carregar_dados_arquivo(arquivo_upload) -> Tuple[pd.DataFrame, Dict]:
    """Carrega dados exportados do ThingSpeak ou salvos localmente (CSV ou JSON)."""
    nome = arquivo_upload.name.lower()
    if nome.endswith(".json"):
        conteudo = json.load(arquivo_upload)
        if isinstance(conteudo, dict) and "feeds" in conteudo:
            return pd.DataFrame(conteudo["feeds"]), conteudo.get("channel", {})
        return pd.DataFrame(conteudo), {"name": arquivo_upload.name}

    df_bruto = pd.read_csv(arquivo_upload)
    return df_bruto, {"name": arquivo_upload.name}


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


class ColetorSerialESP32:
    """Leitor em background da porta USB Serial do ESP32 com persistencia local em CSV."""

    def __init__(self, porta: str = "/dev/ttyUSB0", baudrate: int = 115200):
        self.porta = porta
        self.baudrate = baudrate
        self._lock = threading.Lock()
        self._ativo = False
        self._thread: Optional[threading.Thread] = None
        self._ser: Optional[serial.Serial] = None
        self.conectado = False
        self.ultimo_erro: Optional[str] = None
        self.ultima_linha: str = ""
        self.ultima_previsao: float = np.nan
        self.progresso_previsao: str = "0/20"
        self.ultimo_status_ts: str = ""
        self._janela_30s: List[float] = []
        self._ultimo_ponto_30s: float = 0.0
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
                prevs = df_hist["field5"].dropna()
                if not prevs.empty:
                    self.ultima_previsao = float(prevs.iloc[-1])
        except Exception:
            self.registros = []

    def iniciar(self, porta: str, baudrate: int = 115200) -> None:
        if self._ativo and self.porta == porta and self.baudrate == baudrate and self._thread and self._thread.is_alive():
            return
        self.parar()
        self.porta = porta
        self.baudrate = baudrate
        self._ativo = True
        self._thread = threading.Thread(target=self._loop_leitura, daemon=True)
        self._thread.start()

    def parar(self) -> None:
        self._ativo = False
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self.conectado = False

    def limpar_historico(self) -> None:
        with self._lock:
            self.registros.clear()
            self._janela_30s.clear()
            self._ultimo_ponto_30s = 0.0
            self.ultima_previsao = np.nan
            if ARQUIVO_HISTORICO_SERIAL.exists():
                try:
                    ARQUIVO_HISTORICO_SERIAL.unlink()
                except OSError:
                    pass

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

                with self._lock:
                    self.ultima_linha = linha

                    m_prev = _RE_PREVISAO.search(linha)
                    if m_prev:
                        self.ultima_previsao = float(m_prev.group(1))
                        self.progresso_previsao = f"{N_JANELA}/{N_JANELA}"
                        continue

                    m_col = _RE_COLETANDO.search(linha)
                    if m_col:
                        self.progresso_previsao = f"{m_col.group(1)}/{m_col.group(2)}"
                        continue

                    m_ts = _RE_THINGSPEAK.search(linha)
                    if m_ts:
                        self.ultimo_status_ts = f"HTTP {m_ts.group(1)} (entrada #{m_ts.group(2)})"
                        continue

                    m_leit = _RE_LEITURA.search(linha)
                    if m_leit:
                        luz = round(float(m_leit.group(1)), 1)
                        umid = round(float(m_leit.group(2)), 1)
                        temp = round(float(m_leit.group(3)), 1)
                        estado = _avaliar_semaforo_esp32(temp, umid, luz)

                        agora_ts = time.time()
                        if agora_ts - self._ultimo_ponto_30s >= INTERVALO_ENVIO_SEG:
                            self._ultimo_ponto_30s = agora_ts
                            self._janela_30s.append(temp)
                            if len(self._janela_30s) > N_JANELA:
                                self._janela_30s.pop(0)
                            if len(self._janela_30s) == N_JANELA and np.isnan(self.ultima_previsao):
                                self.ultima_previsao = round(
                                    _prever_temperatura_esp32(np.array(self._janela_30s)), 1
                                )

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
                        self.registros.append(registro)
                        if len(self.registros) > 10000:
                            self.registros = self.registros[-10000:]

                        escrever_cabecalho = not ARQUIVO_HISTORICO_SERIAL.exists()
                        pd.DataFrame([registro]).to_csv(
                            ARQUIVO_HISTORICO_SERIAL,
                            mode="a",
                            header=escrever_cabecalho,
                            index=False,
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
                "name": f"ESP32 em Tempo Real ({self.porta})",
                "conectado": self.conectado,
                "ultimo_erro": self.ultimo_erro,
                "ultima_linha": self.ultima_linha,
                "progresso_previsao": self.progresso_previsao,
                "ultimo_status_ts": self.ultimo_status_ts,
            }
        return pd.DataFrame(fatia), info


@st.cache_resource(show_spinner=False)
def obter_coletor_serial(porta: str = "/dev/ttyUSB0", baudrate: int = 115200) -> ColetorSerialESP32:
    """Mantem uma unica conexao serial aberta em background durante toda a execucao do Streamlit."""
    return ColetorSerialESP32(porta=porta, baudrate=baudrate)
