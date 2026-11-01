# Estacao de Monitoramento Ambiental com ESP32

Este projeto implementa uma estacao de monitoramento ambiental utilizando um microcontrolador ESP32, um sensor de temperatura e umidade DHT22, um sensor de luminosidade (LDR) e indicacao visual por meio de tres LEDs (verde, amarelo e vermelho). Os dados coletados e processados sao enviados periodicamente para a plataforma ThingSpeak via Wi-Fi.

## Funcionalidades

- Leitura continua de temperatura e umidade relativa do ar (DHT22) e luminosidade ambiente (LDR) a cada 2 segundos.
- Identificacao automatica de periodo (dia ou noite) com base no percentual de luminosidade (limite de 40%).
- Sistema de semaforo (LEDs verde, amarelo e vermelho) que avalia o conforto termico e de umidade com faixas ajustadas para o dia e para a noite.
- Previsao de temperatura para 10 minutos a frente utilizando regressao linear simples sobre uma janela deslizante das ultimas 20 leituras.
- Gerenciamento de energia do radio Wi-Fi: a conexao e estabelecida apenas no momento do envio (a cada 30 segundos) e desligada logo em seguida.
- Envio de dados via requisicao HTTP GET para o ThingSpeak.

## Hardware e Pinagem

- Microcontrolador: ESP32 (`esp32:esp32:esp32`)
- Sensor DHT22: Pino GPIO 18
- Sensor LDR (entrada analogica): Pino GPIO 4
- LED Verde: Pino GPIO 21
- LED Amarelo: Pino GPIO 5
- LED Vermelho: Pino GPIO 19

## Regras de Avaliacao (Semaforo)

O estado do semaforo e definido pela soma de pontos de penalidade de temperatura e umidade:

### Temperatura
- Dia (luminosidade >= 40%):
  - 0 pontos (Ideal): 20 C a 26 C
  - 1 ponto (Atencao): 18 C a 28 C (fora da faixa ideal)
  - 2 pontos (Critico): abaixo de 18 C ou acima de 28 C
- Noite (luminosidade < 40%):
  - 0 pontos (Ideal): 18 C a 24 C
  - 1 ponto (Atencao): 16 C a 26 C (fora da faixa ideal)
  - 2 pontos (Critico): abaixo de 16 C ou acima de 26 C

### Umidade
- 0 pontos (Ideal): 40% a 60%
- 1 ponto (Fora do ideal): abaixo de 40% ou acima de 60%

### Estado Final (Soma dos Pontos)
- Soma 0: Estado 0 (LED Verde aceso)
- Soma 1: Estado 1 (LED Amarelo aceso)
- Soma 2 ou mais: Estado 2 (LED Vermelho aceso)

## Previsao de Temperatura

A cada intervalo de envio (30 segundos), a temperatura atual e armazenada em um vetor historico de 20 posicoes (`N_JANELA = 20`). Quando a janela de 20 amostras esta completa, o sistema calcula uma reta de regressao linear pelo metodo dos minimos quadrados e projeta o valor para 20 passos a frente (`PASSOS_FRENTE = 20`), o que corresponde a uma previsao para os proximos 10 minutos. O valor previsto e limitado ao intervalo entre -10.0 C e 50.0 C.

## Integracao com ThingSpeak

Os dados sao enviados a cada 30 segundos para os seguintes campos do canal ThingSpeak:

- `field1`: Temperatura atual (C)
- `field2`: Umidade relativa (%)
- `field3`: Luminosidade (%)
- `field4`: Estado do semaforo (0 = Verde, 1 = Amarelo, 2 = Vermelho)
- `field5`: Temperatura prevista para 10 minutos (C) - enviado apenas apos coletar as 20 amostras iniciais

## Configuracao e Compilacao

### Dependencias
- Core ESP32 para Arduino
- Biblioteca `DHT sensor library` (Adafruit) e suas dependencias (`Adafruit Unified Sensor`)

### Arquivo de Credenciais (`secrets.h`)
Crie um arquivo chamado `secrets.h` na raiz do projeto (este arquivo e ignorado pelo Git) com a seguinte estrutura:

```cpp
#ifndef SECRETS_H
#define SECRETS_H

#define WIFI_SSID "SEU_WIFI_SSID"
#define WIFI_SENHA "SUA_WIFI_SENHA"
#define TS_CHAVE "SUA_WRITE_API_KEY_THINGSPEAK"

#endif
```

