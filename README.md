# HTX New Earn Alarm Bot

Telegram-бот для автоматического мониторинга раздела **HTX Earn → New Listings**.  
Отслеживает появление, исчезновение и изменение условий **Fixed**-продуктов и мгновенно уведомляет администратора.

---

## Как работает

1. По расписанию (каждые N минут, по умолчанию 60) запускается браузер Chromium через **Playwright**.
2. Открывается страница [HTX Earn New](https://www.htx.com/en-us/financial/earn/home?activeTab=new).
3. Бот дважды нажимает кнопку **View More**, каждый раз ожидая реального увеличения высоты страницы (проверка каждые 10 секунд, до 20 попыток на каждый клик).
4. Если страница не расширяется за 20 попыток — в Telegram приходит сообщение об ошибке.
5. После двух успешных кликов делается **полный скриншот** страницы.
6. Из DOM извлекается таблица токенов — только строки с типом **Fixed** (строки `Flexible/Fixed` игнорируются).
7. Данные сохраняются в **SQLite** с меткой времени цикла.
8. Новые данные сравниваются с предыдущим циклом. При наличии изменений в Telegram приходит алерт.

---

## Структура проекта

```
HTX_NEW_EARN_ALARM/
├── bot.py                  # Telegram-бот, планировщик, обработчики
├── scraper.py              # Playwright: открытие страницы, клики View More, DOM-парсинг
├── db.py                   # SQLite (aiosqlite): циклы, строки таблицы, настройки
├── settings.env            # Конфигурация (токен, admin ID, интервал)
├── requirements.txt        # Python-зависимости
├── htx-earn-alarm.service  # systemd unit
└── htx-earn.db             # База данных (создаётся автоматически)
```

---

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/Bornitoo/HTX_NEW_EARN_ALARM.git
cd HTX_NEW_EARN_ALARM
```

### 2. Установить зависимости

```bash
pip install -r requirements.txt --break-system-packages
python3 -m playwright install chromium --with-deps
```

### 3. Настроить `settings.env`

```env
BOT_TOKEN=<токен от @BotFather>
ADMIN_USER_ID=<ваш Telegram user ID>
INTERVAL_MINUTES=60
```

### 4. Запустить как systemd-сервис

```bash
cp htx-earn-alarm.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable htx-earn-alarm
systemctl start htx-earn-alarm
```

### Проверить логи

```bash
journalctl -u htx-earn-alarm -n 50 --no-pager
# или
tail -f /root/HTX_NEW_EARN_ALARM/bot.log
```

---

## Кнопки управления

| Кнопка | Действие |
|--------|----------|
| 📸 **Скриншот** | Немедленно запустить полный цикл. Прислать скриншот + таблицу Fixed-токенов. Сбросить таймер (следующий авто-цикл через N минут от момента нажатия) |
| ⏱ **Время обновления** | Изменить интервал проверки. Бот спрашивает число минут (10–1440). Следующий цикл планируется через новый интервал |
| ❓ **Help** | Показать справку прямо в боте |

---

## Формат алерта об изменениях

```
⚠️ HTX Earn — изменения:

🟢 НОВОЕ:      USDT  12.5%  Fixed  30d
🔴 ПРОПАЛО:    BTC    8.0%  Fixed   7d
🔄 ИЗМЕНИЛОСЬ: ETH  6.0%→7.5%  Fixed  14d
```

---

## Формат таблицы (кнопка Скриншот)

```
HTX Earn — Fixed:

Токен        APY       Срок
--------------------------------
CHECK      200.00%    Fixed
WARD       150.00%    Fixed
WAR        100.00%    Fixed
```

---

## Команды Telegram

| Команда | Описание |
|---------|----------|
| `/start` | Статус бота, текущий интервал, время до следующего цикла |
| `/help` | Полная справка |
| `/cancel` | Отменить ввод интервала |

---

## База данных

SQLite-файл `htx-earn.db`, таблицы:

- **`earn_cycles`** — каждый запуск парсинга (`id`, `scraped_at`)
- **`earn_rows`** — строки Fixed-токенов (`cycle_id`, `token`, `apy`, `term`)
- **`settings`** — `interval_minutes`, `next_run_at`

Diff строится по паре `(token, term)` — сравнивается APY. Новая строка → `🟢`, пропала → `🔴`, изменился APY → `🔄`.

---

## Технологии

- Python 3.12
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21
- [Playwright](https://playwright.dev/python/) (Chromium, headless)
- [aiosqlite](https://github.com/omnilib/aiosqlite)
- SQLite

---

## Важные замечания

- Бот работает только для одного администратора (задаётся через `ADMIN_USER_ID`).
- При перезапуске сервиса расписание сохраняется из БД — таймер не сбрасывается.
- Если Playwright не может найти кнопку View More за 20 попыток — приходит ошибка в Telegram.
- Скриншот страницы отправляется как фото (до 10 МБ) или как файл (если больше).
