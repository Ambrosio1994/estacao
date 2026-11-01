from datetime import datetime
from typing import Dict, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from coleta import LIMITE_DIA

# =============================================================================
# STYLE SETTINGS: edit this block to restyle every chart
# =============================================================================

# Palette: "Lupi" from the MoMAColors R package (MIT), https://github.com/BlakeRMills/MoMAColors
chart_colours = ['#2E92A2', '#FFB651', '#AA3F5D', '#98A54F', '#D85A44', '#61BEA4', '#DAA5AC']

# Semantic colours: use when colour carries meaning (gain/loss, target, etc.)
COLOUR_POSITIVE = '#2E92A2'
COLOUR_NEGATIVE = '#D85A44'
COLOUR_TOTAL = '#8C8C8C'      # cinza medio: legivel nos temas claro e escuro
COLOUR_WARNING = '#FFB651'
COLOUR_HIGHLIGHT = '#AA3F5D'
COLOUR_MUTED = '#A0A0A0'      # de-emphasised series, connectors, reference lines

# Grid, axis and background: neutral/transparent so the Streamlit theme (light or dark)
# supplies text colour and page background. Text colour is intentionally left unset.
COLOUR_GRID = 'rgba(128, 128, 128, 0.25)'
COLOUR_AXIS_LINE = 'rgba(128, 128, 128, 0.6)'
COLOUR_BACKGROUND = 'rgba(0, 0, 0, 0)'
FONT_FAMILY = "Helvetica Neue, Helvetica, Arial, sans-serif"

# Continuous colour scales (heatmaps)
SEQUENTIAL_SCALE = [[0.0, '#E9F7F4'], [0.5, '#61BEA4'], [1.0, '#1B5E6B']]
DIVERGING_SCALE = [[0.0, '#D85A44'], [0.5, '#F7F7F7'], [1.0, '#2E92A2']]

# Default chart size (pixels) and font sizes
CHART_WIDTH = 700
CHART_HEIGHT = 420
TITLE_SIZE = 20
BODY_SIZE = 13

CORES_ESTADOS = {
    "Ideal (Verde)": COLOUR_POSITIVE,
    "Atencao (Amarelo)": COLOUR_WARNING,
    "Critico (Vermelho)": COLOUR_NEGATIVE,
}

# =============================================================================
# Helpers: do not edit below this line
# =============================================================================

def series_colours(n):
    """Return n colours from chart_colours, cycling if n exceeds the palette length."""
    return [chart_colours[i % len(chart_colours)] for i in range(n)]

