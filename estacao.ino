#include <DHT.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "secrets.h"   

#define DHTTYPE DHT22
#define DHTPIN 18
#define LDR_PIN 4      
#define LED_VERDE 21
#define LED_AMARELO 5
#define LED_VERMELHO 19

#define LIMITE_DIA 40        // luz (%) a partir da qual é dia
#define INTERVALO_ENVIO 30000UL   // ms entre envios (e entre pontos do histórico)
#define INTERVALO_LDR   6000UL    // ms entre leituras do LDR no GPIO 4 (ADC2 exige Wi-Fi desligado)
#define N_JANELA 20        // máximo de pontos usados na regressão
#define MIN_PONTOS_PREV 2  // 1 min (2 leituras de 30 s) para iniciar a regressão
#define PASSOS_FRENTE 20        // 10 min, com um ponto a cada 30 s
#define PREV_MIN -10.0f     // limites da previsão
#define PREV_MAX 50.0f

DHT dht(DHTPIN, DHTTYPE);
WiFiClient mqtt_client;

volatile int wifi_ok = 0;          // volatile: o evento muda isso em outra tarefa
unsigned long ultimo_envio = 0;
unsigned long ultima_tentativa_wifi = 0;
unsigned long ultima_leitura_ldr = 0;
int luz_atual = 0;
float hist[N_JANELA];              // últimas temperaturas, do mais antigo ao mais novo
int cont = 0;                      // quantos pontos já estão no histórico

float prever_temperatura(const float *y, int n, int passos) {
  if (n < 2) return NAN;           // sem reta com menos de 2 pontos

  float soma_x = 0, soma_y = 0, soma_xy = 0, soma_xx = 0;
  for (int i = 0; i < n; i++) {
    soma_x  += i;
    soma_y  += y[i];
    soma_xy += i * y[i];
    soma_xx += i * i;
  }

  float b = (n * soma_xy - soma_x * soma_y) / (n * soma_xx - soma_x * soma_x);
  float a = (soma_y - b * soma_x) / n;

  float x_alvo = (n - 1) + passos;   // com n = 20 e passos = 20: x = 39
  float prev = a + b * x_alvo;

  if (prev < PREV_MIN) prev = PREV_MIN;
  if (prev > PREV_MAX) prev = PREV_MAX;
  return prev;
}

void guardar_ponto(float t) {
  if (cont < N_JANELA) {
    hist[cont++] = t;
  } else {
    for (int i = 0; i < N_JANELA - 1; i++) hist[i] = hist[i + 1];
    hist[N_JANELA - 1] = t;
  }
}

int pontos_temp(float t, int dia) {
  float min_v, max_v, min_a, max_a;
  if (dia) {
    min_v = 20; max_v = 26;
    min_a = 18; max_a = 28;
  } else {
    min_v = 18; max_v = 24;
    min_a = 16; max_a = 26;
  }

  if (t >= min_v && t <= max_v) return 0;
  if (t >= min_a && t <= max_a) return 1;
  return 2;
}

int pontos_umid(float u) {
  if (u >= 40 && u <= 60) return 0;
  return 1;
}

int estado_semaforo(int soma) {
  if (soma == 0) return 0;
  if (soma == 1) return 1;
  return 2;
}

void mostrar_led(int estado) {
  digitalWrite(LED_VERDE,    estado == 0);
  digitalWrite(LED_AMARELO,  estado == 1);
  digitalWrite(LED_VERMELHO, estado == 2);
}

void wifi_evento(WiFiEvent_t evento, WiFiEventInfo_t info) {
  if (evento == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    wifi_ok = 0;
    Serial.println("Wi-Fi desconectado");
  }
  else if (evento == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    wifi_ok = 1;
    Serial.print("Conectado! IP: ");
    Serial.println(WiFi.localIP());
  }
}

void wifi_iniciar() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_SENHA);
  ultima_tentativa_wifi = millis();
}

