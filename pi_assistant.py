"""
Raspberry Pi voice client: wake word → record → backend → play.

Supports two wake engines:
- openWakeWord (OWW): pretrained/custom OWW heads (default)
- microWakeWord (MWW): custom .json + .tflite from microwakeword-trainer

STT/LLM/TTS stay on the backend.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
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
import wave
from pathlib import Path

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
# openWakeWord is most efficient on multiples of 80 ms (1280 samples @ 16 kHz).
OWW_FRAME_SAMPLES = 1280

# Below this the barge-in gate sits at the speaker bleed level and lets it through.
BARGE_MULT_MIN = 1.05
# Soften the reply while we listen for an interrupt — leaves headroom for the
# mic at any system volume (bleed scales with the speakers, the user's voice does not).
BARGE_PLAYBACK_GAIN = 0.62
# Keep feeding OWW this long after a burst dips under the gate.
BARGE_HANGOVER_FRAMES = 4
# Replay the frames just before the burst so the wake phrase keeps its onset.
BARGE_ONSET_FRAMES = 2
# Seconds to wait for the actual command after the reply was cut short.
BARGE_COMMAND_WAIT_SEC = 5.0
# After silence is detected, keep a short pad so word endings aren't clipped.
RECORD_END_PAD_SEC = 0.35
# Quiet tail after TTS so ALSA/Pulse underruns don't eat the last syllable.
TTS_END_PAD_SEC = 0.35
# While waiting for wake with music on: keep mpv quieter so speech
# can clear the mic without shouting (fraction of the user's 1–10 volume).
MUSIC_LISTEN_DUCK = 0.35
# Extra duck once a speech burst is heard over the music floor.
MUSIC_SPEECH_DUCK = 0.12
# Gate over music bleed (slightly softer than TTS barge-in).
MUSIC_GATE_MULT = 1.08

DEFAULT_OWW_MODEL = "alexa"
# HFP Bluetooth mics are quiet/noisy — 0.5 is too strict at room distance.
DEFAULT_OWW_THRESHOLD = 0.35
# tflite works on Pi 3B 32-bit; onnxruntime usually needs arm64/x86_64.
DEFAULT_OWW_FRAMEWORK = "tflite"
DEFAULT_WAKE_ENGINE = "oww"


class VoiceClient:
    """Hands-free Raspberry Pi client for the voice-assistant backend."""

    def __init__(
        self,
        backend_url: str,
        wake_words: list[str] | None = None,
        silence_sec: float = 1.8,
        max_utterance_sec: float = 20.0,
        energy_threshold: float = 0.01,
        wake_engine: str = DEFAULT_WAKE_ENGINE,
        oww_model: str = DEFAULT_OWW_MODEL,
        oww_threshold: float = DEFAULT_OWW_THRESHOLD,
        oww_framework: str = DEFAULT_OWW_FRAMEWORK,
        oww_vad_threshold: float = 0.0,
        oww_model_path: str = "",
        mww_model_config: str = "",
        wake_cooldown_sec: float = 2.0,
        wake_stable_frames: int = 1,
        barge_in: bool = True,
        barge_energy_mult: float = 1.12,
        device_id: str = "default",
        music_poll: bool = True,
        music_poll_interval: float = 2.0,
        mpv_command: str = "",
        audio_device: str | int | None = None,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.device_id = (device_id or "default").strip() or "default"
        self.music_poll = music_poll
        self.music_poll_interval = max(0.5, music_poll_interval)
        self.mpv_command = (mpv_command or "").strip() or shutil.which("mpv") or "mpv"
        self.audio_device = audio_device
        self.wake_engine = (wake_engine or DEFAULT_WAKE_ENGINE).strip().lower()
        if self.wake_engine not in ("oww", "mww"):
            self.wake_engine = DEFAULT_WAKE_ENGINE
        # Display / logging only — detection is the OWW model (default alexa).
        label = (oww_model or DEFAULT_OWW_MODEL).strip() or DEFAULT_OWW_MODEL
        if wake_words:
            normalized = [self._normalize(w) for w in wake_words if w.strip()]
            self.wake_words = list(dict.fromkeys(normalized)) or [label]
        else:
            self.wake_words = [label]
        self.oww_model = label
        self.oww_threshold = float(np.clip(oww_threshold, 0.05, 0.99))
        self.oww_framework = (oww_framework or DEFAULT_OWW_FRAMEWORK).strip().lower()
        if self.oww_framework not in ("onnx", "tflite"):
            self.oww_framework = DEFAULT_OWW_FRAMEWORK
        self.oww_vad_threshold = max(0.0, float(oww_vad_threshold))
        self.oww_model_path = (oww_model_path or "").strip()
        self.mww_model_config = (mww_model_config or "").strip()
        self.silence_sec = silence_sec
        self.max_utterance_sec = max_utterance_sec
        self.energy_threshold = energy_threshold
        self.wake_cooldown_sec = wake_cooldown_sec
        # Consecutive OWW frames (80 ms) above threshold before accept.
        self.wake_stable_frames = max(1, wake_stable_frames)
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
        self._oww = None
        self._oww_labels: list[str] = []
        self._mww = None
        self._mww_features = None
        self._mww_wake_word = ""
        self._wake_mode = "none"
        self._last_wake_ts = 0.0
        self._music_proc: subprocess.Popen | None = None
        self._music_lock = threading.Lock()
        self._music_stop = threading.Event()
        self._current_track_id = ""
        self._current_source = ""
        self._volume_level = 7  # 1..10
        self._mpv_ipc_path = Path(tempfile.gettempdir()) / f"voice-mpv-{os.getpid()}.sock"
        self._in_wake_listen = False

    def run(self) -> None:
        logger.info("Backend: %s", self.backend_url)
        logger.info("Device id: %s", self.device_id)
        logger.info(
            "Wake engine=%s model=%s threshold=%.2f",
            self.wake_engine,
            self.oww_model,
            self.oww_threshold,
        )
        self._configure_audio_device()
        self._warmup_backend()
        self._init_wake()
        self._sync_volume_from_backend()
        if self.music_poll:
            self._start_music_poller()
        logger.info(
            "Wake mode: %s — say «%s …» then your command.",
            self._wake_mode,
            self.wake_words[0] if self.wake_words else "alexa",
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
            # One-shot «Джарвис какая погода»: command already trails the wake
            # in preroll — skip the chime so we don't eat the utterance.
            if self._preroll_still_speaking(preroll):
                logger.info("Speech already in preroll — skip listen cue")
            else:
                self._play_listen_cue()
            interrupted = False
            while True:
                wav_bytes = self._record_until_silence(
                    preroll=preroll,
                    require_speech=interrupted,
                    start_timeout_sec=BARGE_COMMAND_WAIT_SEC,
                )
                if not wav_bytes:
                    if not interrupted:
                        logger.warning("Empty recording, back to wake listen")
                    break
                self._play_sent_cue()
                # Non-None preroll means the user interrupted the reply.
                preroll = self._assist_and_play(wav_bytes)
                self._last_wake_ts = time.monotonic()
                interrupted = preroll is not None
                if preroll is None:
                    # Avoid immediate re-trigger from TTS / echo / leftover speech.
                    time.sleep(self.wake_cooldown_sec)
                    break
                logger.info("Barge-in — capturing new command")
                self._play_listen_cue()

    def _configure_audio_device(self) -> None:
        """Pick sounddevice input/output; log what PortAudio sees (Pulse/ALSA)."""
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
            inp = int(left) if left.strip().isdigit() else None
            out = int(right) if right.strip().isdigit() else None
            return (inp, out)

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

    def _warmup_backend(self) -> None:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.backend_url}/health")
                response.raise_for_status()
        except Exception as exc:
            logger.error("Backend not reachable at %s: %s", self.backend_url, exc)
            sys.exit(1)

    def _init_wake(self) -> None:
        if self.wake_engine == "mww":
            self._init_mww()
        else:
            self._init_oww()

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
            # If threshold is explicit in env/CLI, override trainer default.
            self._mww.probability_cutoff = self.oww_threshold
            self._wake_mode = "mww"
            logger.info(
                "microWakeWord ready: wake=%s cutoff=%.2f config=%s",
                self._mww_wake_word,
                self._mww.probability_cutoff,
                config_path,
            )
        except Exception as exc:
            logger.error("microWakeWord init failed: %s", exc)
            sys.exit(1)

    def _init_oww(self) -> None:
        try:
            import openwakeword
            from openwakeword.model import Model
        except Exception as exc:
            logger.error(
                "openWakeWord import failed (%s). Install: pip install openwakeword onnxruntime",
                exc,
            )
            sys.exit(1)

        try:
            # Mel + embedding (+ optional VAD) live next to the wake head.
            openwakeword.utils.download_models()
            wake_ref = self.oww_model_path or self.oww_model
            frameworks = [self.oww_framework]
            if self.oww_framework == "onnx":
                frameworks.append("tflite")
            elif self.oww_framework == "tflite":
                frameworks.append("onnx")
            last_error: Exception | None = None
            for framework in frameworks:
                try:
                    try:
                        self._oww = Model(
                            wakeword_models=[wake_ref],
                            inference_framework=framework,
                            vad_threshold=self.oww_vad_threshold,
                            enable_speex_noise_suppression=True,
                        )
                    except Exception as speex_exc:
                        logger.warning(
                            "OWW speex NS unavailable (%s) — retry without it",
                            speex_exc,
                        )
                        self._oww = Model(
                            wakeword_models=[wake_ref],
                            inference_framework=framework,
                            vad_threshold=self.oww_vad_threshold,
                        )
                    self.oww_framework = framework
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning("openWakeWord %s init failed: %s", framework, exc)
                    self._oww = None
            if self._oww is None:
                raise RuntimeError(last_error or "no inference framework worked")
            self._oww_labels = list(self._oww.models.keys())
            if not self._oww_labels:
                raise RuntimeError("openWakeWord loaded zero models")
            self._wake_mode = "oww"
            logger.info(
                "openWakeWord ready: models=%s framework=%s threshold=%.2f vad=%.2f",
                self._oww_labels,
                self.oww_framework,
                self.oww_threshold,
                self.oww_vad_threshold,
            )
        except Exception as exc:
            logger.error("openWakeWord init failed: %s", exc)
            sys.exit(1)

    def _wait_for_wake(self) -> np.ndarray | None:
        if self._wake_mode == "mww":
            self._in_wake_listen = True
            try:
                return self._wait_mww(
                    echo_gate=self._is_music_playing(),
                    music_duck=True,
                )
            finally:
                self._in_wake_listen = False
        if self._wake_mode == "oww":
            self._in_wake_listen = True
            try:
                return self._wait_oww(
                    echo_gate=self._is_music_playing(),
                    music_duck=True,
                )
            finally:
                self._in_wake_listen = False
        raise RuntimeError("Wake engine is not initialized")

    def _wait_mww(
        self,
        *,
        deadline: float | None = None,
        energy_threshold: float | None = None,
        respect_cooldown: bool = True,
        echo_gate: bool = False,
        stable_frames: int | None = None,
        music_duck: bool = False,
    ) -> np.ndarray | None:
        from collections import deque

        assert self._mww is not None
        assert self._mww_features is not None
        threshold = self.energy_threshold if energy_threshold is None else energy_threshold
        stable_needed = (
            self.wake_stable_frames if stable_frames is None else max(1, stable_frames)
        )
        score_limit = float(self._mww.probability_cutoff)

        block = OWW_FRAME_SAMPLES
        q: queue.Queue[np.ndarray] = queue.Queue()
        stable = 0
        use_gate = bool(echo_gate)
        music_mode = False
        speech_ducked = False
        # Idle: keep ~2.8s so «Джарвис какая погода» keeps the command tail.
        preroll: deque[np.ndarray] = deque(maxlen=4 if use_gate else 35)
        floor_window: deque[float] = deque(maxlen=16)
        pre_burst_f: deque[np.ndarray] = deque(maxlen=BARGE_ONSET_FRAMES)
        feeding = False
        quiet_frames = 0
        gate_logged = False
        last_heartbeat = 0.0
        last_score_log = 0.0
        peak_energy = 0.0
        peak_score = 0.0
        frames_seen = 0

        def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
            if status:
                logger.warning("mic status: %s", status)
            q.put(indata.copy())

        def sync_music_duck() -> None:
            nonlocal use_gate, music_mode, speech_ducked, preroll
            nonlocal stable, feeding, quiet_frames
            if not music_duck:
                return
            playing = self._is_music_playing()
            if playing and not music_mode:
                music_mode = True
                use_gate = True
                preroll = deque(preroll, maxlen=4)
                floor_window.clear()
                pre_burst_f.clear()
                feeding = False
                quiet_frames = 0
                speech_ducked = False
                stable = 0
                self._mww.reset()
                self._mww_features.reset()
                self._duck_music(MUSIC_LISTEN_DUCK)
            elif not playing and music_mode:
                music_mode = False
                speech_ducked = False
                if not echo_gate:
                    use_gate = False
                    preroll = deque(preroll, maxlen=35)

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
            "Listening for microWakeWord %s (score≥%.2f, stable≥%d)",
            self._mww_wake_word or "mww",
            score_limit,
            stable_needed,
        )

        try:
            self._mww.reset()
            self._mww_features.reset()
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
                    try:
                        chunk = q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    mono = chunk.reshape(-1).astype(np.int16, copy=False)
                    frame_f = mono.astype(np.float32) / 32768.0
                    frame_energy = self._rms(frame_f)
                    frames_seen += 1
                    peak_energy = max(peak_energy, frame_energy)

                    if use_gate:
                        gate_mult = MUSIC_GATE_MULT if music_mode else self.barge_energy_mult
                        gate = self._echo_gate(floor_window, threshold, mult=gate_mult)
                        loud = gate is not None and frame_energy >= gate
                        if not loud and not feeding:
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
                    if best_score >= max(0.12, score_limit * 0.25) and (
                        abs(best_score - last_score_log) >= 0.05
                    ):
                        logger.info(
                            "MWW score %.3f energy=%.4f (need ≥%.2f)",
                            best_score,
                            frame_energy,
                            score_limit,
                        )
                        last_score_log = best_score

                    if now - last_heartbeat >= 5.0:
                        logger.info(
                            "Mic heartbeat: rms=%.5f peak_rms=%.5f mww=%.3f peak_score=%.3f frames=%d",
                            frame_energy,
                            peak_energy,
                            best_score,
                            peak_score,
                            frames_seen,
                        )
                        last_heartbeat = now
                        peak_energy = 0.0
                        peak_score = 0.0

                    if best_score >= score_limit:
                        stable += 1
                    else:
                        stable = 0

                    if stable >= stable_needed:
                        logger.info(
                            "Wake matched (mww): %s score=%.3f energy=%.3f",
                            self._mww_wake_word or "mww",
                            best_score,
                            frame_energy,
                        )
                        return self._preroll_array(preroll)
        finally:
            if music_duck and music_mode:
                self._restore_music_volume()

    def _wait_oww(
        self,
        *,
        deadline: float | None = None,
        energy_threshold: float | None = None,
        respect_cooldown: bool = True,
        echo_gate: bool = False,
        stable_frames: int | None = None,
        music_duck: bool = False,
    ) -> np.ndarray | None:
        """
        Stream the mic until openWakeWord fires; return recent preroll audio.

        With a deadline (barge-in during playback) it returns None on timeout.
        echo_gate keeps TTS/music out of the model: inference starts on a burst
        louder than speaker bleed and continues until the burst ends.
        """
        from collections import deque

        assert self._oww is not None
        threshold = (
            self.energy_threshold if energy_threshold is None else energy_threshold
        )
        stable_needed = (
            self.wake_stable_frames if stable_frames is None else max(1, stable_frames)
        )
        score_limit = self.oww_threshold

        block = OWW_FRAME_SAMPLES
        q: queue.Queue[np.ndarray] = queue.Queue()
        stable = 0
        use_gate = bool(echo_gate)
        music_mode = False
        speech_ducked = False
        # ~1.5 s of float audio after wake for a trailing command fragment.
        # Idle: keep ~2.8s so «Джарвис какая погода» keeps the command tail.
        preroll: deque[np.ndarray] = deque(maxlen=4 if use_gate else 35)
        floor_window: deque[float] = deque(maxlen=16)
        pre_burst_f: deque[np.ndarray] = deque(maxlen=BARGE_ONSET_FRAMES)
        feeding = False
        quiet_frames = 0
        gate_logged = False
        last_score_log = 0.0
        last_heartbeat = 0.0
        frames_seen = 0
        peak_energy = 0.0
        peak_score = 0.0

        def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
            if status:
                logger.warning("mic status: %s", status)
            q.put(indata.copy())

        def sync_music_duck() -> None:
            nonlocal use_gate, music_mode, speech_ducked, preroll
            nonlocal stable, feeding, quiet_frames
            if not music_duck:
                return
            playing = self._is_music_playing()
            if playing and not music_mode:
                music_mode = True
                use_gate = True
                preroll = deque(preroll, maxlen=4)
                floor_window.clear()
                pre_burst_f.clear()
                feeding = False
                quiet_frames = 0
                speech_ducked = False
                stable = 0
                self._oww.reset()
                self._duck_music(MUSIC_LISTEN_DUCK)
                logger.info(
                    "Music on — duck×%.2f + speech gate for wake listen",
                    MUSIC_LISTEN_DUCK,
                )
            elif not playing and music_mode:
                music_mode = False
                speech_ducked = False
                if not echo_gate:
                    use_gate = False
                    preroll = deque(preroll, maxlen=35)

        def set_feeding(active: bool) -> None:
            nonlocal feeding, quiet_frames, speech_ducked, stable
            if active == feeding:
                return
            feeding = active
            quiet_frames = 0
            if active:
                self._oww.reset()
                stable = 0
            if not music_mode:
                return
            if active:
                self._duck_music(MUSIC_SPEECH_DUCK)
                speech_ducked = True
            elif speech_ducked:
                self._duck_music(MUSIC_LISTEN_DUCK)
                speech_ducked = False

        if deadline is None:
            logger.info(
                "Listening for openWakeWord %s (score≥%.2f, stable≥%d×80ms, energy≥%.3f)",
                self._oww_labels,
                score_limit,
                stable_needed,
                threshold,
            )
        else:
            logger.info(
                "Barge-in armed: say «%s …» to interrupt (score≥%.2f, energy≥%.3f)",
                self.wake_words[0] if self.wake_words else "alexa",
                score_limit,
                threshold,
            )

        try:
            self._oww.reset()
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
                    try:
                        chunk = q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    mono = chunk.reshape(-1)
                    if mono.size != block:
                        # PortAudio sometimes delivers short frames — pad/trim.
                        if mono.size < block:
                            mono = np.pad(mono, (0, block - mono.size))
                        else:
                            mono = mono[:block]
                    frame_f = mono.astype(np.float32) / 32768.0
                    frame_energy = self._rms(frame_f)
                    frames_seen += 1
                    if frame_energy > peak_energy:
                        peak_energy = frame_energy
                    if frames_seen == 1:
                        logger.info(
                            "Mic first frame: samples=%d rms=%.5f peak=%.5f",
                            mono.size,
                            frame_energy,
                            float(np.max(np.abs(frame_f))),
                        )

                    if use_gate:
                        gate_mult = (
                            MUSIC_GATE_MULT if music_mode else self.barge_energy_mult
                        )
                        gate = self._echo_gate(
                            floor_window, threshold, mult=gate_mult
                        )
                        loud = gate is not None and frame_energy >= gate
                        if not loud and not feeding:
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
                            continue
                        preroll.append(frame_f.copy())
                    else:
                        preroll.append(frame_f.copy())

                    now = time.monotonic()
                    if (
                        respect_cooldown
                        and now - self._last_wake_ts < self.wake_cooldown_sec
                    ):
                        # Keep the model warm but ignore hits during cooldown.
                        self._oww.predict(mono)
                        stable = 0
                        continue

                    prediction = self._oww.predict(mono)
                    best_name = ""
                    best_score = 0.0
                    # Prefer configured labels, but also scan every key OWW returns
                    # (some builds name heads alexa_v0.1 instead of alexa).
                    for name, raw in prediction.items():
                        try:
                            score = float(np.asarray(raw).reshape(-1)[0])
                        except Exception:
                            continue
                        if score > best_score:
                            best_score = score
                            best_name = str(name)
                    if best_score > peak_score:
                        peak_score = best_score

                    now = time.monotonic()
                    if now - last_heartbeat >= 5.0:
                        logger.info(
                            "Mic heartbeat: rms=%.5f peak_rms=%.5f oww=%s=%.3f peak_score=%.3f frames=%d",
                            frame_energy,
                            peak_energy,
                            best_name or (self._oww_labels[0] if self._oww_labels else "?"),
                            best_score,
                            peak_score,
                            frames_seen,
                        )
                        last_heartbeat = now
                        peak_energy = 0.0

                    if best_score >= max(0.12, score_limit * 0.35) and (
                        abs(best_score - last_score_log) >= 0.05
                    ):
                        logger.info(
                            "OWW score %s=%.3f energy=%.4f",
                            best_name,
                            best_score,
                            frame_energy,
                        )
                        last_score_log = best_score

                    # HFP Bluetooth mics are quiet — if OWW is confident, accept
                    # even when RMS looks near silence.
                    energy_ok = (
                        frame_energy >= min(threshold * 0.35, 0.001)
                        or best_score >= score_limit
                    )
                    if best_score >= score_limit and energy_ok:
                        stable += 1
                    else:
                        stable = 0

                    if stable >= stable_needed:
                        logger.info(
                            "Wake matched (oww): %s score=%.3f energy=%.3f",
                            best_name or self.oww_model,
                            best_score,
                            frame_energy,
                        )
                        return self._preroll_array(preroll)
        finally:
            if music_duck and music_mode:
                self._restore_music_volume()

    def _preroll_array(self, preroll) -> np.ndarray | None:
        if not preroll:
            return None
        return np.concatenate(list(preroll))

    def _preroll_still_speaking(self, preroll: np.ndarray | None) -> bool:
        """True if the wake preroll's tail still looks like live speech."""
        if preroll is None or preroll.size < int(SAMPLE_RATE * 0.12):
            return False
        tail = preroll[-int(SAMPLE_RATE * 0.3) :]
        return self._rms(tail) >= max(self.energy_threshold * 0.35, 0.0008)

    def _echo_gate(
        self,
        window,
        threshold: float,
        *,
        mult: float | None = None,
    ) -> float | None:
        """
        Energy a voice must beat to be heard over our own playback.

        Use a mid/upper percentile of the bleed, not the absolute peak: the
        reply's stressed syllables already sit near the top of the window, and
        a peak×margin gate rises with the speaker volume until a normal voice
        can no longer clear it. Playback is also ducked while we listen.
        """
        if len(window) < 3:
            return None
        ordered = sorted(window)
        index = min(len(ordered) - 1, int(len(ordered) * 0.75))
        factor = self.barge_energy_mult if mult is None else max(BARGE_MULT_MIN, mult)
        return max(threshold, ordered[index] * factor)

    def _assist_and_play(self, wav_bytes: bytes) -> np.ndarray | None:
        """Send audio, play the reply; return barge-in preroll when interrupted."""
        logger.info("Sending utterance to backend (%d bytes)", len(wav_bytes))
        with httpx.Client(timeout=300.0) as client:
            files = {"file": ("command.wav", wav_bytes, "audio/wav")}
            data = {
                "return_audio": "true",
                "device_id": self.device_id,
            }
            response = client.post(
                f"{self.backend_url}/v1/assist",
                files=files,
                data=data,
            )
            if response.status_code >= 400:
                logger.error(
                    "Assist failed: %s %s",
                    response.status_code,
                    response.text,
                )
                return None

            transcript, reply, audio, playback = self._parse_assist_response(response)
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
            return preroll

    def _parse_assist_response(
        self,
        response: httpx.Response,
    ) -> tuple[str, str, bytes, dict | None]:
        """
        Prefer JSON {transcript, reply, audio_base64, playback} (UTF-8 safe).
        Fall back to legacy raw WAV + X-*-B64 / X-* headers.
        """
        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                payload = response.json()
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

        transcript = self._header_text(response, "Transcript")
        reply = self._header_text(response, "Reply")
        return transcript, reply, response.content, None

    def _header_text(self, response: httpx.Response, name: str) -> str:
        """Decode UTF-8 text from X-{name}-B64 (fallback to legacy X-{name})."""
        b64 = response.headers.get(f"X-{name}-B64", "")
        if b64:
            try:
                return base64.b64decode(b64.encode("ascii")).decode("utf-8")
            except Exception:
                logger.warning("Failed to decode X-%s-B64", name)
        return response.headers.get(f"X-{name}", "")

    def _record_until_silence(
        self,
        preroll: np.ndarray | None = None,
        require_speech: bool = False,
        start_timeout_sec: float | None = None,
    ) -> bytes:
        """
        Record one utterance: preroll + mic until silence_sec of quiet.

        require_speech waits for fresh speech instead of trusting the preroll —
        after a barge-in the preroll is loud from our own reply, and without it
        we would ship the wake phrase alone as the command.
        """
        block = int(SAMPLE_RATE * 0.1)
        silence_blocks = int(self.silence_sec / 0.1)
        max_blocks = int(self.max_utterance_sec / 0.1)
        wait_blocks = int((start_timeout_sec or self.max_utterance_sec) / 0.1)
        # Soft floor so quiet word endings don't count as silence immediately.
        continue_threshold = self.energy_threshold * 0.4
        lead: list[np.ndarray] = []
        frames: list[np.ndarray] = []
        silent = 0
        started = False

        if preroll is not None and preroll.size:
            lead.append(preroll.astype(np.float32, copy=False))
            if not require_speech and self._rms(preroll) >= self.energy_threshold * 0.5:
                started = True

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=block,
        ) as stream:
            for index in range(max_blocks):
                data, _ = stream.read(block)
                mono = data[:, 0]
                energy = self._rms(mono)
                if energy >= self.energy_threshold:
                    started = True
                    silent = 0
                    frames.append(mono.copy())
                elif started:
                    frames.append(mono.copy())
                    if energy >= continue_threshold:
                        # Soft speech / trailing consonants — don't rush to end.
                        silent = max(0, silent - 1)
                    else:
                        silent += 1
                        if silent >= silence_blocks:
                            break
                else:
                    frames.append(mono.copy())
                    if len(frames) > 5:
                        frames.pop(0)
                    if require_speech and index >= wait_blocks:
                        break

            # Extra quiet pad after end-of-utterance so last syllables survive.
            if started:
                pad_blocks = max(1, int(RECORD_END_PAD_SEC / 0.1))
                for _ in range(pad_blocks):
                    data, _ = stream.read(block)
                    frames.append(data[:, 0].copy())

        if require_speech and not started:
            logger.info("No command after the barge-in — back to wake listen")
            return b""
        chunks = lead + frames
        if not chunks:
            return b""
        return self._frames_to_wav(np.concatenate(chunks))

    def _play_listen_cue(self) -> None:
        """Short pleasant chime: assistant is listening for the command."""
        try:
            audio = self._make_listen_chime(SAMPLE_RATE) * self._assistant_gain()
            sd.play(audio, SAMPLE_RATE, blocking=True)
            # Let the room settle so the chime isn't captured as speech.
            time.sleep(0.12)
        except Exception:
            logger.debug("Listen cue playback failed", exc_info=True)

    def _play_sent_cue(self) -> None:
        """Different chime: listening finished, request is being sent."""
        try:
            audio = self._make_sent_chime(SAMPLE_RATE) * self._assistant_gain()
            sd.play(audio, SAMPLE_RATE, blocking=True)
        except Exception:
            logger.debug("Sent cue playback failed", exc_info=True)

    @staticmethod
    def _make_listen_chime(rate: int) -> np.ndarray:
        """Soft two-tone 'listening' ding (~320ms), ascending."""
        def tone(freq: float, duration: float, amplitude: float = 0.22) -> np.ndarray:
            n = max(1, int(rate * duration))
            t = np.arange(n, dtype=np.float32) / float(rate)
            attack = min(n, int(rate * 0.02))
            release = min(n, int(rate * 0.08))
            env = np.ones(n, dtype=np.float32)
            if attack > 0:
                env[:attack] = np.linspace(0.0, 1.0, attack, dtype=np.float32)
            if release > 0:
                env[-release:] = np.linspace(1.0, 0.0, release, dtype=np.float32)
            wave = np.sin(2.0 * np.pi * freq * t) * env * amplitude
            wave += np.sin(4.0 * np.pi * freq * t) * env * (amplitude * 0.18)
            return wave.astype(np.float32)

        gap = np.zeros(int(rate * 0.04), dtype=np.float32)
        chime = np.concatenate(
            [
                tone(784.0, 0.11),   # G5
                gap,
                tone(1046.5, 0.16), # C6
                np.zeros(int(rate * 0.05), dtype=np.float32),
            ]
        )
        peak = float(np.max(np.abs(chime)) or 1.0)
        if peak > 0.35:
            chime *= 0.35 / peak
        return chime

    @staticmethod
    def _make_sent_chime(rate: int) -> np.ndarray:
        """Soft two-tone 'sent' ding (~280ms), descending — distinct from listen."""
        def tone(freq: float, duration: float, amplitude: float = 0.20) -> np.ndarray:
            n = max(1, int(rate * duration))
            t = np.arange(n, dtype=np.float32) / float(rate)
            attack = min(n, int(rate * 0.015))
            release = min(n, int(rate * 0.07))
            env = np.ones(n, dtype=np.float32)
            if attack > 0:
                env[:attack] = np.linspace(0.0, 1.0, attack, dtype=np.float32)
            if release > 0:
                env[-release:] = np.linspace(1.0, 0.0, release, dtype=np.float32)
            wave = np.sin(2.0 * np.pi * freq * t) * env * amplitude
            wave += np.sin(4.0 * np.pi * freq * t) * env * (amplitude * 0.12)
            return wave.astype(np.float32)

        gap = np.zeros(int(rate * 0.03), dtype=np.float32)
        chime = np.concatenate(
            [
                tone(880.0, 0.09),  # A5
                gap,
                tone(659.3, 0.14),  # E5 — lower confirmation
                np.zeros(int(rate * 0.04), dtype=np.float32),
            ]
        )
        peak = float(np.max(np.abs(chime)) or 1.0)
        if peak > 0.32:
            chime *= 0.32 / peak
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
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(url)
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
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url)
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
        if action == "play":
            stream_url = str(command.get("url") or "").strip()
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

    def _start_music(
        self,
        stream_url: str,
        *,
        track_id: str = "",
        source: str = "",
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
                if self._in_wake_listen:
                    start_vol = max(5, int(round(percent * MUSIC_LISTEN_DUCK)))
                self._music_proc = subprocess.Popen(
                    [
                        self.mpv_command,
                        "--no-video",
                        "--really-quiet",
                        f"--volume={start_vol}",
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
        # Natural end — ask server for next Моя волна track if active.
        if source == "yandex-wave" or track_id:
            self._report_track_finished(track_id)

    def _report_track_finished(self, track_id: str) -> None:
        url = f"{self.backend_url}/v1/music/status/{self.device_id}"
        try:
            with httpx.Client(timeout=20.0) as client:
                client.post(
                    url,
                    json={
                        "device_id": self.device_id,
                        "playing": False,
                        "action": "track_finished",
                        "title": "",
                        "artist": "",
                    },
                )
            logger.info("Reported track_finished (track_id=%s)", track_id or "-")
        except Exception as exc:
            logger.debug("track_finished report failed: %s", exc)

    def _pause_music(self) -> None:
        with self._music_lock:
            if self._music_proc and self._music_proc.poll() is None:
                self._music_proc.send_signal(signal.SIGSTOP)

    def _stop_music(self) -> None:
        with self._music_lock:
            self._current_track_id = ""
            self._current_source = ""
            self._terminate_music_proc()

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

    def _play_wav(self, wav_bytes: bytes, reply_text: str = "") -> np.ndarray | None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            path = Path(tmp.name)
        try:
            with wave.open(str(path), "rb") as wav:
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
            # Trailing silence — ALSA underruns often clip the last syllable.
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

            if not self.barge_in:
                sd.play(data, rate)
                sd.wait()
                return None

            # Duck the reply so bleed stays below a normal speaking voice at any
            # system volume — energy gates alone cannot outrun the speakers.
            data = data * BARGE_PLAYBACK_GAIN
            duration = len(data) / float(rate)
            sd.play(data, rate)
            try:
                preroll = self._watch_barge_in(reply_text, duration)
            finally:
                sd.stop()
            if preroll is not None:
                logger.info("Barge-in detected — playback stopped")
            return preroll
        finally:
            path.unlink(missing_ok=True)

    def _watch_barge_in(
        self,
        reply_text: str,
        duration: float,
    ) -> np.ndarray | None:
        """Watch the mic while TTS plays; non-None result means «interrupt me»."""
        deadline = time.monotonic() + duration + 0.2
        # Accept on the idle energy floor — the echo gate handles bleed separately.
        # Raising this with the speakers made high-volume barge-in impossible.
        threshold = self.energy_threshold
        # Energy gate + OWW score replace Vosk echo-text filtering.
        _ = reply_text
        if self._wake_mode == "mww":
            return self._wait_mww(
                deadline=deadline,
                energy_threshold=threshold,
                respect_cooldown=False,
                echo_gate=True,
                stable_frames=1,
            )
        if self._wake_mode == "oww":
            return self._wait_oww(
                deadline=deadline,
                energy_threshold=threshold,
                respect_cooldown=False,
                echo_gate=True,
                # Over the reply the phrase arrives once — waiting for many
                # matching frames usually means missing it.
                stable_frames=1,
            )
        return None

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

    def _normalize(self, text: str) -> str:
        value = (text or "").lower().replace("ё", "е")
        value = re.sub(r"[^a-zа-я0-9_\s]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        value = value.replace("асистент", "ассистент")
        return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Raspberry Pi voice client for Open WebUI (OWW/MWW + mpv)",
    )
    parser.add_argument(
        "--backend",
        default=os.getenv("VOICE_BACKEND_URL", "http://voice.pora-ai.ru"),
    )
    parser.add_argument(
        "--wake-engine",
        default=os.getenv("WAKE_ENGINE", DEFAULT_WAKE_ENGINE),
        help="Wake engine: oww (openWakeWord) or mww (microWakeWord)",
    )
    parser.add_argument(
        "--oww-model",
        default=os.getenv("OWW_MODEL", DEFAULT_OWW_MODEL),
        help="openWakeWord pretrained name (default: alexa)",
    )
    parser.add_argument(
        "--oww-threshold",
        type=float,
        default=float(os.getenv("OWW_THRESHOLD", str(DEFAULT_OWW_THRESHOLD))),
        help="Wake score threshold 0–1 (default 0.35)",
    )
    parser.add_argument(
        "--oww-framework",
        default=os.getenv("OWW_FRAMEWORK", DEFAULT_OWW_FRAMEWORK),
        help="Inference backend: onnx or tflite (default tflite)",
    )
    parser.add_argument(
        "--oww-vad",
        type=float,
        default=float(os.getenv("OWW_VAD_THRESHOLD", "0")),
        help="Silero VAD gate for OWW (0=off, try 0.5 to cut false accepts)",
    )
    parser.add_argument(
        "--oww-model-path",
        default=os.getenv("OWW_MODEL_PATH", ""),
        help="Optional path to a custom .onnx/.tflite wake model",
    )
    parser.add_argument(
        "--mww-model-config",
        default=os.getenv("MWW_MODEL_CONFIG", ""),
        help="Path to *_mww.json from microwakeword-trainer",
    )
    parser.add_argument(
        "--wake",
        default=os.getenv("WAKE_WORDS", ""),
        help="Optional display label (detection uses OWW_MODEL)",
    )
    parser.add_argument(
        "--silence",
        type=float,
        default=float(os.getenv("SILENCE_SEC", "1.8")),
        help="Seconds of quiet before ending the command recording (default 1.8)",
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
        default=int(os.getenv("WAKE_STABLE_FRAMES", "1")),
        help="OWW: consecutive 80ms frames above threshold before accept",
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
    args = parser.parse_args()

    wake_words = [raw.strip() for raw in args.wake.split(",") if raw.strip()]

    client = VoiceClient(
        backend_url=args.backend,
        wake_words=wake_words or None,
        silence_sec=args.silence,
        max_utterance_sec=args.max_sec,
        energy_threshold=args.energy,
        wake_engine=args.wake_engine,
        oww_model=args.oww_model,
        oww_threshold=args.oww_threshold,
        oww_framework=args.oww_framework,
        oww_vad_threshold=args.oww_vad,
        oww_model_path=args.oww_model_path,
        mww_model_config=args.mww_model_config,
        wake_cooldown_sec=args.wake_cooldown,
        wake_stable_frames=args.wake_stable,
        barge_in=args.barge_in,
        barge_energy_mult=args.barge_energy_mult,
        device_id=args.device_id,
        music_poll=args.music_poll,
        music_poll_interval=args.music_poll_interval,
        mpv_command=args.mpv,
        audio_device=args.audio_device or None,
    )
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
