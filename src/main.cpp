#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <LiquidCrystal_I2C.h>
#include <ESP32Servo.h>
#include <Preferences.h>
#include <time.h>

//  Chân của các thiết bị trong nhà kính
#define DHTPIN 15
#define DHTTYPE DHT22
#define SOIL_PIN 34
#define LDR_PIN 35
#define FAN_PIN 26
#define PUMP_PIN 27
#define LIGHT_PIN 25
#define BUZZER_PIN 14
#define SERVO_PIN 13

//  WiFi + MQTT
const char* WIFI_SSID = "Wokwi-GUEST";
const char* WIFI_PASS = "";
const char* MQTT_HOST = "broker.hivemq.com";
const int   MQTT_PORT = 1883;
// Phải trùng với TOPIC_PREFIX trong dashboard.html
const char* TOPIC_DATA = "qhuy-greenhouse/data";
const char* TOPIC_CMD  = "qhuy-greenhouse/cmd";
const char* TOPIC_STATUS = "qhuy-greenhouse/status";

//  Ngưỡng (có hysteresis)
const float TEMP_ON  = 32.0, TEMP_OFF  = 30.0;
const int   SOIL_ON  = 35,   SOIL_OFF  = 45;   // bật bơm khi đất khô dưới 35%
const int   LIGHT_ON = 30,   LIGHT_OFF = 40;   // bật đèn khi tối dưới 30%

DHT dht(DHTPIN, DHTTYPE);
LiquidCrystal_I2C lcd(0x27, 16, 2);
Servo roofServo;
Preferences preferences;
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

bool autoMode = true;
bool fanOn = false, pumpOn = false, lampOn = false;
bool waterScheduleEnabled = false, scheduledWatering = false;
uint8_t waterHour = 7, waterMinute = 0, waterDuration = 5;
uint8_t servoAngle = 0;
int lastWaterDay = -1;
unsigned long wateringUntil = 0;
unsigned long lastRead = 0, lastAnalogRead = 0, lastTelemetryPublish = 0;
unsigned long lastPublishErrorLog = 0;
unsigned long lastReconnect = 0, lastWifiReconnect = 0;
float lastTemperature = NAN, lastHumidity = NAN;
int lastSoil = 0, lastLight = 0;
unsigned long lastValidSensorRead = 0;
bool hasValidSensorRead = false;
bool timeConfigured = false;

void publishTelemetry() {
  if (!hasValidSensorRead) return;
  if (!mqtt.connected()) return;

  const unsigned long now = millis();
  lastTelemetryPublish = now;

  char json[240];
  snprintf(json, sizeof(json),
    "{\"temp\":%.1f,\"hum\":%.0f,\"soil\":%d,\"light\":%d,"
    "\"fan\":%d,\"pump\":%d,\"lamp\":%d,\"auto\":%d,"
    "\"servo\":%d,\"scheduleEnabled\":%d,\"waterHour\":%d,"
    "\"waterMinute\":%d,\"waterDuration\":%d}",
    lastTemperature, lastHumidity, lastSoil, lastLight,
    fanOn, pumpOn, lampOn, autoMode, servoAngle,
    waterScheduleEnabled, waterHour, waterMinute, waterDuration);

  if (mqtt.publish(TOPIC_DATA, json)) {
    Serial.println(json);
  } else if (now - lastPublishErrorLog >= 5000) {
    lastPublishErrorLog = now;
    Serial.println("MQTT loi: khong publish duoc telemetry");
  }
}

void saveWaterSchedule() {
  preferences.putBool("schedule", waterScheduleEnabled);
  preferences.putUChar("hour", waterHour);
  preferences.putUChar("minute", waterMinute);
  preferences.putUChar("duration", waterDuration);
}

