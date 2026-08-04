# voice-client-pi

Голосовой клиент для Raspberry Pi: wake (**openWakeWord «alexa»**) → запись → backend → TTS + mpv (Яндекс Музыка).

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
| `OWW_MODEL` | `alexa` | Pretrained openWakeWord (`alexa`, `hey jarvis`, …) |
| `OWW_THRESHOLD` | `0.5` | Порог score 0–1 (ниже = чувствительнее) |
| `OWW_FRAMEWORK` | `onnx` | `onnx` или `tflite` |
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

`install.sh` включает systemd timer (~каждые 10 минут):

```bash
scripts/update.sh   # git fetch/pull + compose rebuild при изменениях
```

```bash
systemctl list-timers voice-client-update.timer
journalctl -u voice-client-update.service -n 50
```

Push в git → на Pi контейнер пересоберётся сам.

## Проверка аудио на хосте

```bash
arecord -l
speaker-test -t wav -c 2
```

## Структура

| Путь | Роль |
|------|------|
| `pi_assistant.py` | Клиент (openWakeWord + sounddevice + mpv) |
| `Dockerfile` / `docker-compose.yml` | Образ и запуск |

Wake раньше был на Vosk («ассистент»). Сейчас always-on — только OWW; STT по-прежнему на backend.
