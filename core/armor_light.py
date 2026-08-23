"""BLE control for Modelo 8's chest LED — armoros/core/src/main.cpp
advertises as "Modelo 8-Reactor" and exposes a write-only characteristic
that accepts 'h' (HIGH) / 'l' (LOW) on GPIO23, switching the LED through
an IRFZ20 MOSFET. See core/routes_armor.py's POST /api/armor/model-8/light
and ui/js/armor-detail-concepts-load.js's Controlar panel for model-8.

The USB cable to the board is power-only now — all control goes over BLE.
A persistent connection is kept on a dedicated asyncio event loop thread
(bleak is async-only) rather than reconnecting per command, since BLE
connect/scan takes a couple of seconds.
"""
import asyncio
import logging
import threading

from bleak import BleakClient, BleakScanner

logger = logging.getLogger(__name__)

DEVICE_NAME = "Modelo 8-Reactor"
CHARACTERISTIC_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
# Must clear _get_client()'s own 10s scan timeout with real margin — connect()
# on top of a full 10s scan can easily push past 15s on a cold connection,
# which used to make set_light() give up while the coroutine kept running in
# the background (see set_light()'s future.cancel() below for the other half
# of this fix): the caller reported "not connected" and the LED still lit a
# moment later, since a client-side result() timeout never stops the task.
CONNECT_TIMEOUT_S = 25

_loop = None
_loop_thread = None
_client = None
_lock = threading.Lock()


def _ensure_loop():
    global _loop, _loop_thread
    if _loop is not None:
        return
    _loop = asyncio.new_event_loop()
    _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
    _loop_thread.start()


async def _get_client():
    global _client
    if _client is not None and _client.is_connected:
        return _client

    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10.0)
    if device is None:
        raise RuntimeError(f"'{DEVICE_NAME}' not found — is it powered and in range?")

    _client = BleakClient(device)
    await _client.connect()
    return _client


async def _write_command_async(command: bytes):
    client = await _get_client()
    await client.write_gatt_char(CHARACTERISTIC_UUID, command, response=True)


def _write_command(command: bytes) -> None:
    """Raises RuntimeError/TimeoutError if the board isn't reachable over BLE.

    On a timeout, the underlying coroutine is cancelled rather than just
    abandoned — asyncio.run_coroutine_threadsafe's future.result(timeout=...)
    only stops *this* thread from waiting, it does NOT stop the coroutine
    running on _loop. Without the cancel() below, a slow-but-eventually-
    successful connect+write could complete after we've already told the
    caller "not connected", turning the LED on for real right after LIRA
    reported failure — confusing and silently contradicts what was said.
    """
    _ensure_loop()
    with _lock:
        future = asyncio.run_coroutine_threadsafe(_write_command_async(command), _loop)
        try:
            future.result(timeout=CONNECT_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            raise


def set_light(on: bool) -> None:
    _write_command(b"h" if on else b"l")


def set_baliza() -> None:
    """Starts the firmware's PWM breathing fade (main.cpp's balizaTick()) —
    the firmware keeps running the pattern until a plain 'h'/'l' command is
    sent (set_light() cancels it)."""
    _write_command(b"b")
