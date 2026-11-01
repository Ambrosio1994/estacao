import pandas as pd
import streamlit as st

from coleta import listar_portas_seriais, obter_coletor_serial
from graficos import (
    grafico_avaliacao_previsao,
    grafico_diagnostico_alertas,
    grafico_dispersao_psicrometrica,
    grafico_distribuicao_violin,
    grafico_evolucao_temperatura,
    grafico_indicadores_derivados,
    grafico_matriz_correlacao,
    grafico_medidores_atuais,
    grafico_radar_scorecard,
    grafico_semaforo_distribuicao,
    grafico_umidade_luminosidade,
)
from indicadores import (
    adicionar_indicadores_meteorologicos,
    avaliar_previsao_esp32,
    preparar_dados,
    resumir_indicadores,
)


def renderizar_painel(max_reg_serial: int) -> None:
    porta_padrao = listar_portas_seriais()[0]
    coletor = obter_coletor_serial(porta=porta_padrao, baudrate=115200)
    coletor.iniciar(porta=porta_padrao, baudrate=115200)
    df_bruto, info_canal = coletor.obter_dados(max_registros=max_reg_serial)

    if info_canal.get("ultimo_erro") and df_bruto.empty:
        st.error(info_canal["ultimo_erro"])
        return

    if df_bruto.empty:
        st.info("Aguardando a proxima leitura em tempo real do ESP32 pela porta serial...")
        return

    df_prep, stats_limpeza = preparar_dados(df_bruto, fuso_horario="America/Sao_Paulo")
    if df_prep.empty:
        st.warning("Nao restaram registros validos apos a etapa de limpeza e preparacao dos dados.")
        return

    df_filtrado = adicionar_indicadores_meteorologicos(df_prep, janela_movel=10)
    resumo = resumir_indicadores(df_filtrado)
    comparacao_prev, metricas_prev = avaliar_previsao_esp32(df_filtrado)

    # --------------------------------------------------------------------------
    # Painel de Metricas Principais (KPIs Instantaneos + Diagnostico)
    # --------------------------------------------------------------------------
    nome_canal = info_canal.get("name", "ESP32 em Tempo Real")
    st.subheader(f"Resumo Atual - {nome_canal}")
    st.markdown(
        f"**Ultima medicao:** `{resumo['ultima_leitura'].strftime('%d/%m/%Y %H:%M:%S')}` "
        f"| **Periodo:** `{resumo['periodo_atual']}` "
        f"| **Semaforo:** `{resumo['status_atual']}` (`{resumo['motivo_atual']}`) "
        f"| **Score Ambiental:** `{resumo['scorecard'].get('Indice Global (0-100)', 0.0):.1f}/100` "
        f"| **Amostras:** `{len(df_filtrado)}`"
    )

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric(
        "Temperatura Atual",
        f"{resumo['temp_atual']:.1f} C",
        f"{resumo['temp_delta']:+.1f} C",
    )
    col2.metric(
        "Umidade Relativa",
        f"{resumo['umid_atual']:.1f} %",
        f"{resumo['umid_delta']:+.1f} %",
    )
    col3.metric(
        "Luminosidade (LDR)",
        f"{resumo['luz_atual']:.1f} %",
        f"{resumo['luz_delta']:+.1f} % ({resumo['periodo_atual']})",
    )
    prog_prev = info_canal.get("progresso_previsao", "")
    col4.metric(
        "Previsao +10 min (ESP32)",
        (
            f"{resumo['prev_10min_atual']:.1f} C"
            if pd.notna(resumo["prev_10min_atual"])
            else f"Coletando ({prog_prev})..." if prog_prev else "Coletando..."
        ),
        (
            f"{resumo['prev_10min_atual'] - resumo['temp_atual']:+.1f} C vs atual"
            if pd.notna(resumo["prev_10min_atual"])
            else None
        ),
    )
    col5.metric(
        "Bulbo Umido / Orvalho",
        f"{resumo['bulbo_umido_atual']:.1f} C",
        f"Orvalho: {resumo['orvalho_atual']:.1f} C (Spread {resumo['spread_orvalho_atual']:.1f} C)",
        delta_color="off",
    )
    col6.metric(
        "VPD / Entalpia do Ar",
        f"{resumo['vpd_atual']:.2f} kPa",
        f"{resumo['entalpia_atual']:.1f} kJ/kg | {resumo['umid_abs_atual']:.1f} g/m3",
        delta_color="off",
    )

    with st.expander("Visualizar Medidores Instantaneos (Gauges de Conforto e Qualidade)", expanded=True):
        st.plotly_chart(grafico_medidores_atuais(resumo), use_container_width=True, key="fig_gauges")

    st.divider()

    # --------------------------------------------------------------------------
    # Abas do Dashboard
    # --------------------------------------------------------------------------
    aba_evolucao, aba_indicadores, aba_previsao, aba_exploracao = st.tabs(
        [
            "1. Evolucao e Diagnostico do Semaforo",
            "2. Indicadores Meteorologicos e Bioclimaticos",
            "3. Desempenho da Previsao (ESP32)",
            "4. Qualidade de Dados e Telemetria IoT",
        ]
    )

    # === ABA 1: EVOLUCAO DAS MEDICOES E SEMAFORO ===
    with aba_evolucao:
        st.plotly_chart(grafico_evolucao_temperatura(df_filtrado), use_container_width=True, key="fig_temp")
        st.plotly_chart(grafico_umidade_luminosidade(df_filtrado), use_container_width=True, key="fig_umid_luz")

        fig_pizza, fig_linha_semaforo = grafico_semaforo_distribuicao(df_filtrado)
        col_g1, col_g2 = st.columns([1, 2])
        with col_g1:
            st.plotly_chart(fig_pizza, use_container_width=True, key="fig_pizza")
        with col_g2:
            st.plotly_chart(fig_linha_semaforo, use_container_width=True, key="fig_linha_semaforo")

        st.plotly_chart(grafico_diagnostico_alertas(df_filtrado), use_container_width=True, key="fig_alertas")

    # === ABA 2: INDICADORES METEOROLOGICOS E BIOCLIMATICOS ===
    with aba_indicadores:
        st.subheader("Indicadores Meteorologicos e Bioclimaticos Agregados no Periodo")
        c_ind1, c_ind2, c_ind3, c_ind4 = st.columns(4)
        c_ind1.metric(
            "Temperatura (Media / Extremos)",
            f"{resumo['temp_media']:.1f} C",
            f"Min {resumo['temp_min']:.1f} C | Max {resumo['temp_max']:.1f} C",
            delta_color="off",
        )
        c_ind2.metric(
            "Amplitude e Tendencia Termica",
            f"{resumo['amplitude_termica']:.1f} C",
            f"Taxa: {resumo['taxa_termica_h']:+.2f} C/h | DP: {resumo['temp_std']:.2f} C",
            delta_color="off",
        )
        c_ind3.metric(
            "Indice de Thom / ITU Atual",
            f"{resumo['thom_atual']:.1f} C (DI)",
            f"ITU: {resumo['itu_atual']:.1f} | {resumo['classe_thom_atual']}",
            delta_color="off",
        )
        c_ind4.metric(
            "Tempo em Conforto Ideal (Verde)",
            f"{resumo['pct_verde']:.1f} %",
            f"Amarelo: {resumo['pct_amarelo']:.1f}% | Vermelho: {resumo['pct_vermelho']:.1f}%",
            delta_color="off",
        )

        st.plotly_chart(grafico_indicadores_derivados(df_filtrado), use_container_width=True, key="fig_derivados")

        col_disp, col_corr = st.columns(2)
        with col_disp:
            st.plotly_chart(grafico_dispersao_psicrometrica(df_filtrado), use_container_width=True, key="fig_psic")
        with col_corr:
            st.plotly_chart(grafico_matriz_correlacao(df_filtrado), use_container_width=True, key="fig_corr")

        col_vio, col_rad = st.columns([3, 2])
        with col_vio:
            st.plotly_chart(grafico_distribuicao_violin(df_filtrado), use_container_width=True, key="fig_violin")
        with col_rad:
            st.plotly_chart(
                grafico_radar_scorecard(resumo["scorecard"], df_filtrado),
                use_container_width=True,
                key="fig_radar",
            )

        st.subheader("Comparativo Estatistico por Periodo (Dia vs. Noite)")
        resumo_periodo = (
            df_filtrado.groupby("periodo")[
                [
                    "temperatura",
                    "umidade",
                    "luminosidade",
                    "ponto_orvalho",
                    "bulbo_umido",
                    "sensacao_termica",
                    "indice_thom",
                    "vpd_kpa",
                    "entalpia_kj_kg",
                ]
            ]
            .agg(["mean", "min", "max", "std"])
            .round(2)
        )
        st.dataframe(resumo_periodo, use_container_width=True)

    # === ABA 3: DESEMPENHO DA PREVISAO (ESP32) ===
    with aba_previsao:
        st.subheader("Avaliacao da Regressao Linear Embarcada (Previsao de 10 Minutos)")
        st.markdown(
            "O ESP32 mantem uma janela deslizante de 20 leituras (`N_JANELA = 20`, a cada 30 s) e projeta "
            "a temperatura 20 passos a frente (`PASSOS_FRENTE = 20`, equivalente a 10 minutos). "
            "Abaixo, cada previsao transmitida no `field5` e comparada com a temperatura real observada 10 minutos depois."
        )

        if comparacao_prev.empty:
            st.info(
                "Ainda nao ha pares suficientes (previsao + leitura real 10 minutos apos) no intervalo filtrado "
                "para calcular as metricas de erro."
            )
        else:
            cp1, cp2, cp3, cp4, cp5, cp6 = st.columns(6)
            cp1.metric("Pares Avaliados", f"{metricas_prev['n_comparacoes']}")
            cp2.metric("MAE (Erro Medio Abs.)", f"{metricas_prev['mae']:.2f} C")
            cp3.metric("RMSE", f"{metricas_prev['rmse']:.2f} C")
            cp4.metric("MAPE (%)", f"{metricas_prev['mape']:.2f} %")
            cp5.metric("Vies Medio (Bias)", f"{metricas_prev['vies']:+.2f} C")
            cp6.metric("Acuracia Direcional", f"{metricas_prev['acuracia_direcional']:.1f} %")

            fig_comp, fig_erro, fig_paridade = grafico_avaliacao_previsao(comparacao_prev)
            st.plotly_chart(fig_comp, use_container_width=True, key="fig_prev_comp")

            col_p1, col_p2 = st.columns(2)
            with col_p1:
                st.plotly_chart(fig_erro, use_container_width=True, key="fig_prev_erro")
            with col_p2:
                st.plotly_chart(fig_paridade, use_container_width=True, key="fig_prev_par")

    # === ABA 4: EXPLORACAO, QUALIDADE DE DADOS E TELEMETRIA IOT ===
    with aba_exploracao:
        st.subheader("Pipeline de Limpeza, Qualidade dos Dados e Telemetria IoT")
        q1, q2, q3, q4, q5, q6 = st.columns(6)
        q1.metric("Registros Brutos", stats_limpeza["linhas_brutas"])
        q2.metric("Outliers / Invalidos", stats_limpeza["linhas_outliers"] + stats_limpeza["linhas_sem_sensores"])
        q3.metric("Registros Validos", stats_limpeza["linhas_validas"])
        q4.metric("Intervalo Mediano", f"{resumo['intervalo_mediano_seg']:.0f} s")
        q5.metric("Gaps de Transmissao (>75s)", resumo["quedas_conexao"])
        q6.metric("Eficiencia Recebimento", f"{resumo['eficiencia_iot']:.1f} %")

        col_est1, col_est2 = st.columns([2, 1])
        with col_est1:
            st.markdown("**Estatisticas Descritivas das Variaveis Numericas**")
            colunas_desc = [
                "temperatura",
                "umidade",
                "luminosidade",
                "previsao_10min",
                "ponto_orvalho",
                "spread_orvalho",
                "bulbo_umido",
                "sensacao_termica",
                "indice_thom",
                "itu",
                "vpd_kpa",
                "umidade_absoluta",
                "entalpia_kj_kg",
                "intervalo_seg",
            ]
            st.dataframe(df_filtrado[colunas_desc].describe().T.round(2), use_container_width=True)

        with col_est2:
            st.markdown("**Contagem de Valores Ausentes (Dataset Filtrado)**")
            ausentes = df_filtrado[colunas_desc].isna().sum().reset_index()
            ausentes.columns = ["Coluna", "Valores Ausentes (NaN)"]
            ausentes["Percentual (%)"] = ((ausentes["Valores Ausentes (NaN)"] / len(df_filtrado)) * 100).round(1)
            st.dataframe(ausentes, use_container_width=True, hide_index=True)

        st.markdown("**Tabela de Dados Processados e Enriquecidos**")
        st.dataframe(
            df_filtrado.sort_values("timestamp", ascending=False),
            use_container_width=True,
            height=360,
        )

        csv_bytes = df_filtrado.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Baixar Dados Processados (CSV)",
            data=csv_bytes,
            file_name="dados_estacao_esp32_processados.csv",
            mime="text/csv",
        )


def main():
    st.set_page_config(
        page_title="Dashboard Estacao Meteorologica ESP32",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("Estacao de Monitoramento Ambiental - ESP32")
    st.caption(
        "Aplicacao em Python, Pandas, Plotly e Streamlit para coleta em tempo real, "
        "calculo de indicadores meteorologicos/bioclimaticos e visualizacao das medicoes do ESP32."
    )

    with st.sidebar:
        atualizar_tempo_real = st.toggle("Atualizacao automatica em tempo real (2s)", value=True)
        max_reg_serial = st.slider(
            "Janela de leituras exibidas:",
            min_value=20,
            max_value=5000,
            value=500,
            step=20,
        )

    painel_fragment = st.fragment(run_every=2.0 if atualizar_tempo_real else None)(renderizar_painel)
    painel_fragment(max_reg_serial)


if __name__ == "__main__":
    main()