def hex_to_rgba(hex_colour, alpha):
    """Convert '#RRGGBB' to an 'rgba(r, g, b, alpha)' string for translucent fills."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"

def get_layout(**kwargs):
    """Return a styled Plotly layout. Pass any go.Layout kwargs to override defaults."""
    defaults = dict(
        colorway=chart_colours,
        autosize=True,
        height=CHART_HEIGHT,
        font=dict(family=FONT_FAMILY, size=BODY_SIZE),
        title=dict(x=0.5, font={"size": TITLE_SIZE}),
        xaxis=dict(
            showgrid=False,
            gridcolor=COLOUR_GRID,
            showline=True,
            linecolor=COLOUR_AXIS_LINE,
            automargin=True,
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor=COLOUR_GRID,
            showline=True,
            linecolor=COLOUR_AXIS_LINE,
            automargin=True,
        ),
        legend=dict(x=1, y=1, bgcolor=COLOUR_BACKGROUND),
        margin=dict(l=50, r=50, b=50, t=60, pad=1),
        showlegend=True,
        paper_bgcolor=COLOUR_BACKGROUND,
        plot_bgcolor=COLOUR_BACKGROUND,
        uirevision="estacao_esp32",
        hoverlabel=dict(font=dict(family=FONT_FAMILY, size=12)),
    )
    layout = go.Layout(**defaults)
    layout.update(**kwargs)
    return layout

def add_created_at_annotation(fig, *, offset_px=65, bottom_margin_px=100):
    """Add a 'Chart created: YYYY-MM-DD' annotation to the bottom-right of the chart. Adjust spacing for dense labels."""
    chart_created_at_str = datetime.now().strftime("%Y-%m-%d")
    fig.update_layout(margin=dict(b=max(fig.layout.margin.b or 0, bottom_margin_px)))
    fig.add_annotation(
        text=f"Chart created: {chart_created_at_str}",
        xref="paper", yref="paper",
        x=1, y=0,
        yshift=-offset_px,
        xanchor="right", yanchor="top",
        showarrow=False,
        font=dict(size=12, color=COLOUR_MUTED),
    )


def add_estatisticas_periodo(fig, df, coluna, unidade):
    """Marca maxima e minima no ponto em que ocorreram e traca a media do periodo."""
    serie = df[coluna].dropna()
    if serie.empty:
        return
    idx_max, idx_min = serie.idxmax(), serie.idxmin()
    for idx, rotulo, cor, ay in ((idx_max, "Max", COLOUR_NEGATIVE, -28), (idx_min, "Min", COLOUR_POSITIVE, 28)):
        fig.add_annotation(
            x=df.loc[idx, "timestamp"],
            y=df.loc[idx, coluna],
            text=f"{rotulo}: {df.loc[idx, coluna]:.1f}{unidade}",
            showarrow=True,
            arrowhead=2,
            ax=0,
            ay=ay,
            font=dict(size=11, color=cor),
            bgcolor=COLOUR_BACKGROUND,
        )
    fig.add_hline(
        y=serie.mean(),
        line_dash="dash",
        line_width=1.4,
        line_color=COLOUR_TOTAL,
        annotation_text=f"Media: {serie.mean():.1f}{unidade}",
        annotation_position="bottom right",
    )


# ==============================================================================
# 4. VISUALIZACOES INTERATIVAS (PLOTLY - PADRAO DE DESIGN)
# ==============================================================================

def grafico_medidores_atuais(resumo: Dict) -> go.Figure:
    """
    Painel de medidores (Gauges/Bullet Indicators) com faixas de conforto
    dinamicas (Dia/Noite) para Temperatura, Umidade, Indice de Thom e VPD.
    """
    dia = resumo["periodo_atual"] == "Dia"
    min_v, max_v = (20.0, 26.0) if dia else (18.0, 24.0)
    min_a, max_a = (18.0, 28.0) if dia else (16.0, 26.0)

    fig = make_subplots(
        rows=1,
        cols=4,
        specs=[[{"type": "indicator"}] * 4],
        horizontal_spacing=0.06,
    )

    # 1. Medidor de Temperatura
    fig.add_trace(
        go.Indicator(
            mode="gauge+number+delta",
            value=resumo["temp_atual"],
            delta=dict(
                reference=resumo["temp_atual"] - resumo["temp_delta"],
                valueformat=".1f",
                increasing=dict(color=COLOUR_NEGATIVE),
                decreasing=dict(color=COLOUR_POSITIVE),
            ),
            number=dict(suffix=" C", valueformat=".1f", font=dict(size=26, family=FONT_FAMILY)),
            title=dict(
                text=f"Temperatura ({resumo['periodo_atual']})",
                font=dict(size=14, family=FONT_FAMILY),
            ),
            gauge=dict(
                axis=dict(
                    range=[10, 38],
                    tickwidth=1,
                    tickcolor=COLOUR_AXIS_LINE,
                ),
                bar=dict(color=COLOUR_TOTAL, thickness=0.3),
                bgcolor=COLOUR_BACKGROUND,
                borderwidth=1,
                bordercolor=COLOUR_GRID,
                steps=[
                    dict(range=[10, min_a], color=hex_to_rgba(COLOUR_NEGATIVE, 0.28)),
                    dict(range=[min_a, min_v], color=hex_to_rgba(COLOUR_WARNING, 0.35)),
                    dict(range=[min_v, max_v], color=hex_to_rgba(COLOUR_POSITIVE, 0.35)),
                    dict(range=[max_v, max_a], color=hex_to_rgba(COLOUR_WARNING, 0.35)),
                    dict(range=[max_a, 38], color=hex_to_rgba(COLOUR_NEGATIVE, 0.28)),
                ],
                threshold=dict(
                    line=dict(color=COLOUR_HIGHLIGHT, width=3),
                    thickness=0.8,
                    value=resumo["prev_10min_atual"] if pd.notna(resumo["prev_10min_atual"]) else resumo["temp_atual"],
                ),
            ),
        ),
        row=1,
        col=1,
    )

    # 2. Medidor de Umidade Relativa
    fig.add_trace(
        go.Indicator(
            mode="gauge+number+delta",
            value=resumo["umid_atual"],
            delta=dict(
                reference=resumo["umid_atual"] - resumo["umid_delta"],
                valueformat=".1f",
                increasing=dict(color=COLOUR_POSITIVE),
                decreasing=dict(color=COLOUR_WARNING),
            ),
            number=dict(suffix=" %", valueformat=".1f", font=dict(size=26, family=FONT_FAMILY)),
            title=dict(
                text="Umidade Relativa (Meta 40-60%)",
                font=dict(size=14, family=FONT_FAMILY),
            ),
            gauge=dict(
                axis=dict(
                    range=[0, 100],
                    tickwidth=1,
                    tickcolor=COLOUR_AXIS_LINE,
                ),
                bar=dict(color=COLOUR_TOTAL, thickness=0.3),
                bgcolor=COLOUR_BACKGROUND,
                borderwidth=1,
                bordercolor=COLOUR_GRID,
                steps=[
                    dict(range=[0, 40], color=hex_to_rgba(COLOUR_WARNING, 0.35)),
                    dict(range=[40, 60], color=hex_to_rgba(COLOUR_POSITIVE, 0.35)),
                    dict(range=[60, 100], color=hex_to_rgba(COLOUR_WARNING, 0.35)),
                ],
            ),
        ),
        row=1,
        col=2,
    )

    # 3. Medidor de Desconforto de Thom (DI)
    fig.add_trace(
        go.Indicator(
            mode="gauge+number",
            value=resumo["thom_atual"],
            number=dict(suffix=" C", valueformat=".1f", font=dict(size=26, family=FONT_FAMILY)),
            title=dict(
                text="Indice de Thom (Conforto <21)",
                font=dict(size=14, family=FONT_FAMILY),
            ),
            gauge=dict(
                axis=dict(
                    range=[14, 32],
                    tickwidth=1,
                    tickcolor=COLOUR_AXIS_LINE,
                ),
                bar=dict(color=COLOUR_TOTAL, thickness=0.3),
                bgcolor=COLOUR_BACKGROUND,
                borderwidth=1,
                bordercolor=COLOUR_GRID,
                steps=[
                    dict(range=[14, 21], color=hex_to_rgba(COLOUR_POSITIVE, 0.35)),
                    dict(range=[21, 24], color=hex_to_rgba(chart_colours[5], 0.35)),
                    dict(range=[24, 27], color=hex_to_rgba(COLOUR_WARNING, 0.35)),
                    dict(range=[27, 32], color=hex_to_rgba(COLOUR_NEGATIVE, 0.35)),
                ],
            ),
        ),
        row=1,
        col=3,
    )

    # 4. Medidor de Indice Global de Qualidade Ambiental
    score_global = resumo["scorecard"].get("Indice Global (0-100)", 0.0)
    fig.add_trace(
        go.Indicator(
            mode="gauge+number",
            value=score_global,
            number=dict(suffix="/100", valueformat=".1f", font=dict(size=26, family=FONT_FAMILY)),
            title=dict(
                text="Score Ambiental Global",
                font=dict(size=14, family=FONT_FAMILY),
            ),
            gauge=dict(
                axis=dict(
                    range=[0, 100],
                    tickwidth=1,
                    tickcolor=COLOUR_AXIS_LINE,
                ),
                bar=dict(color=COLOUR_TOTAL, thickness=0.3),
                bgcolor=COLOUR_BACKGROUND,
                borderwidth=1,
                bordercolor=COLOUR_GRID,
                steps=[
                    dict(range=[0, 50], color=hex_to_rgba(COLOUR_NEGATIVE, 0.35)),
                    dict(range=[50, 75], color=hex_to_rgba(COLOUR_WARNING, 0.35)),
                    dict(range=[75, 100], color=hex_to_rgba(COLOUR_POSITIVE, 0.35)),
                ],
            ),
        ),
        row=1,
        col=4,
    )

    fig.update_layout(
        get_layout(
            uirevision="gauges_conforto_esp32",
            height=270,
            margin=dict(l=30, r=30, t=45, b=85),
            showlegend=False,
        )
    )
    add_created_at_annotation(fig, offset_px=35, bottom_margin_px=75)
    return fig


def grafico_evolucao_temperatura(df: pd.DataFrame) -> go.Figure:
    df = df.sort_values("timestamp")

    fig = go.Figure(
        layout=get_layout(
            title=dict(text="Evolucao Termica, Banda de Estabilidade, Bulbo Umido e Projecao (+10 min)"),
            xaxis_title="Data e Hora",
            yaxis_title="Temperatura (C)",
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            height=470,
            margin=dict(t=85),
        )
    )

    # Faixa de conforto diurno de referencia (20 a 26 C)
    fig.add_hrect(
        y0=20.0,
        y1=26.0,
        fillcolor=hex_to_rgba(COLOUR_POSITIVE, 0.10),
        line_width=0,
        annotation_text="Faixa Ideal Dia (20-26 C)",
        annotation_position="top left",
    )

    # Banda de estabilidade (Media Movel +/- 1.5 desvio padrao)
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["temp_banda_sup"],
            mode="lines",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["temp_banda_inf"],
            mode="lines",
            line=dict(width=0),
            fill="tonexty",
            fillcolor=hex_to_rgba(chart_colours[0], 0.14),
            name="Banda Estabilidade (+/-1.5 DP)",
            hovertemplate="Banda Inf: %{y:.1f} C<extra></extra>",
        )
    )

    # Temperatura medida
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["temperatura"],
            mode="lines",
            name="Temperatura Medida",
            line=dict(color=chart_colours[0], width=2.4),
            hovertemplate="<b>Temperatura Medida</b><br>Valor: %{y:.1f} C<extra></extra>",
        )
    )

    # Media movel
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["temp_media_movel"],
            mode="lines",
            name="Media Movel",
            line=dict(color=COLOUR_TOTAL, width=1.6, dash="dot"),
            hovertemplate="<b>Media Movel</b><br>Valor: %{y:.1f} C<extra></extra>",
        )
    )

    # Sensacao termica
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["sensacao_termica"],
            mode="lines",
            name="Sensacao Termica",
            line=dict(color=chart_colours[4], width=1.8),
            hovertemplate="<b>Sensacao Termica</b><br>Valor: %{y:.1f} C<extra></extra>",
        )
    )

    # Bulbo Umido (Stull)
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["bulbo_umido"],
            mode="lines",
            name="Bulbo Umido (Tw)",
            line=dict(color=chart_colours[3], width=1.6, dash="dash"),
            hovertemplate="<b>Bulbo Umido</b><br>Valor: %{y:.1f} C<extra></extra>",
        )
    )

    # Ponto de Orvalho
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["ponto_orvalho"],
            mode="lines",
            name="Ponto de Orvalho",
            line=dict(color=COLOUR_MUTED, width=1.5, dash="dot"),
            hovertemplate="<b>Ponto de Orvalho</b><br>Valor: %{y:.1f} C<extra></extra>",
        )
    )

    # Previsao ESP32 e projecao futura (+10 min a frente do ultimo ponto)
    if df["previsao_10min"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["timestamp"],
                y=df["previsao_10min"],
                mode="lines",
                name="Previsao ESP32 (no instante t)",
                line=dict(color=chart_colours[2], width=1.8, dash="dashdot"),
                hovertemplate="<b>Previsao ESP32 (+10m)</b><br>Valor: %{y:.1f} C<extra></extra>",
            )
        )

        ultimo = df.iloc[-1]
        if pd.notna(ultimo["previsao_10min"]):
            t_futuro = ultimo["timestamp"] + pd.Timedelta(minutes=10)
            fig.add_trace(
                go.Scatter(
                    x=[ultimo["timestamp"], t_futuro],
                    y=[ultimo["temperatura"], ultimo["previsao_10min"]],
                    mode="lines+markers",
                    name="Trajetoria Projetada (+10 min)",
                    line=dict(color=COLOUR_HIGHLIGHT, width=2.5, dash="dash"),
                    marker=dict(size=[7, 11], color=COLOUR_HIGHLIGHT),
                    hovertemplate="<b>Projecao Futura</b><br>Horario: %{x}<br>Temp: %{y:.1f} C<extra></extra>",
                )
            )

    add_estatisticas_periodo(fig, df, "temperatura", " C")

    add_created_at_annotation(fig)
    return fig


def grafico_umidade(df: pd.DataFrame) -> go.Figure:
    df = df.sort_values("timestamp")

    fig = go.Figure(
        layout=get_layout(
            title=dict(text="Umidade Relativa do Ar (%) e Media Movel"),
            xaxis_title="Data e Hora",
            yaxis_title="Umidade (%)",
            hovermode="x unified",
            height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=85),
        )
    )

    fig.add_hrect(
        y0=40.0,
        y1=60.0,
        fillcolor=hex_to_rgba(COLOUR_POSITIVE, 0.12),
        line_width=0,
        annotation_text="Faixa Ideal Umidade (40-60%)",
        annotation_position="top left",
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["umidade"],
            mode="lines",
            name="Umidade Relativa (%)",
            line=dict(color=chart_colours[0], width=2.2),
            hovertemplate="<b>Umidade Relativa</b><br>Valor: %{y:.1f}%<extra></extra>",
        )
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["umid_media_movel"],
            mode="lines",
            name="Media Movel Umidade",
            line=dict(color=COLOUR_TOTAL, width=1.6, dash="dot"),
            hovertemplate="<b>Media Movel</b><br>Valor: %{y:.1f}%<extra></extra>",
        )
    )

    add_estatisticas_periodo(fig, df, "umidade", "%")
    fig.update_yaxes(range=[0, 105])
    add_created_at_annotation(fig)
    return fig


def grafico_luminosidade(df: pd.DataFrame) -> go.Figure:
    df = df.sort_values("timestamp")

    fig = go.Figure(
        layout=get_layout(
            title=dict(text="Luminosidade Ambiente (LDR, %) e Limite Dia/Noite"),
            xaxis_title="Data e Hora",
            yaxis_title="Luz (%)",
            hovermode="x unified",
            height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=85),
        )
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["luminosidade"],
            mode="lines",
            fill="tozeroy",
            name="Luminosidade (%)",
            line=dict(color=chart_colours[3], width=2.0),
            fillcolor=hex_to_rgba(chart_colours[1], 0.28),
            hovertemplate="<b>Luminosidade</b><br>Valor: %{y:.1f}%<extra></extra>",
        )
    )

    fig.add_hline(
        y=LIMITE_DIA,
        line_dash="dash",
        line_color=COLOUR_HIGHLIGHT,
        annotation_text="Limiar Dia/Noite (40%)",
        annotation_position="top left",
    )

    add_estatisticas_periodo(fig, df, "luminosidade", "%")
    fig.update_yaxes(range=[0, 105])
    add_created_at_annotation(fig)
    return fig


def grafico_semaforo_distribuicao(df: pd.DataFrame) -> Tuple[go.Figure, go.Figure]:
    ordem_estados = ["Ideal (Verde)", "Atencao (Amarelo)", "Critico (Vermelho)"]
    contagem = (
        df["status_conforto"]
        .value_counts()
        .reindex(ordem_estados, fill_value=0)
        .reset_index()
    )
    contagem.columns = ["status_conforto", "quantidade"]

    fig_pizza = go.Figure(
        layout=get_layout(
            title=dict(text="Proporcao por Estado do Semaforo"),
            height=400,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=80),
        )
    )
    fig_pizza.add_trace(
        go.Pie(
            labels=contagem["status_conforto"],
            values=contagem["quantidade"],
            hole=0.45,
            marker=dict(colors=[CORES_ESTADOS[s] for s in contagem["status_conforto"]]),
            texttemplate="%{label}<br>%{percent:.1%}",
            hovertemplate="%{label}: %{value:.0f} leituras (%{percent:.1%})<extra></extra>",
        )
    )
    add_created_at_annotation(fig_pizza)

    df_sorted = df.sort_values(["status_conforto", "timestamp"])
    fig_linha_tempo = go.Figure(
        layout=get_layout(
            title=dict(text="Historico do Estado do Semaforo ao Longo do Tempo"),
            xaxis_title="Data e Hora",
            yaxis_title="Temperatura (C)",
            height=400,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=80),
        )
    )

    for estado in ordem_estados:
        sub = df_sorted[df_sorted["status_conforto"] == estado]
        if sub.empty:
            continue
        fig_linha_tempo.add_trace(
            go.Scatter(
                x=sub["timestamp"],
                y=sub["temperatura"],
                mode="markers",
                name=estado,
                marker=dict(
                    color=CORES_ESTADOS[estado],
                    size=8,
                    opacity=0.85,
                    symbol=np.where(sub["periodo"] == "Dia", "circle", "diamond"),
                ),
                customdata=np.stack([sub["umidade"], sub["luminosidade"], sub["motivo_alerta"]], axis=-1),
                hovertemplate=(
                    "<b>" + estado + "</b><br>"
                    "Horario: %{x}<br>"
                    "Temperatura: %{y:.1f} C<br>"
                    "Umidade: %{customdata[0]:.1f}%<br>"
                    "Luminosidade: %{customdata[1]:.1f}%<br>"
                    "Diagnostico: %{customdata[2]}<extra></extra>"
                ),
            )
        )

    fig_linha_tempo.update_xaxes(showgrid=True)
    add_created_at_annotation(fig_linha_tempo)
    return fig_pizza, fig_linha_tempo


def grafico_diagnostico_alertas(df: pd.DataFrame) -> go.Figure:
    """
    Barra horizontal ranqueada das causas de penalidade/desconforto registradas pela estacao.
    """
    contagem = (
        df["motivo_alerta"]
        .value_counts()
        .reset_index()
    )
    contagem.columns = ["motivo", "quantidade"]
    contagem["percentual"] = (contagem["quantidade"] / max(len(df), 1)) * 100.0
    contagem = contagem.sort_values("percentual", ascending=True)

    cores_barras = [
        COLOUR_POSITIVE if m == "Conforto Pleno" else (COLOUR_NEGATIVE if "Extremo" in m or "+" in m else COLOUR_WARNING)
        for m in contagem["motivo"]
    ]

    fig = go.Figure(
        layout=get_layout(
            title=dict(text="Diagnostico de Causas do Semaforo (% do Tempo)"),
            showlegend=False,
            height=380,
            margin=dict(l=60, r=60, t=65, b=95),
        )
    )

    fig.add_trace(
        go.Bar(
            x=contagem["percentual"],
            y=contagem["motivo"],
            orientation="h",
            text=contagem["percentual"],
            textposition="outside",
            texttemplate="%{x:.1f}%",
            cliponaxis=False,
            marker_color=cores_barras,
            customdata=contagem["quantidade"],
            hovertemplate="<b>%{y}</b><br>Proporcao: %{x:.1f}%<br>Leituras: %{customdata:.0f}<extra></extra>",
        )
    )

    fig.update_layout(xaxis_title="Proporcao das Leituras (%)", yaxis_title="")
    fig.update_xaxes(showgrid=True, range=[0, max(contagem["percentual"].max() * 1.18, 10.0)])
    add_created_at_annotation(fig)
    return fig


def grafico_indicadores_derivados(df: pd.DataFrame) -> go.Figure:
    df = df.sort_values("timestamp")

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.update_layout(
        get_layout(
            title=dict(text="Evolucao Psicrometrica: VPD (kPa), Umidade Absoluta (g/m3) e Entalpia"),
            xaxis_title="Data e Hora",
            hovermode="x unified",
            height=430,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=80),
        )
    )

    # Faixa ideal de VPD (0.4 a 1.2 kPa)
    fig.add_hrect(
        y0=0.4,
        y1=1.2,
        fillcolor=hex_to_rgba(COLOUR_POSITIVE, 0.10),
        line_width=0,
        annotation_text="Faixa VPD Equilibrada (0.4-1.2 kPa)",
        annotation_position="top left",
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["vpd_kpa"],
            mode="lines",
            name="VPD (kPa)",
            line=dict(color=chart_colours[4], width=2.2),
            hovertemplate="<b>VPD</b><br>Valor: %{y:.2f} kPa<extra></extra>",
        ),
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["umidade_absoluta"],
            mode="lines",
            name="Umidade Absoluta (g/m3)",
            line=dict(color=chart_colours[0], width=2.0, dash="dot"),
            hovertemplate="<b>Umidade Absoluta</b><br>Valor: %{y:.1f} g/m3<extra></extra>",
        ),
        secondary_y=True,
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["spread_orvalho"],
            mode="lines",
            name="Margem Ate Orvalho (T - Td em C)",
            line=dict(color=chart_colours[2], width=1.6, dash="dash"),
            hovertemplate="<b>Margem Anti-Condensacao</b><br>Spread: %{y:.1f} C<extra></extra>",
        ),
        secondary_y=True,
    )

    fig.update_yaxes(
        title_text="VPD (kPa)",
        showgrid=True,
        gridcolor=COLOUR_GRID,
        showline=True,
        linecolor=COLOUR_AXIS_LINE,
        secondary_y=False,
    )
    fig.update_yaxes(
        title_text="Umid. Absoluta (g/m3) / Spread (C)",
        showgrid=False,
        showline=True,
        linecolor=COLOUR_AXIS_LINE,
        secondary_y=True,
    )
    add_created_at_annotation(fig)
    return fig


def grafico_dispersao_psicrometrica(df: pd.DataFrame) -> go.Figure:
    ordem_estados = ["Ideal (Verde)", "Atencao (Amarelo)", "Critico (Vermelho)"]
    fig = go.Figure(
        layout=get_layout(
            title=dict(text="Carta Psicrometrica Simplificada (Temperatura vs. Umidade)"),
            xaxis_title="Temperatura (C)",
            yaxis_title="Umidade Relativa (%)",
            height=440,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=80),
        )
    )

    # Zona ideal diurna (20-26 C e 40-60% UR) e noturna (18-24 C e 40-60% UR)
    fig.add_shape(
        type="rect",
        x0=20.0,
        x1=26.0,
        y0=40.0,
        y1=60.0,
        line=dict(color=COLOUR_POSITIVE, width=1.8, dash="dash"),
        fillcolor=hex_to_rgba(COLOUR_POSITIVE, 0.10),
    )
    fig.add_shape(
        type="rect",
        x0=18.0,
        x1=24.0,
        y0=40.0,
        y1=60.0,
        line=dict(color=chart_colours[3], width=1.4, dash="dot"),
        fillcolor=hex_to_rgba(chart_colours[3], 0.05),
    )

    max_luz = max(float(df["luminosidade"].max()), 1.0)
    for estado in ordem_estados:
        sub = df[df["status_conforto"] == estado]
        if sub.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=sub["temperatura"],
                y=sub["umidade"],
                mode="markers",
                name=estado,
                marker=dict(
                    size=np.clip(sub["luminosidade"], 15.0, 100.0),
                    sizemode="area",
                    sizeref=2.0 * max_luz / (16.0 ** 2),
                    color=CORES_ESTADOS[estado],
                    opacity=0.72,
                    line=dict(width=1, color="white"),
                ),
                customdata=np.stack(
                    [sub["ponto_orvalho"], sub["sensacao_termica"], sub["vpd_kpa"], sub["entalpia_kj_kg"]],
                    axis=-1,
                ),
                hovertemplate=(
                    "<b>" + estado + "</b><br>"
                    "Temp: %{x:.1f} C | Umidade: %{y:.1f}%<br>"
                    "Orvalho: %{customdata[0]:.1f} C | Sensacao: %{customdata[1]:.1f} C<br>"
                    "VPD: %{customdata[2]:.2f} kPa | Entalpia: %{customdata[3]:.1f} kJ/kg<extra></extra>"
                ),
            )
        )

    fig.update_xaxes(showgrid=True)
    add_created_at_annotation(fig)
    return fig


def grafico_matriz_correlacao(df: pd.DataFrame) -> go.Figure:
    colunas = [
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
    nomes_amigaveis = {
        "temperatura": "Temperatura",
        "umidade": "Umidade Rel.",
        "luminosidade": "Luminosidade",
        "ponto_orvalho": "Ponto Orvalho",
        "bulbo_umido": "Bulbo Umido",
        "sensacao_termica": "Sens. Termica",
        "indice_thom": "Indice Thom",
        "vpd_kpa": "VPD (kPa)",
        "entalpia_kj_kg": "Entalpia",
    }
    corr = df[colunas].rename(columns=nomes_amigaveis).corr()

    fig = go.Figure(
        layout=get_layout(
            title=dict(text="Matriz de Correlacao de Pearson"),
            height=440,
            margin=dict(t=70, b=95),
        )
    )
    fig.add_trace(
        go.Heatmap(
            z=corr.values,
            x=corr.columns,
            y=corr.index,
            colorscale=DIVERGING_SCALE,
            zmid=0,
            zmin=-1,
            zmax=1,
            text=np.round(corr.values, 2),
            texttemplate="%{text:.2f}",
            textfont={"size": 10},
            hoverongaps=False,
            hovertemplate="Var X: %{x}<br>Var Y: %{y}<br>Correlacao (r): %{z:.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        xaxis=dict(tickangle=-25, side="bottom"),
        yaxis=dict(autorange="reversed"),
    )
    add_created_at_annotation(fig)
    return fig


def grafico_distribuicao_violin(df: pd.DataFrame) -> go.Figure:
    """
    Comparacao da distribuicao de Temperatura e Indice de Thom por Periodo (Dia vs. Noite)
    usando Violin Plot com Boxplot interno e linha de media.
    """
    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("Distribuicao de Temperatura (C)", "Distribuicao de Umidade Relativa (%)"),
        horizontal_spacing=0.10,
    )
    fig.update_layout(
        get_layout(
            title=dict(text="Perfil de Distribuicao e Quartis por Periodo (Dia vs. Noite)"),
            height=410,
            legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="right", x=1),
            margin=dict(t=85),
        )
    )

    periodos = [p for p in ["Dia", "Noite"] if p in df["periodo"].unique()]
    cores_p = {"Dia": chart_colours[0], "Noite": chart_colours[2]}

    for per in periodos:
        sub = df[df["periodo"] == per]
        fig.add_trace(
            go.Violin(
                y=sub["temperatura"],
                name=f"{per} (Temp)",
                legendgroup=per,
                marker_color=cores_p.get(per, chart_colours[0]),
                box_visible=True,
                meanline_visible=True,
                hovertemplate="<b>" + per + "</b><br>Temperatura: %{y:.1f} C<extra></extra>",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Violin(
                y=sub["umidade"],
                name=f"{per} (Umid)",
                legendgroup=per,
                showlegend=False,
                marker_color=cores_p.get(per, chart_colours[0]),
                box_visible=True,
                meanline_visible=True,
                hovertemplate="<b>" + per + "</b><br>Umidade: %{y:.1f}%<extra></extra>",
            ),
            row=1,
            col=2,
        )

    fig.update_yaxes(title_text="Temperatura (C)", showgrid=True, gridcolor=COLOUR_GRID, row=1, col=1)
    fig.update_yaxes(title_text="Umidade (%)", showgrid=True, gridcolor=COLOUR_GRID, row=1, col=2)
    add_created_at_annotation(fig)
    return fig


def grafico_radar_scorecard(scorecard_geral: Dict[str, float], df: pd.DataFrame) -> go.Figure:
    """
    Radar / Spider Chart comparando as 5 dimensoes de qualidade ambiental (Dia vs. Noite ou Geral vs. Meta).
    """
    dimensoes = [
        "Conforto Termico",
        "Equilibrio Hidrico",
        "Estabilidade Termica",
        "Seguranca Condensacao",
        "Regularidade IoT",
    ]

    fig = go.Figure(
        layout=get_layout(
            title=dict(text="Scorecard Ambiental Multidimensional (0 a 100)"),
            height=410,
            legend=dict(orientation="h", yanchor="bottom", y=1.04, xanchor="right", x=1),
            polar=dict(
                radialaxis=dict(visible=True, range=[0, 100], gridcolor=COLOUR_GRID),
                angularaxis=dict(gridcolor=COLOUR_GRID),
                bgcolor=COLOUR_BACKGROUND,
            ),
            margin=dict(t=85, b=95),
        )
    )

    valores_geral = [scorecard_geral.get(d, 0.0) for d in dimensoes]
    fig.add_trace(
        go.Scatterpolar(
            r=valores_geral + [valores_geral[0]],
            theta=dimensoes + [dimensoes[0]],
            fill="toself",
            fillcolor=hex_to_rgba(chart_colours[0], 0.20),
            line=dict(color=chart_colours[0], width=2.4),
            name="Periodo Filtrado",
            hovertemplate="<b>%{theta}</b><br>Score: %{r:.1f}/100<extra></extra>",
        )
    )

    # Meta de referencia (85 pontos em todas as dimensoes)
    meta = [85.0] * len(dimensoes)
    fig.add_trace(
        go.Scatterpolar(
            r=meta + [meta[0]],
            theta=dimensoes + [dimensoes[0]],
            fill="none",
            line=dict(color=COLOUR_MUTED, width=1.6, dash="dash"),
            name="Meta de Referencia (85 pts)",
            hovertemplate="<b>Meta: %{theta}</b><br>Alvo: %{r:.1f}/100<extra></extra>",
        )
    )

    add_created_at_annotation(fig)
    return fig


def grafico_avaliacao_previsao(comparacao: pd.DataFrame) -> Tuple[go.Figure, go.Figure, go.Figure]:
    comparacao = comparacao.sort_values("timestamp_alvo")

    fig_comp = go.Figure(
        layout=get_layout(
            title=dict(text="Comparacao Temporal: Previsao Linear do ESP32 vs. Real (+10 min)"),
            xaxis_title="Horario Alvo da Previsao",
            yaxis_title="Temperatura (C)",
            hovermode="x unified",
            height=400,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=80),
        )
    )
    fig_comp.add_trace(
        go.Scatter(
            x=comparacao["timestamp_alvo"],
            y=comparacao["temperatura_real_10min"],
            mode="lines+markers",
            marker=dict(size=6),
            name="Temperatura Real Observada",
            line=dict(color=chart_colours[0], width=2.2),
            hovertemplate="<b>Real Observada</b><br>Temp: %{y:.1f} C<extra></extra>",
        )
    )
    fig_comp.add_trace(
        go.Scatter(
            x=comparacao["timestamp_alvo"],
            y=comparacao["previsao_10min"],
            mode="lines+markers",
            marker=dict(size=6),
            name="Prevista 10 min Antes (ESP32)",
            line=dict(color=chart_colours[2], width=2.0, dash="dash"),
            hovertemplate="<b>Prevista ESP32</b><br>Temp: %{y:.1f} C<extra></extra>",
        )
    )
    add_created_at_annotation(fig_comp)

    # Histograma de Residuos
    fig_erro = go.Figure(
        layout=get_layout(
            title=dict(text="Distribuicao dos Residuos (Previsto - Real em C)"),
            xaxis_title="Erro de Previsao (C)",
            yaxis_title="Frequencia",
            showlegend=False,
            height=390,
            bargap=0.06,
        )
    )
    fig_erro.add_trace(
        go.Histogram(
            x=comparacao["erro"],
            nbinsx=20,
            marker_color=chart_colours[0],
            hovertemplate="Faixa de Erro: %{x} C<br>Contagem: %{y}<extra></extra>",
        )
    )
    fig_erro.add_vline(x=0.0, line_dash="dash", line_color=COLOUR_NEGATIVE, annotation_text="Erro Zero")
    add_created_at_annotation(fig_erro)

    # Dispersao Paridade (Previsto vs. Real)
    min_val = float(min(comparacao["temperatura_real_10min"].min(), comparacao["previsao_10min"].min())) - 0.5
    max_val = float(max(comparacao["temperatura_real_10min"].max(), comparacao["previsao_10min"].max())) + 0.5

    fig_paridade = go.Figure(
        layout=get_layout(
            title=dict(text="Grafico de Paridade: Previsto vs. Real Observado"),
            xaxis_title="Temperatura Real Observada (C)",
            yaxis_title="Temperatura Prevista pelo ESP32 (C)",
            height=390,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=80),
        )
    )
    fig_paridade.add_trace(
        go.Scatter(
            x=[min_val, max_val],
            y=[min_val, max_val],
            mode="lines",
            name="Linha de Paridade Ideal (y = x)",
            line=dict(color=COLOUR_MUTED, width=1.8, dash="dash"),
            hoverinfo="skip",
        )
    )
    fig_paridade.add_trace(
        go.Scatter(
            x=comparacao["temperatura_real_10min"],
            y=comparacao["previsao_10min"],
            mode="markers",
            name="Pares (Real vs. Previsto)",
            marker=dict(color=chart_colours[2], size=8, opacity=0.72),
            customdata=comparacao["erro"],
            hovertemplate="Real: %{x:.1f} C<br>Previsto: %{y:.1f} C<br>Erro: %{customdata:+.2f} C<extra></extra>",
        )
    )
    fig_paridade.update_xaxes(showgrid=True, range=[min_val, max_val])
    fig_paridade.update_yaxes(range=[min_val, max_val])
    add_created_at_annotation(fig_paridade)

    return fig_comp, fig_erro, fig_paridade
