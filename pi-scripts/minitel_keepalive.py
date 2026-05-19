import threading
import time

class MinitelKeepAlive:
    def __init__(self, ser, interval=30):
        self.ser = ser
        self.interval = interval
        self.running = False
        self.thread = None

    def _loop(self):
        while self.running:
            try:
                # 0x11 (DC1) = Minitel "cursor/screen on" — resets the screen-sleep timer
                self.ser.write(b'\x11')
            except Exception as e:
                print(f"[KEEPALIVE ERROR] {e}")
            time.sleep(self.interval)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
