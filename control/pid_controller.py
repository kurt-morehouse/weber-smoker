"""PID logic, one instance per zone. Output is a relay duty cycle 0..1.

- Derivative on measurement, not error, so changing the setpoint doesn't
  kick the output.
- The integral term is clamped to +-integral_limit (its share of the duty)
  and stops growing while the output is saturated in the same direction
  (anti-windup), so a long warm-up doesn't overshoot.
- reset() clears the history (used when a zone faults or a cook starts).
"""


class PID:
    def __init__(self, kp, ki, kd, integral_limit=0.6, out_min=0.0, out_max=1.0):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.integral_limit = integral_limit
        self.out_min, self.out_max = out_min, out_max
        self.reset()

    def reset(self):
        self._integral = 0.0        # already multiplied by ki: duty units
        self._last_measurement = None
        self.last_terms = (0.0, 0.0, 0.0)

    def update(self, setpoint, measurement, dt):
        if dt <= 0:
            dt = 1e-3
        error = setpoint - measurement
        p = self.kp * error
        d = 0.0
        if self._last_measurement is not None:
            d = -self.kd * (measurement - self._last_measurement) / dt
        self._last_measurement = measurement

        candidate = self._integral + self.ki * error * dt
        candidate = max(-self.integral_limit, min(self.integral_limit, candidate))
        unclamped = p + candidate + d
        # Anti-windup: don't grow the integral further into saturation.
        if (unclamped > self.out_max and error > 0) or (unclamped < self.out_min and error < 0):
            candidate = self._integral
        self._integral = candidate

        out = p + self._integral + d
        self.last_terms = (p, self._integral, d)
        return max(self.out_min, min(self.out_max, out))
