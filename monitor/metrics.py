"""Счётчики одного источника в одном сборе: время, страницы, ошибки, 429, капчи, пустые замеры."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class Meter:
    site: str
    source: str  # feed / edge
    started: float = field(default_factory=time.time)
    seconds: float = 0.0
    requests: int = 0  # скачиваний фида / открытых страниц, включая повторы
    pages: int = 0  # страниц сайта
    errors: int = 0
    retries: int = 0
    http_429: int = 0
    captcha: int = 0  # проверка браузера, капча, отказ в доступе
    empty: int = 0  # страница/карточка без цены, пустой фид
    prices: int = 0
    outcome: str = "ok"  # ok / partial / blocked / failed / skipped

    def finish(self, outcome: str | None = None) -> Meter:
        self.seconds = round(time.time() - self.started, 1)
        if outcome:
            self.outcome = outcome
        return self

    def row(self) -> dict:
        return asdict(self)
