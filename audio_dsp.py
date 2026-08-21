"""
Lightweight audio DSP helpers for the Pi voice client.

Keeps hot mic-path math out of pi_assistant without splitting the app class.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

SAMPLE_RATE = 16000

try:
    from scipy.signal import lfilter as _lfilter
except ImportError:  # pragma: no cover - exercised when scipy is absent
    _lfilter = None


class Highpass:
    """2nd-order Butterworth-ish high-pass (RBJ biquad). No scipy required for coeffs."""

    __slots__ = ("_b", "_a", "_zi", "_b0", "_b1", "_b2", "_a1", "_a2", "_z1", "_z2")

    def __init__(self, rate: int, hz: float) -> None:
        w0 = 2.0 * math.pi * float(hz) / float(rate)
        cos_w0 = math.cos(w0)
        sin_w0 = math.sin(w0)
        alpha = sin_w0 / (2.0 * math.sqrt(0.5))
        b0 = (1.0 + cos_w0) * 0.5
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) * 0.5
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        self._b0 = b0 / a0
        self._b1 = b1 / a0
        self._b2 = b2 / a0
        self._a1 = a1 / a0
        self._a2 = a2 / a0
        # scipy.lfilter wants (b, a) with a[0] == 1.
        self._b = np.array([self._b0, self._b1, self._b2], dtype=np.float64)
        self._a = np.array([1.0, self._a1, self._a2], dtype=np.float64)
        self.reset()

    def reset(self) -> None:
        self._z1 = 0.0
        self._z2 = 0.0
        self._zi = np.zeros(2, dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        x64 = np.ascontiguousarray(x, dtype=np.float64).ravel()
        n = int(x64.size)
        if n == 0:
            return np.zeros(0, dtype=np.float32)

        if _lfilter is not None:
            # Same RBJ transfer function; scipy's C lfilter is the fast path on Pi.
            y64, self._zi = _lfilter(self._b, self._a, x64, zi=self._zi)
            return np.ascontiguousarray(y64, dtype=np.float32)

        return self._process_df2_chunked(x64)

    def _process_df2_chunked(self, x64: np.ndarray) -> np.ndarray:
        """
        Direct-Form II biquad in float64 chunks.

        Chunking keeps the inner loop tight (locals + contiguous buffer) and
        avoids per-sample Python enumerate/float() overhead from the old path.
        """
        n = int(x64.size)
        y = np.empty(n, dtype=np.float64)
        z1, z2 = self._z1, self._z2
        b0, b1, b2 = self._b0, self._b1, self._b2
        a1, a2 = self._a1, self._a2
        # ~5–10 ms of audio @ 16 kHz; balances branch cost vs cache.
        chunk = 256
        i = 0
        while i < n:
            j = i + chunk if i + chunk < n else n
            for k in range(i, j):
                w = x64[k] - a1 * z1 - a2 * z2
                y[k] = b0 * w + b1 * z1 + b2 * z2
                z2 = z1
                z1 = w
            i = j
        self._z1, self._z2 = z1, z2
        self._zi[0] = z1
        self._zi[1] = z2
        return np.ascontiguousarray(y, dtype=np.float32)


def resample_int(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Integer-ratio resample. 16 kHz ↔ 48 kHz is exactly ×3 / ÷3."""
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0 or src_rate == dst_rate:
        return x
    g = math.gcd(int(src_rate), int(dst_rate))
    up = int(dst_rate) // g
    down = int(src_rate) // g
    if down == 1:
        n = int(x.size)
        t_dst = np.linspace(0.0, n - 1, n * up, dtype=np.float64)
        return np.interp(t_dst, np.arange(n, dtype=np.float64), x).astype(np.float32)
    if up == 1:
        n = int(x.size) - (int(x.size) % down)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        return x[:n].reshape(-1, down).mean(axis=1).astype(np.float32)
    return resample_int(resample_int(x, src_rate, src_rate * up), src_rate * up, dst_rate)


def echo_gate_energy(
    window: Sequence[float],
    threshold: float,
    *,
    mult: float,
    percentile: float = 0.75,
    min_frames: int = 3,
    mult_floor: float = 1.05,
) -> float | None:
    """
    Energy a voice must beat to be heard over our own playback.

    Mid/upper percentile of bleed × mult, floored by ``threshold``.
    """
    if len(window) < max(3, min_frames):
        return None
    ordered = sorted(window)
    frac = min(0.95, max(0.5, float(percentile)))
    index = min(len(ordered) - 1, int(len(ordered) * frac))
    factor = max(float(mult_floor), float(mult))
    return max(float(threshold), ordered[index] * factor)