void updateWaterSchedule() {
  if (scheduledWatering && (long)(millis() - wateringUntil) >= 0) {
    scheduledWatering = false;
    pumpOn = false;
    digitalWrite(PUMP_PIN, LOW);
  }

  if (!waterScheduleEnabled || scheduledWatering) return;

  struct tm localTime;
  if (!getLocalTime(&localTime, 0) || localTime.tm_year < (2024 - 1900)) return;
  const int today = (localTime.tm_year + 1900) * 10000 +
                    (localTime.tm_mon + 1) * 100 + localTime.tm_mday;
  if (today == lastWaterDay || localTime.tm_hour != waterHour ||
      localTime.tm_min != waterMinute) return;

  lastWaterDay = today;
  preferences.putInt("lastDay", lastWaterDay);
  scheduledWatering = true;
  wateringUntil = millis() + (unsigned long)waterDuration * 60000UL;
  pumpOn = true;
  digitalWrite(PUMP_PIN, HIGH);
  Serial.printf("Tuoi theo lich: %u phut\n", waterDuration);
}

// Nhận lệnh từ dashboard
void callback(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  msg.trim();
  msg.toUpperCase();
  Serial.println("CMD: " + msg);

  if (msg.startsWith("WATER_SCHEDULE=")) {
    int hour, minute, duration;
    if (sscanf(msg.c_str(), "WATER_SCHEDULE=%d:%d,%d", &hour, &minute, &duration) != 3 ||
        hour < 0 || hour > 23 || minute < 0 || minute > 59 ||
        duration < 1 || duration > 120) {
      Serial.println("Lich tuoi khong hop le");
      return;
    }
    waterHour = hour;
    waterMinute = minute;
    waterDuration = duration;
    waterScheduleEnabled = true;
    saveWaterSchedule();
  } else if (msg == "WATER_SCHEDULE_OFF") {
    waterScheduleEnabled = false;
    if (scheduledWatering) pumpOn = false;
    scheduledWatering = false;
    saveWaterSchedule();
  } else if (msg.startsWith("SERVO=")) {
    const int angle = msg.substring(6).toInt();
    if (angle < 0 || angle > 180) {
      Serial.println("Goc servo khong hop le");
      return;
    }
    servoAngle = angle;
    roofServo.write(servoAngle);
  } else if (msg == "AUTO") {
    autoMode = true;
  } else if (msg == "MANUAL") {
    autoMode = false;
  } else if (msg == "FAN_ON" || msg == "FAN_OFF" ||
             msg == "PUMP_ON" || msg == "PUMP_OFF" ||
             msg == "LAMP_ON" || msg == "LAMP_OFF") {
    autoMode = false;
    if (msg == "FAN_ON") fanOn = true;
    if (msg == "FAN_OFF") fanOn = false;
    if (msg == "PUMP_ON") pumpOn = true;
    if (msg == "PUMP_OFF") pumpOn = false;
    if (msg == "LAMP_ON") lampOn = true;
    if (msg == "LAMP_OFF") lampOn = false;
  } else {
    Serial.println("Lenh khong hop le");
    return;
  }

  // Áp dụng ngay, không đợi chu kỳ đọc cảm biến
  digitalWrite(FAN_PIN, fanOn);
  digitalWrite(PUMP_PIN, pumpOn);
  digitalWrite(LIGHT_PIN, lampOn);
  if (hasValidSensorRead && millis() - lastValidSensorRead <= 5000) {
    publishTelemetry();
  }
}

void startWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);   // kênh 6 là của Wokwi-GUEST
  lastWifiReconnect = millis();
  Serial.println("Dang ket noi WiFi...");
}

void reconnectMqtt() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (millis() - lastReconnect < 5000) return;   // thử lại mỗi 5 giây
  lastReconnect = millis();

  String clientId = "esp32-gh-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(clientId.c_str(), TOPIC_STATUS, 0, true, "offline")) {
    Serial.println("MQTT OK");
    mqtt.subscribe(TOPIC_CMD);
    mqtt.publish(TOPIC_STATUS, "online", true);
  } else {
    Serial.printf("MQTT loi, rc=%d\n", mqtt.state());
  }
}

