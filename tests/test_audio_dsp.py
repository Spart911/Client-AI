"""Smoke tests for audio_dsp (no mic / hardware)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from audio_dsp import (
    MIC_RATE,
    SAMPLE_RATE,
    WAKE_RATE,
    Highpass,
    echo_gate_energy,
    resample_int,
)


def test_rate_constants():
    assert MIC_RATE == 48000
    assert WAKE_RATE == 16000
    assert SAMPLE_RATE == WAKE_RATE


def test_highpass_random_finite():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(SAMPLE_RATE).astype(np.float32)
    hp = Highpass(SAMPLE_RATE, 280.0)
    y = hp.process(x)
    assert y.shape == x.shape
    assert y.dtype == np.float32
    assert np.isfinite(y).all()


def test_highpass_at_mic_rate():
    rng = np.random.default_rng(2)
    x = rng.standard_normal(MIC_RATE).astype(np.float32)
    hp = Highpass(MIC_RATE, 280.0)
    y = hp.process(x)
    assert y.shape == x.shape
    assert np.isfinite(y).all()


def test_highpass_reduces_low_freq_sine_dc_energy():
    # 40 Hz sine ≪ 280 Hz cutoff → output energy should drop.
    n = SAMPLE_RATE * 2
    t = np.arange(n, dtype=np.float64) / float(SAMPLE_RATE)
    x = (0.5 * np.sin(2.0 * math.pi * 40.0 * t)).astype(np.float32)
    hp = Highpass(SAMPLE_RATE, 280.0)
    # Warm-up / settle transient, then measure steady-ish tail.
    y = hp.process(x)
    tail = slice(SAMPLE_RATE, None)
    assert float(np.mean(y[tail] ** 2)) < 0.25 * float(np.mean(x[tail] ** 2))
    # Mean near zero (DC blocked).
    assert abs(float(np.mean(y[tail]))) < 0.05


def test_resample_roundtrip_16_48():
    rng = np.random.default_rng(1)
    x = rng.standard_normal(4800).astype(np.float32) * 0.1
    up = resample_int(x, 16000, 48000)
    assert up.size == x.size * 3
    back = resample_int(up, 48000, 16000)
    assert back.size == x.size
    # Integer ×3 / ÷3 path is mean-decimation after linear upsample — rough match.
    err = float(np.sqrt(np.mean((back - x) ** 2)))
    assert err < 0.05
    assert np.isfinite(back).all()


def test_mic_frame_downsamples_to_wake_frame():
    """80 ms @ 48 kHz → 1280 samples @ 16 kHz (MWW window)."""
    rng = np.random.default_rng(3)
    mic_n = int(MIC_RATE * 0.08)
    wake_n = int(SAMPLE_RATE * 0.08)
    assert mic_n == 3840
    assert wake_n == 1280
    x = rng.standard_normal(mic_n).astype(np.float32) * 0.1
    y = resample_int(x, MIC_RATE, SAMPLE_RATE)
    assert y.size == wake_n
    assert np.isfinite(y).all()


def test_echo_gate_basics():
    assert echo_gate_energy([0.1, 0.2], 0.05, mult=1.2) is None
    gate = echo_gate_energy([0.1, 0.2, 0.3, 0.4], 0.05, mult=1.2, percentile=0.75)
    assert gate is not None
    # percentile 0.75 → index int(4*0.75)=3 → 0.4 * 1.2 = 0.48 > threshold
    assert gate == pytest.approx(0.48)
    # Floor by threshold when bleed is quiet.
    gate2 = echo_gate_energy([0.01, 0.01, 0.01], 0.2, mult=1.2)
    assert gate2 == pytest.approx(0.2)
    # mult_floor clamps low multipliers.
    gate3 = echo_gate_energy([0.1, 0.2, 0.3], 0.01, mult=0.5, mult_floor=1.05)
    ordered_index = int(3 * 0.75)  # 2 → 0.3
    assert gate3 == pytest.approx(0.3 * 1.05)
