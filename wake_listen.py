"""
microWakeWord listen loop: idle wake + barge-in over TTS/music/alerts.

Extracted from VoiceClient so playback/alerts stay untouched while the mic
gate / score / music-reopen logic lives in one place.
"""

from __future__ import annotations

import logging
import os
import queue
import time
from collections import deque
from typing import Callable

import numpy as np
import sounddevice as sd

from audio_dsp import MIC_RATE, SAMPLE_RATE, echo_gate_energy, resample_int
from playback_engine import MUSIC_LISTEN_DUCK as _PE_MUSIC_LISTEN_DUCK

logger = logging.getLogger("pi-client")

CHANNELS = 1
# 80 ms capture @ 48 kHz; after ↓16 kHz → FRAME_SAMPLES for microWakeWord.
MIC_FRAME_SAMPLES = 3840  # int(MIC_RATE * 0.08)
FRAME_SAMPLES = 1280  # int(SAMPLE_RATE * 0.08)

# --- Barge / wake tuning ---
# Below this the barge-in gate sits at the speaker bleed level and lets it through.
BARGE_MULT_MIN = 1.05
# Open speaker (A2DP + USB mic): TTS bleed is ~real speech RMS. Voice must
# clear a high percentile of that envelope, not the pre-play silence floor.
BARGE_TTS_GATE_MULT = 1.50
BARGE_TTS_PERCENTILE = 0.85
BARGE_FLOOR_FRAMES = 24
# Don't score wake until playback has a stable bleed floor.
BARGE_ARM_SEC = float(os.getenv("BARGE_ARM_SEC", "0.50"))
# Keep feeding the wake model this long after a burst dips under the gate.
BARGE_HANGOVER_FRAMES = int(os.getenv("BARGE_HANGOVER_FRAMES", "4"))
# Replay the frames just before the burst so the wake phrase keeps its onset.
BARGE_ONSET_FRAMES = 2

# While waiting for wake with music on: keep mpv quieter so speech
# can clear the mic without shouting (fraction of the user's 1–10 volume).
MUSIC_LISTEN_DUCK = _PE_MUSIC_LISTEN_DUCK
# Extra duck once a speech burst is heard over the music floor.
MUSIC_SPEECH_DUCK = 0.12
# Gate over music bleed (slightly softer than TTS barge-in).
MUSIC_GATE_MULT = 1.08
# Sentinel: music ended mid-listen — reopen InputStream after HFP reseat.
REOPEN_MIC = object()

# Peak in the recent window must also outrun the quiet floor.
WAKE_ENERGY_BURST_RATIO = float(os.getenv("WAKE_ENERGY_BURST_RATIO", "2.0"))
# Keep a speech burst alive while microWakeWord's score catches up (80 ms frames).
WAKE_ENERGY_HANGOVER = int(os.getenv("WAKE_ENERGY_HANGOVER", "12"))
# One loud frame is enough — score rises after the word while RMS falls.
WAKE_SPEECH_MIN_FRAMES = 1
# Idle listen: consecutive high-score frames while a recent burst is latched.
WAKE_STABLE_MIN = 3


class WakeListener:
    """Owns the MWW InputStream loop; VoiceClient supplies playback/bt/mww refs."""

    def __init__(
        self,
        *,
        playback,
        bt,
        energy_threshold: float,
        wake_stable_frames: int,
        wake_accept_energy: float,
        barge_energy_mult: float,
        wake_cooldown_sec: float,
        get_last_wake_ts: Callable[[], float],
        denoise=None,
    ) -> None:
        self.playback = playback
        self.bt = bt
        self.energy_threshold = energy_threshold
        self.wake_stable_frames = max(1, wake_stable_frames)
        self.wake_accept_energy = max(0.001, float(wake_accept_energy))
        self.barge_energy_mult = max(BARGE_MULT_MIN, barge_energy_mult)
        self.wake_cooldown_sec = wake_cooldown_sec
        self._get_last_wake_ts = get_last_wake_ts
        self._denoise = denoise
        self._mww = None
        self._mww_features = None
        self._mww_wake_word = ""

    def bind_mww(self, mww, features, wake_word: str) -> None:
        self._mww = mww
        self._mww_features = features
        self._mww_wake_word = wake_word or ""

    def set_denoise(self, denoise) -> None:
        self._denoise = denoise

    def wait_for_wake(self) -> np.ndarray | None:
        self.playback.in_wake_listen = True
        try:
            # echo_gate starts False; sync_music_duck toggles it when mpv starts.
            # Reopen the mic stream after music ends so HFP SCO is not stale.
            while True:
                result = self.wait_mww(
                    echo_gate=False,
                    music_duck=True,
                )
                if result is REOPEN_MIC:
                    logger.info("Reopening mic after music (HFP reseat)")
                    continue
                return result
        finally:
            self.playback.in_wake_listen = False

    def wait_mww(
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
        assert self._mww is not None
        assert self._mww_features is not None
        threshold = self.energy_threshold if energy_threshold is None else energy_threshold
        if stable_frames is None:
            stable_needed = max(WAKE_STABLE_MIN, self.wake_stable_frames)
        else:
            stable_needed = max(1, stable_frames)
        score_limit = float(self._mww.probability_cutoff)

        block = MIC_FRAME_SAMPLES
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
        open_echo = self.bt.open_speaker_echo()
        pre_burst_f: deque[np.ndarray] = deque(maxlen=BARGE_ONSET_FRAMES)
        feeding = False
        quiet_frames = 0
        gate_logged = False
        last_heartbeat = 0.0
        last_score_log = 0.0
        last_hold_log = 0.0
        last_playback_check = 0.0
        host_playback = False
        energy_hangover = 0
        latched_peak = 0.0
        speech_latched = False
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
            nonlocal energy_hangover, latched_peak, speech_latched
            if not music_duck:
                return
            playing = self.playback.is_music_playing()
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
                speech_latched = False
                self._mww.reset()
                self._mww_features.reset()
                # Keep alerts at full volume — ducking them invites false wakes
                # (quiet ring + loud bleed scoring) and makes the cue hard to hear.
                if self.playback.current_source != "alert":
                    self.playback.duck_music(MUSIC_LISTEN_DUCK)
                logger.info(
                    "Music on — %s",
                    (
                        "alert (Jarvis only above playback gate)"
                        if self.playback.current_source == "alert"
                        else (
                            f"duck×{MUSIC_LISTEN_DUCK:.2f} + open-echo gate "
                            "(don't score speaker bleed)"
                            if open_echo
                            else f"duck×{MUSIC_LISTEN_DUCK:.2f} + soft wake scoring"
                        )
                    ),
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
                speech_latched = False
                self._mww.reset()
                self._mww_features.reset()
                # Always reseat HFP after mpv — SCO often dies like after TTS.
                self.bt.restore_hfp_audio()
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
            # Never duck an alert: relative speech-gate sees the ringtone itself
            # as "user speech" and was silencing ~15s timers after ~1s.
            if self.playback.current_source == "alert":
                return
            if active:
                self.playback.duck_music(MUSIC_SPEECH_DUCK)
                speech_ducked = True
            elif speech_ducked:
                self.playback.duck_music(MUSIC_LISTEN_DUCK)
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
                samplerate=MIC_RATE,
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
                        return REOPEN_MIC
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
                    # RNNoise stays at 48 kHz; MWW + preroll need 16 kHz.
                    frame_16 = resample_int(frame_f, MIC_RATE, SAMPLE_RATE)
                    mono = np.clip(frame_16 * 32768.0, -32768, 32767).astype(np.int16)
                    frame_energy = self._rms(frame_16)
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
                            # Open-echo alert: ringtone IS the floor — do not enter
                            # feeding/MWW on self-bleed (that also used to duck it).
                            alert_open = (
                                open_echo and self.playback.current_source == "alert"
                            )
                            if not feeding and not alert_open:
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
                            elif alert_open and not gate_logged:
                                logger.info(
                                    "Alert playback bleed rms=%.3f (ignored for barge)",
                                    frame_energy,
                                )
                                gate_logged = True
                        elif feeding:
                            quiet_frames += 1
                            if quiet_frames > BARGE_HANGOVER_FRAMES:
                                set_feeding(False)
                                stable = 0
                        if not feeding:
                            pre_burst_f.append(frame_16.copy())
                            # Open-echo music/alerts: never score speaker bleed as
                            # wake — that used to stop a 15s timer after ~1s.
                            # Jarvis still works when speech clears the gate
                            # (feeding=True). Soft/HFP may score under the gate.
                            score_under_gate = barge_soft or (
                                music_mode and not open_echo
                            )
                            if not score_under_gate:
                                continue
                        preroll.append(frame_16.copy())
                    else:
                        preroll.append(frame_16.copy())

                    if (
                        respect_cooldown
                        and time.monotonic() - self._get_last_wake_ts()
                        < self.wake_cooldown_sec
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
                    # Idle wake: ignore acoustic echo from host keepalive / other paplay.
                    # Barge-in (use_gate) must keep listening over our own TTS.
                    if not use_gate and now - last_playback_check >= 0.25:
                        host_playback = self.bt.host_playback_active()
                        last_playback_check = now
                        if host_playback:
                            # Don't clear the speech latch — only block accept while
                            # paplay/keepalive is actually on the sink.
                            stable = 0
                    accept_energy = self.wake_accept_energy
                    if barge_soft or (music_mode and not open_echo):
                        accept_energy = max(0.003, accept_energy * 0.5)
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
                    speech_frames = sum(
                        1 for e in recent_energy if e >= accept_energy * 0.85
                    )
                    # Latch on the recent burst, not the current quiet frame —
                    # MWW score peaks after «Джарвис» when RMS has already fallen.
                    if (
                        not host_playback
                        and burst_ok
                        and speech_frames >= WAKE_SPEECH_MIN_FRAMES
                    ):
                        energy_hangover = WAKE_ENERGY_HANGOVER
                        latched_peak = max(latched_peak, burst_peak)
                        speech_latched = True
                    elif energy_hangover > 0:
                        energy_hangover -= 1
                        if energy_hangover == 0:
                            latched_peak = 0.0
                            speech_latched = False
                    energy_ok = energy_hangover > 0 and speech_latched
                    armed = arm_sec <= 0.0 or (now - listen_started) >= arm_sec
                    score_ok = (
                        best_score >= score_limit
                        and energy_ok
                        and armed
                        and not (host_playback and not use_gate)
                    )

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
                        # Quiet by default; WAKE_HEARTBEAT=1 restores INFO for debug.
                        _hb = logger.info if (
                            (os.getenv("WAKE_HEARTBEAT") or "").strip().lower()
                            in ("1", "true", "yes", "on")
                        ) else logger.debug
                        _hb(
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

                    if best_score >= score_limit and not score_ok and armed:
                        if now - last_hold_log >= 0.4:
                            why = (
                                "playback"
                                if host_playback and not use_gate
                                else (
                                    "no-speech-latch"
                                    if not speech_latched
                                    else "hangover-expired"
                                )
                            )
                            logger.info(
                                "MWW hold %.3f energy=%.4f speech_frames=%d (%s; "
                                "need score≥%.2f energy≥%.3f ×%d frames)",
                                best_score,
                                burst_peak,
                                speech_frames,
                                why,
                                score_limit,
                                accept_energy,
                                WAKE_SPEECH_MIN_FRAMES,
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
                self.playback.restore_music_volume()

    @staticmethod
    def _preroll_array(preroll) -> np.ndarray | None:
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
        factor = self.barge_energy_mult if mult is None else max(BARGE_MULT_MIN, mult)
        return echo_gate_energy(
            window,
            threshold,
            mult=factor,
            percentile=percentile,
            min_frames=min_frames,
            mult_floor=BARGE_MULT_MIN,
        )

    @staticmethod
    def _rms(frames: np.ndarray) -> float:
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
