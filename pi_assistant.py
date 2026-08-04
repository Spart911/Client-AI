"""
Raspberry Pi voice client: Vosk wake («ассистент») → record → backend → play.

Optimized for Pi 3B: no openwakeword / onnxruntime — only Vosk + sounddevice + mpv.

Barge-in: the wake phrase is also watched while TTS plays, so saying
«ассистент …» cuts playback short and records the new command right away.
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
import zipfile
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

# Below this the barge-in gate sits at the speaker bleed level and lets it through.
BARGE_MULT_MIN = 1.05
# Soften the reply while we listen for an interrupt — leaves headroom for the
# mic at any system volume (bleed scales with the speakers, the user's voice does not).
BARGE_PLAYBACK_GAIN = 0.62
# Keep decoding this long after a burst dips under the gate: quiet parts inside
# a word are still speech, and dropping them leaves Vosk with unusable scraps.
BARGE_HANGOVER_FRAMES = 4
# Replay the frames just before the burst so the wake phrase keeps its onset.
BARGE_ONSET_FRAMES = 2
# Seconds to wait for the actual command after the reply was cut short.
BARGE_COMMAND_WAIT_SEC = 5.0
# Drop the recognizer if the partial is this long and still no wake — TTS junk.
BARGE_MAX_PARTIAL_WORDS = 8

# Small RU model — fits Raspberry Pi 3B (≈50MB).
VOSK_MODEL_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
)
VOSK_MODEL_DIRNAME = "vosk-model-small-ru-0.22"


class VoiceClient:
    """Hands-free Raspberry Pi client for the voice-assistant backend."""

    def __init__(
        self,
        backend_url: str,
        wake_words: list[str],
        silence_sec: float = 1.2,
        max_utterance_sec: float = 20.0,
        energy_threshold: float = 0.01,
        vosk_model_path: str = "",
        wake_min_conf: float = 0.55,
        wake_cooldown_sec: float = 2.0,
        wake_stable_frames: int = 2,
        barge_in: bool = True,
        barge_energy_mult: float = 1.12,
        device_id: str = "default",
        music_poll: bool = True,
        music_poll_interval: float = 2.0,
        mpv_command: str = "",
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.device_id = (device_id or "default").strip() or "default"
        self.music_poll = music_poll
        self.music_poll_interval = max(0.5, music_poll_interval)
        self.mpv_command = (mpv_command or "").strip() or shutil.which("mpv") or "mpv"
        normalized = [self._normalize(w) for w in wake_words if w.strip()]
        self.wake_words = list(dict.fromkeys(normalized))
        self.silence_sec = silence_sec
        self.max_utterance_sec = max_utterance_sec
        self.energy_threshold = energy_threshold
        self.vosk_model_path = vosk_model_path
        self.wake_min_conf = wake_min_conf
        self.wake_cooldown_sec = wake_cooldown_sec
        # ~250ms blocks: 3 frames ≈ 0.75s of stable partial before accept.
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
        self._vosk_model = None
        self._wake_mode = "none"
        self._last_wake_ts = 0.0
        self._music_proc: subprocess.Popen | None = None
        self._music_lock = threading.Lock()
        self._music_stop = threading.Event()

    def run(self) -> None:
        logger.info("Backend: %s", self.backend_url)
        logger.info("Device id: %s", self.device_id)
        logger.info("Wake phrases: %s", self.wake_words)
        self._warmup_backend()
        self._init_wake()
        if self.music_poll:
            self._start_music_poller()
        logger.info(
            "Wake mode: %s — say «%s …» then your command (one phrase is ok).",
            self._wake_mode,
            self.wake_words[0] if self.wake_words else "wake",
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
                )
                if not wav_bytes:
                    if not interrupted:
                        logger.warning("Empty recording, back to wake listen")
                    break
                # Non-None preroll means the user interrupted the reply.
                preroll = self._assist_and_play(wav_bytes)
                self._last_wake_ts = time.monotonic()
                interrupted = preroll is not None
                if preroll is None:
                    # Avoid immediate re-trigger from TTS / echo / leftover speech.
                    time.sleep(self.wake_cooldown_sec)
                    break
                logger.info("Barge-in — capturing new command")

    def _warmup_backend(self) -> None:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.backend_url}/health")
                response.raise_for_status()
        except Exception as exc:
            logger.error("Backend not reachable at %s: %s", self.backend_url, exc)
            sys.exit(1)

    def _init_wake(self) -> None:
        self._init_vosk()

    def _init_vosk(self) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel

            SetLogLevel(-1)
            model_path = self._ensure_vosk_model()
            self._vosk_model = Model(model_path)
            # Free decoder (no grammar): grammar forces similar words like
            # «асимметрию» into «ассистент».
            _ = KaldiRecognizer(self._vosk_model, SAMPLE_RATE)
            self._wake_mode = "vosk"
            logger.info("Vosk Russian wake ready (%s)", model_path)
            logger.info("Wake match: phrase prefix (no grammar)")
        except Exception as exc:
            logger.error(
                "Vosk wake init failed (%s). Install: pip install vosk",
                exc,
            )
            sys.exit(1)

    def _wait_for_wake(self) -> np.ndarray | None:
        if self._wake_mode == "vosk":
            return self._wait_vosk()
        raise RuntimeError("Wake engine is not initialized")

    def _wait_vosk(
        self,
        *,
        deadline: float | None = None,
        energy_threshold: float | None = None,
        echo_text: str = "",
        respect_cooldown: bool = True,
        echo_gate: bool = False,
        stable_frames: int | None = None,
    ) -> np.ndarray | None:
        """
        Stream the mic until the wake phrase is heard; return the preroll audio.

        With a deadline (barge-in during playback) it returns None on timeout.
        echo_gate keeps our own TTS out of the recognizer: decoding starts on a
        burst louder than the speaker bleed and continues until the burst ends,
        so the phrase reaches Vosk whole.
        """
        from collections import deque

        from vosk import KaldiRecognizer

        assert self._vosk_model is not None
        threshold = (
            self.energy_threshold if energy_threshold is None else energy_threshold
        )
        stable_needed = (
            self.wake_stable_frames if stable_frames is None else max(1, stable_frames)
        )
        # Free ASR — do not use keyword grammar (maps lookalikes to the wake word).
        recognizer = KaldiRecognizer(self._vosk_model, SAMPLE_RATE)
        recognizer.SetWords(True)

        block = 4000  # ~250 ms @ 16 kHz int16
        q: queue.Queue[np.ndarray] = queue.Queue()
        stable = 0
        last_partial = ""
        last_heard_log = ""
        # Finals arrive on a silence frame — keep recent energies for the gate.
        recent_energy: deque[float] = deque(maxlen=8)
        # Keep ~2s of float audio so «ассистент как дела» is not lost after wake.
        # While TTS plays, keep less so the reply itself does not leak into it.
        preroll: deque[np.ndarray] = deque(maxlen=4 if echo_gate else 8)
        # Speaker bleed defines the noise floor during playback.
        floor_window: deque[float] = deque(maxlen=16)
        pre_burst: deque[bytes] = deque(maxlen=BARGE_ONSET_FRAMES)
        pre_burst_f: deque[np.ndarray] = deque(maxlen=BARGE_ONSET_FRAMES)
        feeding = False
        quiet_frames = 0
        gate_logged = False

        def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
            if status:
                logger.debug("mic status: %s", status)
            q.put(indata.copy())

        if deadline is None:
            logger.info(
                "Listening for: %s (prefix, stable≥%d×250ms, energy≥%.3f)",
                ", ".join(self.wake_words),
                self.wake_stable_frames,
                threshold,
            )
        else:
            logger.info(
                "Barge-in armed: say «%s …» to interrupt (energy≥%.3f)",
                self.wake_words[0] if self.wake_words else "wake",
                threshold,
            )
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
                try:
                    chunk = q.get(timeout=0.25)
                except queue.Empty:
                    continue
                mono = chunk.reshape(-1)
                pcm = mono.tobytes()
                frame_f = mono.astype(np.float32) / 32768.0
                frame_energy = self._rms(frame_f)
                recent_energy.append(frame_energy)
                if not echo_gate:
                    preroll.append(frame_f.copy())
                energy = max(recent_energy) if recent_energy else frame_energy

                if echo_gate:
                    gate = self._echo_gate(floor_window, threshold)
                    loud = gate is not None and frame_energy >= gate
                    if not loud and not feeding:
                        # Only the reply feeds the estimate — counting the user's
                        # own voice would raise the gate against them.
                        floor_window.append(frame_energy)
                    if loud:
                        quiet_frames = 0
                        if not feeding:
                            # New burst over the reply: decode it on its own, so
                            # the wake phrase can still be the first thing heard.
                            self._reset_recognizer(recognizer)
                            stable = 0
                            last_partial = ""
                            feeding = True
                            for onset in pre_burst:
                                recognizer.AcceptWaveform(onset)
                            pre_burst.clear()
                            preroll.extend(pre_burst_f)
                            pre_burst_f.clear()
                            if not gate_logged:
                                logger.info(
                                    "Barge-in gate %.3f — heard %.3f over the reply",
                                    gate,
                                    frame_energy,
                                )
                                gate_logged = True
                    elif feeding:
                        quiet_frames += 1
                        if quiet_frames > BARGE_HANGOVER_FRAMES:
                            feeding = False
                    if not feeding:
                        pre_burst.append(pcm)
                        pre_burst_f.append(frame_f.copy())
                        continue
                    # Preroll of a barge-in carries the user only: the reply we
                    # gated out has no business in the command we send off.
                    preroll.append(frame_f.copy())

                now = time.monotonic()
                if respect_cooldown and now - self._last_wake_ts < self.wake_cooldown_sec:
                    stable = 0
                    last_partial = ""
                    recognizer.AcceptWaveform(pcm)
                    continue

                if recognizer.AcceptWaveform(pcm):
                    payload = json.loads(recognizer.Result())
                    heard = self._normalize(payload.get("text") or "")
                    conf = self._wake_confidence(payload)
                    if heard and heard != last_heard_log:
                        logger.info("Heard (final): %r", heard)
                        last_heard_log = heard
                    text = (
                        self._wake_text_from_echo(heard, echo_text)
                        if echo_gate
                        else heard
                    )
                    if self._accept_wake(
                        text,
                        energy,
                        conf,
                        source="final",
                        threshold=threshold,
                        echo_text=echo_text,
                    ):
                        return self._preroll_array(preroll)
                    if echo_gate and (
                        self._looks_like_echo(heard, echo_text)
                        or (heard and not self._wake_match(text))
                    ):
                        self._reset_recognizer(recognizer)
                        feeding = False
                        quiet_frames = 0
                        preroll.clear()
                    stable = 0
                    last_partial = ""
                    continue

                partial = json.loads(recognizer.PartialResult()).get("partial") or ""
                heard = self._normalize(partial)
                text = (
                    self._wake_text_from_echo(heard, echo_text) if echo_gate else heard
                )
                if echo_gate and heard and not text:
                    # Entire partial was reply bleed — clear and wait for a new onset.
                    if heard != last_heard_log:
                        logger.info("Heard (partial): %r", heard)
                        last_heard_log = heard
                    self._reset_recognizer(recognizer)
                    stable = 0
                    last_partial = ""
                    feeding = False
                    quiet_frames = 0
                    preroll.clear()
                    continue
                if not text or frame_energy < threshold * 0.5:
                    # Do not reset stable on short dips if we already matched prefix.
                    if not self._wake_match(last_partial):
                        stable = 0
                        last_partial = ""
                    continue

                matched = self._wake_match(text)
                if not matched:
                    if heard != last_partial and heard != last_heard_log:
                        logger.info("Heard (partial): %r", heard)
                        last_heard_log = heard
                    stable = 0
                    last_partial = text
                    if echo_gate and (
                        self._looks_like_echo(heard, echo_text)
                        or len(heard.split()) >= BARGE_MAX_PARTIAL_WORDS
                    ):
                        # Our words slipped past the gate, or the partial is a
                        # long TTS mess. Drop them so the wake phrase can still
                        # land at the start of the next burst.
                        self._reset_recognizer(recognizer)
                        last_partial = ""
                        feeding = False
                        quiet_frames = 0
                        preroll.clear()
                    continue

                # Prefix stays matched while user continues: «ассистент» → «ассистент как».
                if self._wake_match(last_partial):
                    stable += 1
                else:
                    stable = 1
                    logger.info("Wake candidate start: %r energy=%.3f", text, frame_energy)
                last_partial = text

                if stable >= stable_needed:
                    if self._accept_wake(
                        text,
                        energy,
                        conf=1.0,
                        source="partial",
                        threshold=threshold,
                        echo_text=echo_text,
                    ):
                        return self._preroll_array(preroll)
                    stable = 0
                    last_partial = ""

    def _preroll_array(self, preroll) -> np.ndarray | None:
        if not preroll:
            return None
        return np.concatenate(list(preroll))

    def _echo_gate(self, window, threshold: float) -> float | None:
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
        return max(threshold, ordered[index] * self.barge_energy_mult)

    def _wake_text_from_echo(self, text: str, echo_text: str) -> str:
        """
        Recover a wake-leading phrase from a stream that still contains the reply.

        Interrupting means the recognizer hears the reply and the user in one
        breath. Prefer text that starts at the wake word after reply tokens (or a
        short stretch of misheard junk); otherwise strip leading reply words.
        """
        if not text:
            return text
        tokens = text.split()
        echo_tokens = set(echo_text.split()) if echo_text else set()
        wake_phrases = [w.split() for w in self.wake_words if w.split()]
        for i in range(len(tokens)):
            for phrase in wake_phrases:
                n = len(phrase)
                if tokens[i : i + n] != phrase:
                    continue
                before = tokens[:i]
                if (
                    not before
                    or all(t in echo_tokens for t in before)
                    or len(before) <= 3
                ):
                    return " ".join(tokens[i:])
                return text
        index = 0
        while index < len(tokens) and tokens[index] in echo_tokens:
            index += 1
        return " ".join(tokens[index:]) if index else text

    def _looks_like_echo(self, text: str, echo_text: str) -> bool:
        """True when the words we just decoded are our own reply coming back."""
        if not text or not echo_text:
            return False
        tokens = text.split()
        # Two words in a row is where a coincidence stops being likely.
        for size in (3, 2):
            if len(tokens) >= size and " ".join(tokens[-size:]) in echo_text:
                return True
        # Long partial made mostly of reply words.
        if len(tokens) >= 4:
            echo_tokens = set(echo_text.split())
            hits = sum(1 for t in tokens if t in echo_tokens)
            if hits / len(tokens) >= 0.6:
                return True
        return False

    def _reset_recognizer(self, recognizer) -> None:
        try:
            recognizer.Reset()
        except Exception:
            logger.debug("Vosk Reset failed", exc_info=True)

    def _accept_wake(
        self,
        text: str,
        energy: float,
        conf: float,
        source: str,
        threshold: float | None = None,
        echo_text: str = "",
    ) -> bool:
        if not text or text == "unk":
            return False
        limit = self.energy_threshold if threshold is None else threshold
        if energy < limit:
            logger.info("Skip wake (%s): low energy %.4f text=%r", source, energy, text)
            return False
        matched = self._wake_match(text)
        if not matched:
            return False
        if echo_text and text in echo_text:
            logger.info("Skip wake (%s): TTS echo text=%r", source, text)
            return False
        # conf==0 means "unknown" (no word scores) — do not hard-reject.
        if conf > 0.0 and conf < self.wake_min_conf:
            logger.info(
                "Skip wake (%s): low conf %.2f text=%r",
                source,
                conf,
                text,
            )
            return False
        logger.info(
            "Wake matched (%s): %s ← %r energy=%.3f conf=%.2f",
            source,
            matched,
            text,
            energy,
            conf,
        )
        return True

    def _wake_match(self, text: str) -> str | None:
        """
        Wake if the utterance *starts with* the wake phrase as whole tokens.

        Supports one-shot «ассистент как дела». Rejects lookalikes like «асимметрию».
        """
        if not text:
            return None
        tokens = text.split()
        for phrase in self.wake_words:
            phrase_tokens = phrase.split()
            n = len(phrase_tokens)
            if n == 0 or len(tokens) < n:
                continue
            if tokens[:n] == phrase_tokens:
                return phrase
        return None

    def _wake_confidence(self, payload: dict) -> float:
        words = payload.get("result") or []
        confs = []
        for item in words:
            if not isinstance(item, dict):
                continue
            word = self._normalize(str(item.get("word") or ""))
            if not word or word == "unk":
                continue
            try:
                confs.append(float(item.get("conf") or 0.0))
            except (TypeError, ValueError):
                continue
        if not confs:
            return 0.0
        return float(min(confs))

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
                    silent += 1
                    frames.append(mono.copy())
                    if silent >= silence_blocks:
                        break
                else:
                    frames.append(mono.copy())
                    if len(frames) > 5:
                        frames.pop(0)
                    if require_speech and index >= wait_blocks:
                        break

        if require_speech and not started:
            logger.info("No command after the barge-in — back to wake listen")
            return b""
        chunks = lead + frames
        if not chunks:
            return b""
        return self._frames_to_wav(np.concatenate(chunks))

    def _ensure_vosk_model(self) -> str:
        if self.vosk_model_path:
            path = Path(self.vosk_model_path)
            if not path.is_dir():
                raise FileNotFoundError(f"Vosk model not found: {path}")
            return str(path)

        cache_root = Path.home() / ".cache" / "vosk"
        model_dir = cache_root / VOSK_MODEL_DIRNAME
        if model_dir.is_dir():
            return str(model_dir)

        cache_root.mkdir(parents=True, exist_ok=True)
        zip_path = cache_root / f"{VOSK_MODEL_DIRNAME}.zip"
        urls = [
            VOSK_MODEL_URL,
        ]
        last_error: Exception | None = None
        for url in urls:
            try:
                logger.info("Downloading Vosk RU model (~50MB) from %s …", url)
                self._download_file(url, zip_path)
                break
            except Exception as exc:
                last_error = exc
                logger.warning("Vosk download failed (%s): %s", url, exc)
                zip_path.unlink(missing_ok=True)
        else:
            raise RuntimeError(
                "Failed to download Vosk model. "
                "Seed manually: scripts/seed-vosk-model.sh"
            ) from last_error

        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(cache_root)
        zip_path.unlink(missing_ok=True)
        if not model_dir.is_dir():
            raise RuntimeError(f"Vosk extract failed, expected {model_dir}")
        return str(model_dir)

    @staticmethod
    def _download_file(url: str, dest: Path, *, attempts: int = 3) -> None:
        """Stream download with timeout and retries (alphacephei often stalls on Pi)."""
        timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        total = int(response.headers.get("content-length") or 0)
                        done = 0
                        last_log = 0
                        with dest.open("wb") as out:
                            for chunk in response.iter_bytes(chunk_size=256 * 1024):
                                out.write(chunk)
                                done += len(chunk)
                                if total and done - last_log >= max(total // 10, 1):
                                    logger.info(
                                        "Vosk download: %.0f%% (%d / %d MB)",
                                        100.0 * done / total,
                                        done // (1024 * 1024),
                                        total // (1024 * 1024),
                                    )
                                    last_log = done
                if dest.stat().st_size < 1_000_000:
                    raise RuntimeError(
                        f"Downloaded file too small ({dest.stat().st_size} bytes)"
                    )
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Download attempt %d/%d failed: %s", attempt, attempts, exc
                )
                dest.unlink(missing_ok=True)
                time.sleep(min(5 * attempt, 15))
        raise RuntimeError(
            f"Download failed after {attempts} attempts"
        ) from last_error

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

    def _handle_playback(self, command: dict) -> None:
        action = str(command.get("action") or "").strip().lower()
        if action == "play":
            stream_url = str(command.get("url") or "").strip()
            title = str(command.get("title") or "").strip()
            artist = str(command.get("artist") or "").strip()
            if not stream_url:
                logger.error("Playback command missing stream URL")
                return
            label = f"{artist} — {title}".strip(" —") or title or stream_url
            logger.info("Playing music: %s", label)
            self._start_music(stream_url)
            return
        if action == "pause":
            self._pause_music()
            return
        if action == "stop":
            self._stop_music()

    def _start_music(self, stream_url: str) -> None:
        with self._music_lock:
            self._terminate_music_proc()
            try:
                self._music_proc = subprocess.Popen(
                    [
                        self.mpv_command,
                        "--no-video",
                        "--really-quiet",
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

    def _pause_music(self) -> None:
        with self._music_lock:
            if self._music_proc and self._music_proc.poll() is None:
                self._music_proc.send_signal(signal.SIGSTOP)

    def _stop_music(self) -> None:
        with self._music_lock:
            self._terminate_music_proc()

    def _terminate_music_proc(self) -> None:
        proc = self._music_proc
        self._music_proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()

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
        # Our own reply leaks into the mic — never treat it as a wake phrase.
        echo_text = self._normalize(reply_text)
        if self._wake_mode == "vosk":
            return self._wait_vosk(
                deadline=deadline,
                energy_threshold=threshold,
                echo_text=echo_text,
                respect_cooldown=False,
                echo_gate=True,
                # Over the reply the phrase arrives once — waiting for a second
                # matching frame usually means missing it.
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
        description="Raspberry Pi voice client for Open WebUI (Vosk + mpv)",
    )
    parser.add_argument(
        "--backend",
        default=os.getenv("VOICE_BACKEND_URL", "http://voice.pora-ai.ru"),
    )
    parser.add_argument(
        "--wake",
        default=os.getenv("WAKE_WORDS", "ассистент"),
        help="Wake phrases (comma-separated). Default: ассистент",
    )
    parser.add_argument("--silence", type=float, default=1.2)
    parser.add_argument("--max-sec", type=float, default=20.0)
    parser.add_argument(
        "--energy",
        type=float,
        default=float(os.getenv("WAKE_ENERGY", "0.01")),
        help="Min RMS energy for wake / utterance start",
    )
    parser.add_argument(
        "--wake-conf",
        type=float,
        default=float(os.getenv("WAKE_MIN_CONF", "0.55")),
        help="Min Vosk word confidence when scores are present (finals)",
    )
    parser.add_argument(
        "--wake-stable",
        type=int,
        default=int(os.getenv("WAKE_STABLE_FRAMES", "2")),
        help="Vosk: frames with wake prefix (~250ms) before accept",
    )
    parser.add_argument(
        "--wake-cooldown",
        type=float,
        default=float(os.getenv("WAKE_COOLDOWN", "2.0")),
        help="Seconds to ignore wake after assist / previous wake",
    )
    parser.add_argument(
        "--vosk-model",
        default=os.getenv("VOSK_MODEL_PATH", ""),
        help="Optional path to vosk-model-small-ru-0.22 (auto-download otherwise)",
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
    args = parser.parse_args()

    wake_words = [raw.strip() for raw in args.wake.split(",") if raw.strip()]

    client = VoiceClient(
        backend_url=args.backend,
        wake_words=wake_words,
        silence_sec=args.silence,
        max_utterance_sec=args.max_sec,
        energy_threshold=args.energy,
        vosk_model_path=args.vosk_model,
        wake_min_conf=args.wake_conf,
        wake_cooldown_sec=args.wake_cooldown,
        wake_stable_frames=args.wake_stable,
        barge_in=args.barge_in,
        barge_energy_mult=args.barge_energy_mult,
        device_id=args.device_id,
        music_poll=args.music_poll,
        music_poll_interval=args.music_poll_interval,
        mpv_command=args.mpv,
    )
    try:
        client.run()
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
