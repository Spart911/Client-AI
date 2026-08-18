#!/usr/bin/env python3
"""Collect USB-mic «Джарвис» clips for microwakeword-trainer.

Saves 16 kHz mono 16-bit PCM WAV:
  record/usb/            positives (train_with_recordings: record/)
  record/usb-negatives/  false-wake / room noise (copy to user_negatives/)

Raw mic — no RNNoise. Same Pulse USB source as the voice client.
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
MAX_SEC = 8.0


def _rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


def _ensure_usb_source() -> None:
    source = (os.getenv("PULSE_DEFAULT_SOURCE") or "").strip()
    if not source or not shutil.which("pactl"):
        return
    subprocess.run(
        ["pactl", "set-default-source", source],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3,
    )
    print(f"Pulse source: {source}", flush=True)


def _default_sink() -> str:
    if not shutil.which("pactl"):
        return ""
    try:
        return subprocess.check_output(
            ["pactl", "get-default-sink"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _make_listen_chime(rate: int) -> np.ndarray:
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
        wave_ = np.sin(2.0 * np.pi * freq * t) * env * amplitude
        wave_ += np.sin(4.0 * np.pi * freq * t) * env * (amplitude * 0.18)
        return wave_.astype(np.float32)

    gap = np.zeros(int(rate * 0.04), dtype=np.float32)
    chime = np.concatenate(
        [
            tone(784.0, 0.11),
            gap,
            tone(1046.5, 0.16),
            np.zeros(int(rate * 0.05), dtype=np.float32),
        ]
    )
    peak = float(np.max(np.abs(chime)) or 1.0)
    if peak > 0.35:
        chime *= 0.35 / peak
    return np.concatenate(
        [
            np.zeros(int(rate * 0.18), dtype=np.float32),
            chime,
            np.zeros(int(rate * 0.45), dtype=np.float32),
        ]
    )


def _drain_stdin() -> None:
    """Drop keys typed during beep/record so they don't become the next command."""
    try:
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def _play_wav_file(path: Path) -> None:
    """paplay only — mpv steals the keyboard in docker -it."""
    paplay = shutil.which("paplay")
    if not paplay:
        print("paplay не найден, звук не играю", flush=True)
        return
    cmd = [paplay]
    sink = _default_sink()
    if sink:
        cmd.extend(["--device", sink])
    cmd.append(str(path))
    env = os.environ.copy()
    env.setdefault("PULSE_LATENCY_MSEC", "200")
    subprocess.run(
        cmd,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=12,
        env=env,
    )


def _play_chime() -> None:
    audio = _make_listen_chime(SAMPLE_RATE)
    fd, tmp_name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    path = Path(tmp_name)
    try:
        pcm = np.clip(audio, -1.0, 1.0)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
        _play_wav_file(path)
        time.sleep(0.20)
    finally:
        path.unlink(missing_ok=True)
        _drain_stdin()


