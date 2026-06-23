#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
keeper.py — ядро "сторожа от отката веса".

Здесь нет сети. Только чистая логика:
  * чтение/запись формата приложения Monitor Your Weight (байт-совместимо);
  * EMA по реальному времени (пропуски не врут);
  * храповик пола: вниз — сам, автоматически; вверх — только руками;
  * расчёт зоны (зелёная / жёлтая / красная);
  * грейс-период после паузы (отпуск без весов).

Принцип: всё, что бот "думает", он умеет объяснить числами.
Любую зону можно пересчитать на бумаге из state.json + config.json.
"""

import json
import math
import os
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional


# ───────────────────────── формат Monitor Your Weight ─────────────────────────
#
# Файл бэкапа выглядит так (десятичная запятая, русские сокращения месяцев):
#
#   Начальный вес: 82,7; Начальная дата: дек. 18, 2019;
#   Целевой вес: 64,3; Целевая дата: апр. 08, 2022
#
#   82,7; дек. 18, 2019 05:46;
#   82,2; дек. 19, 2019 05:28;
#   82,7; дек. 18, 2019 07:48; Первый день диеты / упражнений
#
# Внимание: май записан как "мая" (без точки) — это не опечатка приложения,
# это родительный падеж в его локали. Воспроизводим как есть, иначе импорт
# обратно в приложение может сломаться.

MONTHS_RU = {
    1: "янв.", 2: "февр.", 3: "мар.", 4: "апр.", 5: "мая", 6: "июн.",
    7: "июл.", 8: "авг.", 9: "сент.", 10: "окт.", 11: "нояб.", 12: "дек.",
}
MONTHS_RU_REV = {v: k for k, v in MONTHS_RU.items()}


@dataclass
class Entry:
    """Одна строка взвешивания."""
    weight: float
    when: dt.datetime
    note: str = ""


def parse_weight(token: str) -> float:
    """'82,7' -> 82.7   (и '82.7' тоже примем — вдруг руками)."""
    return float(token.strip().replace(",", "."))


def fmt_weight(w: float) -> str:
    """82.7 -> '82,7'   (одна десятичная, запятая — как в приложении)."""
    return f"{w:.1f}".replace(".", ",")


def parse_datetime(token: str) -> dt.datetime:
    """'дек. 18, 2019 05:46' -> datetime."""
    token = token.strip()
    # 'дек. 18, 2019 05:46'  ->  ['дек.', '18,', '2019', '05:46']
    parts = token.split()
    mon = MONTHS_RU_REV[parts[0]]
    day = int(parts[1].rstrip(","))
    year = int(parts[2])
    hh, mm = parts[3].split(":")
    return dt.datetime(year, mon, day, int(hh), int(mm))


def fmt_datetime(d: dt.datetime) -> str:
    """datetime -> 'дек. 18, 2019 05:46'."""
    return f"{MONTHS_RU[d.month]} {d.day:02d}, {d.year} {d.hour:02d}:{d.minute:02d}"


def fmt_line(e: Entry) -> str:
    """
    Entry -> строка файла, байт-в-байт как у приложения.
    Хвост '; ' (точка с запятой + пробел) сохраняем даже без заметки.
    """
    return f"{fmt_weight(e.weight)}; {fmt_datetime(e.when)}; {e.note}"


def parse_line(line: str) -> Optional[Entry]:
    """Строка файла -> Entry. Возвращает None для пустых/служебных строк."""
    raw = line.rstrip("\n")
    if not raw.strip():
        return None
    if raw.startswith("Начальный вес") or raw.startswith("Целевой вес"):
        return None
    # 'вес; дата время; [заметка]'
    fields = raw.split(";")
    if len(fields) < 2:
        return None
    weight = parse_weight(fields[0])
    when = parse_datetime(fields[1])
    note = fields[2].strip() if len(fields) >= 3 else ""
    return Entry(weight=weight, when=when, note=note)


def read_database(path: str) -> tuple[list[str], list[Entry]]:
    """
    Читает файл базы. Возвращает (строки_заголовка, список_взвешиваний).
    Заголовок сохраняем дословно, чтобы при дозаписи ничего не пересобирать.
    """
    header: list[str] = []
    entries: list[Entry] = []
    if not os.path.exists(path):
        return header, entries
    with open(path, "r", encoding="utf-8") as f:
        in_header = True
        for line in f:
            if in_header and (line.startswith("Начальный вес")
                              or line.startswith("Целевой вес")
                              or not line.strip()):
                header.append(line.rstrip("\n"))
                continue
            in_header = False
            e = parse_line(line)
            if e is not None:
                entries.append(e)
    return header, entries


# ───────────────────────────── расчёт EMA / зон ──────────────────────────────


def ema_step(prev_ema: Optional[float], value: float,
             dt_days: float, tau_days: float) -> float:
    """
    Один шаг экспоненциального сглаживания с учётом РЕАЛЬНОГО интервала.

    alpha = 1 - exp(-dt/tau).  Чем больше пропуск — тем сильнее новое
    значение тянет EMA (старое "выдыхается"). Пропуск 10 дней != один день.
    Это и есть аккуратная обработка дыр в данных.
    """
    if prev_ema is None:
        return value
    alpha = 1.0 - math.exp(-dt_days / tau_days)
    return prev_ema + alpha * (value - prev_ema)


def linreg_slope_per_week(points: list[tuple[float, float]]) -> Optional[float]:
    """
    Наклон линейной регрессии (kg в неделю) по точкам (день, EMA).
    day — это порядковый номер дня (float). Нужно >= 4 точек, иначе None.
    """
    n = len(points)
    if n < 4:
        return None
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    slope_per_day = (n * sxy - sx * sy) / denom
    return slope_per_day * 7.0


# ───────────────────────────────── состояние ─────────────────────────────────


@dataclass
class State:
    """
    Состояние сторожа. Лежит в state.json, правится глазами в nano.

    floor              — пол обороны: твой лучший сглаженный вес. Вниз едет сам.
    ema                — текущая сглаженная масса.
    last_ts            — время последнего учтённого взвешивания (ISO).
    last_weigh_date    — дата последнего взвешивания (YYYY-MM-DD).
    last_known_floor   — каким бот ПОМНИТ пол. Если в файле floor стал выше —
                         значит, пол подняли руками: бот это заметит и запишет.
    floor_raise_reason — причина ручного подъёма пола. Это твоё "трение":
                         бот ждёт осмысленный текст. Пусто/коротко -> в журнал
                         попадёт пометка "БЕЗ причины" (стыдит, но не блокирует).
    grace_remaining    — сколько ближайших взвешиваний считать "предварительными"
                         (после паузы). Пока > 0: пол заморожен, красную не даём.
    ema_window         — окно последних (дата, EMA) для наклона тренда.
    last_backup_date   — когда последний раз слали бэкап в ntfy.
    """
    floor: Optional[float] = None
    ema: Optional[float] = None
    last_ts: Optional[str] = None
    last_weigh_date: Optional[str] = None
    last_known_floor: Optional[float] = None
    floor_raise_reason: str = ""
    grace_remaining: int = 0
    ema_window: list = field(default_factory=list)
    last_backup_date: Optional[str] = None
    last_zone: Optional[str] = None  # для гистерезиса: помним прошлую зону

    @staticmethod
    def load(path: str) -> "State":
        # Падаем сразу и громко на кривом JSON — так и договаривались.
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return State(**data)

    def save_atomic(self, path: str) -> None:
        """
        Запись через временный файл + rename. Файл всегда либо старый целый,
        либо новый целый. Обрезанного полу-JSON не бывает даже при сбое питания.
        """
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def validate_state(s: State, cfg: dict) -> None:
    """Проверяем смысл, а не только синтаксис. Кричим по-человечески."""
    lo, hi = cfg["weight_min_plausible"], cfg["weight_max_plausible"]
    for name in ("floor", "ema", "last_known_floor"):
        v = getattr(s, name)
        if v is None:
            continue
        if not isinstance(v, (int, float)):
            raise ValueError(f"state.json: поле '{name}' не число: {v!r}")
        if not (lo <= v <= hi):
            raise ValueError(
                f"state.json: поле '{name}' = {v} вне диапазона {lo}..{hi}. "
                f"Похоже на опечатку (пропущенная точка?). Поправь и перезапусти."
            )


# ───────────────────────────── зоны и решения ────────────────────────────────

GREEN, YELLOW, RED = "green", "yellow", "red"
EMOJI = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴"}
SEVERITY = {GREEN: 0, YELLOW: 1, RED: 2}


@dataclass
class ZoneResult:
    zone: str
    ema: float
    floor: float
    drawdown: float
    slope_per_week: Optional[float]
    ratchet_zone: str
    slope_zone: str
    preliminary: bool


def zone_from_drawdown(drawdown: float, cfg: dict) -> str:
    if drawdown < cfg["yellow_threshold_kg"]:
        return GREEN
    if drawdown < cfg["red_threshold_kg"]:
        return YELLOW
    return RED


def zone_from_slope(slope: Optional[float], cfg: dict) -> str:
    if slope is None:
        return GREEN  # мало данных для тренда — не паникуем
    if slope < cfg["slope_yellow_kg_per_week"]:
        return GREEN
    if slope < cfg["slope_red_kg_per_week"]:
        return YELLOW
    return RED


def zone_from_drawdown_hyst(drawdown: float, cfg: dict, last_zone) -> str:
    """
    Храповик с гистерезисом. Вверх (к тревоге) — сразу. Вниз (к зелёной) —
    только если явно отступил за порог на hysteresis_kg. Иначе зона "залипает"
    и не мигает у границы каждый день.
    """
    h = cfg.get("hysteresis_kg", 0.0)
    y, r = cfg["yellow_threshold_kg"], cfg["red_threshold_kg"]
    # опускаем нижнюю границу зоны, только если мы сейчас в этой (или худшей) зоне
    y_eff = y - h if last_zone in (YELLOW, RED) else y
    r_eff = r - h if last_zone == RED else r
    if drawdown < y_eff:
        return GREEN
    if drawdown < r_eff:
        return YELLOW
    return RED


def compute_zone(s: State, cfg: dict) -> ZoneResult:
    """Зона = худшая из двух (храповик и наклон). В грейсе режем до жёлтой."""
    assert s.ema is not None and s.floor is not None
    drawdown = s.ema - s.floor
    slope = linreg_slope_per_week([(i, v) for i, (_, v) in enumerate(s.ema_window)])

    rz = zone_from_drawdown_hyst(drawdown, cfg, s.last_zone)
    sz = zone_from_slope(slope, cfg)
    zone = rz if SEVERITY[rz] >= SEVERITY[sz] else sz

    preliminary = s.grace_remaining > 0
    if preliminary and SEVERITY[zone] > SEVERITY[YELLOW]:
        zone = YELLOW  # после паузы одно число не имеет права красить в красный

    return ZoneResult(
        zone=zone, ema=s.ema, floor=s.floor, drawdown=drawdown,
        slope_per_week=slope, ratchet_zone=rz, slope_zone=sz,
        preliminary=preliminary,
    )


# ───────────────────────── оркестрация (используется ботом) ───────────────────


def cold_start(entries: list[Entry], cfg: dict) -> tuple[State, list[str]]:
    """
    Первый запуск на чистом хосте: читаем всю историю, считаем EMA до сегодня,
    ставим пол = текущая EMA. С этого момента начинается оборона.
    Возвращает (state, строки_для_журнала).
    """
    entries = sorted(entries, key=lambda e: e.when)
    ema = None
    prev = None
    window: list[tuple[str, float]] = []
    for e in entries:
        dt_days = 0.0 if prev is None else (e.when - prev).total_seconds() / 86400.0
        ema = ema_step(ema, e.weight, dt_days, cfg["tau_days"])
        prev = e.when
        window.append((e.when.date().isoformat(), round(ema, 2)))
    window = window[-cfg["slope_window_days"]:]
    last = entries[-1]
    s = State(
        floor=round(ema, 2), ema=round(ema, 2),
        last_ts=last.when.isoformat(),
        last_weigh_date=last.when.date().isoformat(),
        last_known_floor=round(ema, 2),
        floor_raise_reason="", grace_remaining=0,
        ema_window=window, last_backup_date=None,
    )
    log = [f"холодный старт: прочитано {len(entries)} точек, "
           f"пол инициализирован {ema:.2f} (= EMA на {last.when.date()})"]
    return s, log


def detect_manual_floor_raise(s: State, cfg: dict) -> list[str]:
    """
    Если в state.json пол стал ВЫШE, чем бот помнил — значит, подняли руками.
    Записываем в журнал. Причина есть и осмысленная -> по-человечески.
    Пусто/коротко -> стыдящая пометка (но пол всё равно принимаем: робастность).
    """
    log: list[str] = []
    if s.last_known_floor is None or s.floor is None:
        return log
    eps = 0.001
    if s.floor > s.last_known_floor + eps:
        reason = (s.floor_raise_reason or "").strip()
        min_chars = cfg["floor_raise_reason_min_chars"]
        if len(reason) >= min_chars:
            log.append(f"пол поднят руками {s.last_known_floor:.2f} -> {s.floor:.2f}; "
                       f"причина: {reason}")
        else:
            log.append(f"пол поднят руками {s.last_known_floor:.2f} -> {s.floor:.2f} "
                       f"БЕЗ внятной причины (записано {reason!r})")
        s.last_known_floor = s.floor
        s.floor_raise_reason = ""  # причина одноразовая, гасим
    return log


def process_weight(s: State, weight: float, when: dt.datetime,
                   cfg: dict) -> tuple[ZoneResult, list[str]]:
    """
    Учесть одно взвешивание. Меняет state на месте. Возвращает (зона, журнал).
    Это сердце сторожа — вся арифметика здесь, и она вся объяснимая.
    """
    log: list[str] = []
    today = when.date().isoformat()

    # --- пауза (отпуск)? считаем разрыв по реальному времени ---
    if s.last_weigh_date is not None:
        gap = (when.date() - dt.date.fromisoformat(s.last_weigh_date)).days
        if gap > cfg["gap_days_vacation"]:
            s.grace_remaining = cfg["grace_readings_after_gap"]
            log.append(f"возврат после паузы {gap} дн.: ближайшие "
                       f"{s.grace_remaining} взвешивания — предварительные "
                       f"(пол заморожен, красную не даём)")

    # --- обновляем EMA ---
    prev_ts = dt.datetime.fromisoformat(s.last_ts) if s.last_ts else None
    dt_days = 0.0 if prev_ts is None else (when - prev_ts).total_seconds() / 86400.0
    s.ema = round(ema_step(s.ema, weight, dt_days, cfg["tau_days"]), 2)
    s.last_ts = when.isoformat()
    s.last_weigh_date = today

    # окно для наклона: одна точка на день (перезаписываем, если день повторился)
    s.ema_window = [(d, v) for (d, v) in s.ema_window if d != today]
    s.ema_window.append((today, s.ema))
    s.ema_window = s.ema_window[-cfg["slope_window_days"]:]

    # --- храповик: пол едет вниз. В грейсе — заморожен. ---
    in_grace = s.grace_remaining > 0
    if not in_grace and s.floor is not None and s.ema < s.floor - 0.001:
        old = s.floor
        s.floor = s.ema
        s.last_known_floor = s.floor
        log.append(f"автоспуск пола {old:.2f} -> {s.floor:.2f} (новый минимум)")

    res = compute_zone(s, cfg)
    s.last_zone = res.zone  # запоминаем для гистерезиса

    if in_grace:
        s.grace_remaining -= 1

    return res, log
