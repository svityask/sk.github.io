# Образцы для тестов

- `lemanapro_admitad.yml`, `petrovich_admitad.csv`, `google_merchant.xml` — синтетические фиды в форматах
  Admitad/«Где Слон?» (YML), CSV и Google Merchant. Цены и товары вымышленные.
- `fake-site-cert.pem`, `fake-site-key.pem` — самоподписанный сертификат **только для тестов**: поддельный сайт
  (`tests/fakesite.py`) отвечает по https на 127.0.0.1, а браузер запускается с `--ignore-certificate-errors`.
  Ключ ничего не защищает, его можно публиковать.

Когда окно сбора не смогло разобрать живую страницу, её HTML лежит в `data/failures/…/page.html.gz` или
`data/samples`. Такой файл (без личных данных) кладётся сюда, а в `tests/test_monitor.py` добавляется тест на
`extract.products_from_page` — так поломка разбора ловится в CI в тот же день.
