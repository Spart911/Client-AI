"""
Raspberry Pi voice client: microWakeWord «Джарвис» → record → backend → play.

STT/LLM/TTS stay on the backend.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import io
import json
import logging
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import numpy as np
import sounddevice as sd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("pi-client")

SAMPLE_RATE = 16000
CHANNELS = 1
# 80 ms blocks @ 16 kHz — matches microWakeWord streaming windows.
FRAME_SAMPLES = 1280

# Below this the barge-in gate sits at the speaker bleed level and lets it through.
BARGE_MULT_MIN = 1.05
# Open speaker (A2DP + USB mic): TTS bleed is ~real speech RMS. Voice must
# clear a high percentile of that envelope, not the pre-play silence floor.
BARGE_TTS_GATE_MULT = 1.50
BARGE_TTS_PERCENTILE = 0.85
BARGE_FLOOR_FRAMES = 24
# Don't score wake until playback has a stable bleed floor.
BARGE_ARM_SEC = 0.50
# Soften the reply while we listen for an interrupt — leaves headroom for the
# mic at any system volume (bleed scales with the speakers, the user's voice does not).
BARGE_PLAYBACK_GAIN = 0.62
# Keep feeding the wake model this long after a burst dips under the gate.
BARGE_HANGOVER_FRAMES = 4
# Replay the frames just before the burst so the wake phrase keeps its onset.
BARGE_ONSET_FRAMES = 2
# Seconds to wait for the actual command after the reply was cut short.
BARGE_COMMAND_WAIT_SEC = 5.0
# After silence is detected, keep a short pad so word endings aren't clipped.
RECORD_END_PAD_SEC = 0.12
# Quiet tail after TTS so ALSA/Pulse underruns don't eat the last syllable.
TTS_END_PAD_SEC = 0.35
# While waiting for wake with music on: keep mpv quieter so speech
# can clear the mic without shouting (fraction of the user's 1–10 volume).
MUSIC_LISTEN_DUCK = 0.35
# Extra duck once a speech burst is heard over the music floor.
MUSIC_SPEECH_DUCK = 0.12
# Gate over music bleed (slightly softer than TTS barge-in).
MUSIC_GATE_MULT = 1.08
# Sentinel: music ended mid-listen — reopen InputStream after HFP reseat.
_REOPEN_MIC = object()

DEFAULT_WAKE_THRESHOLD = 0.90
DEFAULT_MWW_CONFIG = "/app/models/ru_jarvis_mww.json"
# Room «Джарвис» on the USB mic is ~0.006–0.013 RMS. Close-mic was ~0.02.
# Flat false accepts sat at ~0.0036. Score peaks after the word (MWW window).
DEFAULT_WAKE_ACCEPT_ENERGY = 0.006
# Peak in the recent window must also outrun the quiet floor.
WAKE_ENERGY_BURST_RATIO = 2.5
# Keep a speech burst alive while microWakeWord's score catches up (80 ms frames).
WAKE_ENERGY_HANGOVER = 16
# Idle listen: consecutive high-score frames while a recent burst is latched.
WAKE_STABLE_MIN = 3
# High-pass in front of RNNoise (Hz).
DEFAULT_NOISE_HP_HZ = 280.0


def _load_librnnoise() -> ctypes.CDLL:
    """Load Xiph librnnoise (Debian: librnnoise0)."""
    candidates = [
        ctypes.util.find_library("rnnoise"),
        "librnnoise.so.0",
        "librnnoise.so",
        "/usr/lib/arm-linux-gnueabihf/librnnoise.so.0",
        "/usr/lib/aarch64-linux-gnu/librnnoise.so.0",
        "/usr/lib/x86_64-linux-gnu/librnnoise.so.0",
    ]
    seen: set[str] = set()
    last_error: OSError | None = None
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            return ctypes.CDLL(name)
        except OSError as exc:
            last_error = exc
    raise FileNotFoundError(
        last_error or "librnnoise not found (install package librnnoise0)"
    )


class _Highpass:
    """2nd-order Butterworth-ish high-pass (RBJ biquad). No scipy."""

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
        self.reset()

    def reset(self) -> None:
        self._z1 = 0.0
        self._z2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        y = np.empty(x.size, dtype=np.float32)
        z1, z2 = self._z1, self._z2
        b0, b1, b2 = self._b0, self._b1, self._b2
        a1, a2 = self._a1, self._a2
        for i, sample in enumerate(x):
            w = float(sample) - a1 * z1 - a2 * z2
            y[i] = b0 * w + b1 * z1 + b2 * z2
            z2 = z1
            z1 = w
        self._z1, self._z2 = z1, z2
        return y


def _resample_int(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
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
    return _resample_int(_resample_int(x, src_rate, src_rate * up), src_rate * up, dst_rate)


class RnnoiseDenoise:
    """
    Xiph RNNoise on 10 ms / 48 kHz frames, with a light high-pass in front.

    Incoming 16 kHz float frames are resampled, denoised, then returned at
    16 kHz. State is kept across calls so the GRU sees a continuous stream.
    """

    FRAME = 480
    MODEL_RATE = 48000

    def __init__(self, rate: int = SAMPLE_RATE, hp_hz: float = DEFAULT_NOISE_HP_HZ) -> None:
        self.rate = int(rate)
        self.hp_hz = float(np.clip(hp_hz, 80.0, 800.0))
        self.label = f"RNNoise + highpass={self.hp_hz:.0f}Hz"
        self._hp = _Highpass(self.rate, self.hp_hz)
        self._buf48 = np.zeros(0, dtype=np.float32)
        self._pending16 = np.zeros(0, dtype=np.float32)
        self._lib = _load_librnnoise()
        self._lib.rnnoise_create.restype = ctypes.c_void_p
        self._lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        self._lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self._lib.rnnoise_process_frame.restype = ctypes.c_float
        self._lib.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]
        self._st = self._lib.rnnoise_create(None)
        if not self._st:
            raise RuntimeError("rnnoise_create failed")

    def reset(self) -> None:
        self._hp.reset()
        self._buf48 = np.zeros(0, dtype=np.float32)
        self._pending16 = np.zeros(0, dtype=np.float32)
        if self._st:
            self._lib.rnnoise_destroy(self._st)
        self._st = self._lib.rnnoise_create(None)
        if not self._st:
            raise RuntimeError("rnnoise_create failed")

    def close(self) -> None:
        if getattr(self, "_st", None):
            self._lib.rnnoise_destroy(self._st)
            self._st = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def process(self, frame: np.ndarray) -> np.ndarray:
        x = frame.astype(np.float32, copy=False)
        if x.size == 0:
            return x
        y = self._hp.process(x)
        x48 = _resample_int(y, self.rate, self.MODEL_RATE)
        self._buf48 = np.concatenate([self._buf48, x48]) if self._buf48.size else x48
        out48: list[np.ndarray] = []
        while self._buf48.size >= self.FRAME:
            chunk = np.ascontiguousarray(
                self._buf48[: self.FRAME] * 32768.0,
                dtype=np.float32,
            )
            self._buf48 = self._buf48[self.FRAME :]
            ptr = chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self._lib.rnnoise_process_frame(self._st, ptr, ptr)
            out48.append(chunk * (1.0 / 32768.0))
        if not out48:
            return np.clip(y, -1.0, 1.0)
        y48 = np.concatenate(out48)
        y16 = _resample_int(y48, self.MODEL_RATE, self.rate)
        if self._pending16.size:
            y16 = np.concatenate([self._pending16, y16])
        if y16.size >= x.size:
            self._pending16 = y16[x.size :]
            y16 = y16[: x.size]
        else:
            y16 = np.pad(y16, (0, x.size - y16.size))
            self._pending16 = np.zeros(0, dtype=np.float32)
        return np.clip(y16, -1.0, 1.0)


def create_mic_denoise(rate: int, hp_hz: float) -> RnnoiseDenoise:
    return RnnoiseDenoise(rate, hp_hz)


class VoiceClient:
    """Hands-free Raspberry Pi client for the voice-assistant backend."""

    def __init__(
        self,
        backend_url: str,
        wake_words: list[str] | None = None,
        silence_sec: float = 0.35,
        max_utterance_sec: float = 20.0,
        energy_threshold: float = 0.01,
        wake_threshold: float = DEFAULT_WAKE_THRESHOLD,
        mww_model_config: str = DEFAULT_MWW_CONFIG,
        wake_cooldown_sec: float = 2.0,
        wake_stable_frames: int = WAKE_STABLE_MIN,
        wake_accept_energy: float = DEFAULT_WAKE_ACCEPT_ENERGY,
        barge_in: bool = True,
        barge_energy_mult: float = 1.12,
        device_id: str = "default",
        music_poll: bool = True,
        music_poll_interval: float = 2.0,
        mpv_command: str = "",
        audio_device: str | int | None = None,
        noise_suppress: bool = True,
        noise_hp_hz: float = DEFAULT_NOISE_HP_HZ,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.device_id = (device_id or "default").strip() or "default"
        self.music_poll = music_poll
        self.music_poll_interval = max(0.5, music_poll_interval)
        self.mpv_command = (mpv_command or "").strip() or shutil.which("mpv") or "mpv"
        self.audio_device = audio_device
        self.noise_suppress = bool(noise_suppress)
        self._denoise: RnnoiseDenoise | None = None
        if self.noise_suppress:
            try:
                self._denoise = create_mic_denoise(SAMPLE_RATE, hp_hz=noise_hp_hz)
            except Exception:
                logger.warning("Mic denoise unavailable — continuing without it", exc_info=True)
                self.noise_suppress = False
        label = "Джарвис"
        if wake_words:
            normalized = [self._normalize(w) for w in wake_words if w.strip()]
            self.wake_words = list(dict.fromkeys(normalized)) or [label]
        else:
            self.wake_words = [label]
        self.wake_threshold = float(np.clip(wake_threshold, 0.05, 0.99))
        self.mww_model_config = (mww_model_config or DEFAULT_MWW_CONFIG).strip()
        self.silence_sec = silence_sec
        self.max_utterance_sec = max_utterance_sec
        self.energy_threshold = energy_threshold
        self.wake_cooldown_sec = wake_cooldown_sec
        # Consecutive 80 ms frames above score+energy before accept.
        self.wake_stable_frames = max(1, wake_stable_frames)
        self.wake_accept_energy = max(0.001, float(wake_accept_energy))
        self.barge_in = barge_in
        # Headroom over the loudest bleed frame of our own reply. Keep it small:
        # normal speech sits barely above the reply's stressed syllables.
        self.barge_energy_mult = max(BARGE_MULT_MIN, barge_energy_mult)
        if barge_energy_mult < BARGE_MULT_MIN:
            logger.warning(
                "BARGE_ENERGY_MULT=%.2f is too low (echo would pass) — using %.2f",
                barge_energy_mult,
                BARGE_MULT_MIN,
            )
        self._mww = None
        self._mww_features = None
        self._mww_wake_word = ""
        self._wake_mode = "none"
        self._last_wake_ts = 0.0
        self._music_proc: subprocess.Popen | None = None
        self._music_lock = threading.Lock()
        self._cue_lock = threading.Lock()
        self._music_stop = threading.Event()
        self._alert_stop = threading.Event()
        self._alert_cv = threading.Condition()
        self._alerts: list[dict] = []
        self._current_track_id = ""
        self._current_source = ""
        self._volume_level = 7  # 1..10
        self._mpv_ipc_path = Path(tempfile.gettempdir()) / f"voice-mpv-{os.getpid()}.sock"
        self._in_wake_listen = False
        # Reuse TCP/TLS across assist + music polls (saves handshake each turn).
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    def run(self) -> None:
        logger.info("Backend: %s", self.backend_url)
        logger.info("Device id: %s", self.device_id)
        logger.info("Wake threshold=%.2f config=%s", self.wake_threshold, self.mww_model_config)
        if self._denoise is not None:
            logger.info("Mic denoise: %s", self._denoise.label)
        else:
            logger.info("Mic denoise: off")
        self._configure_audio_device()
        # Bluetooth HFP SCO often arrives wedged after reboot/TTS — reseat before listen.
        if (os.getenv("BT_DEVICE_MAC") or os.getenv("BT_MAC") or "").strip():
            self._restore_hfp_audio()
        self._warmup_backend()
        self._init_wake()
        self._sync_volume_from_backend()
        if self.music_poll:
            self._start_music_poller()
        self._start_alert_scheduler()
        wake_label = self._mww_wake_word or (self.wake_words[0] if self.wake_words else "Джарвис")
        logger.info(
            "Wake mode: %s — say «%s …» then your command.",
            self._wake_mode,
            wake_label,
        )
        logger.info(
            "Barge-in: %s",
            (
                f"on (duck×{BARGE_PLAYBACK_GAIN:.2f}, energy×{self.barge_energy_mult:.2f})"
                if self.barge_in
                else "off"
            ),
        )

        while True:
            preroll = self._wait_for_wake()
            self._stop_music()
            self._last_wake_ts = time.monotonic()
            logger.info("Wake detected — capturing command")
            interrupted = False
            while True:
                wav_bytes = self._record_until_silence(
                    preroll=preroll,
                    require_speech=interrupted,
                    start_timeout_sec=BARGE_COMMAND_WAIT_SEC,
                    play_listen_cue=not interrupted,
                )
                if not wav_bytes:
                    if not interrupted:
                        logger.warning("Empty recording, back to wake listen")
                    break
                preroll = self._assist_and_play(wav_bytes)
                self._last_wake_ts = time.monotonic()
                interrupted = preroll is not None
                if preroll is None:
                    # Avoid immediate re-trigger from TTS / echo / leftover speech.
                    time.sleep(self.wake_cooldown_sec)
                    break
                logger.info("Barge-in — capturing new command")

    def _configure_audio_device(self) -> None:
        """Pick sounddevice input/output; log what PortAudio sees (Pulse/ALSA)."""
        self._ensure_usb_pulse()
        try:
            devices = sd.query_devices()
        except Exception:
            logger.exception("Cannot query audio devices")
            return

        for index, info in enumerate(devices):
            logger.info(
                "Audio[%d] in=%s out=%s name=%s rate=%.0f",
                index,
                info.get("max_input_channels", 0),
                info.get("max_output_channels", 0),
                info.get("name", "?"),
                info.get("default_samplerate", 0),
            )

        resolved = self._resolve_audio_device(self.audio_device, devices)
        if resolved is None:
            try:
                default_in, default_out = sd.default.device
                logger.info(
                    "Using PortAudio defaults in=%s out=%s",
                    default_in,
                    default_out,
                )
            except Exception:
                logger.info("Using PortAudio system defaults")
            return

        sd.default.device = resolved
        logger.info("AUDIO_INPUT_DEVICE resolved to %s", resolved)

    @staticmethod
    def _resolve_audio_device(
        raw: str | int | None,
        devices,
    ) -> int | tuple[int | None, int | None] | None:
        if raw is None or raw == "":
            return None
        if isinstance(raw, int):
            return raw
        text = str(raw).strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)

        if "," in text:
            left, right = text.split(",", 1)
            return (
                VoiceClient._match_device(left, devices, want_input=True),
                VoiceClient._match_device(right, devices, want_input=False),
            )

        needle = text.lower()
        input_match = None
        output_match = None
        for index, info in enumerate(devices):
            name = str(info.get("name", "")).lower()
            if needle not in name:
                continue
            if info.get("max_input_channels", 0) > 0 and input_match is None:
                input_match = index
            if info.get("max_output_channels", 0) > 0 and output_match is None:
                output_match = index
        if input_match is None and output_match is None:
            logger.warning("AUDIO_INPUT_DEVICE=%r matched no devices", text)
            return None
        return (input_match, output_match)

    @staticmethod
    def _match_device(needle: str, devices, *, want_input: bool) -> int | None:
        text = needle.strip().lower()
        if not text:
            return None
        if text.isdigit():
            idx = int(text)
            return idx if 0 <= idx < len(devices) else None
        for index, info in enumerate(devices):
            name = str(info.get("name", "")).lower()
            if text not in name:
                continue
            key = "max_input_channels" if want_input else "max_output_channels"
            if info.get(key, 0) > 0:
                return index
        return None

    def _ensure_usb_pulse(self) -> None:
        """Keep USB mic on Pulse analog-mono (needed after a previous 'off')."""
        if not shutil.which("pactl"):
            return
        try:
            cards = subprocess.check_output(
                ["pactl", "list", "cards", "short"],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return
        for line in cards.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            lowered = name.lower()
            if "usb" not in lowered:
                continue
            if not any(token in lowered for token in ("c-media", "pnp_sound", "pcm2902")):
                continue
            result = subprocess.run(
                ["pactl", "set-card-profile", name, "input:analog-mono"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if result.returncode == 0:
                logger.info("Pulse USB card %s → analog-mono", name)
                usb_src = (os.getenv("PULSE_DEFAULT_SOURCE") or "").strip()
                if usb_src:
                    subprocess.run(
                        ["pactl", "set-default-source", usb_src],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    mic_vol = (os.getenv("USB_MIC_VOLUME") or "100%").strip()
                    if mic_vol:
                        subprocess.run(
                            ["pactl", "set-source-volume", usb_src, mic_vol],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=3,
                        )
                        logger.info("USB mic volume %s → %s", usb_src, mic_vol)

    def _warmup_backend(self) -> None:
        try:
            response = self._http.get(f"{self.backend_url}/health", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            logger.error("Backend not reachable at %s: %s", self.backend_url, exc)
            sys.exit(1)

    def _init_wake(self) -> None:
        self._init_mww()

    def _init_mww(self) -> None:
        try:
            from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
        except Exception as exc:
            logger.error(
                "microWakeWord import failed (%s). Install: pip install pymicro-wakeword",
                exc,
            )
            sys.exit(1)

        if not self.mww_model_config:
            logger.error(
                "MWW_MODEL_CONFIG is empty. Set path to *_mww.json from microwakeword-trainer."
            )
            sys.exit(1)

        config_path = Path(self.mww_model_config)
        if not config_path.is_file():
            logger.error("MWW model config not found: %s", config_path)
            sys.exit(1)

        try:
            self._mww = MicroWakeWord.from_config(config_path)
            self._mww_features = MicroWakeWordFeatures()
            self._mww_wake_word = getattr(self._mww, "wake_word", "mww")
            # Trainer default is cutoff 0.9 over 10 frames — too strict for a
            # room USB mic. Override both from env.
            self._mww.probability_cutoff = self.wake_threshold
            window = max(1, int(os.getenv("MWW_WINDOW", "10")))
            self._mww.sliding_window_size = window
            self._mww._probabilities = deque(maxlen=window)
            self._wake_mode = "mww"
            logger.info(
                "microWakeWord ready: wake=%s cutoff=%.2f window=%d config=%s",
                self._mww_wake_word,
                self._mww.probability_cutoff,
                window,
                config_path,
            )
        except Exception as exc:
            logger.error("microWakeWord init failed: %s", exc)
            sys.exit(1)

    def _wait_for_wake(self) -> np.ndarray | None:
        if self._wake_mode != "mww":
            raise RuntimeError("Wake engine is not initialized")
        self._in_wake_listen = True
        try:
            # echo_gate starts False; sync_music_duck toggles it when mpv starts.
            # Reopen the mic stream after music ends so HFP SCO is not stale.
            while True:
                result = self._wait_mww(
                    echo_gate=False,
                    music_duck=True,
                )
                if result is _REOPEN_MIC:
                    logger.info("Reopening mic after music (HFP reseat)")
                    continue
                return result
        finally:
            self._in_wake_listen = False

    def _wait_mww(
        self,
        *,
        deadline: float | None = None,
        energy_threshold: float | None = None,
        respect_cooldown: bool = True,
        echo_gate: bool = False,
        stable_frames: int | None = None,
        music_duck: bool = False,
        barge_soft: bool = False,
        arm_sec: float = 0.0,
    ) -> np.ndarray | None:
        from collections import deque

        assert self._mww is not None
        assert self._mww_features is not None
        threshold = self.energy_threshold if energy_threshold is None else energy_threshold
        if stable_frames is None:
            stable_needed = max(WAKE_STABLE_MIN, self.wake_stable_frames)
        else:
            stable_needed = max(1, stable_frames)
        score_limit = float(self._mww.probability_cutoff)

        block = FRAME_SAMPLES
        q: queue.Queue[np.ndarray] = queue.Queue()
        stable = 0
        use_gate = bool(echo_gate)
        music_mode = False
        speech_ducked = False
        reopen_after_music = False
        # Idle: keep ~2.8s so «Джарвис какая погода» keeps the command tail.
        preroll: deque[np.ndarray] = deque(maxlen=4 if use_gate else 35)
        floor_len = BARGE_FLOOR_FRAMES if use_gate and not barge_soft else 16
        floor_window: deque[float] = deque(maxlen=floor_len)
        listen_started = time.monotonic()
        open_echo = self._open_speaker_echo()
        pre_burst_f: deque[np.ndarray] = deque(maxlen=BARGE_ONSET_FRAMES)
        feeding = False
        quiet_frames = 0
        gate_logged = False
        last_heartbeat = 0.0
        last_score_log = 0.0
        last_hold_log = 0.0
        energy_hangover = 0
        latched_peak = 0.0
        peak_energy = 0.0
        peak_score = 0.0
        frames_seen = 0
        recent_energy: deque[float] = deque(maxlen=24)

        def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
            if status:
                logger.warning("mic status: %s", status)
            q.put(indata.copy())

        def sync_music_duck() -> None:
            nonlocal use_gate, music_mode, speech_ducked, preroll, floor_window
            nonlocal stable, feeding, quiet_frames, reopen_after_music
            nonlocal energy_hangover, latched_peak
            if not music_duck:
                return
            playing = self._is_music_playing()
            if playing and not music_mode:
                music_mode = True
                use_gate = True
                preroll = deque(preroll, maxlen=4)
                floor_window = deque(
                    maxlen=BARGE_FLOOR_FRAMES if open_echo else 16
                )
                pre_burst_f.clear()
                feeding = False
                quiet_frames = 0
                speech_ducked = False
                stable = 0
                energy_hangover = 0
                latched_peak = 0.0
                self._mww.reset()
                self._mww_features.reset()
                self._duck_music(MUSIC_LISTEN_DUCK)
                logger.info(
                    "Music on — duck×%.2f + %s",
                    MUSIC_LISTEN_DUCK,
                    "open-echo gate (don't score speaker bleed)"
                    if open_echo
                    else "soft wake scoring over bleed",
                )
            elif not playing and music_mode:
                music_mode = False
                speech_ducked = False
                use_gate = False
                preroll = deque(preroll, maxlen=35)
                floor_window.clear()
                pre_burst_f.clear()
                feeding = False
                quiet_frames = 0
                stable = 0
                energy_hangover = 0
                latched_peak = 0.0
                self._mww.reset()
                self._mww_features.reset()
                # Always reseat HFP after mpv — SCO often dies like after TTS.
                self._restore_hfp_audio()
                reopen_after_music = True
                logger.info("Music off — cleared wake gate, restored HFP")

        def set_feeding(active: bool) -> None:
            nonlocal feeding, quiet_frames, speech_ducked, stable
            if active == feeding:
                return
            feeding = active
            quiet_frames = 0
            if active:
                self._mww.reset()
                self._mww_features.reset()
                stable = 0
            if not music_mode:
                return
            if active:
                self._duck_music(MUSIC_SPEECH_DUCK)
                speech_ducked = True
            elif speech_ducked:
                self._duck_music(MUSIC_LISTEN_DUCK)
                speech_ducked = False

        logger.info(
            "Listening for microWakeWord %s (score≥%.2f, stable≥%d, window=%d, energy≥%.3f%s)",
            self._mww_wake_word or "mww",
            score_limit,
            stable_needed,
            int(getattr(self._mww, "sliding_window_size", 1)),
            self.wake_accept_energy,
            ", open-echo" if (use_gate and open_echo and not barge_soft) else (
                ", barge-soft" if barge_soft else ""
            ),
        )

        try:
            self._mww.reset()
            self._mww_features.reset()
            if self._denoise is not None:
                self._denoise.reset()
            sync_music_duck()
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=block,
                callback=callback,
            ):
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    sync_music_duck()
                    if reopen_after_music:
                        return _REOPEN_MIC
                    try:
                        chunk = q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    mono = chunk.reshape(-1).astype(np.int16, copy=False)
                    frame_f = mono.astype(np.float32) / 32768.0
                    peak = float(np.max(np.abs(frame_f)) or 0.0)
                    if peak > 0.35:
                        frame_f = frame_f * (0.35 / peak)
                    if self._denoise is not None:
                        frame_f = self._denoise.process(frame_f)
                        mono = np.clip(frame_f * 32768.0, -32768, 32767).astype(np.int16)
                    else:
                        mono = np.clip(frame_f * 32768.0, -32768, 32767).astype(np.int16)
                    frame_energy = self._rms(frame_f)
                    frames_seen += 1
                    peak_energy = max(peak_energy, frame_energy)
                    recent_energy.append(frame_energy)

                    if use_gate:
                        # HFP headset: weak bleed, score under the gate.
                        # Open speaker: TTS/music in the room IS the floor —
                        # never treat that envelope as a barge-in.
                        track_playback = (not barge_soft) and (
                            use_gate and (open_echo or not music_mode)
                        )
                        if barge_soft:
                            gate_mult = BARGE_MULT_MIN
                            gate_pct = 0.75
                            gate_min = 3
                        elif music_mode and not open_echo:
                            gate_mult = MUSIC_GATE_MULT
                            gate_pct = 0.75
                            gate_min = 3
                        else:
                            gate_mult = BARGE_TTS_GATE_MULT
                            gate_pct = BARGE_TTS_PERCENTILE
                            gate_min = 8
                        if track_playback:
                            floor_window.append(frame_energy)
                        gate = self._echo_gate(
                            floor_window,
                            threshold,
                            mult=gate_mult,
                            percentile=gate_pct,
                            min_frames=gate_min,
                        )
                        armed_yet = (
                            arm_sec <= 0.0
                            or (time.monotonic() - listen_started) >= arm_sec
                        )
                        loud = (
                            armed_yet
                            and gate is not None
                            and frame_energy >= gate
                        )
                        if not track_playback and not loud and not feeding:
                            floor_window.append(frame_energy)
                        if loud:
                            quiet_frames = 0
                            if not feeding:
                                set_feeding(True)
                                for onset in pre_burst_f:
                                    preroll.append(onset)
                                pre_burst_f.clear()
                                if not gate_logged:
                                    logger.info(
                                        "Speech gate %.3f — heard %.3f over playback",
                                        gate,
                                        frame_energy,
                                    )
                                    gate_logged = True
                        elif feeding:
                            quiet_frames += 1
                            if quiet_frames > BARGE_HANGOVER_FRAMES:
                                set_feeding(False)
                                stable = 0
                        if not feeding:
                            pre_burst_f.append(frame_f.copy())
                            score_under_gate = barge_soft or (
                                music_mode and not open_echo
                            )
                            if not score_under_gate:
                                continue
                        preroll.append(frame_f.copy())
                    else:
                        preroll.append(frame_f.copy())

                    if (
                        respect_cooldown
                        and time.monotonic() - self._last_wake_ts < self.wake_cooldown_sec
                    ):
                        # Keep streaming state warm while cooldown is active.
                        for feat in self._mww_features.process_streaming(mono.tobytes()):
                            _ = self._mww.process_streaming_prob(feat)
                        stable = 0
                        continue

                    best_score = 0.0
                    for feat in self._mww_features.process_streaming(mono.tobytes()):
                        prob = self._mww.process_streaming_prob(feat)
                        if prob is None:
                            continue
                        best_score = max(best_score, float(prob))
                    peak_score = max(peak_score, best_score)

                    now = time.monotonic()
                    accept_energy = self.wake_accept_energy
                    if barge_soft or (music_mode and not open_echo):
                        accept_energy = max(0.004, accept_energy * 0.5)
                    burst_ok, burst_peak, burst_floor = self._wake_burst_ok(
                        recent_energy, accept_energy
                    )
                    if (
                        use_gate
                        and not barge_soft
                        and floor_window
                        and burst_ok
                    ):
                        play_floor = sorted(floor_window)[len(floor_window) // 2]
                        if play_floor > 1e-6 and burst_peak < play_floor * BARGE_TTS_GATE_MULT:
                            burst_ok = False
                            burst_floor = play_floor
                    if burst_ok and frame_energy >= accept_energy:
                        energy_hangover = WAKE_ENERGY_HANGOVER
                        latched_peak = max(latched_peak, burst_peak, frame_energy)
                    elif energy_hangover > 0:
                        energy_hangover -= 1
                        if energy_hangover == 0:
                            latched_peak = 0.0
                    energy_ok = energy_hangover > 0
                    armed = arm_sec <= 0.0 or (now - listen_started) >= arm_sec
                    score_ok = best_score >= score_limit and energy_ok and armed

                    if best_score >= 0.08 and (
                        abs(best_score - last_score_log) >= 0.04
                    ):
                        logger.info(
                            "MWW score %.3f energy=%.4f "
                            "(need score≥%.2f energy≥%.3f)%s",
                            best_score,
                            latched_peak if energy_ok else burst_peak,
                            score_limit,
                            accept_energy,
                            " [music]" if music_mode else (
                                " [barge-soft]" if barge_soft else ""
                            ),
                        )
                        last_score_log = best_score

                    if now - last_heartbeat >= 5.0:
                        logger.info(
                            "Mic heartbeat: rms=%.5f peak_rms=%.5f mww=%.3f peak_score=%.3f frames=%d%s",
                            frame_energy,
                            peak_energy,
                            best_score,
                            peak_score,
                            frames_seen,
                            " [music]" if music_mode else "",
                        )
                        last_heartbeat = now
                        peak_energy = 0.0
                        peak_score = 0.0

                    if best_score >= score_limit and not energy_ok and armed:
                        if now - last_hold_log >= 0.4:
                            logger.info(
                                "MWW hold %.3f energy=%.4f floor=%.4f "
                                "(need energy≥%.3f and ≥%.1f×floor)",
                                best_score,
                                burst_peak,
                                burst_floor,
                                accept_energy,
                                WAKE_ENERGY_BURST_RATIO,
                            )
                            last_hold_log = now
                    if score_ok:
                        stable += 1
                    else:
                        stable = 0

                    if stable >= stable_needed:
                        logger.info(
                            "Wake matched (mww): %s score=%.3f energy=%.3f%s",
                            self._mww_wake_word or "mww",
                            best_score,
                            max(latched_peak, burst_peak),
                            " (music)" if music_mode else (
                                " (barge)" if use_gate else ""
                            ),
                        )
                        return self._preroll_array(preroll)
        finally:
            if music_duck and music_mode:
                self._restore_music_volume()

    def _preroll_array(self, preroll) -> np.ndarray | None:
        if not preroll:
            return None
        return np.concatenate(list(preroll))

    def _echo_gate(
        self,
        window,
        threshold: float,
        *,
        mult: float | None = None,
        percentile: float = 0.75,
        min_frames: int = 3,
    ) -> float | None:
        """
        Energy a voice must beat to be heard over our own playback.

        Use a mid/upper percentile of the bleed, not the absolute peak: the
        reply's stressed syllables already sit near the top of the window, and
        a peak×margin gate rises with the speaker volume until a normal voice
        can no longer clear it. Playback is also ducked while we listen.
        """
        if len(window) < max(3, min_frames):
            return None
        ordered = sorted(window)
        frac = min(0.95, max(0.5, float(percentile)))
        index = min(len(ordered) - 1, int(len(ordered) * frac))
        factor = self.barge_energy_mult if mult is None else max(BARGE_MULT_MIN, mult)
        return max(threshold, ordered[index] * factor)

    def _assist_and_play(self, wav_bytes: bytes) -> np.ndarray | None:
        """Send audio, play the reply; return barge-in preroll when interrupted."""
        t0 = time.monotonic()
        logger.info("Sending utterance to backend (%d bytes)", len(wav_bytes))
        # Pause music polling so the Pi isn't competing for CPU/network mid-assist.
        music_was = self.music_poll
        self.music_poll = False
        try:
            files = {"file": ("command.wav", wav_bytes, "audio/wav")}
            data = {
                "return_audio": "true",
                "device_id": self.device_id,
            }
            # Stream so we can split server wait (TTFB) from body download.
            with self._http.stream(
                "POST",
                f"{self.backend_url}/v1/assist",
                files=files,
                data=data,
                timeout=300.0,
            ) as response:
                t_headers = time.monotonic()
                if response.status_code >= 400:
                    err_body = response.read()
                    logger.error(
                        "Assist failed: %s %s",
                        response.status_code,
                        err_body[:500],
                    )
                    return None
                content_type = response.headers.get("content-type") or ""
                headers = dict(response.headers)
                body = response.read()
                t_body = time.monotonic()

            body_len = len(body)
            transcript, reply, audio, playback = self._parse_assist_body(
                body,
                content_type=content_type,
                headers=headers,
            )
            t_parse = time.monotonic()
            ttfb = t_headers - t0
            download = t_body - t_headers
            parse = t_parse - t_body
            kbps = (body_len / 1024.0 / download) if download > 0.01 else 0.0
            logger.info(
                "Assist timing: ttfb=%.1fs download=%.1fs (%.0f KiB/s) "
                "parse=%.1fs body=%dB audio=%dB reply_chars=%d",
                ttfb,
                download,
                kbps,
                parse,
                body_len,
                len(audio),
                len(reply),
            )
            if transcript:
                logger.info("You: %s", transcript)
            if reply:
                logger.info("OWUI: %s", reply)
            if audio:
                preroll = self._play_wav(audio, reply_text=reply)
            else:
                logger.error("Assist response has no audio payload")
                preroll = None
            if playback:
                self._handle_playback(playback)
            logger.info(
                "Assist total=%.1fs (play included)",
                time.monotonic() - t0,
            )
            return preroll
        finally:
            self.music_poll = music_was

    def _parse_assist_body(
        self,
        body: bytes,
        content_type: str,
        headers: dict[str, str],
    ) -> tuple[str, str, bytes, dict | None]:
        """Parse assist payload from an already-downloaded body."""
        if "application/json" in content_type.lower():
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                logger.exception("Failed to parse assist JSON")
                return "", "", b"", None
            transcript = str(payload.get("transcript") or "")
            reply = str(payload.get("reply") or "")
            audio_b64 = payload.get("audio_base64") or ""
            audio = base64.b64decode(audio_b64) if audio_b64 else b""
            playback = payload.get("playback")
            if isinstance(playback, dict):
                return transcript, reply, audio, playback
            return transcript, reply, audio, None

        transcript = self._header_map_text(headers, "Transcript")
        reply = self._header_map_text(headers, "Reply")
        return transcript, reply, body, None

    def _header_map_text(self, headers: dict[str, str], name: str) -> str:
        """Decode UTF-8 text from X-{name}-B64 (fallback to legacy X-{name})."""
        # httpx lowercases header names; accept either form.
        b64 = headers.get(f"X-{name}-B64") or headers.get(f"x-{name.lower()}-b64") or ""
        if b64:
            try:
                return base64.b64decode(b64.encode("ascii")).decode("utf-8")
            except Exception:
                logger.warning("Failed to decode X-%s-B64", name)
        return headers.get(f"X-{name}") or headers.get(f"x-{name.lower()}") or ""

    def _header_text(self, response: httpx.Response, name: str) -> str:
        """Decode UTF-8 text from X-{name}-B64 (fallback to legacy X-{name})."""
        return self._header_map_text(dict(response.headers), name)

    def _record_until_silence(
        self,
        preroll: np.ndarray | None = None,
        require_speech: bool = False,
        start_timeout_sec: float | None = None,
        play_listen_cue: bool = False,
    ) -> bytes:
        """
        Record one utterance: preroll + mic until silence_sec of quiet.

        Preroll is kept for overlap («Джарвис какая погода»), but it must not
        start the end-of-speech clock — otherwise the listen ding is still
        playing while we already send.

        require_speech waits for fresh speech instead of trusting the preroll —
        after a barge-in the preroll is loud from our own reply.
        """
        block = int(SAMPLE_RATE * 0.1)
        silence_blocks = max(1, int(self.silence_sec / 0.1))
        max_blocks = int(self.max_utterance_sec / 0.1)
        wait_sec = start_timeout_sec or self.max_utterance_sec
        if play_listen_cue:
            wait_sec = max(float(wait_sec), 8.0)
        wait_blocks = int(wait_sec / 0.1)
        continue_threshold = self.energy_threshold * 0.4
        lead: list[np.ndarray] = []
        frames: list[np.ndarray] = []
        silent = 0
        heard_live = False
        listen_thread: threading.Thread | None = None
        listen_done = threading.Event()
        listen_done.set()

        if preroll is not None and preroll.size:
            lead.append(preroll.astype(np.float32, copy=False))

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=block,
        ) as stream:
            if play_listen_cue:
                # Capture-open glitch on A2DP: a short settle, then ding.
                time.sleep(0.12)
                listen_done.clear()

                def _run_listen_cue() -> None:
                    try:
                        self._play_listen_cue()
                    finally:
                        listen_done.set()

                listen_thread = threading.Thread(
                    target=_run_listen_cue,
                    name="listen-cue",
                    daemon=True,
                )
                listen_thread.start()
            for index in range(max_blocks):
                data, _ = stream.read(block)
                mono = data[:, 0]
                if self._denoise is not None:
                    mono = self._denoise.process(mono)
                energy = self._rms(mono)
                if energy >= self.energy_threshold:
                    heard_live = True
                    silent = 0
                    frames.append(mono.copy())
                elif heard_live:
                    frames.append(mono.copy())
                    if energy >= continue_threshold:
                        silent = max(0, silent - 1)
                    else:
                        silent += 1
                        # Wait until the listen ding finished, otherwise the
                        # user is still waiting to speak and we ship "Jarvis".
                        if silent >= silence_blocks and listen_done.is_set():
                            break
                else:
                    frames.append(mono.copy())
                    if len(frames) > 5:
                        frames.pop(0)
                    if listen_done.is_set() and index >= wait_blocks:
                        break

            if heard_live:
                pad_blocks = max(1, int(RECORD_END_PAD_SEC / 0.1))
                for _ in range(pad_blocks):
                    data, _ = stream.read(block)
                    frames.append(data[:, 0].copy())

        if listen_thread is not None:
            listen_thread.join(timeout=3.0)

        if heard_live:
            threading.Thread(
                target=self._play_sent_cue,
                name="sent-cue",
                daemon=True,
            ).start()

        if not heard_live:
            logger.info(
                "No command after %s — back to wake listen",
                "barge-in" if require_speech else "wake",
            )
            return b""
        chunks = lead + frames
        if not chunks:
            return b""
        audio = self._trim_utterance(np.concatenate(chunks))
        if audio.size == 0:
            return b""
        return self._frames_to_wav(audio)

    def _pulse_default_sink(self) -> str:
        try:
            return subprocess.check_output(
                ["pactl", "get-default-sink"],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            return ""

    def _play_chime_array(
        self,
        audio: np.ndarray,
        *,
        drain_sec: float = 0.0,
        audio_buffer: float = 0.2,
    ) -> None:
        """Play a short chime. Prefer mpv (already used for TTS/music on A2DP)."""
        audio = (audio * self._assistant_gain()).astype(np.float32, copy=False)
        duration = float(np.asarray(audio).shape[0]) / float(SAMPLE_RATE)
        fd, tmp_name = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        path = Path(tmp_name)
        try:
            self._write_wav_pcm(path, audio, SAMPLE_RATE)
            env = os.environ.copy()
            env.setdefault("PULSE_LATENCY_MSEC", "200")
            mpv = (self.mpv_command or "").strip() or shutil.which("mpv")
            timeout = max(8.0, duration + drain_sec + 3.0)
            buf = max(0.15, float(audio_buffer))
            sink = self._pulse_default_sink()
            if mpv:
                cmd = [
                    mpv,
                    "--no-video",
                    "--really-quiet",
                    "--no-terminal",
                    f"--audio-buffer={buf:.2f}",
                    "--volume=100",
                ]
                if sink:
                    cmd.append(f"--audio-device=pulse/{sink}")
                cmd.append(str(path))
                result = subprocess.run(
                    cmd,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    env=env,
                )
                if result.returncode != 0:
                    logger.warning("mpv chime failed rc=%s", result.returncode)
                if drain_sec > 0:
                    time.sleep(drain_sec)
                return
            paplay = shutil.which("paplay")
            if paplay:
                cmd = [paplay]
                sink = self._pulse_default_sink()
                if sink:
                    cmd.extend(["--device", sink])
                cmd.append(str(path))
                subprocess.run(
                    cmd,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    env=env,
                )
                if drain_sec > 0:
                    time.sleep(drain_sec)
                return
            sd.play(audio, SAMPLE_RATE, blocking=True)
            if drain_sec > 0:
                time.sleep(drain_sec)
        except Exception:
            logger.warning("chime playback failed", exc_info=True)
        finally:
            path.unlink(missing_ok=True)

    def _play_cue(
        self,
        audio: np.ndarray,
        *,
        pre_sec: float,
        post_sec: float,
        label: str,
    ) -> None:
        try:
            logger.info(label)
            rate = SAMPLE_RATE
            primed = np.concatenate(
                [
                    np.zeros(max(0, int(rate * pre_sec)), dtype=np.float32),
                    audio,
                    np.zeros(max(0, int(rate * post_sec)), dtype=np.float32),
                ]
            )
            with self._cue_lock:
                self._play_chime_array(primed, drain_sec=0.02, audio_buffer=0.18)
        except Exception:
            logger.warning("%s playback failed", label, exc_info=True)

    def _play_listen_cue(self) -> None:
        self._play_cue(
            self._make_chime(SAMPLE_RATE, 784.0, 0.11, 1046.5, 0.16),
            pre_sec=0.05,
            post_sec=0.18,
            label="Listen cue",
        )

    def _play_sent_cue(self) -> None:
        self._play_cue(
            self._make_chime(SAMPLE_RATE, 880.0, 0.09, 659.3, 0.14, amplitude=0.20, harm=0.12),
            pre_sec=0.04,
            post_sec=0.16,
            label="Sent cue",
        )

    @staticmethod
    def _tone(
        rate: int,
        freq: float,
        duration: float,
        amplitude: float = 0.22,
        attack: float = 0.02,
        release: float = 0.08,
        harm: float = 0.18,
    ) -> np.ndarray:
        n = max(1, int(rate * duration))
        t = np.arange(n, dtype=np.float32) / float(rate)
        att = min(n, int(rate * attack))
        rel = min(n, int(rate * release))
        env = np.ones(n, dtype=np.float32)
        if att > 0:
            env[:att] = np.linspace(0.0, 1.0, att, dtype=np.float32)
        if rel > 0:
            env[-rel:] = np.linspace(1.0, 0.0, rel, dtype=np.float32)
        wave = np.sin(2.0 * np.pi * freq * t) * env * amplitude
        wave += np.sin(4.0 * np.pi * freq * t) * env * (amplitude * harm)
        return wave.astype(np.float32)

    @classmethod
    def _make_chime(
        cls,
        rate: int,
        f1: float,
        d1: float,
        f2: float,
        d2: float,
        *,
        amplitude: float = 0.22,
        harm: float = 0.18,
    ) -> np.ndarray:
        gap = np.zeros(int(rate * 0.04), dtype=np.float32)
        chime = np.concatenate(
            [
                cls._tone(rate, f1, d1, amplitude=amplitude, harm=harm),
                gap,
                cls._tone(rate, f2, d2, amplitude=amplitude, harm=harm),
            ]
        )
        peak = float(np.max(np.abs(chime)) or 1.0)
        if peak > 0.35:
            chime *= 0.35 / peak
        return chime

    def _start_music_poller(self) -> None:
        thread = threading.Thread(
            target=self._music_poll_loop,
            name="music-poller",
            daemon=True,
        )
        thread.start()
        logger.info(
            "Music poller enabled (interval=%.1fs, mpv=%s)",
            self.music_poll_interval,
            self.mpv_command,
        )

    def _music_poll_loop(self) -> None:
        url = f"{self.backend_url}/v1/music/pending/{self.device_id}"
        while not self._music_stop.is_set():
            if not self.music_poll:
                self._music_stop.wait(self.music_poll_interval)
                continue
            try:
                response = self._http.get(url, timeout=30.0)
                if response.status_code < 400:
                    payload = response.json()
                    for command in payload.get("commands") or []:
                        if isinstance(command, dict):
                            self._handle_playback(command)
            except Exception as exc:
                logger.debug("Music poll failed: %s", exc)
            self._music_stop.wait(self.music_poll_interval)

    def _sync_volume_from_backend(self) -> None:
        """Pull last known volume from server; fall back to local default."""
        url = f"{self.backend_url}/v1/music/status/{self.device_id}"
        try:
            response = self._http.get(url, timeout=10.0)
            if response.status_code < 400:
                payload = response.json() or {}
                level = int(payload.get("volume") or self._volume_level)
                self._set_volume(level)
                return
        except Exception as exc:
            logger.debug("Volume sync failed: %s", exc)
        self._apply_pulse_volume(self._volume_to_percent(self._volume_level))

    def _handle_playback(self, command: dict) -> None:
        action = str(command.get("action") or "").strip().lower()
        stream_url = str(command.get("url") or "").strip()
        if action in ("timer", "alarm", "cancel_alert", "cancel_timer", "cancel_alarm") or stream_url.startswith(
            "pi-alert://"
        ):
            self._handle_alert_command(command)
            return
        if action == "play":
            title = str(command.get("title") or "").strip()
            artist = str(command.get("artist") or "").strip()
            track_id = str(command.get("track_id") or "").strip()
            source = str(command.get("source") or "").strip()
            if not stream_url:
                logger.error("Playback command missing stream URL")
                return
            label = f"{artist} — {title}".strip(" —") or title or stream_url
            logger.info("Playing music: %s", label)
            self._start_music(stream_url, track_id=track_id, source=source)
            return
        if action == "pause":
            self._pause_music()
            return
        if action == "stop":
            self._stop_music()
            return
        if action == "volume":
            try:
                level = int(command.get("volume") or 0)
            except (TypeError, ValueError):
                level = 0
            if level:
                self._set_volume(level)

    @staticmethod
    def _volume_to_percent(level: int) -> int:
        level = max(1, min(10, int(level)))
        return level * 10

    def _assistant_gain(self) -> float:
        """Linear gain for TTS / any sounddevice playback. 1→0.1 … 10→1.0."""
        return self._volume_to_percent(self._volume_level) / 100.0

    def _is_music_playing(self) -> bool:
        with self._music_lock:
            proc = self._music_proc
            return proc is not None and proc.poll() is None

    def _duck_music(self, factor: float) -> None:
        """Lower mpv only (not Pulse) so mic headroom returns while we listen."""
        if not self._is_music_playing():
            return
        base = self._volume_to_percent(self._volume_level)
        percent = max(5, min(100, int(round(base * max(0.05, factor)))))
        self._apply_mpv_volume(percent)

    def _restore_music_volume(self) -> None:
        if not self._is_music_playing():
            return
        self._apply_mpv_volume(self._volume_to_percent(self._volume_level))

    def _set_volume(self, level: int) -> None:
        """Master volume for the whole assistant: TTS + music + Pulse sink."""
        level = max(1, min(10, int(level)))
        self._volume_level = level
        percent = self._volume_to_percent(level)
        logger.info("Assistant volume %s/10 (%s%%)", level, percent)
        # Keep listen-duck while waiting for wake; otherwise a volume command
        # would blast music back to full and bury the mic again.
        if self._in_wake_listen and self._is_music_playing():
            self._apply_mpv_volume(max(5, int(round(percent * MUSIC_LISTEN_DUCK))))
        else:
            self._apply_mpv_volume(percent)
        self._apply_pulse_volume(percent)

    def _apply_mpv_volume(self, percent: int) -> None:
        sock = self._mpv_ipc_path
        if not sock.exists():
            return
        try:
            import socket

            payload = json.dumps(
                {"command": ["set_property", "volume", float(percent)]}
            ).encode("utf-8") + b"\n"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.5)
                client.connect(str(sock))
                client.sendall(payload)
                client.recv(4096)
        except Exception as exc:
            logger.debug("mpv volume IPC failed: %s", exc)

    def _apply_pulse_volume(self, percent: int) -> None:
        # Affects Bluetooth/USB sink used for TTS + music host audio.
        try:
            subprocess.run(
                [
                    "pactl",
                    "set-sink-volume",
                    "@DEFAULT_SINK@",
                    f"{int(percent)}%",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except FileNotFoundError:
            logger.debug("pactl not found — skipping system volume")
        except Exception as exc:
            logger.debug("pactl volume failed: %s", exc)

    def _start_alert_scheduler(self) -> None:
        thread = threading.Thread(
            target=self._alert_scheduler_loop,
            name="alert-scheduler",
            daemon=True,
        )
        thread.start()
        logger.info("Alert scheduler enabled (timer/alarm)")

    def _handle_alert_command(self, command: dict) -> None:
        parsed = dict(command)
        url = str(parsed.get("url") or "").strip()
        if url.startswith("pi-alert://"):
            parsed.update(self._parse_alert_url(url))
        kind = str(parsed.get("kind") or parsed.get("action") or "").strip().lower()
        if kind in ("cancel", "cancel_alert", "cancel_timer", "cancel_alarm"):
            target = str(parsed.get("cancel_kind") or parsed.get("target") or "").strip().lower()
            if kind == "cancel_timer":
                target = "timer"
            elif kind == "cancel_alarm":
                target = "alarm"
            self._cancel_alerts(kind=target or None)
            return
        fire_at = parsed.get("fire_at")
        delay_sec = parsed.get("delay_sec")
        try:
            if fire_at is not None and str(fire_at).strip() != "":
                when = float(fire_at)
            elif delay_sec is not None and str(delay_sec).strip() != "":
                when = time.time() + max(0.0, float(delay_sec))
            else:
                when = time.time()
        except (TypeError, ValueError):
            logger.error("Alert command missing fire_at/delay_sec: %s", parsed)
            return
        job = {
            "id": str(parsed.get("id") or uuid.uuid4())[:12],
            "kind": "alarm" if kind == "alarm" else "timer",
            "fire_at": when,
            "sound": str(parsed.get("sound") or "classic").strip() or "classic",
            "media_url": str(parsed.get("media_url") or parsed.get("sound_url") or "").strip(),
            "label": str(parsed.get("title") or parsed.get("label") or "").strip(),
            "loop": str(parsed.get("loop") or ("1" if kind == "alarm" else "0")).strip()
            not in ("0", "false", "no"),
        }
        wait = max(0.0, when - time.time())
        logger.info(
            "Scheduled %s in %.0fs sound=%s loop=%s",
            job["kind"],
            wait,
            job["sound"],
            job["loop"],
        )
        with self._alert_cv:
            self._alerts.append(job)
            self._alert_cv.notify_all()

    @staticmethod
    def _parse_alert_url(url: str) -> dict:
        parsed = urlparse(url)
        kind = (parsed.netloc or parsed.path.lstrip("/")).strip().lower()
        q = {k: unquote(v[-1]) for k, v in parse_qs(parsed.query).items() if v}
        out = {"kind": kind, **q}
        if "url" in q and "media_url" not in out:
            out["media_url"] = q["url"]
        return out

    def _cancel_alerts(self, *, kind: str | None = None) -> None:
        with self._alert_cv:
            before = len(self._alerts)
            if kind:
                self._alerts = [j for j in self._alerts if j.get("kind") != kind]
            else:
                self._alerts.clear()
            removed = before - len(self._alerts)
            self._alert_cv.notify_all()
        if kind in (None, "alarm", "timer"):
            with self._music_lock:
                if self._current_source == "alert":
                    self._terminate_music_proc()
                    self._current_source = ""
        logger.info("Cancelled alerts kind=%s removed=%d", kind or "all", removed)

    def _alert_scheduler_loop(self) -> None:
        while not self._alert_stop.is_set():
            with self._alert_cv:
                if not self._alerts:
                    self._alert_cv.wait(timeout=1.0)
                    continue
                self._alerts.sort(key=lambda job: float(job.get("fire_at") or 0.0))
                nxt = self._alerts[0]
                delay = float(nxt["fire_at"]) - time.time()
                if delay > 0:
                    self._alert_cv.wait(timeout=min(delay, 1.0))
                    continue
                self._alerts.pop(0)
            try:
                self._fire_alert(nxt)
            except Exception:
                logger.exception("Alert playback failed")

    def _fire_alert(self, job: dict) -> None:
        label = job.get("label") or job.get("kind")
        logger.info("Firing %s (%s)", job.get("kind"), label)
        media = str(job.get("media_url") or "").strip()
        tmp: Path | None = None
        if not media:
            tmp = self._write_builtin_alert(
                str(job.get("kind") or "timer"),
                str(job.get("sound") or "classic"),
            )
            media = str(tmp)
        loop_file = "inf" if job.get("loop") else "4"
        self._start_music(media, source="alert", loop_file=loop_file)

    def _write_builtin_alert(self, kind: str, sound: str) -> Path:
        rate = 16000
        if kind == "timer":
            audio = self._synth_timer_tone(rate)
        elif sound == "digital":
            audio = self._synth_alarm_digital(rate)
        elif sound == "soft":
            audio = self._synth_alarm_soft(rate)
        else:
            audio = self._synth_alarm_classic(rate)
        fd, name = tempfile.mkstemp(prefix=f"{kind}-", suffix=".wav")
        os.close(fd)
        path = Path(name)
        self._write_wav_pcm(path, audio, rate)
        return path

    @classmethod
    def _synth_timer_tone(cls, rate: int) -> np.ndarray:
        gap = np.zeros(int(rate * 0.12), dtype=np.float32)
        burst = np.concatenate(
            [
                cls._tone(rate, 880.0, 0.12, amplitude=0.28),
                gap,
                cls._tone(rate, 1320.0, 0.18, amplitude=0.30),
                np.zeros(int(rate * 0.35), dtype=np.float32),
            ]
        )
        return np.tile(burst, 2)

    @classmethod
    def _synth_alarm_classic(cls, rate: int) -> np.ndarray:
        hi = cls._tone(rate, 880.0, 0.35, amplitude=0.32, harm=0.08)
        lo = cls._tone(rate, 698.5, 0.35, amplitude=0.32, harm=0.08)
        pause = np.zeros(int(rate * 0.12), dtype=np.float32)
        return np.concatenate([hi, pause, lo, pause, hi, pause, lo, np.zeros(int(rate * 0.25), dtype=np.float32)])

    @classmethod
    def _synth_alarm_digital(cls, rate: int) -> np.ndarray:
        pieces: list[np.ndarray] = []
        for freq in (1200.0, 1500.0, 1200.0, 1500.0):
            pieces.append(cls._tone(rate, freq, 0.08, amplitude=0.28, attack=0.005, release=0.02, harm=0.02))
            pieces.append(np.zeros(int(rate * 0.06), dtype=np.float32))
        pieces.append(np.zeros(int(rate * 0.2), dtype=np.float32))
        return np.concatenate(pieces)

    @classmethod
    def _synth_alarm_soft(cls, rate: int) -> np.ndarray:
        return np.concatenate(
            [
                cls._tone(rate, 523.25, 0.22, amplitude=0.22),
                np.zeros(int(rate * 0.08), dtype=np.float32),
                cls._tone(rate, 659.25, 0.28, amplitude=0.24),
                np.zeros(int(rate * 0.35), dtype=np.float32),
            ]
        )

    def _start_music(
        self,
        stream_url: str,
        *,
        track_id: str = "",
        source: str = "",
        loop_file: str = "no",
    ) -> None:
        with self._music_lock:
            self._terminate_music_proc()
            self._current_track_id = track_id
            self._current_source = source
            percent = self._volume_to_percent(self._volume_level)
            ipc = str(self._mpv_ipc_path)
            try:
                self._mpv_ipc_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                # Start already ducked if we are in wake-listen — full blast would
                # bury the mic until the next gate sync tick.
                start_vol = percent
                if source == "alert":
                    start_vol = max(percent, 70)
                elif self._in_wake_listen:
                    start_vol = max(5, int(round(percent * MUSIC_LISTEN_DUCK)))
                self._music_proc = subprocess.Popen(
                    [
                        self.mpv_command,
                        "--no-video",
                        "--really-quiet",
                        f"--volume={start_vol}",
                        f"--loop-file={loop_file}",
                        f"--input-ipc-server={ipc}",
                        stream_url,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                logger.error(
                    "mpv not found (%s). Install: sudo apt install mpv",
                    self.mpv_command,
                )
                self._music_proc = None
            except Exception:
                logger.exception("Failed to start mpv")
                self._music_proc = None
            else:
                threading.Thread(
                    target=self._watch_music_proc,
                    name="music-watch",
                    daemon=True,
                ).start()

    def _watch_music_proc(self) -> None:
        proc = self._music_proc
        if proc is None:
            return
        code = proc.wait()
        with self._music_lock:
            if self._music_proc is not proc:
                return  # replaced or stopped
            self._music_proc = None
            track_id = self._current_track_id
            source = self._current_source
            self._current_track_id = ""
            self._current_source = ""
        if code != 0:
            return
        if source == "alert":
            return
        # Natural end — ask server for next Моя волна track if active.
        if source == "yandex-wave" or track_id:
            self._report_track_finished(track_id)

    def _report_track_finished(self, track_id: str) -> None:
        url = f"{self.backend_url}/v1/music/status/{self.device_id}"
        try:
            self._http.post(
                url,
                json={
                    "device_id": self.device_id,
                    "playing": False,
                    "action": "track_finished",
                    "title": "",
                    "artist": "",
                },
                timeout=20.0,
            )
            logger.info("Reported track_finished (track_id=%s)", track_id or "-")
        except Exception as exc:
            logger.debug("track_finished report failed: %s", exc)

    def _pause_music(self) -> None:
        with self._music_lock:
            if self._music_proc and self._music_proc.poll() is None:
                self._music_proc.send_signal(signal.SIGSTOP)

    def _stop_music(self) -> None:
        had_proc = False
        with self._music_lock:
            had_proc = self._music_proc is not None
            self._current_track_id = ""
            self._current_source = ""
            self._terminate_music_proc()
        # mpv on HFP often wedges the SCO mic — same as after TTS.
        if had_proc and (os.getenv("BT_DEVICE_MAC") or os.getenv("BT_MAC") or "").strip():
            self._restore_hfp_audio()

    def _terminate_music_proc(self) -> None:
        proc = self._music_proc
        self._music_proc = None
        if proc is None:
            try:
                self._mpv_ipc_path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        if proc.poll() is not None:
            try:
                self._mpv_ipc_path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            self._mpv_ipc_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _write_wav_pcm(self, path: Path, data: np.ndarray, rate: int) -> None:
        """Write float32 mono/stereo [-1,1] as 16-bit PCM WAV."""
        pcm = np.clip(data, -1.0, 1.0)
        if pcm.ndim == 1:
            channels = 1
            flat = pcm
        else:
            channels = int(pcm.shape[1])
            flat = pcm.reshape(-1)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes((flat * 32767.0).astype(np.int16).tobytes())

    def _play_wav(self, wav_bytes: bytes, reply_text: str = "") -> np.ndarray | None:
        """
        Play TTS reply.

        On Bluetooth HFP, PortAudio duplex (sd.play + InputStream) often deadlocks
        Pulse — the client then hangs right after «Listening for microWakeWord»
        during barge-in. Prefer paplay for output and keep sounddevice for mic only.
        """
        fd, tmp_name = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        path = Path(tmp_name)
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
                rate = wav.getframerate()
                frames = wav.readframes(wav.getnframes())
                width = wav.getsampwidth()
                channels = wav.getnchannels()
            if width != 2:
                logger.error("Unsupported sample width: %s", width)
                return None
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
            if channels > 1:
                data = data.reshape(-1, channels)

            # Master assistant volume (1–10) applies to spoken replies too.
            data = data * self._assistant_gain()
            if self.barge_in:
                # Duck the reply so bleed stays below a normal speaking voice.
                data = data * BARGE_PLAYBACK_GAIN
                if self._hfp_duplex():
                    # Extra duck on HFP so barge-in mic can clear TTS bleed.
                    data = data * 0.75
            # Trailing silence — ALSA/Pulse underruns often clip the last syllable.
            pad_n = int(rate * TTS_END_PAD_SEC)
            if pad_n > 0:
                if data.ndim == 1:
                    data = np.concatenate(
                        [data, np.zeros(pad_n, dtype=np.float32)]
                    )
                else:
                    data = np.concatenate(
                        [
                            data,
                            np.zeros((pad_n, data.shape[1]), dtype=np.float32),
                        ]
                    )

            duration = float(data.shape[0]) / float(rate)
            self._write_wav_pcm(path, data, rate)
            logger.info(
                "Playing reply (%.1fs, barge_in=%s)",
                duration,
                "on" if self.barge_in else "off",
            )

            paplay = shutil.which("paplay")
            if paplay:
                return self._play_wav_paplay(
                    paplay, path, duration, reply_text=reply_text
                )

            # Fallback: never open mic while PortAudio output is active (HFP hang).
            if self.barge_in:
                logger.warning(
                    "paplay not found — TTS without barge-in (avoids HFP deadlock)"
                )
            sd.play(data, rate)
            sd.wait()
            return None
        finally:
            path.unlink(missing_ok=True)

    def _bt_profile(self) -> str:
        return (os.getenv("BT_PROFILE") or "handsfree_head_unit").strip().lower()

    def _bluez_sink_active(self) -> bool:
        if not shutil.which("pactl"):
            return False
        try:
            sink = subprocess.check_output(
                ["pactl", "get-default-sink"],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            return False
        return "bluez" in sink.lower()

    def _open_speaker_echo(self) -> bool:
        """True when a room speaker plays into a separate mic (A2DP + USB)."""
        if self._bt_profile() == "a2dp_sink":
            return True
        source = (os.getenv("PULSE_DEFAULT_SOURCE") or "").strip().lower()
        return "usb" in source

    def _hfp_duplex(self) -> bool:
        """Same Bluetooth headset for mic and speaker (weak acoustic echo)."""
        return self._bt_profile() != "a2dp_sink" and self._bluez_sink_active()

    def _restore_hfp_audio(self) -> None:
        """
        Re-assert Bluetooth HFP after TTS.

        paplay / Pulse often leave the SCO link wedged or flip the card away from
        handsfree_head_unit — mic then stays silent until profile is bounced.
        """
        if not shutil.which("pactl"):
            return
        mac = (os.getenv("BT_DEVICE_MAC") or os.getenv("BT_MAC") or "").strip()
        if not mac:
            return
        profile = (os.getenv("BT_PROFILE") or "handsfree_head_unit").strip()
        usb_source = (os.getenv("PULSE_DEFAULT_SOURCE") or "").strip()
        # USB mic + A2DP speaker: never bounce the card (that drops the speaker)
        # and never steal default source back to Bluetooth HFP.
        if profile == "a2dp_sink":
            if usb_source and shutil.which("pactl"):
                subprocess.run(
                    ["pactl", "set-default-source", usb_source],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            return
        mac_us = mac.upper().replace(":", "_").replace("-", "_")
        card = f"bluez_card.{mac_us}"
        try:
            cards = subprocess.check_output(
                ["pactl", "list", "cards", "short"],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return
        if card not in cards:
            return
        try:
            # Bounce profile so the HFP capture source comes back alive.
            subprocess.run(
                ["pactl", "set-card-profile", card, "off"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            time.sleep(0.35)
            subprocess.run(
                ["pactl", "set-card-profile", card, profile],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            time.sleep(0.4)
            sink = ""
            source = ""
            for line in subprocess.check_output(
                ["pactl", "list", "short", "sinks"],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).splitlines():
                name = line.split()[1] if len(line.split()) > 1 else ""
                if mac_us in name:
                    sink = name
                    break
            for line in subprocess.check_output(
                ["pactl", "list", "short", "sources"],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                name = parts[1]
                if mac_us in name and not name.endswith(".monitor"):
                    source = name
                    break
            if sink:
                subprocess.run(
                    ["pactl", "set-default-sink", sink],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            if source:
                subprocess.run(
                    ["pactl", "set-default-source", source],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                mic_vol = (os.getenv("BT_MIC_VOLUME") or "200%").strip()
                if mic_vol:
                    subprocess.run(
                        ["pactl", "set-source-volume", source, mic_vol],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
            logger.info("Restored BT audio profile %s on %s", profile, card)
        except Exception:
            logger.debug("HFP restore failed", exc_info=True)

    def _play_wav_paplay(
        self,
        paplay: str,
        path: Path,
        duration: float,
        reply_text: str = "",
    ) -> np.ndarray | None:
        """Play via Pulse paplay; listen for barge-in on the mic in parallel."""
        open_echo = self._open_speaker_echo()
        hfp = self._hfp_duplex()
        use_barge = self.barge_in
        if use_barge:
            logger.info(
                "Barge-in armed during TTS (%s, %.1fs)",
                "open-echo" if open_echo else ("hfp-soft" if hfp else "local"),
                duration,
            )

        proc = subprocess.Popen(
            [paplay, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        preroll: np.ndarray | None = None
        try:
            if use_barge:
                preroll = self._watch_barge_in(reply_text, duration)
                if preroll is not None:
                    logger.info("Barge-in detected — playback stopped")
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return preroll
            try:
                proc.wait(timeout=max(2.0, duration + 2.0))
            except subprocess.TimeoutExpired:
                logger.warning("paplay still running after %.1fs — killing", duration)
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            return None
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            # Always re-seat HFP after TTS so wake/mic survive duplex SCO use.
            if (os.getenv("BT_DEVICE_MAC") or os.getenv("BT_MAC") or "").strip():
                self._restore_hfp_audio()

    def _watch_barge_in(
        self,
        reply_text: str,
        duration: float,
    ) -> np.ndarray | None:
        """Watch the mic while TTS plays; non-None result means «interrupt me»."""
        deadline = time.monotonic() + duration + 0.2
        # Accept on the idle energy floor — the echo gate handles bleed separately.
        threshold = self.energy_threshold
        open_echo = self._open_speaker_echo()
        hfp = self._hfp_duplex()
        # HFP SCO is poor duplex: TTS bleed is weak/noisy and a strict echo gate
        # often blocks real «Джарвис». Soften only on a headset mic, never when
        # a room speaker dumps the reply back into a USB mic.
        barge_soft = hfp and not open_echo
        if barge_soft:
            threshold = max(0.0008, threshold * 0.55)
            stable_frames = 1
            arm_sec = 0.0
        else:
            stable_frames = max(WAKE_STABLE_MIN, self.wake_stable_frames)
            arm_sec = BARGE_ARM_SEC
        # Energy gate + MWW score replace Vosk echo-text filtering.
        _ = reply_text
        return self._wait_mww(
            deadline=deadline,
            energy_threshold=threshold,
            respect_cooldown=False,
            echo_gate=True,
            stable_frames=stable_frames,
            barge_soft=barge_soft,
            arm_sec=arm_sec,
        )

    def _trim_utterance(self, frames: np.ndarray) -> np.ndarray:
        """Drop leading/trailing hush so upload + STT see less silence."""
        if frames.size < int(SAMPLE_RATE * 0.2):
            return frames
        win = max(1, int(SAMPLE_RATE * 0.02))  # 20 ms
        thr = self.energy_threshold * 0.45
        n_wins = frames.size // win
        if n_wins < 3:
            return frames
        energies = np.empty(n_wins, dtype=np.float32)
        for i in range(n_wins):
            chunk = frames[i * win : (i + 1) * win]
            energies[i] = float(np.sqrt(np.mean(np.square(chunk))))
        speech = np.flatnonzero(energies >= thr)
        if speech.size == 0:
            return frames
        pad_pre = 4   # ~80 ms before first speech
        pad_post = 6  # ~120 ms after last speech
        start = max(0, int(speech[0]) - pad_pre) * win
        end = min(frames.size, (int(speech[-1]) + 1 + pad_post) * win)
        trimmed = frames[start:end]
        if trimmed.size < int(SAMPLE_RATE * 0.15):
            return frames
        dropped_ms = int(1000 * (frames.size - trimmed.size) / SAMPLE_RATE)
        if dropped_ms >= 80:
            logger.info(
                "Trimmed utterance: %d→%d samples (−%dms)",
                frames.size,
                trimmed.size,
                dropped_ms,
            )
        return trimmed

    def _frames_to_wav(self, frames: np.ndarray) -> bytes:
        pcm = (np.clip(frames, -1.0, 1.0) * 32767.0).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm.tobytes())
        return buf.getvalue()

    def _rms(self, frames: np.ndarray) -> float:
        if frames.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(frames.astype(np.float32)))))

    @staticmethod
    def _wake_burst_ok(
        energies,
        accept: float,
    ) -> tuple[bool, float, float]:
        """True if a recent RMS burst looks like speech, not a flat noise floor."""
        vals = [float(x) for x in energies]
        if not vals:
            return False, 0.0, 0.0
        peak = max(vals)
        body = vals[:-4] if len(vals) > 8 else vals
        ordered = sorted(body)
        floor = ordered[max(0, len(ordered) // 3)]
        if peak < accept:
            return False, peak, floor
        if floor > 1e-6 and peak < floor * WAKE_ENERGY_BURST_RATIO:
            return False, peak, floor
        return True, peak, floor

    def _normalize(self, text: str) -> str:
        value = (text or "").lower().replace("ё", "е")
        value = re.sub(r"[^a-zа-я0-9_\s]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        value = value.replace("асистент", "ассистент")
        return value

    def close(self) -> None:
        """Stop background polling and release the shared HTTP client."""
        self._alert_stop.set()
        with self._alert_cv:
            self._alert_cv.notify_all()
        self._music_stop.set()
        try:
            self._http.close()
        except Exception:
            pass


def _env_wake_threshold() -> float:
    raw = os.getenv("WAKE_THRESHOLD") or os.getenv("OWW_THRESHOLD")
    if raw:
        return float(raw)
    return DEFAULT_WAKE_THRESHOLD


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raspberry Pi voice client: microWakeWord Jarvis + backend TTS",
    )
    parser.add_argument(
        "--backend",
        default=os.getenv("VOICE_BACKEND_URL", "http://voice.pora-ai.ru"),
    )
    parser.add_argument(
        "--wake-threshold",
        type=float,
        default=_env_wake_threshold(),
        help="Wake score threshold 0–1 (default 0.90; also reads OWW_THRESHOLD)",
    )
    parser.add_argument(
        "--mww-model-config",
        default=os.getenv("MWW_MODEL_CONFIG", DEFAULT_MWW_CONFIG),
        help="Path to ru_jarvis_mww.json",
    )
    parser.add_argument(
        "--wake",
        default=os.getenv("WAKE_WORDS", ""),
        help="Optional display label (detection uses the MWW model)",
    )
    parser.add_argument(
        "--silence",
        type=float,
        default=float(os.getenv("SILENCE_SEC", "0.35")),
        help="Seconds of quiet before ending the command recording (default 0.6)",
    )
    parser.add_argument("--max-sec", type=float, default=20.0)
    parser.add_argument(
        "--energy",
        type=float,
        default=float(os.getenv("WAKE_ENERGY", "0.002")),
        help="Min RMS energy for wake / utterance start (HFP mics: try 0.001–0.002)",
    )
    parser.add_argument(
        "--wake-stable",
        type=int,
        default=int(os.getenv("WAKE_STABLE_FRAMES", str(WAKE_STABLE_MIN))),
        help="Consecutive 80ms frames above score+energy before accept (idle min 3)",
    )
    parser.add_argument(
        "--wake-accept-energy",
        type=float,
        default=float(os.getenv("WAKE_ACCEPT_ENERGY", str(DEFAULT_WAKE_ACCEPT_ENERGY))),
        help="Min RMS burst to accept a wake (room speech ~0.006–0.013; rejects ~0.004 noise)",
    )
    parser.add_argument(
        "--wake-cooldown",
        type=float,
        default=float(os.getenv("WAKE_COOLDOWN", "2.0")),
        help="Seconds to ignore wake after assist / previous wake",
    )
    parser.add_argument(
        "--no-barge-in",
        dest="barge_in",
        action="store_false",
        default=os.getenv("BARGE_IN", "true").lower() in ("1", "true", "yes"),
        help="Do not listen for the wake phrase while the reply is playing",
    )
    parser.add_argument(
        "--barge-energy-mult",
        type=float,
        default=float(os.getenv("BARGE_ENERGY_MULT", "1.12")),
        help="Headroom over reply bleed needed to interrupt (playback is also ducked)",
    )
    parser.add_argument(
        "--device-id",
        default=os.getenv("MUSIC_DEVICE_ID", "pi-default"),
        help="Unique id for this Pi (must match Open WebUI music tool context)",
    )
    parser.add_argument(
        "--no-music-poll",
        dest="music_poll",
        action="store_false",
        default=os.getenv("MUSIC_POLL", "true").lower() in ("1", "true", "yes"),
        help="Disable background polling for music commands from web chat",
    )
    parser.add_argument(
        "--music-poll-interval",
        type=float,
        default=float(os.getenv("MUSIC_POLL_INTERVAL", "2.0")),
        help="Seconds between /v1/music/pending polls",
    )
    parser.add_argument(
        "--mpv",
        default=os.getenv("MPV_COMMAND", ""),
        help="Path to mpv binary (default: mpv from PATH)",
    )
    parser.add_argument(
        "--audio-device",
        default=os.getenv("AUDIO_INPUT_DEVICE", ""),
        help="sounddevice index, 'in,out', or name substring (e.g. pulse / bluez)",
    )
    parser.add_argument(
        "--noise-suppress",
        dest="noise_suppress",
        action="store_true",
        default=os.getenv("NOISE_SUPPRESS", "true").lower() in ("1", "true", "yes"),
        help="Mic denoise via RNNoise (default on)",
    )
    parser.add_argument(
        "--no-noise-suppress",
        dest="noise_suppress",
        action="store_false",
        help="Disable mic denoise",
    )
    parser.add_argument(
        "--noise-hp-hz",
        type=float,
        default=float(os.getenv("NOISE_HP_HZ", str(DEFAULT_NOISE_HP_HZ))),
        help="Mic high-pass cutoff Hz for traffic rumble (default 280)",
    )
    args = parser.parse_args()

    wake_words = [raw.strip() for raw in args.wake.split(",") if raw.strip()]

    client = VoiceClient(
        backend_url=args.backend,
        wake_words=wake_words or None,
        silence_sec=args.silence,
        max_utterance_sec=args.max_sec,
        energy_threshold=args.energy,
        wake_threshold=args.wake_threshold,
        mww_model_config=args.mww_model_config,
        wake_cooldown_sec=args.wake_cooldown,
        wake_stable_frames=args.wake_stable,
        wake_accept_energy=args.wake_accept_energy,
        barge_in=args.barge_in,
        barge_energy_mult=args.barge_energy_mult,
        device_id=args.device_id,
        music_poll=args.music_poll,
        music_poll_interval=args.music_poll_interval,
        noise_suppress=args.noise_suppress,
        noise_hp_hz=args.noise_hp_hz,
        mpv_command=args.mpv,
        audio_device=args.audio_device or None,
    )
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Stopped")
    finally:
        client.close()


if __name__ == "__main__":
    main()
