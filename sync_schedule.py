#!/usr/bin/env python3
"""
Забирает расписание группы с https://ya.mininuniver.ru/shedule
и собирает из него календарь в формате ICS.

Как это работает:
  1. GET /shedule            -> получаем cookie сессии и CSRF-токен (_token)
  2. GET /api/v1/groups/search -> находим id группы по её названию
  3. POST /shedule           -> в ответе HTML, внутри которого лежит
                                window.CalendarData = [...]  (всё расписание в JSON)
  4. Из JSON собираем .ics (события в UTC, чтобы не возиться с VTIMEZONE)

Запуск:
  python3 sync_schedule.py                      # -> schedule.ics
  python3 sync_schedule.py docs/schedule.ics    # свой путь
Настройки группы и подгруппы — в блоке НАСТРОЙКИ ниже.
Только стандартная библиотека, ничего ставить не нужно (Python 3.9+).
"""

import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------- НАСТРОЙКИ ----------------------------
BASE_URL = "https://ya.mininuniver.ru"
GROUP_NAME = "ПКИ-25-1"
SUBGROUP = 1          # 1 или 2 — только своя подгруппа; None — все пары, с пометкой подгруппы
ALARM_MINUTES = 15       # напоминание за N минут до пары; 0 — без напоминаний
MIN_LESSONS = 20         # защита: если пар нашлось меньше, считаем, что что-то сломалось
# --------------------------------------------------------------------

MOSCOW = ZoneInfo("Europe/Moscow")
USER_AGENT = "Mozilla/5.0 (compatible; schedule-sync/1.0)"


