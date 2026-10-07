"""Листание выдачи раздела по схеме адресов сайта.

У каждого сайта своя схема (config/sites.yaml, свои правки — в data/sites.yaml):
  Петрович  — 1-я страница: адрес как есть; N-я: тот же адрес + p=N-1 (sort и фильтры сохраняются);
  Лемана ПРО — N-я: ?page=N&shiftIds=<токен с 1-й страницы>; без shiftIds сайт отдаёт те же товары.

Схема адресов надёжнее ссылок «Дальше»: у Петровича это <button> без адреса. Переход по ссылкам и кнопка
«Показать ещё» остаются запасным путём (visitor.py), если схема не сработала.

Здесь только адреса, разбор настроек и журнал листания; открывает страницы visitor.Visitor.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import config, log, sites

CONFIG_PATH = os.path.join(config.ROOT, "config", "sites.yaml")
USER_PATH = os.path.join(config.DATA, "sites.yaml")

# если config/sites.yaml потерялся или испорчен — эти же значения
DEFAULTS: dict[str, dict[str, Any]] = {
    "petrovich": {
        "base_pattern": "https://moscow.petrovich.ru/catalog/{id}/",
        "page_param": "p",
        "page_offset": -1,
        "first_page_has_param": False,
        "preserve_query_params": True,
    },
    "lemanapro": {
        "base_pattern": "https://lemanapro.ru/catalogue/{slug}/",
        "page_param": "page",
        "page_offset": 0,
        "first_page_has_param": False,
        "preserve_query_params": True,
        "requires_shift_ids": True,
    },
}

# причины остановки (в журнал — stop_reason)
LAST_PAGE = "last_page"
NO_NEW_ITEMS = "no_new_items"
EMPTY_PAGE = "empty_page"
PAGE_LIMIT = "page_limit"
FETCH_ERROR = "fetch_error"
MISSING_SHIFT_IDS = "missing_shiftIds"
ROBOTS = "robots"


# ---------------------------------------------------------------- настройки


def _scalar(text: str) -> Any:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    return t


def parse_yaml(text: str) -> dict[str, dict[str, Any]]:
    """Простой YAML из двух уровней («сайт:» и «ключ: значение» с отступом) — без сторонних библиотек."""
    out: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"строка {no}: нет двоеточия — {raw.strip()!r}")
        key, value = line.split(":", 1)
        if not line[0].isspace():
            if value.strip():
                raise ValueError(f"строка {no}: у сайта «{key}» не должно быть значения в той же строке")
            current = out.setdefault(key.strip(), {})
        elif current is None:
            raise ValueError(f"строка {no}: ключ с отступом до названия сайта")
        else:
            current[key.strip()] = _scalar(value)
    return out


def load() -> dict[str, dict[str, Any]]:
    """Схемы сайтов: встроенные ← config/sites.yaml ← data/sites.yaml. Ошибка в файле — в журнал, файл пропускается."""
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    for path in (CONFIG_PATH, USER_PATH):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = parse_yaml(f.read())
        except (OSError, ValueError) as e:
            log.warning("pager.config", f"{path} не прочитан: {e} — схема адресов из встроенных настроек", path=path)
            continue
        for site, conf in data.items():
            out.setdefault(site, {}).update(conf)
    return out


def _path_regex(pattern: str) -> re.Pattern[str]:
    path = urlparse(pattern).path or "/"
    parts = re.split(r"(\{[^}]+\})", path)
    rx = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + rx + "$")


# ---------------------------------------------------------------- схема адресов


@dataclass
class SitePagination:
    domain: str
    base_url: str
    page_param: str = "page"
    page_offset: int = 0
    first_page_has_param: bool = False
    requires_shift_ids: bool = False
    preserve_query_params: bool = True
    extra_params: dict[str, str | None] = field(default_factory=dict)

    def __post_init__(self):
        # номер страницы и shiftIds в исходном адресе — от прошлой выдачи: 1-я страница всегда без них
        drop = {self.page_param, *self.extra_params}
        parsed = urlparse(self.base_url)
        qs = {k: v for k, v in parse_qs(parsed.query, keep_blank_values=True).items() if k not in drop}
        self.base_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True), fragment=""))

    def build_url(self, page_num: int) -> str:
        if page_num < 1:
            raise ValueError("номер страницы начинается с 1")
        if page_num == 1 and not self.first_page_has_param:
            return self.base_url
        parsed = urlparse(self.base_url)
        qs = parse_qs(parsed.query, keep_blank_values=True) if self.preserve_query_params else {}
        qs[self.page_param] = [str(page_num + self.page_offset)]
        for k, v in self.extra_params.items():
            if v is None:
                raise ValueError(f"нет {k} — без него сайт отдаёт ту же страницу")
            qs[k] = [v]
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

    def ready_for(self, page_num: int) -> bool:
        """Можно ли строить адрес страницы: для 2-й и дальше нужны все динамические параметры (shiftIds)."""
        return page_num == 1 or all(v is not None for v in self.extra_params.values())

    def landed(self, final_url: str, page_num: int) -> bool:
        """Сайт открыл именно эту страницу, а не перенаправил (на 1-ю, на другой раздел)."""
        if page_num == 1:
            return True
        got = parse_qs(urlparse(final_url or "").query).get(self.page_param, [""])[0]
        same_path = urlparse(final_url or "").path.rstrip("/") == urlparse(self.base_url).path.rstrip("/")
        return same_path and got == str(page_num + self.page_offset)


def for_url(site: str, url: str, schemes: dict[str, dict[str, Any]] | None = None) -> SitePagination | None:
    """Схема для адреса раздела или None (поиск, карточка, сайт без схемы — листаем по ссылкам)."""
    conf = (schemes if schemes is not None else load()).get(site)
    if not conf or not conf.get("page_param") or sites.site_of(url) != site:
        return None
    if not _path_regex(str(conf.get("base_pattern") or "")).match(urlparse(url).path or "/"):
        return None
    return SitePagination(
        domain=urlparse(sites.SITES[site]["home"]).hostname or site,
        base_url=url,
        page_param=str(conf["page_param"]),
        page_offset=int(conf.get("page_offset") or 0),
        first_page_has_param=bool(conf.get("first_page_has_param")),
        requires_shift_ids=bool(conf.get("requires_shift_ids")),
        preserve_query_params=conf.get("preserve_query_params") is not False,
        extra_params={"shiftIds": None} if conf.get("requires_shift_ids") else {},
    )


# ---------------------------------------------------------------- журнал листания


def signature(items: list[dict[str, Any]]) -> str:
    """md5 названий первых 5 товаров: одинаковая подпись — сайт отдал ту же страницу."""
    names = [str(i.get("name") or i.get("url") or "") for i in items[:5]]
    return hashlib.md5("\n".join(names).encode("utf-8")).hexdigest()


class SectionLog:
    """Строки журнала по разделу: одна на страницу и одна при остановке."""

    def __init__(self, category: str, domain: str):
        self.category = category
        self.domain = domain
        self.total_found = 0
        self.last_page = 0
        self.stop_reason = ""

    def page(self, page: int, found: int, method: str, total: int | None = None, status: str = "ok") -> None:
        self.total_found += found
        self.last_page = page
        of = f"/{total}" if total and total > 1 else ""
        log.info(
            "page.section",
            f"Категория «{self.category}», страница {page}{of}, найдено {found} товаров (всего {self.total_found})",
            category=self.category,
            site=self.domain,
            page=page,
            pages_total=total if total and total > 1 else None,
            found=found,
            total_found=self.total_found,
            method=method,
            status=status,
        )

    def stop(self, reason: str, detail: str = "") -> None:
        if self.stop_reason:
            return
        self.stop_reason = reason
        log.info(
            "page.section_stop",
            f"Категория «{self.category}»: листание остановлено ({reason}) на странице {self.last_page}"
            + (f" — {detail}" if detail else ""),
            category=self.category,
            site=self.domain,
            stop_reason=reason,
            last_page=self.last_page,
            total_found=self.total_found,
            detail=detail or None,
        )
