# voice-client-pi

Голосовой клиент для Raspberry Pi: wake (Vosk «ассистент») → запись → backend → TTS + mpv (Яндекс Музыка).

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

Скажите: «ассистент, включи кадилак» — ответ и музыка должны идти с колонки Pi.

## Конфигурация (`.env`)

| Переменная | Пример | Назначение |
|------------|--------|------------|
| `VOICE_BACKEND_URL` | `http://voice.pora-ai.ru` | Backend assist API |
| `MUSIC_DEVICE_ID` | `pi-livingroom` | ID устройства (как в Open WebUI music tool) |
| `WAKE_WORDS` | `ассистент` | Wake-фразы через запятую |
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
| `pi_assistant.py` | Клиент (Vosk + sounddevice + mpv) |
| `Dockerfile` / `docker-compose.yml` | Образ и запуск |
| `models/vosk-model-small-ru-0.22.zip` | Модель Vosk в git (~44MB, без скачивания с alphacephei) |
| `requirements.txt` | Python deps |
| `.env.example` | Шаблон env |
| `scripts/update.sh` | Git pull + rebuild |
| `systemd/` | Timer автообновления |
| `install.sh` | Docker + compose + timer |

## Open WebUI music tool

В Valves укажите `default_device_id` = тот же `MUSIC_DEVICE_ID` (например `pi-livingroom`).

## Без Docker (отладка)

```bash
sudo apt install -y python3-venv portaudio19-dev mpv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a
python pi_assistant.py
```
