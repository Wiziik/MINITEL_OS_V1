/**
 * Minitel <-> Pi bridge for Arduino Mega.
 *
 *   USB Serial  : 9600 8N1   (Pi <-> Arduino)
 *   Serial1     : 1200 7E1   (Arduino <-> Minitel DIN)
 *
 * Pin 19 (RX1) needs INPUT_PULLUP because the Minitel TX is open-collector;
 * without the pullup the keyboard never makes it back to the Pi.
 *
 * Wiring (DIN-5):
 *   DIN 1 (Rx) -> Pin 18 (TX1)
 *   DIN 3 (Tx) -> Pin 19 (RX1)
 *   DIN 2 (GND)-> GND
 */

void setup() {
  Serial.begin(9600);
  Serial1.begin(1200, SERIAL_7E1);
  pinMode(19, INPUT_PULLUP);
  delay(500);
  Serial1.write(0x0C);           // clear Minitel screen
  Serial1.print("3615 TV STORE...");
}

void loop() {
  if (Serial.available()  > 0) Serial1.write(Serial.read());
  if (Serial1.available() > 0) Serial.write(Serial1.read());
}
