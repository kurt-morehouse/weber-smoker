"""GPIO control of one solid state relay (SSR-25DA), by slow time-proportioning.

A duty of 0.3 with a 10 s window means the relay is on for the first 3 s of
every 10 s. On-times shorter than min_on_seconds are skipped, and off-times
that short are filled in, so the SSR isn't chattered.

Active-high: GPIO HIGH = SSR on. Pin numbers are BCM (GPIO numbers), not
physical header positions.

Failsafe: the control loop must call set_duty() at least every
failsafe_timeout_s. If it stops (hung, crashed, stuck on I/O), the driver's
own ticker thread turns the relay off by itself. The pin is also driven low
on close() and at interpreter exit.

simulate=True never touches GPIO; is_on still behaves the same, so the
simulator and tests can watch it.
"""
import atexit
import threading
import time

import hardware


class SSRDriver:
    def __init__(self, pin, window_s, min_on_s, failsafe_timeout_s, simulate=False,
                 gpio=None, clock=time.monotonic, threaded=True, tick_s=0.05):
        self.pin = pin
        self.window_s = window_s
        self.min_on_s = min_on_s
        self.failsafe_timeout_s = failsafe_timeout_s
        self.simulate = simulate
        self._clock = clock
        self._lock = threading.RLock()
        self._duty = 0.0
        self._refreshed = None          # never refreshed: off
        self._window_start = clock()
        self._on = False
        self.failsafe_tripped = False
        self._closed = False
        self._gpio = None
        if not simulate:
            self._gpio = gpio or hardware.load_gpio()
            self._gpio.setwarnings(False)
            self._gpio.setmode(self._gpio.BCM)
            self._gpio.setup(self.pin, self._gpio.OUT, initial=self._gpio.LOW)
        atexit.register(self.close)
        self._stop = threading.Event()
        self._thread = None
        if threaded:
            self._thread = threading.Thread(target=self._run, args=(tick_s,),
                                            name=f"ssr-gpio{pin}", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------ public
    @property
    def duty(self):
        return self._duty

    @property
    def is_on(self):
        return self._on

    def set_duty(self, duty):
        """Set the duty cycle 0..1 and re-arm the failsafe."""
        duty = min(1.0, max(0.0, float(duty)))
        with self._lock:
            if self._closed:
                return
            self._duty = duty
            self._refreshed = self._clock()
            self.failsafe_tripped = False
        self.update()

    def off(self):
        self.set_duty(0.0)

    def update(self, now=None):
        """Drive the pin for the current moment. The ticker thread calls this."""
        with self._lock:
            if self._closed:
                return
            now = self._clock() if now is None else now
            if self._refreshed is None or now - self._refreshed > self.failsafe_timeout_s:
                if self._refreshed is not None and self._duty > 0:
                    self.failsafe_tripped = True
                self._write(False)
                return
            on_time = self._duty * self.window_s
            if on_time < self.min_on_s:
                on_time = 0.0
            elif self.window_s - on_time < self.min_on_s:
                on_time = self.window_s
            elapsed = now - self._window_start
            if elapsed >= self.window_s:
                self._window_start += self.window_s * int(elapsed // self.window_s)
                elapsed = now - self._window_start
            self._write(elapsed < on_time)

    def close(self):
        """Drive the pin low and release it. Safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._write(False)
            self._closed = True
            self._duty = 0.0
            if self._gpio is not None:
                try:
                    self._gpio.cleanup(self.pin)
                except Exception:
                    pass
        self._stop.set()

    # ----------------------------------------------------------- private
    def _write(self, on):
        if self._gpio is not None:
            self._gpio.output(self.pin, self._gpio.HIGH if on else self._gpio.LOW)
        self._on = on

    def _run(self, tick_s):
        while not self._stop.wait(tick_s):
            try:
                self.update()
            except Exception:
                # Never leave the relay on because the ticker broke.
                try:
                    self._write(False)
                except Exception:
                    pass