void mqtt_escrever_tamanho(uint8_t *buf, int &pos, int tam) {
  do {
    uint8_t b = tam & 0x7F;
    tam >>= 7;
    if (tam > 0) b |= 0x80;
    buf[pos++] = b;
  } while (tam > 0);
}

bool mqtt_conectar() {
  if (!wifi_ok) return false;
  if (mqtt_client.connected()) return true;

  if (!mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 3000)) {
    Serial.println("Falha ao conectar no broker MQTT");
    return false;
  }

  char client_id[32];
  snprintf(client_id, sizeof(client_id), "esp32_pd2_%06X", (uint32_t)(ESP.getEfuseMac() & 0xFFFFFF));
  int id_len = strlen(client_id);
  int rem_len = 10 + 2 + id_len;

  uint8_t pkt[64];
  int p = 0;
  pkt[p++] = 0x10; // Pacote CONNECT MQTT 3.1.1
  mqtt_escrever_tamanho(pkt, p, rem_len);
  pkt[p++] = 0x00; pkt[p++] = 0x04;
  pkt[p++] = 'M';  pkt[p++] = 'Q'; pkt[p++] = 'T'; pkt[p++] = 'T';
  pkt[p++] = 0x04; // Protocol level 4 (v3.1.1)
  pkt[p++] = 0x02; // Clean session
  pkt[p++] = 0x00; pkt[p++] = 0x3C; // Keepalive 60s
  pkt[p++] = (id_len >> 8) & 0xFF;
  pkt[p++] = id_len & 0xFF;
  memcpy(pkt + p, client_id, id_len);
  p += id_len;

  mqtt_client.write(pkt, p);

  unsigned long t0 = millis();
  while (mqtt_client.available() < 4 && millis() - t0 < 3000) delay(10);
  if (mqtt_client.available() < 4) {
    mqtt_client.stop();
    return false;
  }

  uint8_t connack[4];
  mqtt_client.read(connack, 4);
  if (connack[0] != 0x20 || connack[3] != 0x00) {
    mqtt_client.stop();
    return false;
  }

  Serial.printf("MQTT conectado em %s:%d (%s)\n", MQTT_BROKER, MQTT_PORT, client_id);
  return true;
}

void enviar_mqtt(float temp, float umid, float luz, int estado, float prev) {
  if (!mqtt_conectar()) return;

  while (mqtt_client.available()) mqtt_client.read();

  char payload[180];
  if (!isnan(prev)) {
    snprintf(payload, sizeof(payload),
      "{\"temperatura\":%.1f,\"umidade\":%.1f,\"luminosidade\":%.1f,\"estado\":%d,\"previsao\":%.1f}",
      temp, umid, luz, estado, prev);
  } else {
    snprintf(payload, sizeof(payload),
      "{\"temperatura\":%.1f,\"umidade\":%.1f,\"luminosidade\":%.1f,\"estado\":%d,\"previsao\":null}",
      temp, umid, luz, estado);
  }

  int topic_len = strlen(MQTT_TOPIC);
  int payload_len = strlen(payload);
  int rem_len = 2 + topic_len + payload_len;

  uint8_t pkt[320];
  int p = 0;
  pkt[p++] = 0x30; // Pacote PUBLISH QoS 0
  mqtt_escrever_tamanho(pkt, p, rem_len);
  pkt[p++] = (topic_len >> 8) & 0xFF;
  pkt[p++] = topic_len & 0xFF;
  memcpy(pkt + p, MQTT_TOPIC, topic_len);
  p += topic_len;
  memcpy(pkt + p, payload, payload_len);
  p += payload_len;

  if (mqtt_client.write(pkt, p) == (size_t)p) {
    Serial.printf("MQTT [%s]: %s\n", MQTT_TOPIC, payload);
  } else {
    Serial.println("Falha no envio MQTT");
    mqtt_client.stop();
  }
}

