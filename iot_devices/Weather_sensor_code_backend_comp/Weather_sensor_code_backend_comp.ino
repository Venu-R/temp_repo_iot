#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266mDNS.h>
//#include <ArduinoOTA.h>
#include <DHT.h>
#include <time.h>

// DHT setup
#define DHTPIN D4
#define DHTTYPE DHT22
DHT dht(DHTPIN, DHTTYPE);

// WiFi / Server
const char* ssid = "Realme 8";
const char* password = "qwertyuiop";

// IMPORTANT: change this to your BACKEND IP
// AND correct endpoint
const char* server = "http://10.235.221.112:8000/api/external-data";

// Assign numeric ID for this device (must match the DB device_id)
const int DEVICE_ID = 1;

// Hostname for OTA
//const char* hostName = "esp-weather-01";

// NTP config
const long gmtOffset_sec = 5 * 3600 + 30 * 60;
const int daylightOffset_sec = 0;

unsigned long WIFI_RECONNECT_INTERVAL_MS = 5000;
unsigned long SEND_INTERVAL_MS = 5000;
unsigned long lastWifiAttempt = 0;
unsigned long lastSend = 0;

void setupWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(150);
    Serial.print(".");
    if (millis() - start > 10000) break;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.print("WiFi connected, IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println();
    Serial.println("WiFi connect attempt timed out.");
  }
}

// Modified: ensure we return a proper ISO timestamp string.
// Wait briefly for NTP (system time) to sync (up to ~2s) instead of returning plain millis().
String getIsoTimestamp() {
  time_t now = time(nullptr);

  // If time() seems uninitialized (very small), wait briefly for NTP to populate it.
  if (now <= 1000) {
    unsigned long start = millis();
    while (millis() - start < 2000) { // wait up to 2 seconds
      delay(200);
      now = time(nullptr);
      if (now > 1000) break;
    }
  }

  // If after waiting we still don't have a valid epoch, fall back to using current millis()
  // but format it as a human-readable ISO-like string (so it's not a tiny integer like "6014").
  if (now <= 1000) {
    // fallback: produce an ISO-like timestamp using system uptime (not ideal, but not raw millis())
    unsigned long ms = millis();
    unsigned long sec = ms / 1000;
    unsigned long hh = (sec / 3600) % 24;
    unsigned long mm = (sec / 60) % 60;
    unsigned long ss = sec % 60;
    char buf_fb[32];
    // use a placeholder date to keep format stable (YYYY-MM-DDTHH:MM:SS)
    snprintf(buf_fb, sizeof(buf_fb), "1970-01-01T%02lu:%02lu:%02lu", hh, mm, ss);
    return String(buf_fb);
  }

  struct tm timeinfo;
  gmtime_r(&now, &timeinfo);
  char buf[30];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &timeinfo);
  return String(buf);
}

/*
void setupOTA() {
  ArduinoOTA.setHostname(hostName);
  ArduinoOTA.begin();
  Serial.println("OTA ready");
}
*/

void setupTime() {
  configTime(gmtOffset_sec, daylightOffset_sec, "pool.ntp.org", "time.nist.gov");
}

void setup() {
  Serial.begin(115200);
  Serial.println("\nStarting ESP Weather Node");

  dht.begin();
  setupWiFi();
  //setupOTA();
  setupTime();
}

void reconnectIfNeeded() {
  if (WiFi.status() == WL_CONNECTED) return;
  unsigned long now = millis();
  if (now - lastWifiAttempt < WIFI_RECONNECT_INTERVAL_MS) return;

  lastWifiAttempt = now;

  Serial.println("WiFi dropped. Reconnecting...");
  WiFi.disconnect();
  WiFi.begin(ssid, password);
}

void sendSensorData() {
  float h = dht.readHumidity();
  float t = dht.readTemperature();

  if (isnan(h) || isnan(t)) {
    Serial.println("DHT read failed.");
    return;
  }

  String timestamp = getIsoTimestamp();

  // ---- CORRECT JSON FORMAT FOR YOUR BACKEND ----
  String json = "{";
  json += "\"device_id\": " + String(DEVICE_ID) + ",";
  json += "\"timestamp\": \"" + timestamp + "\",";
  json += "\"temperature\": " + String(t, 2) + ",";
  json += "\"humidity\": " + String(h, 2) + ",";
  json += "\"motion\": 0";      // no PIR on ESP12E (yet)
  json += "}";

  Serial.println("Sending JSON:\n" + json);

  HTTPClient http;
  WiFiClient client;

  if (http.begin(client, server)) {
    http.addHeader("Content-Type", "application/json");

    int code = http.POST(json);
    if (code > 0) {
      Serial.printf("HTTP %d → %s\n", code, http.getString().c_str());
    } else {
      Serial.printf("POST failed: %s\n",
                    http.errorToString(code).c_str());
    }
    http.end();
  } else {
    Serial.println("HTTP begin() FAILED");
  }
}

void loop() {
  //ArduinoOTA.handle();
  reconnectIfNeeded();

  unsigned long now = millis();
  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;

    if (WiFi.status() == WL_CONNECTED) {
      sendSensorData();
    } else {
      Serial.println("WiFi not connected. Skipping.");
    }
  }

  delay(10);
}
