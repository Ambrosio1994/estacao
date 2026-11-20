#!/usr/bin/env python3
"""
Assinante MQTT para a Estacao de Monitoramento Ambiental (ESP32).

Fica executando continuamente no servidor, assinando o topico MQTT configurado,
validando e tratando o payload JSON recebido do ESP32 e persistindo cada leitura
em um arquivo CSV.
"""

import argparse
import csv
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

BASE_DIR = Path(__file__).resolve().parent
ARQUIVO_SECRETS = BASE_DIR / "secrets.h"
ARQUIVO_CSV_PADRAO = BASE_DIR / "dados_mqtt.csv"

LIMITE_DIA = 40.0
FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")

MAPA_ESTADOS = {
    0: "Ideal (Verde)",
    1: "Atencao (Amarelo)",
    2: "Critico (Vermelho)",
}

COLUNAS_CSV = [
    "timestamp",
    "temperatura",
    "umidade",
    "luminosidade",
    "estado_semaforo",
    "status_semaforo",
    "periodo",
    "previsao_10min",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("assinante_mqtt")


def carregar_config_mqtt() -> Tuple[str, int, str]:
    """Carrega broker, porta e topico de secrets.h e permite sobrescrita via env."""
    broker = "broker.hivemq.com"
    porta = 1883
    topico = "prof_thiago/unip/pd2/grupo_ambrosio/dados"

    if ARQUIVO_SECRETS.exists():
        conteudo = ARQUIVO_SECRETS.read_text(encoding="utf-8", errors="ignore")
        m_broker = re.search(r'#define\s+MQTT_BROKER\s+"([^"]+)"', conteudo)
        m_porta = re.search(r"#define\s+MQTT_PORT\s+(\d+)", conteudo)
        m_topico = re.search(r'#define\s+MQTT_TOPIC\s+"([^"]+)"', conteudo)
        if m_broker:
            broker = m_broker.group(1).strip()
        if m_porta:
            porta = int(m_porta.group(1))
        if m_topico:
            topico = m_topico.group(1).strip()

    broker = os.environ.get("MQTT_BROKER", broker)
    porta = int(os.environ.get("MQTT_PORT", porta))
    topico = os.environ.get("MQTT_TOPIC", topico)
    return broker, porta, topico


def _para_float(valor: Any) -> Optional[float]:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        num = float(valor)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(num) or math.isinf(num) else num


def tratar_payload_json(payload_bytes: bytes) -> Optional[Dict[str, Any]]:
    """
    Decodifica, valida e normaliza o JSON enviado pelo ESP32.

    Formato esperado do firmware (estacao.ino):
      {"temperatura":24.5,"umidade":52.0,"luminosidade":75.0,"estado":0,"previsao":24.8}
    """
    try:
        texto = payload_bytes.decode("utf-8").strip()
        dados = json.loads(texto)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Payload descartado (JSON invalido): %s", exc)
        return None

    if not isinstance(dados, dict):
        logger.warning("Payload descartado (JSON nao e um objeto): %r", dados)
        return None

    temp = _para_float(dados.get("temperatura"))
    umid = _para_float(dados.get("umidade"))
    luz = _para_float(dados.get("luminosidade"))

    if temp is None or umid is None or luz is None:
        logger.warning("Payload descartado (campos obrigatorios ausentes): %s", texto)
        return None

    # Validacao dos limites fisicos dos sensores (DHT22 e LDR)
    if not (-40.0 <= temp <= 80.0 and 0.0 <= umid <= 100.0 and 0.0 <= luz <= 100.0):
        logger.warning(
            "Payload descartado (valores fora da faixa fisica): temp=%.1f umid=%.1f luz=%.1f",
            temp,
            umid,
            luz,
        )
        return None

    estado_raw = dados.get("estado")
    try:
        estado = int(estado_raw) if estado_raw is not None and not isinstance(estado_raw, bool) else -1
    except (TypeError, ValueError):
        estado = -1
    if estado not in MAPA_ESTADOS:
        logger.warning("Campo 'estado' invalido (%r); usando -1.", estado_raw)
        estado = -1

    previsao = _para_float(dados.get("previsao"))
    periodo = "Dia" if luz >= LIMITE_DIA else "Noite"

    return {
        "timestamp": datetime.now(FUSO_LOCAL).isoformat(timespec="seconds"),
        "temperatura": round(temp, 1),
        "umidade": round(umid, 1),
        "luminosidade": round(luz, 1),
        "estado_semaforo": estado,
        "status_semaforo": MAPA_ESTADOS.get(estado, "Desconhecido"),
        "periodo": periodo,
        "previsao_10min": "" if previsao is None else round(previsao, 1),
    }


def salvar_csv(caminho_csv: Path, registro: Dict[str, Any]) -> None:
    """Adiciona o registro tratado ao arquivo CSV, criando o cabecalho se necessario."""
    caminho_csv.parent.mkdir(parents=True, exist_ok=True)
    precisa_cabecalho = not caminho_csv.exists() or caminho_csv.stat().st_size == 0

    with caminho_csv.open("a", newline="", encoding="utf-8") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=COLUNAS_CSV)
        if precisa_cabecalho:
            escritor.writeheader()
        escritor.writerow(registro)
        arquivo.flush()


