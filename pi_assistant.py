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
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd

from alert_scheduler import AlertScheduler
from audio_dsp import (
    MIC_RATE,
    SAMPLE_RATE,
    Highpass as _Highpass,
    resample_int as _resample_int,
)
from bt_audio import BtAudio
from music_poller import MusicPoller
from playback_engine import (
    BARGE_PLAYBACK_GAIN as _PE_BARGE_PLAYBACK_GAIN,
    PlaybackEngine,
    TTS_END_PAD_SEC as _PE_TTS_END_PAD_SEC,
)
from wake_listen import (
    BARGE_ARM_SEC,
    BARGE_MULT_MIN,
    CHANNELS,
    WAKE_STABLE_MIN,
    WakeListener,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("pi-client")

# 80 ms capture @ 48 kHz; after ↓16 kHz → 1280 for microWakeWord.
MIC_FRAME_SAMPLES = 3840  # int(MIC_RATE * 0.08)
# --- Barge / wake tuning (listen loop constants live in wake_listen.py) ---
# Env + CLI: BARGE_IN, BARGE_ENERGY_MULT, WAKE_*; optional env below keeps defaults.
#
# Soften the reply while we listen for an interrupt — leaves headroom for the
# mic at any system volume (bleed scales with the speakers, the user's voice does not).
#
# Volume layers (do not mix):
# - playback `_volume_level` (1–10) → Pulse % + mpv
# - MUSIC_LISTEN_DUCK / MUSIC_SPEECH_DUCK: mpv fractions while listen / speech-over-music
# - BARGE_PLAYBACK_GAIN: softens TTS PCM while barge-in listens
# - BT_KEEPALIVE_VOL: host bt-connect paplay blip (scripts/, not here)
BARGE_PLAYBACK_GAIN = float(os.getenv("BARGE_PLAYBACK_GAIN", str(_PE_BARGE_PLAYBACK_GAIN)))
# Seconds to wait for the actual command after the reply was cut short.
BARGE_COMMAND_WAIT_SEC = 5.0
# After silence is detected, keep a short pad so word endings aren't clipped.
RECORD_END_PAD_SEC = 0.12
# Quiet tail after TTS so ALSA/Pulse underruns don't eat the last syllable.
TTS_END_PAD_SEC = _PE_TTS_END_PAD_SEC

DEFAULT_WAKE_THRESHOLD = 0.90
# Keep env from dropping too low (flat-noise false accepts lived at 0.85).
WAKE_THRESHOLD_FLOOR = 0.90
DEFAULT_MWW_CONFIG = "/app/models/ru_jarvis_mww.json"
# Room «Джарвис» on USB often peaks ~0.0034–0.005; louder takes ~0.01.
# v2 is specific to the wake word — keep this under real speech energy.
DEFAULT_WAKE_ACCEPT_ENERGY = 0.003
# After a wake with no command, stay deaf longer (room noise often re-triggers).
EMPTY_WAKE_COOLDOWN_SEC = float(os.getenv("EMPTY_WAKE_COOLDOWN_SEC", "6.0"))
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


class RnnoiseDenoise:
    """
    Xiph RNNoise on 10 ms / 48 kHz frames, with a light high-pass in front.

    Mic frames are expected at 48 kHz (native). High-pass and RNNoise run at
    48 kHz with no upsample. Output stays at 48 kHz; callers downsample to
    16 kHz only where needed (microWakeWord, WAV upload).
    """

    FRAME = 480
    MODEL_RATE = 48000

    def __init__(self, rate: int = MIC_RATE, hp_hz: float = DEFAULT_NOISE_HP_HZ) -> None:
        self.rate = int(rate)
        if self.rate != self.MODEL_RATE:
            raise ValueError(
                f"RnnoiseDenoise expects {self.MODEL_RATE} Hz mic audio, got {self.rate}"
            )
        self.hp_hz = float(np.clip(hp_hz, 80.0, 800.0))
        self.label = f"RNNoise + highpass={self.hp_hz:.0f}Hz @{self.rate}Hz"
        self._hp = _Highpass(self.rate, self.hp_hz)
        self._buf = np.zeros(0, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
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
        self._buf = np.zeros(0, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
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
        self._buf = np.concatenate([self._buf, y]) if self._buf.size else y
        out: list[np.ndarray] = []
        while self._buf.size >= self.FRAME:
            chunk = np.ascontiguousarray(
                self._buf[: self.FRAME] * 32768.0,
                dtype=np.float32,
            )
            self._buf = self._buf[self.FRAME :]
            ptr = chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self._lib.rnnoise_process_frame(self._st, ptr, ptr)
            out.append(chunk * (1.0 / 32768.0))
        if not out:
            return np.clip(y, -1.0, 1.0)
        y_out = np.concatenate(out)
        if self._pending.size:
            y_out = np.concatenate([self._pending, y_out])
        if y_out.size >= x.size:
            self._pending = y_out[x.size :]
            y_out = y_out[: x.size]
        else:
            y_out = np.pad(y_out, (0, x.size - y_out.size))
            self._pending = np.zeros(0, dtype=np.float32)
        return np.clip(y_out, -1.0, 1.0)


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
                self._denoise = create_mic_denoise(MIC_RATE, hp_hz=noise_hp_hz)
            except Exception:
                logger.warning("Mic denoise unavailable — continuing without it", exc_info=True)
                self.noise_suppress = False
        label = "Джарвис"
        if wake_words:
            normalized = [self._normalize(w) for w in wake_words if w.strip()]
            self.wake_words = list(dict.fromkeys(normalized)) or [label]
        else:
            self.wake_words = [label]
        self.wake_threshold = float(
            np.clip(max(WAKE_THRESHOLD_FLOOR, wake_threshold), 0.05, 0.99)
        )
        if wake_threshold < WAKE_THRESHOLD_FLOOR:
            logger.warning(
                "Wake threshold %.2f is below floor %.2f — using %.2f",
                wake_threshold,
                WAKE_THRESHOLD_FLOOR,
                self.wake_threshold,
            )
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
        # Reuse TCP/TLS across assist + music polls (saves handshake each turn).
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
        self.bt = BtAudio()
        self.playback = PlaybackEngine(
            self.bt,
            mpv_command=self.mpv_command,
            backend_url=self.backend_url,
            device_id=self.device_id,
            http_client=self._http,
            barge_in=self.barge_in,
        )
        self.wake = WakeListener(
            playback=self.playback,
            bt=self.bt,
            energy_threshold=self.energy_threshold,
            wake_stable_frames=self.wake_stable_frames,
            wake_accept_energy=self.wake_accept_energy,
            barge_energy_mult=self.barge_energy_mult,
            wake_cooldown_sec=self.wake_cooldown_sec,
            get_last_wake_ts=lambda: self._last_wake_ts,
            denoise=self._denoise,
        )
        self.alerts = AlertScheduler(playback=self.playback, http_client=self._http)
        self.music = MusicPoller(
            backend_url=self.backend_url,
            device_id=self.device_id,
            http_client=self._http,
            playback=self.playback,
            alerts=self.alerts,
            enabled=self.music_poll,
            interval=self.music_poll_interval,
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
            self.bt.restore_hfp_audio()
        self._warmup_backend()
        self._init_wake()
        self.playback.set_barge_watch(self._watch_barge_in)
        self.music.sync_volume_from_backend()
        if self.music_poll:
            self.music.start()
        self.alerts.start()
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
            self.playback.stop_music()
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
                        # Likely false wake: no command followed the trigger.
                        self._last_wake_ts = time.monotonic() + max(
                            0.0,
                            EMPTY_WAKE_COOLDOWN_SEC - self.wake_cooldown_sec,
                        )
                        logger.warning(
                            "Empty recording after wake — cooldown %.1fs, back to listen",
                            EMPTY_WAKE_COOLDOWN_SEC,
                        )
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
        if not self.bt._which("pactl"):
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
            raise RuntimeError(
                f"Backend not reachable at {self.backend_url}: {exc}"
            ) from exc

    def _init_wake(self) -> None:
        self._init_mww()

    def _init_mww(self) -> None:
        try:
            from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
        except Exception as exc:
            raise RuntimeError(
                "microWakeWord import failed. Install: pip install pymicro-wakeword"
            ) from exc

        if not self.mww_model_config:
            raise RuntimeError(
                "MWW_MODEL_CONFIG is empty. Set path to *_mww.json from microwakeword-trainer."
            )

        config_path = Path(self.mww_model_config)
        if not config_path.is_file():
            raise RuntimeError(f"MWW model config not found: {config_path}")

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
            self.wake.bind_mww(self._mww, self._mww_features, self._mww_wake_word)
            logger.info(
                "microWakeWord ready: wake=%s cutoff=%.2f window=%d config=%s",
                self._mww_wake_word,
                self._mww.probability_cutoff,
                window,
                config_path,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"microWakeWord init failed: {exc}") from exc

    def _wait_for_wake(self) -> np.ndarray | None:
        if self._wake_mode != "mww":
            raise RuntimeError("Wake engine is not initialized")
        return self.wake.wait_for_wake()

    def _assist_and_play(self, wav_bytes: bytes) -> np.ndarray | None:
        """Send audio, play the reply; return barge-in preroll when interrupted."""
        t0 = time.monotonic()
        logger.info("Sending utterance to backend (%d bytes)", len(wav_bytes))
        # Pause music polling so the Pi isn't competing for CPU/network mid-assist.
        music_was = self.music_poll
        self.music_poll = False
        self.music.music_poll = False
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
                preroll = self.playback.play_wav(audio, reply_text=reply)
            else:
                logger.error("Assist response has no audio payload")
                preroll = None
            if playback:
                self.music.handle_playback(playback)
            logger.info(
                "Assist total=%.1fs (play included)",
                time.monotonic() - t0,
            )
            return preroll
        finally:
            self.music_poll = music_was
            self.music.music_poll = music_was

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
        block = int(MIC_RATE * 0.1)
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

        # Ding first (blocking), then open the mic. Overlapping mpv+InputStream
        # on A2DP often eats the listen chime; recording after the ding is also
        # what the user expects to send to the model.
        if play_listen_cue:
            self.playback.play_listen_cue()
        elif preroll is not None and preroll.size:
            lead.append(preroll.astype(np.float32, copy=False))

        with sd.InputStream(
            samplerate=MIC_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=block,
        ) as stream:
            for index in range(max_blocks):
                data, _ = stream.read(block)
                mono = data[:, 0]
                if self._denoise is not None:
                    mono = self._denoise.process(mono)
                # ↓16 kHz for energy helpers + assist WAV upload.
                mono16 = _resample_int(mono, MIC_RATE, SAMPLE_RATE)
                energy = self._rms(mono16)
                if energy >= self.energy_threshold:
                    heard_live = True
                    silent = 0
                    frames.append(mono16.copy())
                elif heard_live:
                    frames.append(mono16.copy())
                    if energy >= continue_threshold:
                        silent = max(0, silent - 1)
                    else:
                        silent += 1
                        if silent >= silence_blocks:
                            break
                else:
                    frames.append(mono16.copy())
                    if len(frames) > 5:
                        frames.pop(0)
                    if index >= wait_blocks:
                        break

            if heard_live:
                pad_blocks = max(1, int(RECORD_END_PAD_SEC / 0.1))
                for _ in range(pad_blocks):
                    data, _ = stream.read(block)
                    mono = data[:, 0]
                    if self._denoise is not None:
                        mono = self._denoise.process(mono)
                    frames.append(_resample_int(mono, MIC_RATE, SAMPLE_RATE))

        if heard_live:
            threading.Thread(
                target=self.playback.play_sent_cue,
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








    def _watch_barge_in(
        self,
        reply_text: str,
        duration: float,
    ) -> np.ndarray | None:
        """Watch the mic while TTS plays; non-None result means «interrupt me»."""
        deadline = time.monotonic() + duration + 0.2
        # Accept on the idle energy floor — the echo gate handles bleed separately.
        threshold = self.energy_threshold
        open_echo = self.bt.open_speaker_echo()
        hfp = self.bt.hfp_duplex()
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
        return self.wake.wait_mww(
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
    def _normalize(self, text: str) -> str:
        value = (text or "").lower().replace("ё", "е")
        value = re.sub(r"[^a-zа-я0-9_\s]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        value = value.replace("асистент", "ассистент")
        return value

    def close(self) -> None:
        """Stop background polling and release the shared HTTP client."""
        self.alerts.stop()
        self.music.stop()
        try:
            self._http.close()
        except Exception:
            pass


# Facade name for composed services (VoiceClient wires bt/playback/music/alerts).
PiAssistant = VoiceClient


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
        help="Wake score threshold 0–1 (default 0.90, floor 0.90; also reads OWW_THRESHOLD)",
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
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
