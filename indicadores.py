from typing import Dict, Tuple

import numpy as np
import pandas as pd

from coleta import (
    INTERVALO_ENVIO_SEG,
    LIMITE_DIA,
    MAPA_ESTADOS,
    _avaliar_penalidades_esp32,
)


# ==============================================================================
# 2. EXPLORACAO E PREPARACAO DOS DADOS
# ==============================================================================

def preparar_dados(df_bruto: pd.DataFrame, fuso_horario: str = "America/Sao_Paulo") -> Tuple[pd.DataFrame, Dict]:
    """
    Padroniza nomes de colunas, converte tipos, trata ausentes e outliers,
    ordena cronologicamente e deriva variaveis categoricas da estacao.
    """
    estatisticas_limpeza = {
        "linhas_brutas": len(df_bruto),
        "linhas_sem_timestamp": 0,
        "linhas_sem_sensores": 0,
        "linhas_outliers": 0,
        "duplicatas_removidas": 0,
        "linhas_validas": 0,
    }

    if df_bruto is None or df_bruto.empty:
        return pd.DataFrame(), estatisticas_limpeza

    df = df_bruto.copy()

    mapa_colunas = {
        "created_at": "timestamp",
        "field1": "temperatura",
        "field2": "umidade",
        "field3": "luminosidade",
        "field4": "estado_semaforo",
        "field5": "previsao_10min",
    }
    df = df.rename(columns={k: v for k, v in mapa_colunas.items() if k in df.columns})

    for col_obrigatoria in ["timestamp", "temperatura", "umidade", "luminosidade", "estado_semaforo", "previsao_10min"]:
        if col_obrigatoria not in df.columns:
            df[col_obrigatoria] = np.nan

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    estatisticas_limpeza["linhas_sem_timestamp"] = int(df["timestamp"].isna().sum())
    df = df.dropna(subset=["timestamp"]).copy()

    try:
        df["timestamp"] = df["timestamp"].dt.tz_convert(fuso_horario)
    except Exception:
        pass

    colunas_numericas = ["temperatura", "umidade", "luminosidade", "estado_semaforo", "previsao_10min"]
    for col in colunas_numericas:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "entry_id" in df.columns:
        df["entry_id"] = pd.to_numeric(df["entry_id"], errors="coerce")

    antes_sensores = len(df)
    df = df.dropna(subset=["temperatura", "umidade"]).copy()
    estatisticas_limpeza["linhas_sem_sensores"] = antes_sensores - len(df)

    # Filtro de limites fisicos do DHT22 (-40 a 80 C, 0 a 100%) e LDR (0 a 100%)
    antes_outliers = len(df)
    mascara_valida = (
        df["temperatura"].between(-40.0, 80.0)
        & df["umidade"].between(0.0, 100.0)
        & (df["luminosidade"].isna() | df["luminosidade"].between(0.0, 100.0))
    )
    df = df[mascara_valida].copy()
    estatisticas_limpeza["linhas_outliers"] = antes_outliers - len(df)

    df = df.sort_values("timestamp").reset_index(drop=True)
    antes_dup = len(df)
    if "entry_id" in df.columns and df["entry_id"].notna().any():
        df = df.drop_duplicates(subset=["entry_id"], keep="last")
    else:
        df = df.drop_duplicates(subset=["timestamp"], keep="last")
    estatisticas_limpeza["duplicatas_removidas"] = antes_dup - len(df)
    df = df.reset_index(drop=True)

    if df.empty:
        return df, estatisticas_limpeza

    df["luminosidade"] = df["luminosidade"].ffill().bfill().fillna(0.0)
    df["periodo"] = np.where(df["luminosidade"] >= LIMITE_DIA, "Dia", "Noite")

    # Decomposicao de penalidades e estado do semaforo
    avaliacoes = [
        _avaliar_penalidades_esp32(t, u, l)
        for t, u, l in zip(df["temperatura"], df["umidade"], df["luminosidade"])
    ]
    df["pts_temp"] = [av[0] for av in avaliacoes]
    df["pts_umid"] = [av[1] for av in avaliacoes]
    estados_calculados = [av[2] for av in avaliacoes]
    df["motivo_alerta"] = [av[3] for av in avaliacoes]
    df["soma_penalidades"] = df["pts_temp"] + df["pts_umid"]

    df["estado_semaforo"] = (
        df["estado_semaforo"]
        .where(df["estado_semaforo"].isin([0, 1, 2]), estados_calculados)
        .astype(int)
    )
    df["status_conforto"] = df["estado_semaforo"].map(MAPA_ESTADOS)

    df["intervalo_seg"] = df["timestamp"].diff().dt.total_seconds()

    estatisticas_limpeza["linhas_validas"] = len(df)
    return df, estatisticas_limpeza


