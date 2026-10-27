"""
GPIO capability.

Uses gpiozero on a Raspberry Pi; anywhere else (a laptop, CI) it falls back to
an in-memory mock so the full action path can be exercised without hardware.
Each pin is declared in config: {"id", "kind": "digital"|"pwm", "label", ...}.
"""

import sys

try:
    from gpiozero import LED, AngularServo
    HAVE_GPIO = True
except Exception:  # noqa: BLE001 — not on a Pi, or lib missing
    HAVE_GPIO = False


class _MockPin:
    def __init__(self, pin, kind):
        self.pin = pin
        self.kind = kind
        self.value = False if kind == "digital" else 0

    def apply(self, value):
        self.value = value
        print(f"[gpio:mock] pin {self.pin} ({self.kind}) -> {value}")


class GpioController:
    def __init__(self, pins):
        self.kinds = {int(p["id"]): p.get("kind", "digital") for p in pins}
        self.meta = {int(p["id"]): p for p in pins}
        self.devices = {}  # pin id -> gpiozero device or _MockPin
        if not HAVE_GPIO:
            print("[gpio] gpiozero not available — running in MOCK mode", file=sys.stderr)

    def _device(self, pin):
        if pin in self.devices:
            return self.devices[pin]
        if pin not in self.kinds:
            raise ValueError(f"pin {pin} is not configured")
        kind = self.kinds[pin]
        if HAVE_GPIO:
            if kind == "pwm":
                m = self.meta[pin]
                dev = AngularServo(pin, min_angle=m.get("min_angle", -90), max_angle=m.get("max_angle", 90))
            else:
                dev = LED(pin)
        else:
            dev = _MockPin(pin, kind)
        self.devices[pin] = dev
        return dev

    def set(self, pin, value):
        kind = self.kinds.get(pin)
        if kind is None:
            raise ValueError(f"pin {pin} is not configured")
        dev = self._device(pin)

        if kind == "digital":
            on = bool(value)
            if HAVE_GPIO:
                dev.on() if on else dev.off()
            else:
                dev.apply(on)
            return {"pin": pin, "kind": kind, "value": on}

        if kind == "pwm":
            angle = float(value)
            if HAVE_GPIO:
                dev.angle = angle
            else:
                dev.apply(angle)
            return {"pin": pin, "kind": kind, "value": angle}

        raise ValueError(f"unknown pin kind: {kind}")

    def close(self):
        for dev in self.devices.values():
            close = getattr(dev, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        self.devices.clear()
