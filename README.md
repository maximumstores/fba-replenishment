# FBA Replenishment — что пополнять и когда (сейчас рынок US)

Считает по каждому ASIN (и магазину), когда закончится сток на FBA, каким каналом (AWD / склад / воздух) успеваем пополнить и сколько штук нужно. Показывает это в дашборде (Streamlit) и шлёт алерт в Telegram только про новые и ухудшившиеся позиции.

## Статус (важно прочитать)

- Расчёт, дашборд и алерт написаны, 48 тестов проходят.
- Запускалось только на синтетических данных (`demo/`) и на настоящей шапке листа Hopted US V2. На живых Hopted / BigQuery / Telegram не запускалось.
- Сроки доставки (AWD 7, склад 14, воздух 10 дней), приёмка и «в пути» — **предположения**. Замените на настоящие в `.env` или в боковой панели дашборда.
- Данные Orders-Stock (партии, остатки на AWD/складе) подключаются необязательно (`SOURCES_CSV`, `BATCHES_CSV`). Без них канал выбирается только по срокам.

## Что нужно из данных

| Источник | Что берём | Как |
|---|---|---|
| Hopted, лист «US V2», колонки A:T | остатки FBA, inbound, продажи 7/30 дней, магазин | `sheet:<ID>` (сервисный аккаунт как читатель) или CSV |
| Справочник (`dim_product` + план месяца) | группа, цвет, размер, active_us, план | `bash export_reference.sh` в Cloud Shell → `reference.csv`, или `REFERENCE_SOURCE=bq` |
| Остатки AWD / склад (необязательно) | `asin, awd_qty, wrh_qty` | `sources.csv` |
| Партии в пути (необязательно) | `asin, qty, eta, destination` (берутся только AMZ) | `batches.csv` |

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env        # заполнить
python make_demo_data.py    # демо-данные в demo/ (по умолчанию используются они)
streamlit run app.py        # дашборд
python alert.py --dry-run   # показать алерт без отправки
python alert.py             # отправить в Telegram (нужны TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
pytest                      # тесты
```

Параметры алерта: `--min-status URGENT|CRITICAL|OUT|...`, `--top N`, `--min-lost N` (порог потерянных штук за 30 дней, по умолчанию 5), `--always` (слать всё, не только новое).
Состояние алертов хранится в `state/alert_state.json`; сохраняется только после успешной отправки.

Cron, раз в день в 9:00:
```
0 9 * * * cd /path/to/fba-replenishment-us && /usr/bin/python3 alert.py >> alert.log 2>&1
```

## Как считается

1. Скорость продаж = 0.5·(7 дн / 7) + 0.5·(30 дн / 30). Режим `PLAN_MODE=fallback` для ASIN без продаж берёт план (ловит ASIN, которые уже давно в ауте, но шумит: попадают и неактивные); по умолчанию `off`. План задан на ASIN, поэтому в режимах fallback/floor он отдаётся одной строке магазина, а не каждой.
2. Симуляция стока на 90 дней: остаток + поставки по датам прихода (ETA партии + задержка 14 дн.; без ETA — по статусу inbound: receiving / shipped / working).
3. День обнуления сравнивается со сроками каналов. Выбирается самый дешёвый из успевающих и доступных: AWD → склад → воздух.
4. Количество = скорость·(срок канала + целевой запас 45 дн.) − (остаток + inbound), но не меньше «моста» до следующего прихода.
5. Статусы: OUT (уже ноль), CRITICAL, URGENT, ACTION, PLAN, OK, NO_SALES. ABC считается по продажам за 30 дней.

## Известные ограничения

- Если ASIN продаётся в нескольких магазинах, партии назначаются магазину с наибольшими продажами (допущение).
- В Hopted нет AWD-стока и дат прихода; «Days of supply» не используется (ограничен 366).
- В Hopted 590 из 5054 строк active_us с внутренними кодами вместо ASIN; они помечены в «Качество данных» и в расчёт не идут.
- Таблицы планировщика в BigQuery не обновляются с 2 октября: `forecast-refresh` получает 403 на Orders-Stock. Нужно дать сервисному аккаунту `streamlit-planner@reorder-497714.iam.gserviceaccount.com` доступ читателя к Orders-Stock.
- Даты партий в Orders-Stock = максимальная дата приёмки; задержки в них не отражаются.
- Отправка в Telegram и чтение из BigQuery вживую не проверялись.

## Файлы

`config.py` настройки · `calc.py` расчёт · `loaders.py` загрузка · `alert.py` Telegram · `app.py` дашборд · `make_demo_data.py` демо · `export_reference.sh` выгрузка справочника · `tests/` тесты


## Каналы и сроки (обновлено 08.10)

- AWD → FBA 7 дн. и склад → FBA 14 дн. — допущения. Море 30 дн. — факт: по 216 доставленным отправкам US (Logistics Dashboard) медиана ETD → доставка около 30 дн. (FAST SEA 32, Matson 26, MAX/EXX 24, Regular 39); реальная доставка позже ETA в среднем на 11–16 дн. Воздух 10 дн. — допущение: воздушных отправок в истории нет.
- Море и воздух считаются доступными, только если по ASIN есть заказ в производстве (`order_qty`, колонка Order US листа остатков); если заказа и остатков в США нет, канал пустой.


## Источники и запуск без ручных выгрузок (обновлено 08.10)

Всё читается из BigQuery (набор `mt` обновляется ночью около 02:00) и из листа calc_shipments:

| Что | Откуда | Переменная |
|---|---|---|
| Остатки и продажи FBA | `mt.hopted_us_native` | `HOPTED_SOURCE=bq` |
| Группа, цвет, размер, active_us, план | `forecast.dim_product` + `forecast.psi_projection` | `REFERENCE_SOURCE=bq` |
| Остатки AWD и склада, заказы в производстве | `mt.amazon_starting_balance_native` (колонки ищутся по заголовкам «AWD US», «WRH US+ Inbound», «Order US») | `SOURCES_SOURCE=bq` |
| Приходы по месяцам (необязательно, по умолчанию выключены) | `forecast.psi_projection.incoming` | `INCOMING_SOURCE=bq`, `USE_PLANNER_INCOMING` |
| Сроки доставки по способам | Logistics Dashboard, вкладка `calc_shipments` | `SHIPMENTS_SOURCE=sheet:<ID>` |

Вкладка «Источники» в дашборде показывает, откуда что взято, на какую дату, какие сроки факт и какие допущения и чего не хватает.

Запуск в Cloud Shell (там уже есть доступ к BigQuery и листам):

```
unzip fba-replenishment-us.zip && cd fba-replenishment-us
pip install -q -r requirements.txt
cp .env.example .env
streamlit run app.py --server.port 8080 --server.headless true
```

Затем в Cloud Shell: Web Preview → Preview on port 8080.


## Деплой: GitHub → Streamlit Cloud + алерт (обновлено 08.10)

1. Сервисный аккаунт **только на чтение** (например `fba-dashboard-ro`): BigQuery Data Viewer + BigQuery Job User на проекте `reorder-497714`, доступ читателя к листу Logistics Dashboard. Ключ json — в Secrets, не в репозиторий.
2. Streamlit Cloud: New app → репозиторий, ветка `main`, файл `app.py` → Advanced settings → Secrets: содержимое `.streamlit/secrets.toml.example` с реальными значениями. Доступ к приложению — только по приглашению (email).
3. Алерт: GitHub → Settings → Secrets and variables → Actions: secrets `GOOGLE_SERVICE_ACCOUNT_JSON` (весь json), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; variable `DASHBOARD_URL`. Запуск `.github/workflows/alert.yml` ежедневно, состояние алертов коммитится в `state/alert_state.json`.


## Использование (Scorecard) — стандарт как в BSR Radar и Kabinet

Работает, когда включён вход через Google (раздел `[auth]` в Secrets). Пишем три журнала в BigQuery, датасет `fba_replenishment`:
`login_log` (вход), `page_views` (какой раздел открыл), `edit_log` (изменил допущения в боковой панели). Время хранится в UTC, показывается киевским.
Вкладка «Использование» видна только `ADMIN_EMAILS` (по умолчанию v.tereshyn@ и s.yaremenko@maximumstores.online). Регулярность = среднее по сотрудникам доля рабочих дней периода с входом; сотрудники = все, кто когда-либо заходил. Запись идёт в фоне и не ломает дашборд.

Один раз создать датасет и дать сервисному аккаунту запись только в него (в Cloud Shell):

```bash
bq --location=EU mk -d --description "FBA replenishment: журналы использования" reorder-497714:fba_replenishment
bq query --use_legacy_sql=false --location=EU 'GRANT `roles/bigquery.dataEditor` ON SCHEMA `reorder-497714.fba_replenishment` TO "serviceAccount:fba-dashboard-ro@reorder-497714.iam.gserviceaccount.com"'
```

Выключить запись: `USAGE_LOG=off`. Другой датасет: `USAGE_DATASET`.