def _write_wav(path: Path, frames: np.ndarray) -> None:
    pcm = (np.clip(frames, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def _play_wav(path: Path) -> None:
    _play_wav_file(path)
    _drain_stdin()


def _enter_pressed() -> bool:
    if not sys.stdin.isatty():
        return False
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return False
    sys.stdin.readline()
    return True


def _record_until_enter(*, max_sec: float) -> np.ndarray | None:
    """Record until the user presses Enter (or max_sec)."""
    block = int(SAMPLE_RATE * 0.08)
    max_blocks = max(8, int(max_sec / 0.08))
    frames: list[np.ndarray] = []
    stopped = False
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=block,
    ) as stream:
        for _ in range(max_blocks):
            data, _ = stream.read(block)
            frames.append(data[:, 0].copy())
            if _enter_pressed():
                stopped = True
                break
    _drain_stdin()
    if not frames:
        return None
    audio = np.concatenate(frames)
    if not stopped and audio.size < int(SAMPLE_RATE * 0.2):
        return None
    return audio


def _count_wavs(folder: Path) -> int:
    return len(list(folder.glob("*.wav")))


def _parse_cmd(raw: str) -> str:
    s = raw.strip().lower()
    if not s:
        return "next"
    if s[0] in ("q", "й") or s in ("quit", "exit", "выход"):
        return "quit"
    if s[0] in ("r", "к") or s in ("replay", "play"):
        return "replay"
    if s[0] in ("d", "у") or s in ("del", "delete", "rm"):
        return "delete"
    if s[0] in ("n", "т") or s in ("next",):
        return "next"
    return "help"


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect USB «Джарвис» clips")
    parser.add_argument(
        "--negatives",
        action="store_true",
        help="Collect false-wake / room audio, not the wake word",
    )
    parser.add_argument("--out", default="", help="Output directory")
    parser.add_argument("--count", type=int, default=40, help="Target number of clips")
    args = parser.parse_args()

    root = Path(os.getenv("COLLECT_ROOT") or "/app")
    if not (root / "pi_assistant.py").is_file():
        root = Path(__file__).resolve().parents[1]
    if args.out:
        out = Path(args.out)
    elif args.negatives:
        out = root / "record" / "usb-negatives"
    else:
        out = root / "record" / "usb"
    out.mkdir(parents=True, exist_ok=True)
    host_dir = (os.getenv("HOST_RECORD_DIR") or "").strip() or str(
        Path.home() / "Client-AI" / "record" / ("usb-negatives" if args.negatives else "usb")
    )

    _ensure_usb_source()
    device = (os.getenv("AUDIO_INPUT_DEVICE") or "pulse").strip()
    if device:
        sd.default.device = device

    kind = "негативы (НЕ «Джарвис»)" if args.negatives else "позитивы «Джарвис»"
    prefix = "neg_usb" if args.negatives else "jarvis_usb"
    print(f"В контейнере: {out}", flush=True)
    print(f"На Pi смотреть так:  ls {host_dir}", flush=True)
    print(f"Режим: {kind}", flush=True)
    print("Enter — старт записи   Enter ещё раз — стоп   r — слушать   d — удалить   q — выход", flush=True)
    if args.negatives:
        print("Между двумя Enter: обычная речь/шум. Не говорите «Джарвис».", flush=True)
    else:
        print("Между двумя Enter скажите только «Джарвис».", flush=True)

    last: Path | None = None
    session = 0
    while _count_wavs(out) < args.count:
        n = _count_wavs(out)
        prompt = f"[{n}/{args.count}] Enter = старт > "
        _drain_stdin()
        try:
            kind_cmd = _parse_cmd(input(prompt))
        except (EOFError, KeyboardInterrupt):
            print("\nСтоп.", flush=True)
            break
        if kind_cmd == "quit":
            break
        if kind_cmd == "replay":
            if last and last.is_file():
                print(f"Играю {last.name} …", flush=True)
                _play_wav(last)
            else:
                print("Пока нечего слушать.", flush=True)
            continue
        if kind_cmd == "delete":
            if last and last.is_file():
                last.unlink(missing_ok=True)
                print(f"Удалено {last.name}. Осталось {_count_wavs(out)}.", flush=True)
                last = None
            else:
                print("Удалять нечего.", flush=True)
            continue
        if kind_cmd == "help":
            print("Команды: Enter старт, Enter стоп, r, d, q", flush=True)
            continue

        print("Пилик. Запись пошла — скажите, потом Enter = стоп.", flush=True)
        _play_chime()
        print("● пишется…  Enter = стоп", flush=True)
        audio = _record_until_enter(max_sec=MAX_SEC)
        _drain_stdin()
        if audio is None or audio.size < int(SAMPLE_RATE * 0.12):
            print("Слишком коротко. Enter — старт ещё раз.", flush=True)
            continue
        session += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out / f"{prefix}_{stamp}_{session:03d}.wav"
        _write_wav(path, audio)
        try:
            os.sync()
        except Exception:
            pass
        last = path
        dur_ms = int(1000 * audio.size / SAMPLE_RATE)
        print(
            f"OK {path.name}  {dur_ms} ms  rms={_rms(audio):.4f}  всего={_count_wavs(out)}",
            flush=True,
        )
        print(f"   файл: {host_dir}/{path.name}", flush=True)
        print("Когда будете готовы — Enter старт, потом Enter стоп.", flush=True)

    print(f"Готово: {_count_wavs(out)} файлов", flush=True)
    print(f"На Pi: ls {host_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
