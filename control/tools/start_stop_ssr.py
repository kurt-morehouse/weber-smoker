"""Bare SSR pin check: drive both SSR GPIO pins high for 10 s, then low.

Uses RPi.GPIO directly and bypasses relay_driver.py, the safety checks and
the GPIO lock, so only run it with main.py and the service stopped. For a
normal one-zone relay test use ssr_test.py instead.

    python tools/start_stop_ssr.py
"""
import time
import RPi.GPIO as GPIO

PINS = {"SSR 1 (zone 1)": 11, "SSR 2 (zone 2)": 13}   # physical pin numbers
ON_SECONDS = 10

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BOARD)                 # use physical pin numbers
try:
    for name, pin in PINS.items():
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
    for name, pin in PINS.items():
        GPIO.output(pin, GPIO.HIGH)
        print(f"{name}: physical pin {pin} HIGH")
    print(f"Measure now. Turning off in {ON_SECONDS} s (Ctrl+C to stop early).")
    time.sleep(ON_SECONDS)
finally:
    for pin in PINS.values():
        GPIO.output(pin, GPIO.LOW)
    GPIO.cleanup()                       # pins back to inputs, off
    print("Both pins LOW, GPIO released.")
