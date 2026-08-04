# TASK: Raspberry Pi voice client — standalone Git + Docker + auto-update

> Этот файл — бриф для нового агента / нового репозитория.  
> **Не тянуть весь monorepo OpenWebUI на Raspberry Pi.**

---

## Цель

Сделать **отдельный Git-репозиторий** только для Pi-клиента голосового ассистента:

1. Запуск в **Docker** на Raspberry Pi  
2. **Автообновления через Git** (pull + rebuild/restart)  
3. На Pi клонируется **только этот маленький репозиторий**, не OpenWebUI

Исходники сейчас лежат в monorepo как черновик:

`OpenWebUI/deploy/voice-client-pi/`

Их нужно **вынести в новый repo** (например `voice-client-pi`) и доработать под Docker + git updates.

---

## Что делает клиент (логика уже есть)

```
Wake (openWakeWord «alexa»)
  → запись команды (sounddevice)
  → POST http://voice.pora-ai.ru/v1/assist  (+ device_id)
  → проиграть TTS (WAV из ответа)
  → если есть playback.url → mpv (Яндекс Музыка)
  → фоновый poll GET /v1/music/pending/{device_id}
```

Серверная часть (STT / LLM / TTS / Yandex Music) **уже на сервере** — клиент только mic + speaker + HTTP.

Ключевые файлы-черновики в этой папке:

| Файл | Статус |
|------|--------|
| `pi_assistant.py` | ✅ openWakeWord (alexa) + mpv |
| `requirements.txt` | ✅ лёгкие deps |
| `install.sh` / `voice-client.service` | ⚠️ venv/systemd — заменить на Docker |
| `README.md` | обновить под новый repo |

---

## Архитектура целевого решения

```
[ новый GitHub repo: voice-client-pi ]
        │
        │  git clone (только этот repo)
        ▼
[ Raspberry Pi ]
  docker compose up -d
    └─ container: pi_assistant.py
         devices: /dev/snd
         env: VOICE_BACKEND_URL, MUSIC_DEVICE_ID, ...

  update loop (systemd timer / cron):
    git fetch && git pull
    если код изменился → docker compose up -d --build
```

### Что НЕ делать

- ❌ Не клонировать `OpenWebUI` целиком на Pi  
- ❌ Не тащить backend STT/TTS на Pi  
- ❌ Не делать Watchtower как единственный путь, если образы не публикуются — приоритет **git pull на Pi**  
- ⚠️ Wake: openWakeWord + onnxruntime (тяжёлее Vosk по RAM — следить на Pi 3B 1 GB)

---

## Nuances (важно)

### Железо / ОС

- Целевая плата: **Raspberry Pi 3B / 3B+**, 1 GB RAM  
- ОС: **Raspberry Pi OS Lite 32-bit** (или 64-bit если 3B+)  
- Пользователь на тестовой Pi: `server-pi`, hostname `pi-livingroom`  
- Backend: `http://voice.pora-ai.ru` (HTTP, без TLS на voice Ingress)  
- `device_id`: например `pi-livingroom` — должен совпадать с Open WebUI music tool Valves  

### Аудио в Docker

- Нужен доступ к ALSA: `--device /dev/snd` или `devices: ["/dev/snd:/dev/snd"]`  
- Группа `audio` в контейнере  
- `mpv` внутри образа (apt)  
- `portaudio19-dev` / `libportaudio2` для `sounddevice`  
- USB-мик предпочтительнее BT-mic; BT-колонка возможна, но через Pulse/BlueZ сложнее в Docker — **сначала ALSA/USB**  
- На Pi 3B встроенный Wi‑Fi: 2.4 GHz (3B) / 5 GHz есть на 3B+; Ethernet надёжнее для первого сетапа  

### Docker на Pi 3B

- Мало RAM: образ держать **slim**, один сервис  
- Платформа: `linux/arm/v7` (32-bit) или `linux/arm64` (если 64-bit OS)  
- Prefetch openWakeWord models at Docker build (`openwakeword.utils.download_models()`)  
- `restart: unless-stopped`  

### Git auto-update

Рекомендуемая схема:

1. На Pi: `git clone <NEW_REPO_URL> ~/voice-client-pi`  
2. `.env` **не в git** (только `.env.example`) — лежит рядом / в compose `env_file`  
3. Systemd timer каждые N минут (например 5–15):
   ```bash
   cd ~/voice-client-pi
   git fetch origin
   LOCAL=$(git rev-parse HEAD)
   REMOTE=$(git rev-parse @{u})
   if [ "$LOCAL" != "$REMOTE" ]; then
     git pull --ff-only
     docker compose up -d --build
   fi
   ```
4. Опционально: sparse — если когда-то останется в monorepo, использовать `git sparse-checkout` только на `deploy/voice-client-pi`; **предпочтительно отдельный repo**  
5. Deploy key / public repo / fine-grained PAT с read-only — для приватного Git  

### API контракт с backend (не менять без нужды)

- `GET /health`  
- `POST /v1/assist` — multipart: `file`, form: `return_audio`, `device_id`  
  Ответ JSON: `transcript`, `reply`, `audio_base64`, optional `playback` `{action,url,title,artist}`  
- `GET /v1/music/pending/{device_id}` → `{commands: [...]}`  
- Music tool в Open WebUI: `deploy/tools/openwebui_yandex_music.py` (в monorepo) — `default_device_id` = id Pi  

### Безопасность

- `.env` с `MUSIC_DEVICE_ID` / backend URL — не коммитить  
- Backend voice сейчас на HTTP — ок для LAN/доверенной сети; не светить лишнее наружу  
- Git deploy key — read-only  

---

## Deliverables для нового агента

В **новом** репозитории `voice-client-pi`:

1. `Dockerfile` (arm-friendly, mpv + portaudio + python deps)  
2. `docker-compose.yml` (devices `/dev/snd`, env_file, restart)  
3. `pi_assistant.py` (перенести/адаптировать из черновика)  
4. `requirements.txt`  
5. `.env.example`  
6. `scripts/update.sh` — git pull + compose rebuild if changed  
7. `systemd/voice-client-update.timer` + `.service`  
8. `install.sh` — ставит Docker (если нет), клонирует repo, создаёт `.env`, `compose up -d`, включает timer  
9. `README.md` — установка на Pi в 5 команд  

Критерий готовности:

```bash
# на Pi
git clone <repo> ~/voice-client-pi
cd ~/voice-client-pi && cp .env.example .env   # править BACKEND / DEVICE_ID
bash install.sh
# «alexa, включи кадилак» → звук с колонки Pi
# push в git → через timer контейнер обновляется сам
```

---

## Контекст monorepo (только для справки, не клонировать на Pi)

| Путь в OpenWebUI | Зачем |
|------------------|--------|
| `deploy/voice-assistant/` | серверный backend (k8s) |
| `deploy/voice-assistant.yaml` | k8s манифест, Ingress HTTP `voice.pora-ai.ru` |
| `deploy/tools/openwebui_yandex_music.py` | OWUI tool → `/v1/music/play` |
| `deploy/voice-client-pi/` | **этот черновик клиента** → вынести в отдельный git |

---

## Порядок работы новому агенту

1. Создать/открыть **отдельный** git repo (не OpenWebUI)  
2. Скопировать сюда `pi_assistant.py`, `requirements.txt`, `.env.example` как базу  
3. Добавить Docker + compose + update scripts  
4. Проверить на Pi: mic + mpv + assist + music poll  
5. Включить systemd timer автообновления  
6. Задокументировать в README  

**Не** добавлять в repo серверный код OpenWebUI / GigaAM / OmniVoice / yandex-music lib.
