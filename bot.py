#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — демон "сторожа от отката веса".

Без сторонних зависимостей: только стандартная библиотека Python.
На дешёвой VPS это значит "git clone и запустил" — ни pip, ни venv.

Что делает:
  * слушает топик priv_weight_in на ТВОЁМ ntfy-сервере (стрим, реконнект);
  * на число — учитывает вес, пишет зону в priv_weight_out;
  * на команды status / undo / help — отвечает соответственно;
  * раз в неделю шлёт бэкап базы (weight.txt) себе в ntfy вложением;
  * всё состояние — в человекочитаемых файлах рядом (см. runbook.md).

Транспорт — ntfy. Хранение — локальные текстовые файлы. ntfy базу не трогает.

Запуск:
    python3 bot.py --cold-start   # первый раз: построить state.json из weight.txt
    python3 bot.py                # демон (его и запускает systemd)
"""

import base64
import datetime as dt
import json
import os
import shutil
import sys
import time
import urllib.request
import urllib.error

import keeper as k

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py < 3.9, на Ubuntu не должно случиться
    ZoneInfo = None


# ────────────────────────────── конфиг и .env ────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path: str) -> dict:
    """Простой парсер .env: KEY=VALUE по строкам. # — комментарий."""
    env = {}
    if not os.path.exists(path):
        raise SystemExit(f"Нет файла {path}. Скопируй .env.example в .env и заполни.")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def now_local(cfg: dict) -> dt.datetime:
    """Текущее время в таймзоне из конфига, naive (как хранит приложение)."""
    tz = cfg.get("timezone", "Europe/Warsaw")
    if ZoneInfo is not None:
        return dt.datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    return dt.datetime.now()


# ─────────────────────────────── клиент ntfy ─────────────────────────────────


