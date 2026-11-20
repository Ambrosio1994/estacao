# Estacao de Monitoramento Ambiental com ESP32

Este projeto implementa uma estacao de monitoramento ambiental utilizando um microcontrolador ESP32, um sensor de temperatura e umidade DHT22, um sensor de luminosidade (LDR) e indicacao visual por meio de tres LEDs (verde, amarelo e vermelho). Os dados coletados e processados sao publicados em formato JSON via protocolo MQTT (broker HiveMQ) e persistidos em CSV por um script assinante em Python executado no servidor.

## Funcionalidades

- Leitura continua de temperatura e umidade relativa do ar (DHT22) e luminosidade ambiente (LDR) a cada 2 segundos.
- Identificacao automatica de periodo (dia ou noite) com base no percentual de luminosidade (limite de 40%).
- Sistema de semaforo (LEDs verde, amarelo e vermelho) que avalia o conforto termico e de umidade com faixas ajustadas para o dia e para a noite.
- Previsao de temperatura para 10 minutos a frente utilizando regressao linear simples sobre uma janela deslizante das ultimas 20 leituras.
- Publicacao continua das leituras em JSON via protocolo MQTT (`broker.hivemq.com:1883`).
- Script assinante MQTT em Python (`assinante_mqtt.py`) que permanece em execucao no servidor, valida o JSON recebido e persiste os dados em CSV (`dados_mqtt.csv`).

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

## Integracao com MQTT e Assinante Python (`assinante_mqtt.py`)

O ESP32 publica as medicoes em formato JSON no topico MQTT configurado em `secrets.h`:

```json
{"temperatura":24.5,"umidade":52.0,"luminosidade":75.0,"estado":0,"previsao":24.8}
```

- `temperatura`: Temperatura atual (C)
- `umidade`: Umidade relativa (%)
- `luminosidade`: Luminosidade (%)
- `estado`: Estado do semaforo (0 = Verde, 1 = Amarelo, 2 = Vermelho)
- `previsao`: Temperatura prevista para 10 minutos (C) ou `null` enquanto coleta os pontos iniciais

No servidor, o script `assinante_mqtt.py` permanece em execucao ouvindo o topico MQTT, validando o JSON e gravando cada leitura no arquivo `dados_mqtt.csv` com as colunas:
`timestamp,temperatura,umidade,luminosidade,estado_semaforo,status_semaforo,periodo,previsao_10min`.

Para executar o assinante no servidor:

```bash
.venv/bin/python assinante_mqtt.py
```

Opcionalmente, os parametros podem ser sobrescritos via linha de comando (`--broker`, `--port`, `--topic`, `--csv`) ou variaveis de ambiente (`MQTT_BROKER`, `MQTT_PORT`, `MQTT_TOPIC`, `CSV_SAIDA`).

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

#define MQTT_BROKER "broker.hivemq.com"
#define MQTT_PORT   1883
#define MQTT_TOPIC  "prof_thiago/unip/pd2/NOME_DO_GRUPO/dados"

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
   - Coleta em tempo real via MQTT (`broker.hivemq.com:1883`) e porta USB Serial (`/dev/ttyUSB0`, 115200 bps) com persistencia local automatica.
   - Leitura dos arquivos CSV gerados localmente (`dados_mqtt.csv` ou `dados_esp32_serial.csv`).
2. Exploracao e preparacao dos dados (Pandas):
   - Padronizacao dos campos recebidos via MQTT (`temperatura`, `umidade`, `luminosidade`, `estado_semaforo` e `previsao_10min`).
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

## Passo a Passo para Apresentacao (Sala de Aula)

1. **Conectar a internet (Wi-Fi):**
   - Ligue o roteador do celular com o mesmo SSID e senha configurados em `secrets.h` e conecte o notebook nele.
   - *(So precisa editar `secrets.h` e rodar `arduino-cli compile && arduino-cli upload` se for usar outro Wi-Fi ou trocar o nome do grupo em `MQTT_TOPIC`.)*

2. **Ligar o ESP32:**
   - Conecte o ESP32 na energia (USB do notebook, carregador ou powerbank).
   - Se estiver no USB do notebook, ele tambem serve de plano B automatico caso o Wi-Fi da placa falhe.

3. **Iniciar o Assinante MQTT (CSV) e o Dashboard Web em Tempo Real:**
   No terminal, dentro da pasta do projeto:
   ```bash
   arduino-cli compile && arduino-cli upload
   .venv/bin/python assinante_mqtt.py
   ```
   Em outro terminal (opcional, para visualizar em tempo real no navegador):
   ```bash
   .venv/bin/python servidor_tempo_real.py
   ```
   Abra no navegador: **http://localhost:8000**  
   *(Opcional — painel analitico completo em Streamlit: `.venv/bin/streamlit run app.py`)*

4. **Confirmar a conexao MQTT no topo da tela:**
   - Verifique se o indicador esta **verde** (`MQTT Ativo (broker.hivemq.com:1883)` ou `MQTT + Serial`) e se o topico exibido e o do grupo (`prof_thiago/unip/pd2/...`).

5. **Demonstracao ao vivo:**
   - **Luminosidade (LDR):** cubra o sensor com a mao ou ilumine com a lanterna do celular (atualiza a cada ~6 s).
   - **Temperatura / Umidade (DHT22):** aproxime a mao ou sopre levemente no sensor para variar os graficos e o semaforo de LEDs em tempo real (~2 s).
