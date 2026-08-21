"""
title: Timer & Alarm (Pi living room)
author: Client-AI
version: 0.4.3
description: >
  Ставит таймер/будильник через pi-alert:// на Raspberry Pi.
  Таймер по умолчанию — мелодия classic (как будильник), не отдельный timer-писк.
  В query уходит pi-alert://… — бэкенд кладёт это в очередь без Яндекса.
  Нужен тот же X-Music-Api-Key, что у «Yandex Music Player».
  ВАЖНО: обнови tool в Open WebUI после правки.
required_open_webui_version: 0.4.0
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        backend_url: str = Field(
            default="http://voice.pora-ai.ru",
            description="VOICE_BACKEND_URL — тот же хост, что у Pi",
        )
        music_api_key: str = Field(
            default="",
            description="Тот же X-Music-Api-Key / MUSIC_API_KEY, что у «Yandex Music Player»",
        )
        device_id: str = Field(
            default="pi-livingroom",
            description="MUSIC_DEVICE_ID клиента",
        )
        timezone: str = Field(
            default="Europe/Moscow",
            description="Часовой пояс будильника",
        )
        timer_sound_url: str = Field(
            default="",
            description="URL сигнала таймера. Пусто = встроенный писк на Pi",
        )
        alarm_catalog: str = Field(
            default="classic=\ndigital=\nsoft=",
            description=(
                "Мелодии будильника, по одной на строку: имя=URL. "
                "Пустой URL = встроенный звук на клиенте. "
                "Модель выбирает имя (classic / digital / soft / ваши)."
            ),
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def set_timer(self, seconds: int = 0, minutes: int = 0, label: str = "") -> str:
        """
        Поставить таймер. По истечении времени на колонке Pi проиграется сигнал таймера.
        :param seconds: Секунды (например 30)
        :param minutes: Минуты, плюсуются к секундам
        :param label: Необязательное название («яйца», «чай»)
        """
        total = max(0, int(minutes or 0)) * 60 + max(0, int(seconds or 0))
        if total < 1:
            return "Укажи длительность таймера: секунды и/или минуты."
        if not (self.valves.music_api_key or "").strip():
            return (
                "В Valves укажи music_api_key — тот же ключ, что у "
                "«Yandex Music Player»."
            )
        fire_at = int(time.time()) + total
        title = label.strip() or f"Таймер {self._fmt_duration(total)}"
        # Prefer classic alarm melody; empty timer_sound_url → builtin classic on Pi.
        catalog = self._catalog()
        timer_media = self.valves.timer_sound_url.strip() or catalog.get("classic", "")
        url = self._alert_url(
            "timer",
            fire_at=fire_at,
            sound="classic",
            media_url=timer_media,
            loop=0,
            title=title,
        )
        err = self._enqueue(url, title)
        if err:
            return err
        when = datetime.fromtimestamp(fire_at, tz=self._tz()).strftime("%H:%M:%S")
        return f"Таймер «{title}» на {self._fmt_duration(total)}. Сигнал около {when}."

    def set_alarm(self, time_hhmm: str, sound: str = "classic", label: str = "") -> str:
        """
        Поставить будильник на сегодня (или на завтра, если время уже прошло).
        Мелодию бери из списка tool Valves (alarm_catalog): по умолчанию classic, digital, soft.
        :param time_hhmm: Время ЧЧ:ММ, например 07:30
        :param sound: Имя мелодии из каталога Valves
        :param label: Необязательное название
        """
        if not (self.valves.music_api_key or "").strip():
            return (
                "В Valves укажи music_api_key — тот же ключ, что у "
                "«Yandex Music Player»."
            )
        hhmm = self._parse_hhmm(time_hhmm)
        if hhmm is None:
            return "Время будильника не понял. Нужно ЧЧ:ММ, например 07:30."
        catalog = self._catalog()
        key = (sound or "classic").strip().lower()
        if key not in catalog:
            names = ", ".join(catalog) or "(каталог пуст)"
            return f"Мелодии «{key}» нет. Доступно: {names}."
        fire_at = self._next_alarm_unix(hhmm[0], hhmm[1])
        title = label.strip() or f"Будильник {hhmm[0]:02d}:{hhmm[1]:02d}"
        url = self._alert_url(
            "alarm",
            fire_at=fire_at,
            sound=key,
            media_url=catalog[key],
            loop=1,
            title=title,
        )
        err = self._enqueue(url, title)
        if err:
            return err
        when = datetime.fromtimestamp(fire_at, tz=self._tz()).strftime("%Y-%m-%d %H:%M")
        return f"Будильник «{title}» на {when}, мелодия «{key}». Выключить: скажи «Джарвис» или cancel_alarm."

    def list_alarm_sounds(self) -> str:
        """
        Список мелодий будильника из Valves (alarm_catalog).
        """
        catalog = self._catalog()
        if not catalog:
            return "Каталог пуст — заполни alarm_catalog в настройках tool."
        lines = []
        for name, media in catalog.items():
            kind = media or "встроенный сигнал на Pi"
            lines.append(f"- {name}: {kind}")
        return "Мелодии будильника:\n" + "\n".join(lines)

    def cancel_timer(self) -> str:
        """Отменить все ожидающие таймеры на Pi."""
        url = self._alert_url("cancel", cancel_kind="timer")
        err = self._enqueue(url, "Отмена таймера")
        return err or "Таймеры отменены."

    def cancel_alarm(self) -> str:
        """Отменить все ожидающие будильники и выключить текущий звонок на Pi."""
        url = self._alert_url("cancel", cancel_kind="alarm")
        err = self._enqueue(url, "Отмена будильника")
        return err or "Будильники отменены."

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.valves.timezone.strip() or "Europe/Moscow")
        except Exception:
            return ZoneInfo("Europe/Moscow")

    def _catalog(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for raw in (self.valves.alarm_catalog or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                name, media = line.split("=", 1)
            else:
                name, media = line, ""
            name = name.strip().lower()
            if name:
                out[name] = media.strip()
        return out

    def _next_alarm_unix(self, hour: int, minute: int) -> int:
        tz = self._tz()
        now = datetime.now(tz)
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now + timedelta(seconds=15):
            when += timedelta(days=1)
        return int(when.timestamp())

    @staticmethod
    def _parse_hhmm(value: str) -> Optional[tuple[int, int]]:
        text = (value or "").strip().lower().replace(".", ":").replace("-", ":")
        text = text.replace(" часов", "").replace(" часа", "").replace(" час", "")
        parts = [p for p in text.replace(" ", ":").split(":") if p != ""]
        if len(parts) < 2:
            digits = "".join(ch for ch in text if ch.isdigit())
            if len(digits) in (3, 4):
                digits = digits.zfill(4)
                parts = [digits[:2], digits[2:]]
            else:
                return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour, minute

    @staticmethod
    def _fmt_duration(total: int) -> str:
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours} ч {minutes} мин"
        if minutes and seconds:
            return f"{minutes} мин {seconds} с"
        if minutes:
            return f"{minutes} мин"
        return f"{seconds} с"

    @staticmethod
    def _alert_url(kind: str, **params) -> str:
        clean = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
        return f"pi-alert://{kind}?{urlencode(clean)}"

    def _enqueue(self, alert_url: str, title: str) -> str:
        backend = (self.valves.backend_url or "").rstrip("/")
        device = (self.valves.device_id or "").strip()
        api_key = (self.valves.music_api_key or "").strip()
        if not backend or not device:
            return "Заполни backend_url и device_id в Valves tool."
        if not api_key:
            return (
                "В Valves укажи music_api_key — тот же ключ, что у "
                "«Yandex Music Player»."
            )
        # Backend MusicPlayRequest only accepts query+device_id.
        # pi-alert:// in query is queued as-is (no Yandex search).
        payload = {
            "device_id": device,
            "query": alert_url,
        }
        endpoint = f"{backend}/v1/music/play"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Music-Api-Key": api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code == 401:
                return (
                    "Бэкенд отверг music API key (401). "
                    "Скопируй ключ из Valves «Yandex Music Player» в music_api_key."
                )
            return f"Бэкенд {endpoint} ответил {exc.code}: {detail}"
        except Exception as exc:
            return f"Не удалось отправить на Pi через {endpoint}: {exc}"
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("success") is False:
            return f"Бэкенд не принял таймер: {data.get('error') or raw[:200]}"
        _ = title
        return ""
