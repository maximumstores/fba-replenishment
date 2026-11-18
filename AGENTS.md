# FBA Restock Report — памятка для агента

Ежедневный отчёт: какие ASIN на FBA в ауте и что пополнять на 2 месяца, по рынкам US / CA / DE / UK.
Основной результат — Google-таблица. Плюс сообщение в Telegram. Приложение Streamlit — вторичное, только US, логика старая (см. «Приложение»).

## Где что

| Что | Где |
|---|---|
| Репо | `maximumstores/fba-replenishment` (публичный, ветка `main`) |
| Таблица-отчёт | Google Sheet `1IHJHoZMHKc8AePrd_k74DzP3yW1A_xHQuKOMu1nL_mQ` (Operations shared drive) |
| Telegram-бот | @maximum_fba_alert_bot, подписчики в `state/subscribers.json` |
| BigQuery | проект `reorder-497714`, EU |
| Сервисный аккаунт | `fba-dashboard-ro@reorder-497714.iam.gserviceaccount.com` (читает файлы Нины, пишет в отчёт) |

Секреты GitHub: `GOOGLE_SERVICE_ACCOUNT_JSON`, `TELEGRAM_BOT_TOKEN`. Переменные: `RESTOCK_REPORT_SHEET`, `DASHBOARD_URL`.

## Источники данных

| Данные | Откуда | Код |
|---|---|---|
| Остатки FBA (Available, Inbound) | BQ `mt.hopted_{us,ca,de,uk}_native` (обновляются ночью ~03:15–04:00 UTC) | `market_stock.py` |
| Справочник (группа, цвет, размер, A/B, Category+parent, old_asin, active_*) | BQ `forecast.dim_product` + `forecast.group_cogs` | `market_stock.DIM_SQL` |
| План US | вкладка `Orders` файла Нины Stock Amazon Merino `1acpHlfBiJjFm96VmFksRmKTnktULTgmu7iESYFDLUZw`, колонки `Plan M'YY` текущего и следующего месяца | `nina_sheet.load_us_plan` |
| План + цена CA | вкладка `Orders` файла `1FRP8eWrJRNraSeua1kJn_rki9H4XshUdiKRMw9w8QnA`; остатки CA — из Hopted | `nina_sheet.load_ca_plan` |
| DE / UK целиком (сток, план, цена) | вкладки `Order DE` / `Orders UK` файла `1XKUoiKUa8aF0DT7pNgXRqEvlejVu_FYVpMRPyGcOlLI`; цена UK = цена DE | `nina_sheet.load_nina` |
| AWD, склад 3PL + inbound, «В заказе» | BQ `mt.amazon_starting_balance_native` (копия Orders-Stock) | `market_stock.SOURCES_SQL / SOURCE_COLS` |
| Было ли хоть раз продано | BQ `forecast.fact_sales_monthly`, `forecast.summer_src` | `market_stock.SOLD_SQL` |

Деньги: US — себестоимость группы ($), CA/DE — цена из таблиц Нины, UK — цена DE. Подпись основы в заголовке колонки «Потери».

## Логика (`restock_report.py`)

- Старый ASIN складывается с новым (перелистинг, `old_asin`).
- Правило Нины (19.10): ASIN без единой продажи в отчёт не попадает. Срезает много: US 1464 → 414 аутов (12.11).
- Категория = Category+parent из справочника.
- **Аут сейчас** = Available ≤ 0. **Рест на 2 мес** = Demand 2 мес − (Available + Inbound) > 0.
- «Срочно слать» делится: «Можно слать сейчас» = min(нужно, AWD + 3PL) → «Ждёт заказа» = min(остаток, В заказе) → «Нечем пополнить» = остаток.
- Сортировка по «Потери» = нехватка × цена.
- Все числовые колонки приводятся к числу (`coerce_numeric`): источники иногда отдают числа текстом («30,16», «2 204»).
- Если у рынка план пустой (сумма 0) — вкладки рынка НЕ перезаписываются, в Telegram пишется, что план пустой.

## Вкладки таблицы

`{US,CA,DE,UK} · Аут сейчас`, `· Рест на 2 мес`, то же `(группы)`; `Индекс ASIN` + `Поиск ASIN` (ввести ASIN → где у него сток не 0 по всем вкладкам файлов Нины, `asin_index.py`).

## Расписание и запуск

- Основной запуск: запланированная задача Claude «Утренний запуск отчёта FBA Restock», будни 06:17 Киев → `workflow_dispatch` для `restock.yml` и `asin_index.yml`.
- Запасной: cron GitHub 04:20 / 04:40 UTC. GitHub опаздывает на 5–10 ч; если сегодня уже был запуск (`state/last_run.txt`), запасной пропускается — второго сообщения не будет.
- Вручную: `gh api -X POST repos/maximumstores/fba-replenishment/actions/workflows/restock.yml/dispatches -f ref=main` (тихо, без Telegram: добавить `-f 'inputs[silent]=true'`).
- `gh workflow run` не работает (GraphQL) — только REST как выше.
- Логи: `gh run view <id> --log-failed`.

## Состояние (`state/`, коммитит бот — перед push всегда `git pull --rebase`)

- `restock_state.json` — списки ASIN по рынкам (`US:now`, `US:plan`…), с ними сравнивается следующий запуск.
- `last_run.txt` — время последнего удачного запуска.
- `verify_report.json` — результат полной сверки (`verify_report.yml`): состав строк, каждая цифра против источников, арифметика. `ok: true` = всё сошлось.
- `asin_index.json`, `*_probe.json` — индекс и разовые проверки.

## Проверка после изменений

1. `pytest` (106 тестов).
2. Запуск `restock.yml` с `silent=true`, затем `verify_report.yml` → `ok: true`.
3. Сверить 3–5 ASIN руками с таблицами Нины до того, как что-то показывать коллегам.

## Приложение (fba-replenishment.streamlit.app)

`app.py`, `calc.py`, `loaders.py`, вход Google только `@maximumstores.online`. Только US, считает по скорости продаж и строкам ASIN × магазин — цифры НЕ совпадают с таблицей (12.11: 204 аута против 414). Коллегам не давать, пока не переведено на логику таблицы. `README.md` описывает именно приложение и устарел.

## Открыто

- Решение Серёжи по деньгам (US по себестоимости, UK по цене DE) и нужна ли колонка «По воздуху» (для неё нужны даты прихода партий — их нет).
- Фидбек Нины; где держать «Поиск ASIN».
- 18.11 в `Orders UK` у Нины все колонки плана (2026-10/11/12, PLAN) нули → UK-вкладки отчёта пустые.
