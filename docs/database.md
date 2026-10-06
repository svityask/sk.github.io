# База данных

Файл: `data\monitor-2.1.sqlite` (имя осталось от 2.1 — база общая для 2.1–2.3). SQLite в режиме WAL: рядом
лежат `-wal` и `-shm`, удалять их при работающем приложении нельзя.

## Как устроена запись

- Соединение работает в режиме autocommit; всё, что должно быть атомарным, — внутри `db.transaction(con)`
  (`BEGIN IMMEDIATE … COMMIT`, при ошибке `ROLLBACK`; вложенные блоки — `SAVEPOINT`).
- Цены одной сети за сбор пишутся **одной транзакцией** (`collect.Collector._write`): либо все, либо ничего.
  Если база занята, запись повторяется до 3 раз с паузой 2 → 8 с — повтор безопасен, потому что откат полный.
- **Идемпотентность.** В `history` есть `run_id` и уникальный индекс `(key, run_id)`: повторная запись того же
  сбора не задваивает историю. `market_points`, `crosscheck`, `site_reads`, `metrics` — ключи с `run_id`,
  пишутся через `INSERT OR REPLACE`.
- **Один сбор за раз.** Таблица `locks`: сбор берёт блокировку `collect` и обновляет `heartbeat` каждые 30 с.
  Если блокировка старше 15 минут без сердцебиения — она считается мёртвой (компьютер выключили) и забирается.
- Сборы, оставшиеся в статусе «идёт» после сбоя, при следующем запуске помечаются «прерван».

## Миграции

Номер схемы — `PRAGMA user_version`. Список — `db.MIGRATIONS`; каждая выполняется в своей транзакции и
повышает номер. Текущая версия схемы — **4**.

| № | Что делает |
|---|---|
| 1 | Базовые таблицы 2.1–2.2 (для старых баз — ничего не меняет, всё `IF NOT EXISTS`) |
| 2 | `history.run_id` + уникальный индекс `history_run` |
| 3 | `metrics`, `breakers`, `locks` |
| 4 | `pack_overrides`, `spot_checks`, `kv` |

Новая миграция: добавить `(4, "…SQL…")` в конец `MIGRATIONS`, тест на апгрейд — в
`tests/test_reliability.py::Migrations`. Миграция не должна ломать базу предыдущей версии: только добавлять.

## Таблицы

### Что отслеживаем и справочники

**tracked** — список «Что отслеживаем».
| Поле | Смысл |
|---|---|
| id | номер записи; по нему товар привязан к группе (`products.group_id`) |
| site | `petrovich` / `lemanapro` |
| kind | `category` — категория фида, `section` — раздел сайта, `product` — карточка товара |
| ref | id категории фида или адрес раздела/товара |
| title, added | название и когда добавлено (секунды Unix) |

**categories** — дерево категорий фида (перезаписывается при каждом чтении): `site, id, parent, name, count`.

**kind_overrides** — вид товара, заданный вручную: `key → kind` («Штукатурка цементная»).

**pack_overrides** — фасовка, заданная вручную: `key → qty, unit` (кг или л). Применяется при каждой записи
замера (`db.record`) и сразу пишется в `products`, поэтому цена за единицу пересчитывается без нового сбора.

**decisions** — решения по «На проверку»: `kind = exclude` (не конкурент) или `accept:<причина>` (проверено).

### Цены

**products** — последнее состояние товара.
| Поле | Смысл |
|---|---|
| key | `сеть:артикул` — один и тот же для фида и сайта |
| code, name, url, vendor | артикул, название, адрес карточки, производитель |
| category_id, group_id | категория фида и запись `tracked`, через которую товар попал в сбор |
| pack_qty, pack_unit | фасовка в кг или л (для цены за единицу) |
| is_ours | 1 — Основит |
| first_seen, last_seen, last_run | первый и последний замер, номер последнего сбора |
| price, old_price, available, source, city | цена на полке, зачёркнутая цена, наличие (1/0/NULL), `feed`/`edge`, город |
| prev_price, prev_source | цена и источник до последнего изменения |

**history** — изменения: новая строка только когда изменились цена, старая цена или наличие.
`key, ts, price, old_price, available, source, city, run_id`.

**market_points** — Основит против аналогов в каждом сборе (для графиков):
`run_id, ts, key, price, per_unit, median, min, count, level (аналоги / тот же тип / раздел), kind, unit`.

**crosscheck** — сверка фида с полкой: `run_id, site, key, feed_price, site_price, ts`.

**site_reads** — удачные полные чтения фида: от них считается «появился / пропал из фида».

### Сборы и надёжность

**runs** — сборы: `id, started, finished, status (идёт / готово / без цен / ошибка / прерван), summary (JSON)`.
`summary` — всё, что показывает Главная: предупреждения, изменения, ассортимент, «На проверку», метрики.

**metrics** — по источнику (сеть × `feed`/`edge`) в каждом сборе:
| Поле | Смысл |
|---|---|
| seconds | время работы источника |
| requests | скачиваний фида / открытых страниц, включая повторы |
| pages | страниц сайта |
| errors, retries | сбоев и повторов |
| http_429, captcha | ответов 429 и проверок браузера / отказов |
| empty | пустых замеров: страница без товаров, карточка без цены, фид без нужных товаров |
| prices | цен получено |
| outcome | `ok` / `partial` / `blocked` / `failed` / `skipped` |

**breakers** — предохранитель источника: `state (closed / open / half), failures, opens, opened_at, until, reason`.
Правила — в `monitor/guard.py` и [troubleshooting.md](troubleshooting.md#источник-на-паузе).

**locks** — блокировка «один сбор за раз»: `name, owner, pid, started, heartbeat`.

### Выборочная проверка

**spot_checks** — отметки человека: `key, ts, verdict (ok / price / pack / kind / not_competitor), note` и что
приложение показывало в момент проверки (`price, pack, kind`). Точность — по последней отметке каждого товара за
90 дней (`spot.stats`).

**kv** — мелкие значения в JSON; `spot_sample` — текущая выборка `{keys, ts}`.

## Полезные запросы

```sql
-- история цены товара
SELECT datetime(ts, 'unixepoch', 'localtime') AS когда, price, old_price, available, source
FROM history WHERE key = 'lemanapro:82065432' ORDER BY ts;

-- последние сборы и их итог по источникам
SELECT r.id, r.status, m.site, m.source, m.seconds, m.pages, m.errors, m.http_429, m.captcha, m.outcome
FROM runs r LEFT JOIN metrics m ON m.run_id = r.id ORDER BY r.id DESC LIMIT 20;

-- источники на паузе
SELECT site, source, datetime(until, 'unixepoch', 'localtime') AS до, reason FROM breakers WHERE state = 'open';
```

Открыть базу можно в [DB Browser for SQLite](https://sqlitebrowser.org/) — при закрытом приложении.

## Резервные копии

`data\backups\monitor-ГГГГММДД-ЧЧММСС.zip` — снимок базы (через SQLite backup API, согласованный даже во время
работы) и файл настроек. Делается после удачного сбора не чаще раза в 20 часов и кнопкой на вкладке «Состояние».
Перед копией — `PRAGMA quick_check`: испорченная база не копируется, чтобы не вытеснить хорошие копии.
Хранятся 10 последних и по одной за каждую из 8 последних недель.

Восстановление — [troubleshooting.md](troubleshooting.md#восстановить-базу-из-копии).