void setup() {
  Serial.begin(115200);
  dht.begin();
  lcd.init();
  lcd.backlight();

  pinMode(FAN_PIN, OUTPUT);
  pinMode(PUMP_PIN, OUTPUT);
  pinMode(LIGHT_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(SERVO_PIN, OUTPUT);
  roofServo.setPeriodHertz(50);
  roofServo.attach(SERVO_PIN, 500, 2400);
  roofServo.write(servoAngle);
  preferences.begin("greenhouse", false);
  waterScheduleEnabled = preferences.getBool("schedule", false);
  waterHour = preferences.getUChar("hour", 7);
  waterMinute = preferences.getUChar("minute", 0);
  waterDuration = preferences.getUChar("duration", 5);
  lastWaterDay = preferences.getInt("lastDay", -1);

  lcd.setCursor(0, 0);
  lcd.print("Dang ket noi...");
  startWiFi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(callback);
}

void loop() {
  const unsigned long now = millis();
  const bool wifiConnected = WiFi.status() == WL_CONNECTED;
  if (wifiConnected) {
    if (!timeConfigured) {
      Serial.println("WiFi OK, IP: " + WiFi.localIP().toString());
      configTime(7 * 3600, 0, "pool.ntp.org", "time.google.com");
      timeConfigured = true;
      lcd.clear();
    }
    if (!mqtt.connected()) reconnectMqtt();
  } else {
    if (mqtt.connected()) mqtt.disconnect();
    if (now - lastWifiReconnect >= 5000) {
      lastWifiReconnect = now;
      Serial.println("WiFi mat ket noi, dang thu lai...");
      WiFi.reconnect();
    }
  }
  mqtt.loop();                       // bắt buộc gọi liên tục để nhận lệnh
  updateWaterSchedule();

  bool sensorUpdated = false;

  if (now - lastRead >= 2000) {
    lastRead = now;
    const float temperature = dht.readTemperature();
    const float humidity = dht.readHumidity();
    if (isnan(temperature) || isnan(humidity)) {
      Serial.println("Loi doc DHT22!");
    } else {
      lastTemperature = temperature;
      lastHumidity = humidity;
      lastValidSensorRead = now;
      hasValidSensorRead = true;
      sensorUpdated = true;
    }
  }

  if (now - lastAnalogRead >= 500) {
    lastAnalogRead = now;
    lastSoil = map(analogRead(SOIL_PIN), 0, 4095, 100, 0);
    lastLight = map(analogRead(LDR_PIN), 0, 4095, 0, 100);
    sensorUpdated = true;
  }

  // Chế độ tự động: có hysteresis
  if (autoMode && hasValidSensorRead) {
    if (lastTemperature > TEMP_ON)       fanOn = true;
    else if (lastTemperature < TEMP_OFF) fanOn = false;

    if (!scheduledWatering) {
      if (lastSoil < SOIL_ON)        pumpOn = true;
      else if (lastSoil > SOIL_OFF)  pumpOn = false;
    }

    if (lastLight < LIGHT_ON)        lampOn = true;
    else if (lastLight > LIGHT_OFF)  lampOn = false;
  }

  digitalWrite(FAN_PIN, fanOn);
  digitalWrite(PUMP_PIN, pumpOn);
  digitalWrite(LIGHT_PIN, lampOn);
  if (hasValidSensorRead) digitalWrite(BUZZER_PIN, lastTemperature > TEMP_ON + 5);

  if (sensorUpdated && hasValidSensorRead) {
    // LCD (ký tự cuối dòng 1: A = auto, M = manual)
    lcd.setCursor(0, 0);
    lcd.printf("T:%.1fC H:%.0f%% %s", lastTemperature, lastHumidity, autoMode ? "A" : "M");
    lcd.setCursor(0, 1);
    lcd.printf("S:%d%% L:%d%%  ", lastSoil, lastLight);
  }

  if (hasValidSensorRead &&
      now - lastValidSensorRead <= 5000 &&
      now - lastTelemetryPublish >= 500) {
    publishTelemetry();
  }
}