void enviar_thingspeak(float temp, float umid, float luz, int estado, float prev) {
  if (!wifi_ok) return;

  char url[220];
  int n = snprintf(url, sizeof(url),
    "http://api.thingspeak.com/update?api_key=%s&field1=%.1f&field2=%.1f&field3=%.1f&field4=%d",
    TS_CHAVE, temp, umid, luz, estado);

  if (!isnan(prev) && n > 0 && n < (int)sizeof(url)) {
    snprintf(url + n, sizeof(url) - n, "&field5=%.1f", prev);
  }

  HTTPClient http;
  http.setTimeout(5000);
  http.begin(url);
  int status = http.GET();
  if (status > 0) {
    Serial.printf("ThingSpeak: status %d, entrada %s\n", status, http.getString().c_str());
  } else {
    Serial.println("Falha no envio");
  }
  http.end();
}

int ler_ldr_sem_wifi() {
  // GPIO 4 usa ADC2, que conflita com o radio Wi-Fi ativo no ESP32
  mqtt_client.stop();
  WiFi.disconnect(false);
  WiFi.mode(WIFI_OFF);
  wifi_ok = 0;
  delay(20);

  int valLDR = analogRead(LDR_PIN);
  int pct = (valLDR / 4095.0) * 100;

  wifi_iniciar();
  unsigned long inicio = millis();
  while (!wifi_ok && millis() - inicio < 8000UL) delay(50);

  ultima_leitura_ldr = millis();
  return pct;
}

void setup() {
  Serial.begin(115200);
  WiFi.onEvent(wifi_evento);
  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_AMARELO, OUTPUT);
  pinMode(LED_VERMELHO, OUTPUT);
  Serial.println(F("Iniciando o sensor DHT22 com ESP32..."));

  dht.begin();
  luz_atual = (analogRead(LDR_PIN) / 4095.0) * 100;
  ultima_leitura_ldr = millis();
  wifi_iniciar();
}

void loop() {
  delay(2000);

  if (millis() - ultima_leitura_ldr >= INTERVALO_LDR) {
    luz_atual = ler_ldr_sem_wifi();
  } else if (!wifi_ok && millis() - ultima_tentativa_wifi >= 10000UL) {
    wifi_iniciar();
  }

  float h = dht.readHumidity();
  float t = dht.readTemperature();

  // Leitura falhou: sai antes de usar ou guardar valores inválidos
  if (isnan(h) || isnan(t)) {
    Serial.println(F("Falha ao ler dados do sensor DHT22!"));
    return;
  }

  int luz = luz_atual;
  int dia = (luz >= LIMITE_DIA);

  int soma = pontos_temp(t, dia) + pontos_umid(h);
  int estado = estado_semaforo(soma);
  mostrar_led(estado);

  bool ciclo_30s = (cont == 0) || (millis() - ultimo_envio >= INTERVALO_ENVIO);
  if (ciclo_30s) {
    ultimo_envio = millis();
    guardar_ponto(t);
  }

  float prev = NAN;
  if (cont >= MIN_PONTOS_PREV) {
    float janela_atual[N_JANELA];
    memcpy(janela_atual, hist, cont * sizeof(float));
    janela_atual[cont - 1] = t;
    prev = prever_temperatura(janela_atual, cont, PASSOS_FRENTE);
  }

  if (ciclo_30s) {
    if (!isnan(prev)) Serial.printf("Previsao 10 min: %.1f C\n", prev);
    else Serial.printf("Previsao: coletando %d/%d pontos\n", cont, MIN_PONTOS_PREV);
  }

  Serial.print(F("Luminosidade: "));
  Serial.print(luz);
  Serial.print(F("%  |  Umidade: "));
  Serial.print(h);
  Serial.print(F("%  |  Temperatura: "));
  Serial.print(t);
  Serial.println(F("°C"));

  enviar_mqtt(t, h, luz, estado, prev);
  if (ciclo_30s) {
    enviar_thingspeak(t, h, luz, estado, prev);
  }
}