# ==============================================================================
# 3. CALCULO DE INDICADORES METEOROLOGICOS E BIOCLIMATICOS
# ==============================================================================

def adicionar_indicadores_meteorologicos(df: pd.DataFrame, janela_movel: int = 10) -> pd.DataFrame:
    """
    Calcula variaveis meteorologicas, psicrometricas e bioclimaticas para cada registro:
    - Ponto de Orvalho (Magnus-Tetens) e Depressao do Ponto de Orvalho (margem anti-condensacao)
    - Sensacao Termica / Indice de Calor (Steadman / Rothfusz)
    - Temperatura de Bulbo Umido (Formula empirica de Stull, 2011)
    - Indice de Desconforto Termico de Thom (DI) e Indice de Temperatura e Umidade (ITU)
    - Umidade Absoluta (g/m3) e Razao de Mistura (g/kg)
    - Entalpia Especifica do Ar Umido (kJ/kg de ar seco)
    - Deficit de Pressao de Vapor - VPD (kPa) e classificacao bioclimatica
    - Medias moveis, bandas de estabilidade (desvio padrao movel) e taxa de variacao
    """
    if df.empty:
        return df

    df = df.copy()
    t = df["temperatura"].to_numpy(dtype=float)
    rh = np.clip(df["umidade"].to_numpy(dtype=float), 1e-3, 100.0)

    # 1. Ponto de Orvalho (Formula de Magnus-Tetens, em C) e Depressao do Ponto de Orvalho
    a_mag, b_mag = 17.625, 243.04
    gamma = (a_mag * t) / (b_mag + t) + np.log(rh / 100.0)
    ponto_orvalho = (b_mag * gamma) / (a_mag - gamma)
    df["ponto_orvalho"] = np.round(ponto_orvalho, 2)
    df["spread_orvalho"] = np.round(np.maximum(t - ponto_orvalho, 0.0), 2)

    # 2. Sensacao Termica / Indice de Calor (Equacao NOAA Steadman/Rothfusz adaptada para C)
    t_f = t * 9.0 / 5.0 + 32.0
    hi_simples_f = 0.5 * (t_f + 61.0 + ((t_f - 68.0) * 1.2) + (rh * 0.094))
    hi_f = np.where(
        (hi_simples_f + t_f) / 2.0 >= 80.0,
        -42.379
        + 2.04901523 * t_f
        + 10.14333127 * rh
        - 0.22475541 * t_f * rh
        - 0.00683783 * (t_f ** 2)
        - 0.05481717 * (rh ** 2)
        + 0.00122874 * (t_f ** 2) * rh
        + 0.00085282 * t_f * (rh ** 2)
        - 0.00000199 * (t_f ** 2) * (rh ** 2),
        hi_simples_f,
    )
    df["sensacao_termica"] = np.round((hi_f - 32.0) * 5.0 / 9.0, 2)

    # 3. Temperatura de Bulbo Umido (Formula de Stull 2011, em C)
    tw = (
        t * np.arctan(0.151977 * np.sqrt(rh + 8.313659))
        + np.arctan(t + rh)
        - np.arctan(rh - 1.676331)
        + 0.00391838 * (rh ** 1.5) * np.arctan(0.023101 * rh)
        - 4.686035
    )
    df["bulbo_umido"] = np.round(tw, 2)

    # 4. Indice de Desconforto de Thom (DI, em C) e ITU (Indice de Temperatura e Umidade)
    di_thom = t - 0.55 * (1.0 - 0.01 * rh) * (t - 14.5)
    df["indice_thom"] = np.round(di_thom, 2)
    df["classe_thom"] = np.select(
        [
            di_thom < 21.0,
            di_thom < 24.0,
            di_thom < 27.0,
            di_thom < 29.0,
        ],
        [
            "Confortavel (<21)",
            "Leve Desconforto (21-24)",
            "Moderado (24-27)",
            "Forte Desconforto (27-29)",
        ],
        default="Estresse Critico (>=29)",
    )

    itu = 0.8 * t + (rh / 100.0) * (t - 14.4) + 46.4
    df["itu"] = np.round(itu, 1)

    # 5. Pressao de Saturacao de Vapor (es), Pressao Real (ea) e VPD (kPa)
    es_kpa = 0.61078 * np.exp((17.27 * t) / (t + 237.3))
    ea_kpa = es_kpa * (rh / 100.0)
    vpd = np.maximum(es_kpa - ea_kpa, 0.0)
    df["vpd_kpa"] = np.round(vpd, 3)
    df["classe_vpd"] = np.select(
        [
            vpd < 0.4,
            vpd <= 0.8,
            vpd <= 1.2,
            vpd <= 1.6,
        ],
        [
            "Umido / Risco Condensacao (<0.4 kPa)",
            "Equilibrado / Vegetativo (0.4-0.8 kPa)",
            "Transpiracao Ativa (0.8-1.2 kPa)",
            "Ar Seco / Demanda Alta (1.2-1.6 kPa)",
        ],
        default="Estresse de Secura (>1.6 kPa)",
    )

    # 6. Umidade Absoluta (g/m3) e Entalpia Especifica do Ar (kJ/kg de ar seco, P_atm ~ 101.325 kPa)
    es_hpa = 6.112 * np.exp((17.67 * t) / (t + 243.5))
    df["umidade_absoluta"] = np.round((es_hpa * rh * 2.1674) / (273.15 + t), 2)

    p_atm_kpa = 101.325
    razao_mistura = 0.62198 * ea_kpa / np.maximum(p_atm_kpa - ea_kpa, 1.0)  # kg_vapor / kg_ar_seco
    entalpia = 1.006 * t + razao_mistura * (2501.0 + 1.86 * t)
    df["entalpia_kj_kg"] = np.round(entalpia, 2)

    # 7. Medias moveis e bandas de volatilidade para suavizacao de ruido e deteccao de choques
    df["temp_media_movel"] = df["temperatura"].rolling(window=janela_movel, min_periods=1).mean().round(2)
    df["temp_std_movel"] = df["temperatura"].rolling(window=janela_movel, min_periods=2).std().fillna(0.0).round(2)
    df["temp_banda_sup"] = np.round(df["temp_media_movel"] + 1.5 * df["temp_std_movel"], 2)
    df["temp_banda_inf"] = np.round(df["temp_media_movel"] - 1.5 * df["temp_std_movel"], 2)
    df["umid_media_movel"] = df["umidade"].rolling(window=janela_movel, min_periods=1).mean().round(2)

    return df


