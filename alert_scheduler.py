"""Timer/alarm scheduling and builtin alert synthesis."""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from playback_engine import PlaybackEngine

logger = logging.getLogger("pi-client")


class AlertScheduler:
    """Background timer/alarm queue that fires through PlaybackEngine."""

    def __init__(self, *, playback: PlaybackEngine, http_client) -> None:
        self.playback = playback
        self._http = http_client
        self._alert_stop = threading.Event()
        self._alert_cv = threading.Condition()
        self._alerts = []  # type: list

    def start(self) -> None:
        thread = threading.Thread(
            target=self.alert_scheduler_loop,
            name="alert-scheduler",
            daemon=True,
        )
        thread.start()
        logger.info("Alert scheduler enabled (timer/alarm)")

    def handle_alert_command(self, command: dict) -> None:
        parsed = dict(command)
        url = str(parsed.get("url") or "").strip()
        if url.startswith("pi-alert://"):
            parsed.update(self.parse_alert_url(url))
        kind = str(parsed.get("kind") or parsed.get("action") or "").strip().lower()
        if kind in ("cancel", "cancel_alert", "cancel_timer", "cancel_alarm"):
            target = str(parsed.get("cancel_kind") or parsed.get("target") or "").strip().lower()
            if kind == "cancel_timer":
                target = "timer"
            elif kind == "cancel_alarm":
                target = "alarm"
            self.cancel_alerts(kind=target or None)
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
    def parse_alert_url(url: str) -> dict:
        parsed = urlparse(url)
        kind = (parsed.netloc or parsed.path.lstrip("/")).strip().lower()
        q = {k: unquote(v[-1]) for k, v in parse_qs(parsed.query).items() if v}
        out = {"kind": kind, **q}
        if "url" in q and "media_url" not in out:
            out["media_url"] = q["url"]
        return out

    def cancel_alerts(self, *, kind: str | None = None) -> None:
        with self._alert_cv:
            before = len(self._alerts)
            if kind:
                self._alerts = [j for j in self._alerts if j.get("kind") != kind]
            else:
                self._alerts.clear()
            removed = before - len(self._alerts)
            self._alert_cv.notify_all()
        if kind in (None, "alarm", "timer"):
            with self.playback._music_lock:
                if self.playback._current_source == "alert":
                    self.playback.terminate_music_proc()
                    self.playback._current_source = ""
        logger.info("Cancelled alerts kind=%s removed=%d", kind or "all", removed)

    def alert_scheduler_loop(self) -> None:
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
                self.fire_alert(nxt)
            except Exception:
                logger.exception("Alert playback failed")

    def fire_alert(self, job: dict) -> None:
        label = job.get("label") or job.get("kind")
        logger.info("Firing %s (%s)", job.get("kind"), label)
        media = str(job.get("media_url") or "").strip()
        cleanup: Path | None = None
        if not media:
            cleanup = self.write_builtin_alert(
                str(job.get("kind") or "timer"),
                str(job.get("sound") or "classic"),
            )
            media = str(cleanup)
        elif media.startswith(("http://", "https://")):
            downloaded = self.download_alert_media(media)
            if downloaded is not None:
                cleanup = downloaded
                media = str(downloaded)
            else:
                logger.warning(
                    "Alert URL failed, falling back to builtin sound: %s",
                    media[:120],
                )
                cleanup = self.write_builtin_alert(
                    str(job.get("kind") or "timer"),
                    str(job.get("sound") or "classic"),
                )
                media = str(cleanup)
        loop_file = "inf" if job.get("loop") else "no"
        logger.info("Alert media=%s loop=%s", media[:100], loop_file)
        self.playback.start_music(
            media,
            source="alert",
            loop_file=loop_file,
            cleanup_path=cleanup,
        )

    def download_alert_media(self, url: str) -> Path | None:
        """Fetch remote alert audio to a temp file so mpv plays the full clip."""
        try:
            response = self._http.get(url, timeout=45.0, follow_redirects=True)
            response.raise_for_status()
            data = response.content
            if len(data) < 256:
                logger.error("Alert media too small (%d bytes): %s", len(data), url[:120])
                return None
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix not in (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"):
                ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                suffix = {
                    "audio/mpeg": ".mp3",
                    "audio/mp3": ".mp3",
                    "audio/wav": ".wav",
                    "audio/x-wav": ".wav",
                    "audio/ogg": ".ogg",
                    "audio/flac": ".flac",
                    "audio/mp4": ".m4a",
                    "audio/aac": ".aac",
                }.get(ctype, ".mp3")
            fd, name = tempfile.mkstemp(prefix="alert-", suffix=suffix)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            path = Path(name)
            logger.info("Alert media downloaded %d bytes → %s", len(data), path.name)
            return path
        except Exception:
            logger.exception("Alert media download failed: %s", url[:120])
            return None

    def write_builtin_alert(self, kind: str, sound: str) -> Path:
        rate = 16000
        sound_key = (sound or "").strip().lower()
        # Melodies by sound name; kind no longer forces the short timer beep.
        if sound_key == "digital":
            audio = self.synth_alarm_digital(rate)
        elif sound_key == "soft":
            audio = self.synth_alarm_soft(rate)
        elif sound_key == "timer":
            audio = self.synth_timer_tone(rate)
        else:
            audio = self.synth_alarm_classic(rate)
        fd, name = tempfile.mkstemp(prefix=f"{kind}-", suffix=".wav")
        os.close(fd)
        path = Path(name)
        self.playback.write_wav_pcm(path, audio, rate)
        return path

    @classmethod
    def synth_timer_tone(cls, rate: int) -> np.ndarray:
        gap = np.zeros(int(rate * 0.12), dtype=np.float32)
        burst = np.concatenate(
            [
                PlaybackEngine.tone(rate, 880.0, 0.12, amplitude=0.28),
                gap,
                PlaybackEngine.tone(rate, 1320.0, 0.18, amplitude=0.30),
                np.zeros(int(rate * 0.35), dtype=np.float32),
            ]
        )
        return np.tile(burst, 2)

    @classmethod
    def synth_alarm_classic(cls, rate: int) -> np.ndarray:
        hi = PlaybackEngine.tone(rate, 880.0, 0.35, amplitude=0.32, harm=0.08)
        lo = PlaybackEngine.tone(rate, 698.5, 0.35, amplitude=0.32, harm=0.08)
        pause = np.zeros(int(rate * 0.12), dtype=np.float32)
        return np.concatenate([hi, pause, lo, pause, hi, pause, lo, np.zeros(int(rate * 0.25), dtype=np.float32)])

    @classmethod
    def synth_alarm_digital(cls, rate: int) -> np.ndarray:
        pieces: list[np.ndarray] = []
        for freq in (1200.0, 1500.0, 1200.0, 1500.0):
            pieces.append(PlaybackEngine.tone(rate, freq, 0.08, amplitude=0.28, attack=0.005, release=0.02, harm=0.02))
            pieces.append(np.zeros(int(rate * 0.06), dtype=np.float32))
        pieces.append(np.zeros(int(rate * 0.2), dtype=np.float32))
        return np.concatenate(pieces)

    @classmethod
    def synth_alarm_soft(cls, rate: int) -> np.ndarray:
        return np.concatenate(
            [
                PlaybackEngine.tone(rate, 523.25, 0.22, amplitude=0.22),
                np.zeros(int(rate * 0.08), dtype=np.float32),
                PlaybackEngine.tone(rate, 659.25, 0.28, amplitude=0.24),
                np.zeros(int(rate * 0.35), dtype=np.float32),
            ]
        )

    def stop(self) -> None:
        self._alert_stop.set()
        with self._alert_cv:
            self._alert_cv.notify_all()

    def handle_command(self, command: dict) -> None:
        self.handle_alert_command(command)
