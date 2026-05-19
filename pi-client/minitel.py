import asyncio
import serial
import time
from typing import Optional

FF  = b"\x0C"
CR  = b"\x0D"
LF  = b"\x0A"
SEP = 0x13

KEY_ENVOI      = 0x41
KEY_RETOUR     = 0x42
KEY_REPETITION = 0x43
KEY_GUIDE      = 0x44
KEY_ANNULATION = 0x45
KEY_SOMMAIRE   = 0x46
KEY_CORRECTION = 0x47
KEY_SUITE      = 0x48


class Minitel:
    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 9600,
                 timeout: float = 0.2):
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )

    def _encode(self, text: str) -> bytes:
        return text.encode("ascii", errors="replace")

    # ── sync methods (for use inside threads: splash, auth, read_input) ──

    def send_raw(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()
        time.sleep(len(data) / 100.0)

    def send_text(self, text: str) -> None:
        self.send_raw(self._encode(text))

    def send_line(self, text: str) -> None:
        self.send_raw(self._encode(text) + CR + LF)

    def clear_screen(self) -> None:
        self.send_raw(FF)

    def move_cursor(self, row: int, col: int) -> None:
        """row: 1-24, col: 1-40. Minitel Videotex US addressing."""
        self.send_raw(bytes([0x1F, 0x40 + row, 0x40 + col]))

    # ── async methods (for use in coroutines: chat display) ──

    async def async_send_raw(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()
        await asyncio.sleep(len(data) / 100.0)

    async def async_send_text(self, text: str) -> None:
        await self.async_send_raw(self._encode(text))

    async def async_send_line(self, text: str) -> None:
        await self.async_send_raw(self._encode(text) + CR + LF)

    async def async_clear_screen(self) -> None:
        await self.async_send_raw(FF)

    async def async_move_cursor(self, row: int, col: int) -> None:
        """row: 1-24, col: 1-40. Minitel Videotex US addressing."""
        await self.async_send_raw(bytes([0x1F, 0x40 + row, 0x40 + col]))

    async def async_clear_to_eol(self) -> None:
        """CAN (0x18): clear to end of line in Minitel videotex mode."""
        await self.async_send_raw(b"\x18")

    # ── input ──

    def read_input(self) -> str:
        buf = bytearray()
        while True:
            b = self.ser.read(1)
            if not b:
                continue
            byte = b[0]

            if byte == SEP:
                code = self.ser.read(1)
                if not code:
                    continue
                if code[0] == KEY_ENVOI:
                    break
                if code[0] == KEY_CORRECTION:
                    if buf:
                        buf.pop()
                        self.ser.write(b"\x08 \x08")
                        self.ser.flush()
                if code[0] == KEY_SOMMAIRE:
                    return "/rooms"
                if code[0] == KEY_RETOUR:
                    return "/older"
                if code[0] == KEY_GUIDE:
                    return "/who"
                if code[0] == KEY_ANNULATION:
                    return "/quit"
                if code[0] == KEY_REPETITION:
                    return "/clear"
                continue

            if byte == 0x0D:
                break

            if byte in (0x08, 0x7F):
                if buf:
                    buf.pop()
                    self.ser.write(b"\x08 \x08")
                    self.ser.flush()
                continue

            buf.append(byte)

        return buf.decode("ascii", errors="replace")

    def read_password(self) -> str:
        buf = bytearray()
        while True:
            b = self.ser.read(1)
            if not b:
                continue
            byte = b[0]

            if byte == SEP:
                code = self.ser.read(1)
                if not code:
                    continue
                if code[0] == KEY_ENVOI:
                    break
                if code[0] == KEY_CORRECTION:
                    if buf:
                        buf.pop()
                        self.send_raw(b"\x08 \x08")
                continue

            if byte == 0x0D:
                break

            if byte in (0x08, 0x7F):
                if buf:
                    buf.pop()
                    self.send_raw(b"\x08 \x08")
                continue

            buf.append(byte)
            self.send_raw(b"\x08*")

        return buf.decode("ascii", errors="replace")

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass
