"""Bluetooth / PulseAudio sink helpers for the Pi voice client."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

logger = logging.getLogger("pi-client")


class BtAudio:
    """BT profile detection, HFP restore, and Pulse default-sink helpers."""

    def __init__(self) -> None:
        self._which_cache: dict[str, str | None] = {}

    def _which(self, cmd: str) -> str | None:
        if cmd not in self._which_cache:
            self._which_cache[cmd] = shutil.which(cmd)
        return self._which_cache[cmd]

    def pulse_default_sink(self) -> str:
        try:
            return subprocess.check_output(
                ["pactl", "get-default-sink"],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            return ""

    def pulse_unsuspend_sink(self, sink: str) -> None:
        if not sink or not self._which("pactl"):
            return
        try:
            subprocess.run(
                ["pactl", "suspend-sink", sink, "0"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    def ensure_bt_playback_sink(self) -> None:
        """Point Pulse default sink at A2DP when the BT card is connected."""
        if not self._which("pactl"):
            return
        mac = (os.getenv("BT_DEVICE_MAC") or os.getenv("BT_MAC") or "").strip()
        if not mac or self.bt_profile() != "a2dp_sink":
            return
        mac_us = mac.upper().replace(":", "_").replace("-", "_")
        want = f"bluez_sink.{mac_us}.a2dp_sink"
        try:
            current = self.pulse_default_sink()
            if current == want:
                return
            sinks = subprocess.check_output(
                ["pactl", "list", "short", "sinks"],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            )
            if want not in sinks:
                return
            subprocess.run(
                ["pactl", "set-default-sink", want],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            logger.info("Cue sink → %s (was %s)", want, current or "?")
        except Exception:
            logger.debug("BT sink restore for cue failed", exc_info=True)

    def bt_profile(self) -> str:
        return (os.getenv("BT_PROFILE") or "handsfree_head_unit").strip().lower()

    def bluez_sink_active(self) -> bool:
        if not self._which("pactl"):
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

    def host_playback_active(self) -> bool:
        """True if Pulse already has a sink-input (keepalive, music, TTS, …)."""
        if not self._which("pactl"):
            return False
        try:
            out = subprocess.check_output(
                ["pactl", "list", "short", "sink-inputs"],
                text=True,
                timeout=1.5,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False
        return any(line.strip() for line in out.splitlines())

    def open_speaker_echo(self) -> bool:
        """True when a room speaker plays into a separate mic (A2DP + USB)."""
        if self.bt_profile() == "a2dp_sink":
            return True
        source = (os.getenv("PULSE_DEFAULT_SOURCE") or "").strip().lower()
        return "usb" in source

    def hfp_duplex(self) -> bool:
        """Same Bluetooth headset for mic and speaker (weak acoustic echo)."""
        return self.bt_profile() != "a2dp_sink" and self.bluez_sink_active()

    def restore_hfp_audio(self) -> None:
        """
        Re-assert Bluetooth HFP after TTS.

        paplay / Pulse often leave the SCO link wedged or flip the card away from
        handsfree_head_unit — mic then stays silent until profile is bounced.
        """
        if not self._which("pactl"):
            return
        mac = (os.getenv("BT_DEVICE_MAC") or os.getenv("BT_MAC") or "").strip()
        if not mac:
            return
        profile = (os.getenv("BT_PROFILE") or "handsfree_head_unit").strip()
        usb_source = (os.getenv("PULSE_DEFAULT_SOURCE") or "").strip()
        # USB mic + A2DP speaker: never bounce the card (that drops the speaker)
        # and never steal default source back to Bluetooth HFP.
        if profile == "a2dp_sink":
            if usb_source and self._which("pactl"):
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
