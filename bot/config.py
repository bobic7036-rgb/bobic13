import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "5994037577:AAFySeJ-huI_ATLuCsfu0OWOqCE2EQED08Q")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1813590984"))

# SQLite file (on Railway use a volume path, e.g. /data/bot.db)
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "bot.db"))

BINANCE_FUTURES_URL = "https://fapi.binance.com"

# Ціни підписок у Telegram Star (зірках).
# ВАЖЛИВО (перевірено на живому API): для валюти XTR amount у send_invoice
# передається ПРЯМО В ЗІРКАХ (amount=7 → "Pay 7 ⭐"), а НЕ в тонах.
# Старий STAR=1_000_000 давав CURRENCY_TOTAL_AMOUNT_INVALID — оплата не працювала.
# Адмін може змінити наживо: /settings starsbase 5 / starspro 12 (кількість зірок)
STAR = 1

# Default settings (admin can change them live via /settings — stored in DB)
DEFAULTS = {
    "signals_interval_min": 30,   # як часто генерувати нові зделки
    "signals_count": 3,           # скільки зделок видавати за один прохід (менше, але якісніших)
    "min_score": 80,              # жорсткий фільтр: лише сильні сетапи (вищий винрейт)
    "stars_base": 3,              # ціна базової підписки (зірок)
    "base_days": 30,              # тривалість базової підписки (днів)
    "stars_pro": 7,               # ціна професійної підписки (зірок)
    "pro_days": 30,               # тривалість професійної підписки (днів)
    "ai_enabled": 1,              # безкоштовний AI-фільтр (Groq llama-3.3-70B): 1=ввімкнено
    "max_open_trades": 6,         # ліміт одночасно відкритих зделок на користувача
    "max_consecutive_losses": 2,  # стоп серії: після N SL поспіль — пауза сигналів
    "risk_per_trade_pct": 2,      # ризик на зделку (% депозиту) — PRO
    "signal_ttl_min": 180,        # час життя сигналу: старі зделки авто-закриваються (щоб не блокували нові)
    "min_symbols": 3,             # МІНІМУМ зделок на раунд: якщо ІІ знайшов менше — розширює пошук і знижує планку
    "scan_universe": 60,          # скільки монет аналізує ІІ за раунд
    "auto_relax": 1,              # self-learning: при низькому винрейті фільтри жорстчішають, при високому — м'якшають
    "pro_auto_rounds": 1,         # PRO: автоматичні раунди зделок (як на базовій) — 1 увімк / 0 вимк
    "pro_signals_command": 1,     # PRO: команда /pro видає пакет зделок просто зараз — 1 увімк / 0 вимк
    "charts_enabled": 1,          # графік монети до кожної зделки (1=увімкнено, 0=вимкнено)
}

# Безкоштовне ІІ: Groq (llama-3.3-70b-versatile) — безкоштовний ключ на console.groq.com.
# Якщо ключ не задано — працює локальний AI-енжин (мультитаймфаймовий аналіз + бажання/страх).
# Ключ ІІ вбудовано напряму (безкоштовний Groq) — можна перевизначити змінною середовища GROQ_API_KEY.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")   # перевірено цим ключем: працює, валідує JSON
# ФІЛЬТР №3: аналіз РЕАЛЬНОГО скріншота графіка Binance (vision-модель).
# Цей ключ не має доступу до vision-моделей Groq — тому Фільтр №3 автоматично
# використовує текстовий фолбек (GROQ_MODEL) + локальний розрахунок ETA.
# Якщо колись додиш новий ключ з vision — просто вкажи GROQ_VISION_MODEL середовищем.
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "")