### Compilacao e Upload
O projeto contem um arquivo `sketch.yaml` configurado para o `arduino-cli` com o FQBN `esp32:esp32:esp32` e a porta padrao `/dev/ttyUSB0`.

Para compilar e enviar via `arduino-cli`:

```bash
arduino-cli compile
arduino-cli upload
```

Para acompanhar a saida serial (baud rate 115200):

```bash
arduino-cli monitor -c baudrate=115200
```

## Dashboard Interativo em Python (Streamlit, Pandas e Plotly)

O arquivo `app.py` disponibiliza uma aplicacao web interativa em Python para coletar, preparar, analisar e visualizar os dados transmitidos pelo ESP32.

### Funcionalidades do Dashboard

1. Carregamento de dados reais:
   - Coleta direta em tempo real pela porta USB Serial (`/dev/ttyUSB0`, 115200 bps) a cada 2 segundos com persistencia local automatica (`dados_esp32_serial.csv`).
   - Consulta em tempo real via API REST do ThingSpeak (informando o `Channel ID` e, opcionalmente, a `Read API Key`).
   - Upload de arquivos locais exportados do ThingSpeak (`feeds.csv` ou `feeds.json`).
2. Exploracao e preparacao dos dados (Pandas):
   - Padronizacao das colunas (`field1` a `field5` para nomes descritivos).
   - Conversao de timestamps para o fuso horario local (`America/Sao_Paulo`).
   - Tratamento de valores ausentes (`NaN`), remocao de duplicatas e filtragem de leituras fora dos limites fisicos dos sensores.
   - Classificacao de periodo (`Dia` ou `Noite`) e estado de conforto (`Ideal (Verde)`, `Atencao (Amarelo)`, `Critico (Vermelho)`).
   - Exportacao dos dados processados em formato CSV.
3. Calculo de indicadores meteorologicos, bioclimaticos e de telemetria:
   - Ponto de Orvalho (formula de Magnus-Tetens) e Depressao do Ponto de Orvalho (margem anti-condensacao $T - T_d$).
   - Temperatura de Bulbo Umido (formula empirica de Stull, 2011).
   - Sensacao Termica / Indice de Calor (equacao de Steadman/Rothfusz).
   - Indice de Desconforto Termico de Thom (DI) e Indice de Temperatura e Umidade (ITU).
   - Deficit de Pressao de Vapor - VPD (kPa) com classificacao bioclimatica.
   - Umidade Absoluta ($\text{g/m}^3$) e Entalpia Especifica do Ar Umido ($\text{kJ/kg}$ de ar seco).
   - Decomposicao automatica das causas de alerta do semaforo (calor, frio, ar seco, ar umido).
   - Scorecard Ambiental Multidimensional (0 a 100) em 5 dimensoes (Conforto Termico, Equilibrio Hidrico, Estabilidade Termica, Seguranca Anti-Condensacao e Regularidade IoT).
   - Amplitude termica, taxa de variacao horaria ($\text{C/h}$), bandas de estabilidade ($\text{MM} \pm 1.5\sigma$) e metricas completas da previsao de 10 minutos do ESP32 (MAE, RMSE, MAPE, Vies, $R^2$ e Acuracia Direcional).
4. Visualizacao interativa padronizada (Plotly):
   - Medidores instantaneos (*Gauges*) com faixas dinamicas Dia/Noite para Temperatura, Umidade, Indice de Thom e Score Ambiental Global.
   - Evolucao temporal de temperatura com banda de estabilidade, bulbo umido, sensacao termica, ponto de orvalho, anotacoes automaticas de extremos (Min/Max) e trajetoria projetada (+10 min).
   - Series temporais de umidade relativa e luminosidade com limites de referencia.
   - Graficos de proporcao, linha do tempo do semaforo e barra horizontal ranqueada com o diagnostico das causas de desconforto.
   - Evolucao psicrometrica (VPD, umidade absoluta e spread de orvalho), carta psicrometrica simplificada (bolhas de Temperatura vs. Umidade) e matriz de correlacao de Pearson.
   - Graficos de distribuicao Violin/Boxplot (Dia vs. Noite) e Radar (*Spider Chart*) do Scorecard Ambiental.
   - Avaliacao da previsao embarcada com comparativo temporal, histograma de residuos e grafico de paridade (Previsto vs. Real).

### Como Executar o Dashboard

Crie um ambiente virtual, instale as dependencias listadas em `requirements.txt` e inicie o Streamlit:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```
