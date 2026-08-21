"""Background poller for pending music / alert commands."""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alert_scheduler import AlertScheduler
    from playback_engine import PlaybackEngine

logger = logging.getLogger("pi-client")


class MusicPoller:
    """Poll /v1/music/pending and dispatch play/pause/stop/volume/alerts."""

    def __init__(
        self,
        *,
        backend_url: str,
        device_id: str,
        http_client,
        playback,  # PlaybackEngine
        alerts,  # AlertScheduler
        enabled: bool = True,
        interval: float = 2.0,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.device_id = device_id
        self._http = http_client
        self.playback = playback
        self.alerts = alerts
        self.music_poll = enabled
        self.music_poll_interval = max(0.5, float(interval))
        self._stop = threading.Event()
        self.mpv_command = playback.mpv_command

    def start(self) -> None:
        thread = threading.Thread(
            target=self.music_poll_loop,
            name="music-poller",
            daemon=True,
        )
        thread.start()
        logger.info(
            "Music poller enabled (interval=%.1fs, mpv=%s)",
            self.music_poll_interval,
            self.mpv_command,
        )

    def music_poll_loop(self) -> None:
        url = f"{self.backend_url}/v1/music/pending/{self.device_id}"
        while not self._stop.is_set():
            if not self.music_poll:
                self._stop.wait(self.music_poll_interval)
                continue
            try:
                response = self._http.get(url, timeout=30.0)
                if response.status_code < 400:
                    payload = response.json()
                    for command in payload.get("commands") or []:
                        if isinstance(command, dict):
                            self.handle_playback(command)
            except Exception as exc:
                logger.debug("Music poll failed: %s", exc)
            self._stop.wait(self.music_poll_interval)

    def sync_volume_from_backend(self) -> None:
        """Pull last known volume from server; fall back to local default."""
        url = f"{self.backend_url}/v1/music/status/{self.device_id}"
        try:
            response = self._http.get(url, timeout=10.0)
            if response.status_code < 400:
                payload = response.json() or {}
                level = int(payload.get("volume") or self.playback._volume_level)
                self.playback.set_volume(level)
                return
        except Exception as exc:
            logger.debug("Volume sync failed: %s", exc)
        self.playback.apply_pulse_volume(self.playback.volume_to_percent(self.playback._volume_level))

    def handle_playback(self, command: dict) -> None:
        action = str(command.get("action") or "").strip().lower()
        stream_url = str(command.get("url") or "").strip()
        if action in ("timer", "alarm", "cancel_alert", "cancel_timer", "cancel_alarm") or stream_url.startswith(
            "pi-alert://"
        ):
            self.alerts.handle_command(command)
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
            self.playback.start_music(stream_url, track_id=track_id, source=source)
            return
        if action == "pause":
            self.playback.pause_music()
            return
        if action == "stop":
            self.playback.stop_music()
            return
        if action == "volume":
            try:
                level = int(command.get("volume") or 0)
            except (TypeError, ValueError):
                level = 0
            if level:
                self.playback.set_volume(level)


    def stop(self) -> None:
        self._stop.set()