def avaliar_previsao_esp32(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
    """
    Alinha cada previsao de 10 minutos feita pelo ESP32 (field5) com a temperatura
    real medida 10 minutos depois para calcular MAE, RMSE, Vies, MAPE, R2 e Acuracia Direcional.
    """
    metricas = {
        "n_comparacoes": 0,
        "mae": np.nan,
        "rmse": np.nan,
        "vies": np.nan,
        "mape": np.nan,
        "r2": np.nan,
        "erro_max": np.nan,
        "acuracia_direcional": np.nan,
    }
    if df.empty or df["previsao_10min"].dropna().empty:
        return pd.DataFrame(), metricas

    df_prev = df[["timestamp", "temperatura", "previsao_10min"]].dropna(subset=["previsao_10min"]).copy()
    df_prev["timestamp_alvo"] = df_prev["timestamp"] + pd.Timedelta(minutes=10)

    df_real = df[["timestamp", "temperatura"]].rename(
        columns={"timestamp": "timestamp_real", "temperatura": "temperatura_real_10min"}
    )

    comparacao = pd.merge_asof(
        df_prev.sort_values("timestamp_alvo"),
        df_real.sort_values("timestamp_real"),
        left_on="timestamp_alvo",
        right_on="timestamp_real",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=90),
    ).dropna(subset=["temperatura_real_10min"])

    if comparacao.empty:
        return comparacao, metricas

    comparacao["erro"] = comparacao["previsao_10min"] - comparacao["temperatura_real_10min"]
    comparacao["erro_abs"] = comparacao["erro"].abs()
    denom = comparacao["temperatura_real_10min"].abs().replace(0, np.nan)
    comparacao["erro_pct"] = (comparacao["erro_abs"] / denom) * 100.0

    # Acuracia direcional: se o sinal da mudanca prevista coincide com o sinal da mudanca real
    delta_prev = comparacao["previsao_10min"] - comparacao["temperatura"]
    delta_real = comparacao["temperatura_real_10min"] - comparacao["temperatura"]
    acerto_direcao = (np.sign(delta_prev) == np.sign(delta_real)) | (delta_real.abs() <= 0.15)

    ss_res = float(np.sum(comparacao["erro"] ** 2))
    ss_tot = float(np.sum((comparacao["temperatura_real_10min"] - comparacao["temperatura_real_10min"].mean()) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-6 else np.nan

    metricas["n_comparacoes"] = len(comparacao)
    metricas["mae"] = float(comparacao["erro_abs"].mean())
    metricas["rmse"] = float(np.sqrt((comparacao["erro"] ** 2).mean()))
    metricas["vies"] = float(comparacao["erro"].mean())
    metricas["mape"] = float(comparacao["erro_pct"].mean())
    metricas["r2"] = float(r2) if pd.notna(r2) else np.nan
    metricas["erro_max"] = float(comparacao["erro_abs"].max())
    metricas["acuracia_direcional"] = float(acerto_direcao.mean() * 100.0)
    return comparacao, metricas


def calcular_scorecard_ambiental(df: pd.DataFrame) -> Dict[str, float]:
    """
    Calcula um indice multicriterio (0 a 100) em 5 dimensoes para avaliar a qualidade
    ambiental e operacional da estacao no periodo filtrado.
    """
    if df.empty:
        return {}

    # 1. Conforto Termico (% de leituras sem penalidade de temperatura + meio peso para atencao)
    score_termico = float(np.mean(np.where(df["pts_temp"] == 0, 100.0, np.where(df["pts_temp"] == 1, 55.0, 10.0))))

    # 2. Equilibrio Hidrico (% dentro da faixa 40-60% com decaimento suave fora da faixa)
    desvio_umid = np.maximum(0.0, np.maximum(40.0 - df["umidade"], df["umidade"] - 60.0))
    score_hidrico = float(np.mean(np.clip(100.0 - desvio_umid * 3.5, 0.0, 100.0)))

    # 3. Estabilidade Microclimatica (baseada no desvio padrao movel da temperatura)
    std_medio = float(df["temp_std_movel"].mean()) if "temp_std_movel" in df.columns else 0.5
    score_estabilidade = float(np.clip(100.0 - std_medio * 40.0, 10.0, 100.0))

    # 4. Seguranca Anti-Condensacao (margem entre Temperatura e Ponto de Orvalho >= 3 C)
    spread_medio = float(df["spread_orvalho"].mean())
    score_condensacao = float(np.clip((spread_medio / 5.0) * 100.0, 0.0, 100.0))

    # 5. Qualidade de Telemetria IoT (regularidade dos pacotes de 30s)
    intervalos = df["intervalo_seg"].dropna()
    if len(intervalos) > 0:
        pct_regulares = float((intervalos <= INTERVALO_ENVIO_SEG * 2.2).mean() * 100.0)
    else:
        pct_regulares = 100.0

    score_global = float(
        0.30 * score_termico
        + 0.25 * score_hidrico
        + 0.15 * score_estabilidade
        + 0.15 * score_condensacao
        + 0.15 * pct_regulares
    )

    return {
        "Conforto Termico": round(score_termico, 1),
        "Equilibrio Hidrico": round(score_hidrico, 1),
        "Estabilidade Termica": round(score_estabilidade, 1),
        "Seguranca Condensacao": round(score_condensacao, 1),
        "Regularidade IoT": round(pct_regulares, 1),
        "Indice Global (0-100)": round(score_global, 1),
    }


def resumir_indicadores(df: pd.DataFrame) -> Dict:
    """Agrega estatisticas globais, bioclimaticas e de telemetria do periodo filtrado."""
    if df.empty:
        return {}

    atual = df.iloc[-1]
    anterior = df.iloc[-2] if len(df) > 1 else atual

    duracao_seg = max((df["timestamp"].max() - df["timestamp"].min()).total_seconds(), 0.0)
    duracao_horas = duracao_seg / 3600.0
    taxa_termica = (
        (df["temperatura"].iloc[-1] - df["temperatura"].iloc[0]) / duracao_horas
        if duracao_horas >= 0.05
        else 0.0
    )

    dist_estados = df["status_conforto"].value_counts(normalize=True) * 100.0

    intervalos = df["intervalo_seg"].dropna()
    intervalo_medio = float(intervalos.median()) if not intervalos.empty else float(INTERVALO_ENVIO_SEG)
    intervalo_base = max(intervalo_medio, 2.0)
    quedas_conexao = int((intervalos > max(intervalo_base * 2.5, INTERVALO_ENVIO_SEG * 2.5)).sum()) if not intervalos.empty else 0
    esperados = max(int(duracao_seg / intervalo_base) + 1, len(df)) if duracao_seg > 0 else len(df)
    eficiencia_iot = min(100.0, (len(df) / max(esperados, 1)) * 100.0)

    idx_max_t = df["temperatura"].idxmax()
    idx_min_t = df["temperatura"].idxmin()

    scorecard = calcular_scorecard_ambiental(df)

    return {
        "temp_atual": float(atual["temperatura"]),
        "temp_delta": float(atual["temperatura"] - anterior["temperatura"]),
        "temp_min": float(df["temperatura"].min()),
        "temp_min_hora": df.loc[idx_min_t, "timestamp"],
        "temp_max": float(df["temperatura"].max()),
        "temp_max_hora": df.loc[idx_max_t, "timestamp"],
        "temp_media": float(df["temperatura"].mean()),
        "temp_std": float(df["temperatura"].std()) if len(df) > 1 else 0.0,
        "amplitude_termica": float(df["temperatura"].max() - df["temperatura"].min()),
        "taxa_termica_h": float(taxa_termica),
        "umid_atual": float(atual["umidade"]),
        "umid_delta": float(atual["umidade"] - anterior["umidade"]),
        "umid_min": float(df["umidade"].min()),
        "umid_max": float(df["umidade"].max()),
        "umid_media": float(df["umidade"].mean()),
        "luz_atual": float(atual["luminosidade"]),
        "luz_delta": float(atual["luminosidade"] - anterior["luminosidade"]),
        "periodo_atual": str(atual["periodo"]),
        "status_atual": str(atual["status_conforto"]),
        "motivo_atual": str(atual["motivo_alerta"]),
        "prev_10min_atual": float(atual["previsao_10min"]) if pd.notna(atual["previsao_10min"]) else np.nan,
        "orvalho_atual": float(atual["ponto_orvalho"]),
        "spread_orvalho_atual": float(atual["spread_orvalho"]),
        "bulbo_umido_atual": float(atual["bulbo_umido"]),
        "bulbo_umido_max": float(df["bulbo_umido"].max()),
        "sensacao_atual": float(atual["sensacao_termica"]),
        "sensacao_max": float(df["sensacao_termica"].max()),
        "thom_atual": float(atual["indice_thom"]),
        "classe_thom_atual": str(atual["classe_thom"]),
        "itu_atual": float(atual["itu"]),
        "entalpia_atual": float(atual["entalpia_kj_kg"]),
        "entalpia_media": float(df["entalpia_kj_kg"].mean()),
        "vpd_atual": float(atual["vpd_kpa"]),
        "vpd_medio": float(df["vpd_kpa"].mean()),
        "classe_vpd_atual": str(atual["classe_vpd"]),
        "umid_abs_atual": float(atual["umidade_absoluta"]),
        "pct_verde": float(dist_estados.get("Ideal (Verde)", 0.0)),
        "pct_amarelo": float(dist_estados.get("Atencao (Amarelo)", 0.0)),
        "pct_vermelho": float(dist_estados.get("Critico (Vermelho)", 0.0)),
        "intervalo_mediano_seg": intervalo_medio,
        "quedas_conexao": quedas_conexao,
        "eficiencia_iot": float(eficiencia_iot),
        "ultima_leitura": atual["timestamp"],
        "scorecard": scorecard,
    }