class Ntfy:
    """Тонкая обёртка над ntfy поверх стандартного urllib. Никаких зависимостей."""

    def __init__(self, base_url: str, user: str, password: str):
        self.base = base_url.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {token}"

    def publish(self, topic: str, message: str, title: str = "") -> None:
        """Отправить текстовое сообщение в топик."""
        headers = {"Authorization": self.auth, "Content-Type": "text/plain; charset=utf-8"}
        if title:
            # ntfy требует заголовки latin-1; кириллицу шлём как RFC2047
            headers["Title"] = title.encode("utf-8").decode("latin-1", "replace")
        req = urllib.request.Request(
            f"{self.base}/{topic}", data=message.encode("utf-8"),
            headers=headers, method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()

    def publish_file(self, topic: str, file_path: str, filename: str,
                     message: str = "") -> None:
        """Отправить файл вложением (PUT + заголовок Filename)."""
        with open(file_path, "rb") as f:
            data = f.read()
        headers = {
            "Authorization": self.auth,
            "Filename": filename,
        }
        if message:
            headers["Message"] = message.encode("utf-8").decode("latin-1", "replace")
        req = urllib.request.Request(
            f"{self.base}/{topic}", data=data, headers=headers, method="PUT",
        )
        urllib.request.urlopen(req, timeout=30).read()

    def subscribe(self, topic: str, on_message, read_timeout: int = 120):
        """
        Подписка на стрим. Блокирующая. Для каждого входящего сообщения
        зовёт on_message(text). Бросает исключение при обрыве — внешний цикл
        переподключится. read_timeout режет мёртвые коннекты (ntfy шлёт
        keepalive ~раз в 45с, так что 120с с запасом).
        """
        url = f"{self.base}/{topic}/json"
        req = urllib.request.Request(url, headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=read_timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") != "message":
                    continue  # open / keepalive — пропускаем
                text = (msg.get("message") or "").strip()
                if text:
                    on_message(text)


# ─────────────────────────── работа с файлами базы ───────────────────────────


def append_or_update_today(path: str, entry: k.Entry) -> None:
    """
    Записать взвешивание за сегодня. Если за сегодня запись уже есть —
    обновляем её (одно число в день), иначе дописываем в конец.
    Формат строки байт-совместим с приложением.
    """
    today = entry.when.date()
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

    new_line = k.fmt_line(entry) + "\n"
    # ищем последнюю строку данных за сегодня
    idx_today = None
    for i, line in enumerate(lines):
        e = k.parse_line(line)
        if e is not None and e.when.date() == today:
            idx_today = i
    if idx_today is not None:
        lines[idx_today] = new_line
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remove_last_today(path: str, today: dt.date) -> bool:
    """Убрать последнюю строку данных за сегодня (для undo). True если убрали."""
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    idx = None
    for i, line in enumerate(lines):
        e = k.parse_line(line)
        if e is not None and e.when.date() == today:
            idx = i
    if idx is None:
        return False
    del lines[idx]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, path)
    return True


# ─────────────────────────────── журнал решений ──────────────────────────────


def log_decision(paths: dict, *messages: str) -> None:
    """Допись в decisions.log: одна строка на событие, человекочитаемо, append-only."""
    if not messages:
        return
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(paths["decisions"], "a", encoding="utf-8") as f:
        for m in messages:
            f.write(f"{stamp} — {m}\n")


# ─────────────────────────── форматирование ответов ──────────────────────────


def render_zone(res: k.ZoneResult, raw_weight=None) -> str:
    names = {k.GREEN: "зелёная", k.YELLOW: "жёлтая", k.RED: "красная"}
    head = f"{k.EMOJI[res.zone]} {names[res.zone]}"
    if res.preliminary:
        head += "  (предварительно, после паузы)"
    parts = []
    if raw_weight is not None:
        parts.append(f"вес {k.fmt_weight(raw_weight)}")
    parts.append(f"EMA {res.ema:.1f}")
    parts.append(f"пол {res.floor:.1f}")
    parts.append(f"просадка {res.drawdown:+.1f}")
    line2 = " · ".join(parts)
    line3 = ""
    if res.slope_per_week is not None:
        line3 = f"\nтренд {res.slope_per_week:+.2f} кг/нед"
    return f"{head}\n{line2}{line3}"


# ──────────────────────────── диспетчер сообщений ────────────────────────────
#
# handle_message возвращает список действий [("text", str)] или [("file", path)].
# Так его легко тестировать без сети: подменяем исполнителя действий заглушкой.


def handle_message(text: str, paths: dict, cfg: dict, now: dt.datetime) -> list:
    cmd = text.strip().lower()

    if cmd == "help":
        return [("file", paths["help"])]

    if cmd == "status":
        try:
            s = k.State.load(paths["state"])
        except FileNotFoundError:
            return [("text", "Состояния ещё нет. Сначала пришли число или сделай cold-start.")]
        res = k.compute_zone(s, cfg)
        return [("text", render_zone(res))]

    if cmd == "undo":
        prev = paths["state"] + ".prev"
        if not os.path.exists(prev):
            return [("text", "Нечего отменять: нет предыдущего состояния.")]
        shutil.copyfile(prev, paths["state"])
        removed = remove_last_today(paths["weight"], now.date())
        log_decision(paths, "undo: откат последней записи за сегодня"
                            + ("" if removed else " (строки в базе не было)"))
        s = k.State.load(paths["state"])
        res = k.compute_zone(s, cfg)
        return [("text", "Откатил последнюю запись.\n" + render_zone(res))]

    # иначе — пробуем как вес
    try:
        weight = k.parse_weight(text)
    except (ValueError, IndexError):
        return [("text", f"Не понял «{text}». Пришли число (например 64.7) "
                         f"или команду: status, undo, help.")]
    if not (cfg["weight_min_plausible"] <= weight <= cfg["weight_max_plausible"]):
        return [("text", f"{weight} вне разумного диапазона "
                         f"{cfg['weight_min_plausible']}..{cfg['weight_max_plausible']}. "
                         f"Опечатка? Ничего не записал.")]

    # снимок состояния для undo
    if os.path.exists(paths["state"]):
        shutil.copyfile(paths["state"], paths["state"] + ".prev")

    s = k.State.load(paths["state"])
    # заметить ручной подъём пола, если правили файл
    log_decision(paths, *k.detect_manual_floor_raise(s, cfg))

    res, decisions = k.process_weight(s, weight, now, cfg)
    s.save_atomic(paths["state"])
    append_or_update_today(paths["weight"], k.Entry(weight=weight, when=now))
    log_decision(paths, *decisions)

    return [("text", render_zone(res, raw_weight=weight))]


# ─────────────────────────── еженедельный бэкап ──────────────────────────────


def maybe_backup(ntfy: Ntfy, out_topic: str, paths: dict, cfg: dict,
                 now: dt.datetime) -> None:
    """Раз в неделю в назначенный день шлём weight.txt вложением в ntfy."""
    try:
        s = k.State.load(paths["state"])
    except FileNotFoundError:
        return
    today = now.date().isoformat()
    if now.weekday() != cfg.get("backup_weekday", 0):
        return
    if s.last_backup_date == today:
        return
    if not os.path.exists(paths["weight"]):
        return
    try:
        ntfy.publish_file(out_topic, paths["weight"], "weight.txt",
                          message=f"Еженедельный бэкап базы ({today})")
        s.last_backup_date = today
        s.save_atomic(paths["state"])
        log_decision(paths, f"бэкап базы отправлен в ntfy ({today})")
    except Exception as e:  # бэкап не должен ронять бота
        log_decision(paths, f"бэкап не удался: {e}")


# ─────────────────────────────── холодный старт ──────────────────────────────


def do_cold_start(paths: dict, cfg: dict) -> None:
    header, entries = k.read_database(paths["weight"])
    if not entries:
        raise SystemExit(f"В {paths['weight']} нет данных. Положи туда базу и повтори.")
    s, log = k.cold_start(entries, cfg)
    s.last_zone = k.GREEN
    s.save_atomic(paths["state"])
    log_decision(paths, *log)
    print("Холодный старт выполнен:")
    for l in log:
        print("  " + l)
    res = k.compute_zone(s, cfg)
    print("\nТекущая оценка:")
    print(render_zone(res))


# ─────────────────────────────── главный цикл ────────────────────────────────


def main() -> None:
    env = load_env(os.path.join(HERE, ".env"))
    cfg = load_config(os.path.join(HERE, "config.json"))

    data_dir = env.get("DATA_DIR", HERE)
    paths = {
        "weight": os.path.join(data_dir, "weight.txt"),
        "state": os.path.join(data_dir, "state.json"),
        "decisions": os.path.join(data_dir, "decisions.log"),
        "help": os.path.join(HERE, "help.md"),
    }
    k_state = None  # noqa

    # валидация состояния, если оно есть — падаем сразу и громко
    if os.path.exists(paths["state"]):
        s = k.State.load(paths["state"])
        k.validate_state(s, cfg)

    if "--cold-start" in sys.argv:
        do_cold_start(paths, cfg)
        return

    if not os.path.exists(paths["state"]):
        raise SystemExit("Нет state.json. Запусти сначала: python3 bot.py --cold-start")

    ntfy = Ntfy(env["NTFY_URL"], env["NTFY_USER"], env["NTFY_PASS"])
    in_topic = env["TOPIC_IN"]
    out_topic = env["TOPIC_OUT"]

    log_decision(paths, "бот запущен")

    def on_message(text: str) -> None:
        now = now_local(cfg)
        try:
            actions = handle_message(text, paths, cfg, now)
        except Exception as e:
            ntfy.publish(out_topic, f"⚠️ Ошибка обработки: {e}")
            log_decision(paths, f"ошибка обработки '{text}': {e}")
            return
        for kind, payload in actions:
            if kind == "text":
                ntfy.publish(out_topic, payload)
            elif kind == "file":
                ntfy.publish_file(out_topic, payload, os.path.basename(payload),
                                  message="Справка")

    backoff = 1
    while True:
        try:
            maybe_backup(ntfy, out_topic, paths, cfg, now_local(cfg))
            ntfy.subscribe(in_topic, on_message)
            backoff = 1  # успешный цикл — сбросили задержку
        except KeyboardInterrupt:
            log_decision(paths, "бот остановлен (Ctrl-C)")
            return
        except Exception as e:
            # обрыв стрима / сеть / таймаут — переподключаемся с нарастающей паузой
            log_decision(paths, f"обрыв соединения с ntfy ({e}); переподключение через {backoff}с")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
