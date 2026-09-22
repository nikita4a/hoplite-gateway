# 🎯 ТОЧНЫЙ ГАЙД — Hoplite Gateway: Opus 5.5 + tools + streaming

> Всё фактическое: model-id проверены живыми запросами к API Hoplite (2026-09).
> Шлюз: `server.py` на `http://127.0.0.1:8787` — OpenAI v1 совместимый.

---

## 0. Как это работает (30 секунд теории)

```
Твой клиент (OMP/OpenCode/curl)
   │  POST /v1/chat/completions   ← обычный OpenAI-формат
   ▼
Шлюз :8787
   │  POST /api/threads {projectId, prompt, model:"claude-opus-5"}   ← X-Api-Key
   ▼
Hoplite cloud sandbox → агент на Claude Opus 5.5 делает работу
   │
   ▼
Шлюз поллит /api/threads/{id}/messages, стримит дельты обратно в SSE
```

Каждый «запрос» = реальный прогон облачного агента: **30–120 секунд**
(при очереди sandbox — до 9 минут; шлюз держит соединение heartbeat'ами).

---

## 1. Установка (чистая машина)

```bash
git clone https://github.com/MeshFinancial/hoplite-gateway
cd hoplite-gateway
pip install -r requirements.txt          # fastapi uvicorn httpx
cp config.example.json config.json
```

В `config.json` вписать ключ:
```json
{
  "api_key": "hop_XXXX...",
  "api_base": "https://api.hoplite.sh",
  "project_id": "",
  "ngrok_token": ""
}
```

**Ключ взять:** https://app.hoplite.sh → Settings → API Keys → New API Key.
Ключ обязан уметь создавать треды (проверка: `POST /api/threads` → 201, а не 403).

**Проект:** нужен хотя бы один проект в Hoplite (создаётся в веб-UI).
Для чистого Q&A GitHub App не обязателен; для работы с репой — подключить.

Запуск (Windows): двойной клик `start.bat` → меню → ▶ Start gateway.
Запуск (везде): `python server.py`.

Проверка: `curl http://127.0.0.1:8787/health` → `{"ok":true,"auth":true,...}`

---

## 2. Модели (проверено живым API)

| Модель шлюза | Hoplite `model` | Что это |
|---|---|---|
| **`hoplite-opus-5`** | `claude-opus-5` | **Claude Opus 5.5** — главный |
| `hoplite-sonnet-5` | `claude-sonnet-5` | Sonnet 5 |
| `hoplite-opus-4.8` | `claude-opus-4-8` | Opus 4.8 |
| `hoplite-gpt-5.5` | `gpt-5.5` | GPT-5.5 |
| `hoplite-gpt-5.6-terra` | `gpt-5.6-terra` | GPT-5.6 Terra |
| `hoplite-agent` | (default проекта) | дефолтный агент |
| любой + `-fast` | + `speed:"fast"` | быстрый режим (напр. `hoplite-opus-5-fast`) |

Невалидная модель → API отвечает `400 invalid_model` → шлюз автоматически
падает на default проекта (запрос не теряется).

### Reasoning effort

OpenAI-поле `reasoning_effort` пробрасывается в Hoplite `reasoning.mode`.
Допустимые значения (enum из OpenAPI-спеки Hoplite):
`off | none | minimal | low | medium | high | xhigh | max`

```json
{"model": "hoplite-opus-5", "reasoning_effort": "xhigh", "messages": [...]}
```

---

## 3. Подключение клиентов

### 3.1 OMP CLI (уже настроен на этой машине)

Файлы `~/.omp/agent/`:

**models.yml** → `providers:`
```yaml
  hermes-hoplite:
    api: openai-completions
    apiKey: sk-local
    authHeader: true
    baseUrl: http://127.0.0.1:8787/v1
    compat:
      maxTokensField: max_tokens
      supportsDeveloperRole: false
      supportsStore: false
      supportsStreaming: true
      supportsToolChoice: true
      supportsUsageInStreaming: false
    models:
      - id: hoplite-opus-5
        name: hoplite-opus-5 (Claude Opus 5.5)
        contextWindow: 200000
        maxTokens: 16384
        input: [text]
        reasoning: true
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}
      # + hoplite-sonnet-5, hoplite-gpt-5.5, hoplite-gpt-5.6-terra,
      #   hoplite-opus-4.8, hoplite-agent  (аналогично)
```

**providers.yml** → `providers:`
```yaml
  hermes-hoplite:
    apiKey: sk-local
    baseUrl: http://127.0.0.1:8787/v1
    discoverModels: false
    provider: openai
    streamIdleTimeoutSeconds: 600     # важно: агент думает долго
    models:
      - {id: hoplite-opus-5, name: hoplite-opus-5}
      # + остальные
```

**config.yml**
```yaml
enabledModels:
  - hermes-hoplite/*
```

Перезапустить OMP → `omp /model hermes-hoplite/hoplite-opus-5`
(в меню моделей: provider `hermes-hoplite`, 6 моделей).

### 3.2 OpenCode (`~/.config/opencode/opencode.json`)

```json
{
  "provider": {
    "hoplite": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-local"
      },
      "models": {
        "hoplite-opus-5":  { "name": "Hoplite Opus 5.5" },
        "hoplite-sonnet-5":{ "name": "Hoplite Sonnet 5" },
        "hoplite-gpt-5.5": { "name": "Hoplite GPT-5.5" }
      }
    }
  }
}
```
В OpenCode: `/models` → `hoplite/hoplite-opus-5`.

### 3.3 Любой OpenAI-клиент / curl

```bash
# обычный
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hoplite-opus-5","messages":[{"role":"user","content":"привет"}]}'

# стриминг
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hoplite-opus-5","stream":true,"messages":[{"role":"user","content":"привет"}]}'
```

Python:
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local")
r = client.chat.completions.create(
    model="hoplite-opus-5",
    messages=[{"role": "user", "content": "напиши функцию fizzbuzz"}],
    stream=True)
for chunk in r:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### 3.4 Снаружи машины (ngrok)

Кнопка **🔗 Start ngrok** в меню, или дашборд `http://127.0.0.1:8787/`, или:
```bash
curl -X POST http://127.0.0.1:8787/admin/ngrok -d '{"action":"start"}'
```
Ответ: `{"ok":true,"url":"https://xxx.ngrok-free.dev"}` → base URL для внешних
клиентов: `https://xxx.ngrok-free.dev/v1`. Остановить: `{"action":"stop"}`.
Свой токен — в `config.json` → `ngrok_token` (или `winget install ngrok.ngrok` + `ngrok config add-authtoken`).

---

## 4. Streaming — как устроен (честно)

- Шлюз **не** фейкает стрим: он поллит сообщения треда каждую 1–2.5 с и отдаёт
  новые куски текста агента сразу, как они появляются.
- Пока sandbox запускается (десятки секунд), в SSE идут heartbeat-комментарии
  `: hb` каждые 15 с — соединение не рвётся (проверено: 35 heartbeat за 547 s).
- Конец: `finish_reason:"stop"` + `data: [DONE]`.
- Таймаут клиента ставь ≥ 600 s (в OMP это `streamIdleTimeoutSeconds: 600`).

## 5. Tool use — как устроен (честно)

Hoplite-агент — автономный: у него свои внутренние инструменты (файлы, bash,
репа). Внешние OpenAI-tools клиента прокидываются так:

1. Клиент шлёт `tools:[...]` → шлюз вкладывает их JSON-схемы в промпт
   агенту с инструкцией отвечать протоколом `{"tool_calls":[...]}`.
2. Агент отвечает JSON'ом → шлюз парсит (в т.ч. из markdown-обёртки) →
   возвращает честный OpenAI `tool_calls` (`finish_reason:"tool_calls"`).
3. Клиент присылает `role:"tool"` результаты → шлюз аппендит их в **тот же
   тред** → агент продолжает.
4. Парсер покрыт юнит-тестами: `python tests_gateway.py` (5 групп).

⚠️ Реальность: каждый tool-round-trip = ход облачного агента (30–120 s).
Для быстрых агентных циклов это медленно; для «поставь задачу — получи
результат» (сделать фикс, написать код, разобрать репу) — в самый раз.

## 6. Память диалога

- Один разговор = один тред Hoplite. Ключ разговора: поле `user` запроса
  (или хэш первого user-сообщения).
- Следующее сообщение той же беседы **аппендится в тред** — агент помнит
  контекст (проверено: «запомни BANANA-42» → следующий ход → «BANANA-42»).
- Если аппенд не удался (тред занят/мёртв) — шлюз создаёт новый тред и
  переносит историю сжатым транскриптом (последние 8 сообщений).

## 7. Дашборд и админка

| URL | Что |
|---|---|
| `GET /` | веб-дашборд: статус, ключ, ngrok, тест-чат, конфиги для копирования |
| `GET /health` | auth/статистика/uptime |
| `GET /setup` | машиночитаемая инструкция |
| `POST /admin/config` | `{"api_key":"hop_...","project_id":"proj_..."}` — hot reload, без рестарта |
| `POST /admin/ngrok` | `{"action":"start\|stop\|status"}` |

## 8. Траблшутинг (проверенные случаи)

| Симптом | Причина | Лечение |
|---|---|---|
| `auth failed (401)` | ключ невалиден | новый ключ в app.hoplite.sh |
| `403` при создании треда | ключ без прав (read-only `hop_` ключ старого типа) | пересоздать ключ; сервисный аккаунт |
| `429 after N attempts` | лимит тредов аккаунта (много тестов за день) | ждать; шлюз сам ретраит с backoff |
| `invalid_model` (400) | модель недоступна аккаунту | шлюз сам упадёт на default; либо `hoplite-agent` |
| `did not finish within 540s` | очередь sandbox | тред обычно доделывается позже — открыть app.hoplite.sh; поднять `DEADLINE` в server.py |
| `invalid_thread` (400) | нет проекта / проект без репы (для repo-задач) | создать проект; подключить GitHub App |
| OMP рвёт стрим | дефолтный idle-таймаут | `streamIdleTimeoutSeconds: 600` (уже выставлен) |

## 9. Безопасность

- `config.json`, `cookies.json`, `*_token.json`, `.env` — в `.gitignore`, никогда не коммитятся.
- Шлюз слушает только `127.0.0.1`; наружу — только явным ngrok (и он без авторизации — не свети надолго).
- Ключ скомпрометирован (попал в публичную репу/скрин) → **немедленно** ротировать в app.hoplite.sh.

## 10. Файлы

```
server.py            весь шлюз (один файл)
menu.pyw             desktop-меню (Windows)
start.bat            запуск меню
config.json          ТВОИ секреты (gitignored)
config.example.json  шаблон
tests_gateway.py     офлайн юнит-тесты (5 групп)
push_all.py          пуш в GitHub через API
opencode_config.json сниппет OpenCode
README.md            overview
GUIDE.md             этот гайд
```
