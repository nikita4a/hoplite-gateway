⚡ **Hoplite OpenAI Gateway** — Claude Opus 5.5 как обычный OpenAI API

Облачные агенты Hoplite (Opus 5.5, GPT-5.5, Sonnet 5, Terra) теперь доступны из ЛЮБОГО OpenAI-клиента: OMP, OpenCode, Cursor, LiteLLM, curl.

🔧 **Что умеет:**
• OpenAI v1 `/v1/chat/completions` — streaming + tool use
• Реальный стриминг (дельты с агента, heartbeat против обрыва)
• Tool calling: агент отвечает протоколом `{"tool_calls":[...]}` → честный OpenAI-формат
• Память диалога: один тред = один разговор, агент помнит контекст
• 6 моделей + `-fast` варианты + `reasoning_effort` (off…max)
• Watchdog: авто-рестарт, авто-обновление MCP-токена, status.json
• Desktop-меню (Tkinter): старт/стоп, ключ, ngrok, тест — в один клик
• Веб-дашборд на `:8787/` — статус, конфиг, тест-чат
• ngrok-туннель встроен (кнопка → публичный URL)

🚀 **Установка (2 минуты):**
```
git clone https://github.com/MeshFinancial/hoplite-gateway
cd hoplite-gateway
pip install -r requirements.txt
start.bat   ← меню: вставил ключ hop_... → Start → работает
```

Ключ: app.hoplite.sh → Settings → API Keys.

📡 **Подключение:**
```
Base URL: http://127.0.0.1:8787/v1
API key:  sk-local (любой)
Model:    hoplite-opus-5
```

⚠️ Особенность: каждый запрос = реальный прогон облачного агента (30–120 сек), это не чат-миллисекунды. Зато агент умеет ВСЁ: код, файлы, GitHub-репы, PR.

🛡 Приватность: ключи в config.json (gitignored), шлюз только на 127.0.0.1, наружу — только явным ngrok.

#AI #OpenAI #Claude #Opus55 #gateway #opensource
