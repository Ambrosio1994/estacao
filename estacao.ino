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
#define N_JANELA 20        // pontos usados na regressão
#define PASSOS_FRENTE 20        // 10 min, com um ponto a cada 30 s
#define PREV_MIN -10.0f     // limites da previsão
#define PREV_MAX 50.0f

DHT dht(DHTPIN, DHTTYPE);

volatile int wifi_ok = 0;          // volatile: o evento muda isso em outra tarefa
unsigned long ultimo_envio = 0;
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
  WiFi.setAutoReconnect(false);
  WiFi.begin(WIFI_SSID, WIFI_SENHA);
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

void enviar_com_wifi(float t, float h, float luz, int estado, float prev) {
  wifi_iniciar();
  unsigned long inicio = millis();
  while (!wifi_ok && millis() - inicio < 20000) delay(100);   // espera até 20 s

  if (wifi_ok) enviar_thingspeak(t, h, luz, estado, prev);
  else Serial.println("Sem Wi-Fi, envio cancelado");

  WiFi.mode(WIFI_OFF);
  wifi_ok = 0;
}

void setup() {
  Serial.begin(115200);
  WiFi.onEvent(wifi_evento);
  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_AMARELO, OUTPUT);
  pinMode(LED_VERMELHO, OUTPUT);
  Serial.println(F("Iniciando o sensor DHT22 com ESP32..."));

  dht.begin();
}

void loop() {
  delay(2000);

  float h = dht.readHumidity();
  float t = dht.readTemperature();

  // Leitura falhou: sai antes de usar ou guardar valores inválidos
  if (isnan(h) || isnan(t)) {
    Serial.println(F("Falha ao ler dados do sensor DHT22!"));
    return;
  }

  int valLDR = analogRead(LDR_PIN);
  int luz = (valLDR / 4095.0) * 100;
  int dia = (luz >= LIMITE_DIA);

  int soma = pontos_temp(t, dia) + pontos_umid(h);
  int estado = estado_semaforo(soma);
  mostrar_led(estado);

  // A cada envio: guarda um ponto no histórico, calcula a previsão e envia
  if (millis() - ultimo_envio >= INTERVALO_ENVIO) {
    ultimo_envio = millis();
    guardar_ponto(t);

    float prev = (cont == N_JANELA)
                 ? prever_temperatura(hist, N_JANELA, PASSOS_FRENTE)
                 : NAN;

    if (!isnan(prev)) Serial.printf("Previsao 10 min: %.1f C\n", prev);
    else Serial.printf("Previsao: coletando %d/%d pontos\n", cont, N_JANELA);

    enviar_com_wifi(t, h, luz, estado, prev);
  }

  Serial.print(F("Luminosidade: "));
  Serial.print(luz);
  Serial.print(F("%  |  Umidade: "));
  Serial.print(h);
  Serial.print(F("%  |  Temperatura: "));
  Serial.print(t);
  Serial.println(F("°C"));
}