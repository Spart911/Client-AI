"""Playback: chimes, TTS/paplay, mpv music, and volume IPC."""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from bt_audio import BtAudio

logger = logging.getLogger("pi-client")

SAMPLE_RATE = 16000
#
# Volume layers (do not mix):
# - `_volume_level` (1–10): user-facing assistant/music volume → Pulse % + mpv.
# - MUSIC_LISTEN_DUCK: fraction of that level while idle-listen (mpv IPC).
# - BARGE_PLAYBACK_GAIN: softens TTS PCM while barge-in listens.
# - BT_KEEPALIVE_VOL: host scripts/bt-connect.sh paplay blip (not used here).
BARGE_PLAYBACK_GAIN = 0.62
TTS_END_PAD_SEC = 0.35
MUSIC_LISTEN_DUCK = 0.35


class PlaybackEngine:
    """paplay/mpv/wav playback, volume control, and music process helpers."""

    def __init__(
        self,
        bt: BtAudio,
        *,
        mpv_command: str,
        backend_url: str,
        device_id: str,
        http_client,
        barge_in: bool = True,
        barge_watch: Optional[Callable] = None,
    ) -> None:
        self.bt = bt
        self.mpv_command = mpv_command
        self.backend_url = backend_url.rstrip("/")
        self.device_id = device_id
        self._http = http_client
        self.barge_in = barge_in
        self._barge_watch = barge_watch
        self._music_proc = None  # type: Optional[subprocess.Popen]
        self._music_lock = threading.Lock()
        self._cue_lock = threading.Lock()
        self._current_track_id = ""
        self._current_source = ""
        self._volume_level = 7  # 1..10
        self._mpv_ipc_path = Path(tempfile.gettempdir()) / ("voice-mpv-%s.sock" % os.getpid())
        self.in_wake_listen = False
        self._which_cache: dict[str, str | None] = {}
        self._playback_env = os.environ.copy()
        self._playback_env.setdefault("PULSE_LATENCY_MSEC", "60")

    def _which(self, cmd: str) -> str | None:
        if cmd not in self._which_cache:
            self._which_cache[cmd] = shutil.which(cmd)
        return self._which_cache[cmd]

    @property
    def current_source(self) -> str:
        return self._current_source

    def set_barge_watch(self, fn: Callable) -> None:
        self._barge_watch = fn

    def play_chime_array(
        self,
        audio: np.ndarray,
        *,
        drain_sec: float = 0.0,
        audio_buffer: float = 0.2,
        min_gain: float = 0.0,
        label: str = "cue",
    ) -> None:
        """Play a short chime on the Pulse default sink (A2DP).

        Prefer paplay over mpv: mpv cold-start is slow, and A2DP often drops the
        first ~200–400 ms after IDLE — a short ding vanishes even when rc=0.
        """
        gain = max(float(min_gain), self.assistant_gain())
        audio = (audio * gain).astype(np.float32, copy=False)
        # Non-zero prime: digital silence does not wake a suspended A2DP link.
        prime_n = int(SAMPLE_RATE * 0.40)
        t = np.arange(prime_n, dtype=np.float32) / float(SAMPLE_RATE)
        ramp = np.clip(t / 0.08, 0.0, 1.0)
        prime = (np.sin(2.0 * np.pi * 160.0 * t) * 0.035 * ramp).astype(np.float32)
        audio = np.concatenate([prime, audio])
        duration = float(audio.shape[0]) / float(SAMPLE_RATE)
        fd, tmp_name = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        path = Path(tmp_name)
        try:
            self.write_wav_pcm(path, audio, SAMPLE_RATE)
            env = self._playback_env
            sink = self.bt.pulse_default_sink()
            self.bt.pulse_unsuspend_sink(sink)
            timeout = max(8.0, duration + drain_sec + 3.0)
            paplay = self._which("paplay")
            backend = "none"
            rc = -1
            # Louder than assistant TTS volume so ding is obvious at 4/10.
            paplay_vol = max(28000, int(65536 * max(0.55, gain)))
            if paplay:
                cmd = [paplay, f"--volume={paplay_vol}"]
                if sink:
                    cmd.extend(["--device", sink])
                cmd.append(str(path))
                result = subprocess.run(
                    cmd,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    env=env,
                    text=True,
                )
                backend = "paplay"
                rc = int(result.returncode)
                if rc != 0:
                    err = (result.stderr or "").strip()[:200]
                    logger.warning("paplay chime failed rc=%s %s", rc, err)
            if backend == "none" or rc != 0:
                mpv = (self.mpv_command or "").strip() or self._which("mpv")
                if mpv:
                    buf = max(0.15, float(audio_buffer))
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
                    backend = "mpv"
                    rc = int(result.returncode)
                    if rc != 0:
                        logger.warning("mpv chime failed rc=%s", rc)
            if backend == "none":
                sd.play(audio, SAMPLE_RATE, blocking=True)
                backend = "sounddevice"
                rc = 0
            if drain_sec > 0:
                time.sleep(drain_sec)
            logger.info(
                "%s played via %s sink=%s dur=%.2fs gain=%.2f rc=%s",
                label,
                backend,
                sink or "default",
                duration,
                gain,
                rc,
            )
        except Exception:
            logger.warning("%s playback failed", label, exc_info=True)
        finally:
            path.unlink(missing_ok=True)

    def play_cue(
        self,
        audio: np.ndarray,
        *,
        pre_sec: float,
        post_sec: float,
        label: str,
        min_gain: float = 0.55,
    ) -> None:
        try:
            # Prefer the Bluetooth speaker when the card is up — mailbox/HDMI
            # often becomes default after a BT drop and the ding is inaudible.
            self.bt.ensure_bt_playback_sink()
            rate = SAMPLE_RATE
            # Tiny pad only — A2DP prime lives inside _play_chime_array (non-zero).
            primed = np.concatenate(
                [
                    np.zeros(max(0, int(rate * pre_sec)), dtype=np.float32),
                    audio,
                    np.zeros(max(0, int(rate * post_sec)), dtype=np.float32),
                ]
            )
            with self._cue_lock:
                self.play_chime_array(
                    primed,
                    drain_sec=0.08,
                    audio_buffer=0.18,
                    min_gain=min_gain,
                    label=label,
                )
        except Exception:
            logger.warning("%s playback failed", label, exc_info=True)

    def play_listen_cue(self) -> None:
        # Longer / louder two-tone so A2DP has something left after the prime.
        self.play_cue(
            self.make_chime(
                SAMPLE_RATE, 880.0, 0.16, 1174.0, 0.22, amplitude=0.42, harm=0.12
            ),
            pre_sec=0.0,
            post_sec=0.05,
            label="Listen cue",
            min_gain=0.75,
        )

    def play_sent_cue(self) -> None:
        self.play_cue(
            self.make_chime(
                SAMPLE_RATE, 988.0, 0.10, 740.0, 0.16, amplitude=0.34, harm=0.10
            ),
            pre_sec=0.0,
            post_sec=0.04,
            label="Sent cue",
            min_gain=0.65,
        )

    @staticmethod
    def tone(
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
    def make_chime(
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
                cls.tone(rate, f1, d1, amplitude=amplitude, harm=harm),
                gap,
                cls.tone(rate, f2, d2, amplitude=amplitude, harm=harm),
            ]
        )
        peak = float(np.max(np.abs(chime)) or 1.0)
        if peak > 0.35:
            chime *= 0.35 / peak
        return chime

    @staticmethod
    def volume_to_percent(level: int) -> int:
        level = max(1, min(10, int(level)))
        return level * 10

    def assistant_gain(self) -> float:
        """Linear gain for TTS / any sounddevice playback. 1→0.1 … 10→1.0."""
        return self.volume_to_percent(self._volume_level) / 100.0

    def is_music_playing(self) -> bool:
        with self._music_lock:
            proc = self._music_proc
            return proc is not None and proc.poll() is None

    def duck_music(self, factor: float) -> None:
        """Lower mpv only (not Pulse) so mic headroom returns while we listen."""
        if not self.is_music_playing():
            return
        base = self.volume_to_percent(self._volume_level)
        percent = max(5, min(100, int(round(base * max(0.05, factor)))))
        self.apply_mpv_volume(percent)

    def restore_music_volume(self) -> None:
        if not self.is_music_playing():
            return
        self.apply_mpv_volume(self.volume_to_percent(self._volume_level))

    def set_volume(self, level: int) -> None:
        """Master volume for the whole assistant: TTS + music + Pulse sink."""
        level = max(1, min(10, int(level)))
        self._volume_level = level
        percent = self.volume_to_percent(level)
        logger.info("Assistant volume %s/10 (%s%%)", level, percent)
        # Keep listen-duck while waiting for wake; otherwise a volume command
        # would blast music back to full and bury the mic again.
        if self.in_wake_listen and self.is_music_playing():
            self.apply_mpv_volume(max(5, int(round(percent * MUSIC_LISTEN_DUCK))))
        else:
            self.apply_mpv_volume(percent)
        self.apply_pulse_volume(percent)

    def apply_mpv_volume(self, percent: int) -> None:
        sock = self._mpv_ipc_path
        if not sock.exists():
            return
        try:
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

    def apply_pulse_volume(self, percent: int) -> None:
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

    def start_music(
        self,
        stream_url: str,
        *,
        track_id: str = "",
        source: str = "",
        loop_file: str = "no",
        cleanup_path: Path | None = None,
    ) -> None:
        with self._music_lock:
            self.terminate_music_proc()
            self._current_track_id = track_id
            self._current_source = source
            percent = self.volume_to_percent(self._volume_level)
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
                elif self.in_wake_listen:
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
                self.unlink_quiet(cleanup_path)
            except Exception:
                logger.exception("Failed to start mpv")
                self._music_proc = None
                self.unlink_quiet(cleanup_path)
            else:
                threading.Thread(
                    target=self.watch_music_proc,
                    kwargs={"cleanup_path": cleanup_path},
                    name="music-watch",
                    daemon=True,
                ).start()

    def watch_music_proc(self, cleanup_path: Path | None = None) -> None:
        proc = self._music_proc
        if proc is None:
            self.unlink_quiet(cleanup_path)
            return
        code = proc.wait()
        with self._music_lock:
            if self._music_proc is not proc:
                self.unlink_quiet(cleanup_path)
                return  # replaced or stopped
            self._music_proc = None
            track_id = self._current_track_id
            source = self._current_source
            self._current_track_id = ""
            self._current_source = ""
        self.unlink_quiet(cleanup_path)
        if code != 0:
            return
        if source == "alert":
            return
        # Natural end — ask server for next Моя волна track if active.
        if source == "yandex-wave" or track_id:
            self.report_track_finished(track_id)

    @staticmethod
    def unlink_quiet(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def report_track_finished(self, track_id: str) -> None:
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

    def pause_music(self) -> None:
        with self._music_lock:
            if self._music_proc and self._music_proc.poll() is None:
                self._music_proc.send_signal(signal.SIGSTOP)

    def stop_music(self) -> None:
        had_proc = False
        with self._music_lock:
            had_proc = self._music_proc is not None
            self._current_track_id = ""
            self._current_source = ""
            self.terminate_music_proc()
        # mpv on HFP often wedges the SCO mic — same as after TTS.
        if had_proc and (os.getenv("BT_DEVICE_MAC") or os.getenv("BT_MAC") or "").strip():
            self.bt.restore_hfp_audio()

    def terminate_music_proc(self) -> None:
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

    def write_wav_pcm(self, path: Path, data: np.ndarray, rate: int) -> None:
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

    def play_wav(self, wav_bytes: bytes, reply_text: str = "") -> np.ndarray | None:
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
            data = data * self.assistant_gain()
            if self.barge_in:
                # Duck the reply so bleed stays below a normal speaking voice.
                data = data * BARGE_PLAYBACK_GAIN
                if self.bt.hfp_duplex():
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
            self.write_wav_pcm(path, data, rate)
            logger.info(
                "Playing reply (%.1fs, barge_in=%s)",
                duration,
                "on" if self.barge_in else "off",
            )

            paplay = self._which("paplay")
            if paplay:
                return self.play_wav_paplay(
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

    def play_wav_paplay(
        self,
        paplay: str,
        path: Path,
        duration: float,
        reply_text: str = "",
    ) -> np.ndarray | None:
        """Play via Pulse paplay; listen for barge-in on the mic in parallel."""
        open_echo = self.bt.open_speaker_echo()
        hfp = self.bt.hfp_duplex()
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
                preroll = self._barge_watch(reply_text, duration)
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
                self.bt.restore_hfp_audio()