def make_opener():
    """Opener с cookie: сайт привязывает CSRF-токен к сессии."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def fetch_text(opener, url, data=None, headers=None):
    """GET (или POST, если есть data) и возвращает текст ответа."""
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with opener.open(request, timeout=60) as response:
        return response.read().decode("utf-8")


def get_csrf_token(page_html):
    """Достаёт значение скрытого поля _token из формы."""
    # Берём тег <input ...> с name="_token" и достаём из него value — порядок атрибутов
    # не важен (на сайте это <input name="_token" type="hidden" value="...">)
    for tag in re.findall(r"<input[^>]*>", page_html):
        if re.search(r'name=["\']_token["\']', tag):
            value = re.search(r'value=["\']([^"\']+)["\']', tag)
            if value:
                return value.group(1)
    raise RuntimeError("Не нашёл _token на странице расписания — вёрстка сайта изменилась?")


def find_group_id(opener, group_name):
    """Ищет id группы через тот же API, что использует автодополнение на сайте."""
    query = urllib.parse.urlencode({"query": group_name})
    body = fetch_text(
        opener,
        f"{BASE_URL}/api/v1/groups/search?{query}",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    for item in json.loads(body).get("suggestions", []):
        if item["value"] == group_name:
            return item["data"]["id"]
    raise RuntimeError(f"Группа {group_name!r} не найдена через /api/v1/groups/search")


def download_calendar_data(group_name):
    """Возвращает список дней из window.CalendarData."""
    opener = make_opener()
    page = fetch_text(opener, f"{BASE_URL}/shedule")
    token = get_csrf_token(page)
    group_id = find_group_id(opener, group_name)

    form = urllib.parse.urlencode({
        "_token": token,
        "searchType": "0",       # 0 = поиск по группе
        "search": group_name,
        "group": str(group_id),
    }).encode("utf-8")
    result = fetch_text(opener, f"{BASE_URL}/shedule", data=form)

    marker = "window.CalendarData = "
    start = result.find(marker)
    if start == -1:
        raise RuntimeError("В ответе нет window.CalendarData — вёрстка сайта изменилась?")
    # raw_decode сам находит конец JSON-массива, скобки считать не нужно
    data, _ = json.JSONDecoder().raw_decode(result, start + len(marker))
    return data


def collect_lessons(calendar_data):
    """Разворачивает структуру «день -> номер пары -> занятия» в плоский список.

    ВАЖНО: настоящая дата занятия — это day["date"] (ГГГГ-ММ-ДД) из обёртки дня.
    Поле lesson["date"] ненадёжно: у повторяющихся пар (например, физкультура)
    один id и одна и та же «первая» дата на все недели. Поэтому кладём
    верную дату в lesson["_date"] и дальше пользуемся только ей.
    """
    lessons = []
    for day in calendar_data:
        for couple in day["title"].values():
            for lesson in couple["lessons"]:
                lessons.append({**lesson, "_date": day["date"]})
    return lessons


# ------------------------------ ICS --------------------------------

def ics_escape(text):
    """Экранирование спецсимволов по RFC 5545."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold_line(line):
    """Строки в ICS не длиннее 75 байт; длинные переносим (с пробелом в начале).
    Режем по байтам UTF-8, но не посередине символа."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts = []
    while raw:
        limit = 75 if not parts else 74  # на продолжениях один байт занимает пробел
        chunk = raw[:limit]
        while True:
            try:
                chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8"))
        raw = raw[len(chunk):]
    return "\r\n ".join(parts)


TIME_RANGE = re.compile(r"^(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$")


def to_utc(date_str, time_str):
    """'2025-09-02' + '08:30' -> datetime в UTC (расписание по Москве)."""
    local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=MOSCOW)
    return local.astimezone(timezone.utc)


def format_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


def build_location(place):
    # у части записей place = false (нет аудитории) — тогда место просто пустое
    place = place or {}
    cabinet = (place.get("cabinet") or "").strip()
    housing = place.get("housing") or {}
    if "ЭИОС" in cabinet:
        return "Онлайн (ЭИОС, Сферум)"
    parts = []
    if cabinet:
        parts.append(f"ауд. {cabinet}")
    if housing.get("number") is not None:
        parts.append(f"корп. {housing['number']}")
    if housing.get("address"):
        parts.append(housing["address"])
    return ", ".join(parts)


def lesson_to_event(lesson):
    """Один урок -> блок VEVENT (список строк) или None, если урок не подходит."""
    subgroup = (lesson.get("subgroup") or {}).get("subgroup_numbers")
    if SUBGROUP is not None and subgroup and subgroup != SUBGROUP:
        return None  # пара другой подгруппы

    couple = lesson["couple"]
    time_match = TIME_RANGE.match(couple.get("time") or "")
    if not time_match:
        return None  # на сайте бывают записи без времени (time = false) — их не в чем показывать
    date = lesson["_date"]
    start = to_utc(date, time_match.group(1))
    end = to_utc(date, time_match.group(2))

    summary = lesson["discipline"]
    if couple.get("couple_type"):
        summary += f" ({couple['couple_type']})"
    if subgroup and SUBGROUP is None:
        summary += f" [подгр. {subgroup}]"

    teacher = lesson.get("teacher") or {}
    description = [f"Пара №{couple['number']}, {couple.get('couple_type', '')}"]
    if teacher.get("name"):
        description.append(f"Преподаватель: {teacher['name']}")
    if subgroup:
        description.append(f"Подгруппа: {subgroup}")
    if teacher.get("spherum_link"):
        description.append(f"Ссылка на занятие: {teacher['spherum_link']}")

    lines = [
        "BEGIN:VEVENT",
        # UID = id занятия + дата: id у повторяющихся пар один на все недели, а дата
        # делает UID уникальным. Он одинаковый при каждом запуске, поэтому календарь
        # обновляет событие, а не дублирует его
        f"UID:lesson-{lesson['id']}-{date}-{couple['number']}@ya.mininuniver.ru",
        # DTSTAMP = начало пары: так файл не меняется между запусками, пока расписание то же
        f"DTSTAMP:{format_dt(start)}",
        f"DTSTART:{format_dt(start)}",
        f"DTEND:{format_dt(end)}",
        f"SUMMARY:{ics_escape(summary)}",
        f"LOCATION:{ics_escape(build_location(lesson['place']))}",
        f"DESCRIPTION:{ics_escape(chr(10).join(description))}",
    ]
    if ALARM_MINUTES:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{ics_escape(summary)}",
            f"TRIGGER:-PT{ALARM_MINUTES}M",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def build_ics(lessons):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//schedule-sync//mininuniver//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape('Расписание ' + GROUP_NAME)}",
        "X-WR-TIMEZONE:Europe/Moscow",
        # подсказки клиентам, как часто перечитывать (Apple Calendar учитывает)
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    count = 0
    skipped = 0
    seen_uids = set()
    for lesson in sorted(lessons, key=lambda l: (l["_date"], l["couple"]["number"], l["id"])):
        event = lesson_to_event(lesson)
        if event is None:
            # не показываем пары другой подгруппы; считаем только «сломанные» записи
            if not TIME_RANGE.match(lesson["couple"].get("time") or ""):
                skipped += 1
            continue
        if event[1] in seen_uids:
            continue  # на сайте бывают полные дубли одной и той же записи
        seen_uids.add(event[1])
        lines += event
        count += 1
    lines.append("END:VCALENDAR")
    text = "\r\n".join(fold_line(line) for line in lines) + "\r\n"
    return text, count, skipped


def main():
    output_path = sys.argv[1] if len(sys.argv) > 1 else "schedule.ics"

    calendar_data = download_calendar_data(GROUP_NAME)
    lessons = collect_lessons(calendar_data)
    ics_text, count, skipped = build_ics(lessons)

    # Не перезаписываем рабочий файл пустым: при сбое сайта лучше оставить старое расписание
    if count < MIN_LESSONS:
        raise SystemExit(f"Подозрительно мало пар ({count}), файл не обновлён")
    if skipped:
        print(f"Пропущено записей без времени: {skipped}")

    folder = os.path.dirname(output_path)
    if folder:
        os.makedirs(folder, exist_ok=True)  # например, docs/ при первом запуске
    with open(output_path, "w", encoding="utf-8", newline="") as file:
        file.write(ics_text)
    print(f"Готово: {count} событий -> {output_path}")


if __name__ == "__main__":
    main()
