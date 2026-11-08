"""Walk every GPIO pin on the 40-pin header: each one HIGH for 1 s, then LOW,
in physical pin order, over and over until Ctrl+C.

Use it to find dead pins with an LED (+ 330 ohm resistor to GND), a meter, or
an SSR's input LED as the probe.

BEFORE RUNNING
  - sudo systemctl stop smoker        (main.py holds the relay pins)
  - UNPLUG THE HEATING ELEMENTS. The relay pins (GPIO22, GPIO23) are walked
    too, so each SSR switches on for 1 s every pass.
  - Best: disconnect the MAX31855 boards. Their DO line drives pin 21 back.
  - Afterwards, reboot: this takes the SPI pins (GPIO7-11) away from the SPI
    driver, and SPI won't work again until a reboot.

    cd ~/smoker/control && .venv/bin/python tools/gpio_walk.py
    .venv/bin/python tools/gpio_walk.py --seconds 2     # slower
    .venv/bin/python tools/gpio_walk.py --only 11 13 15 # just these physical pins

Only one pin is an output at a time: each pin goes back to an input after its
turn, so nothing else on the header is driven while you probe.
"""
import argparse
import sys
import time

try:
    import RPi.GPIO as GPIO
except ImportError as e:
    sys.exit(f"RPi.GPIO won't load ({e}). Run it on the Pi with .venv/bin/python, "
             f"or: sudo apt install python3-rpi.gpio")

# Physical header pin -> BCM GPIO number, in header order. Pins 27/28
# (GPIO0/1, the HAT ID EEPROM bus) are left out on purpose.
PHYSICAL_TO_BCM = {
    3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27, 15: 22, 16: 23,
    18: 24, 19: 10, 21: 9, 22: 25, 23: 11, 24: 8, 26: 7, 29: 5, 31: 6, 32: 12,
    33: 13, 35: 19, 36: 16, 37: 26, 38: 20, 40: 21,
}
NOTES = {15: "relay 1 SSR", 16: "relay 2 SSR", 21: "SPI0 MISO (MAX31855 DO)",
         23: "SPI0 SCLK", 24: "SPI0 CE0", 26: "SPI0 CE1", 19: "SPI0 MOSI",
         3: "I2C SDA, has a 1.8k pull-up", 5: "I2C SCL, has a 1.8k pull-up",
         8: "UART TX", 10: "UART RX"}


def main():
    ap = argparse.ArgumentParser(description="turn each GPIO pin on for a moment, in turn")
    ap.add_argument("--seconds", type=float, default=1.0, help="time HIGH per pin (default 1)")
    ap.add_argument("--only", type=int, nargs="+", metavar="PIN",
                    help="physical pin numbers to walk (default: all)")
    args = ap.parse_args()

    pins = args.only or sorted(PHYSICAL_TO_BCM)
    bad = [p for p in pins if p not in PHYSICAL_TO_BCM]
    if bad:
        sys.exit(f"not GPIO pins: {bad}. Choose from {sorted(PHYSICAL_TO_BCM)}")

    print("ELEMENTS UNPLUGGED? smoker service stopped? Ctrl+C to stop.\n")
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    current = None
    passes = 0
    try:
        while True:
            passes += 1
            print(f"--- pass {passes} ---")
            for phys in pins:
                bcm = PHYSICAL_TO_BCM[phys]
                current = bcm
                note = f"  ({NOTES[phys]})" if phys in NOTES else ""
                print(f"pin {phys:2}  GPIO{bcm:<2}  HIGH{note}", flush=True)
                GPIO.setup(bcm, GPIO.OUT, initial=GPIO.HIGH)
                time.sleep(args.seconds)
                GPIO.output(bcm, GPIO.LOW)
                GPIO.setup(bcm, GPIO.IN)          # back to input: one output at a time
                current = None
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if current is not None:
            GPIO.output(current, GPIO.LOW)
        GPIO.cleanup()                            # every pin used back to input
        print("all pins released. Reboot before using SPI (the thermocouples) again.")


if __name__ == "__main__":
    main()
