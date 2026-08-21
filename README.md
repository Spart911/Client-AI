# voice-client-pi

Голосовой клиент для Raspberry Pi: wake (**openWakeWord** или **microWakeWord**) → запись → backend → TTS + mpv (Яндекс Музыка).

Запуск в **Docker**, автообновление через **git pull** + rebuild. На Pi клонируется только этот репозиторий.

Целевая плата: **Raspberry Pi 3B / 3B+**, Raspberry Pi OS Lite.

## Быстрый старт (на Pi)

```bash
git clone https://github.com/Spart911/Client-AI.git ~/voice-client-pi
cd ~/voice-client-pi
cp .env.example .env
# отредактируйте VOICE_BACKEND_URL и MUSIC_DEVICE_ID
nano .env

bash install.sh
```

Один раз с параметрами:

```bash
cd ~/voice-client-pi
DEVICE_ID=pi-livingroom BACKEND=http://voice.pora-ai.ru bash install.sh
```

Скажите: «**alexa**, включи кадилак» — ответ и музыка должны идти с колонки Pi.

## Конфигурация (`.env`)

| Переменная | Пример | Назначение |
|------------|--------|------------|
| `VOICE_BACKEND_URL` | `http://voice.pora-ai.ru` | Backend assist API |
| `MUSIC_DEVICE_ID` | `pi-livingroom` | ID устройства (как в Open WebUI music tool) |
| `WAKE_ENGINE` | `oww` | `oww` (openWakeWord) или `mww` (microWakeWord) |
| `OWW_MODEL` | `alexa` | Pretrained openWakeWord (`alexa`, `hey jarvis`, …) |
| `OWW_THRESHOLD` | `0.35` | Порог score 0–1 (ниже = чувствительнее) |
| `OWW_FRAMEWORK` | `tflite` | `onnx` или `tflite` |
| `MWW_MODEL_CONFIG` | `/app/models/ru_jarvis_mww.json` | Конфиг модели из `interkelstar/microwakeword-trainer` |
| `MUSIC_POLL` | `true` | Фоновый poll `/v1/music/pending` |

`.env` не коммитится.

## Docker

```bash
docker compose up -d --build
docker compose logs -f
docker compose restart
docker compose down
```

Контейнер получает `/dev/snd` (ALSA). Предпочтительнее USB-мик; BT сложнее в Docker.

## Автообновление

`install.sh` включает systemd timer (~каждые 30 секунд, `OnCalendar=*-*-* *:*:00/30`):

```bash
scripts/update.sh   # git fetch/pull + compose rebuild при изменениях
```

```bash
systemctl list-timers voice-client-update.timer
journalctl -u voice-client-update.service -n 50
```

Push в git → на Pi контейнер пересоберётся сам.

## Bluetooth-колонка (автоподключение)

1. Один раз спарить колонку (`bluetoothctl pair/trust/connect`).
2. В `.env` указать MAC:

```bash
bluetoothctl devices
# Device AA:BB:CC:DD:EE:FF MySpeaker
```

```env
BT_DEVICE_MAC=AA:BB:CC:DD:EE:FF
BT_PROFILE=handsfree_head_unit
AUDIO_INPUT_DEVICE=pulse
```

3. Включить сервис:

```bash
chmod +x scripts/bt-connect.sh
mkdir -p ~/.config/systemd/user
# путь подставьте свой (install.sh делает это сам)
systemctl --user daemon-reload
systemctl --user enable --now voice-bt-connect.service
sudo loginctl enable-linger "$USER"
```

Сервис каждые ~15 с переподключает колонку и ставит HFP + default sink/source.

Ручной тест: `bash scripts/bt-connect.sh` (нужен `BT_DEVICE_MAC` в `.env`).

## Проверка аудио на хосте

```bash
arecord -l
speaker-test -t wav -c 2
```

## Структура

| Путь | Роль |
|------|------|
| `pi_assistant.py` | Клиент (openWakeWord/microWakeWord + sounddevice + mpv) |
| `Dockerfile` / `docker-compose.yml` | Образ и запуск |

Wake раньше был на Vosk («ассистент»). Сейчас always-on — OWW или MWW; STT по-прежнему на backend.