def executar_assinante(broker: str, porta: int, topico: str, caminho_csv: Path) -> None:
    """Conecta ao broker MQTT e mantem o loop de escuta ativo no servidor."""
    client_id = f"servidor_estacao_csv_{os.getpid()}"
    cliente = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
    )
    cliente.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info("Conectado ao broker MQTT %s:%d", broker, porta)
            client.subscribe(topico, qos=0)
            logger.info("Assinado no topico: %s", topico)
            logger.info("Persistindo dados em: %s", caminho_csv)
        else:
            logger.error("Falha na conexao MQTT (codigo: %s)", reason_code)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logger.warning("Desconectado inesperadamente do broker (%s). Reconectando...", reason_code)
        else:
            logger.info("Desconectado do broker MQTT.")

    def on_message(client, userdata, msg):
        registro = tratar_payload_json(msg.payload)
        if registro is None:
            return
        try:
            salvar_csv(caminho_csv, registro)
            logger.info(
                "Salvo | T=%.1f C | U=%.1f%% | L=%.1f%% (%s) | Semaforo=%s | Prev=%s",
                registro["temperatura"],
                registro["umidade"],
                registro["luminosidade"],
                registro["periodo"],
                registro["status_semaforo"],
                registro["previsao_10min"] if registro["previsao_10min"] != "" else "null",
            )
        except OSError as exc:
            logger.error("Erro ao gravar no CSV %s: %s", caminho_csv, exc)

    cliente.on_connect = on_connect
    cliente.on_disconnect = on_disconnect
    cliente.on_message = on_message

    def encerrar(signum, frame):
        logger.info("Sinal %s recebido. Encerrando assinante MQTT...", signum)
        cliente.disconnect()

    signal.signal(signal.SIGINT, encerrar)
    signal.signal(signal.SIGTERM, encerrar)

    logger.info("Iniciando assinante MQTT (%s:%d)...", broker, porta)
    cliente.connect(broker, porta, keepalive=60)
    cliente.loop_forever()


def main() -> int:
    broker_cfg, porta_cfg, topico_cfg = carregar_config_mqtt()
    csv_cfg = Path(os.environ.get("CSV_SAIDA", str(ARQUIVO_CSV_PADRAO)))

    parser = argparse.ArgumentParser(
        description="Assinante MQTT que trata JSON do ESP32 e persiste em CSV."
    )
    parser.add_argument("--broker", default=broker_cfg, help="Endereco do broker MQTT")
    parser.add_argument("--port", type=int, default=porta_cfg, help="Porta do broker MQTT")
    parser.add_argument("--topic", default=topico_cfg, help="Topico MQTT a assinar")
    parser.add_argument("--csv", type=Path, default=csv_cfg, help="Arquivo CSV de saida")
    args = parser.parse_args()

    try:
        executar_assinante(args.broker, args.port, args.topic, args.csv)
    except Exception as exc:
        logger.exception("Erro fatal no assinante MQTT: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
