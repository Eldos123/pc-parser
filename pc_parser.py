"""
Парсер объявлений о ПК в Астане и Алматы на OLX.kz.

Ищет объявления с Ryzen 7 7800X3D / 9800X3D и RTX 5070 / 5070 Ti / 5080,
оставляет только города из CITIES, запоминает уже виденные и присылает новые
в Telegram с пометкой города (или просто печатает в консоль).

Установка:
    pip install requests curl_cffi

Запуск:
    python pc_parser.py            # один проход
    python pc_parser.py --loop 15  # проверять каждые 15 минут

Настройки через переменные окружения (все необязательные):
    TG_BOT_TOKEN      токен бота от @BotFather
    TG_CHAT_ID        ваш chat_id (узнать: написать боту и открыть
                      https://api.telegram.org/bot<TOKEN>/getUpdates)
    MAX_PRICE         максимальная цена в тенге, например 1100000
    REQUIRE_BOTH      по умолчанию 1 = нужны И CPU, И GPU из списка; 0 = хватит одного
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# curl_cffi умеет притворяться настоящим Chrome (TLS-отпечаток),
# без этого OLX отвечает 403 Forbidden.
try:
    from curl_cffi import requests as cffi_requests
    HTTP = cffi_requests.Session(impersonate="chrome")
except ImportError:
    cffi_requests = None
    HTTP = requests.Session()
    print("[!] Не установлен curl_cffi — OLX может блокировать запросы. "
          "Выполните: pip install curl_cffi", file=sys.stderr)

# ---------------------------------------------------------------- настройки

SEARCH_QUERIES = [
    "7800x3d",
    "9800x3d",
    "rtx 5070",
    "5070 ti",
    "rtx 5080",
]

# Как распознавать модели в тексте (порядок важен: 5070 Ti раньше 5070).
# [xх] — латинская и русская «х», [iі] — латинская и казахская/укр. «і».
X3D = r"\s*[xх]\s*3\s*[dд]"
TI = r"\s*-?\s*[tт][iі](?![a-zа-я])"
CPU_PATTERNS = {
    "Ryzen 7 7800X3D": re.compile(r"(?<!\d)7800" + X3D, re.I),
    "Ryzen 7 9800X3D": re.compile(r"(?<!\d)9800" + X3D, re.I),
}
GPU_PATTERNS = {
    "RTX 5070 Ti": re.compile(r"(?<!\d)5070" + TI, re.I),
    "RTX 5080": re.compile(r"(?<!\d)5080(?!\d)", re.I),
    "RTX 5070": re.compile(r"(?<!\d)5070(?!\d)(?!" + TI + ")", re.I),
}

# Города, в которых ищем: название → (как узнать в тексте, адрес города на OLX)
CITIES = {
    "Астана": (re.compile(r"астан|astana|нур-?султан|nur-?sultan|акмол", re.I), "astana"),
    "Алматы": (re.compile(r"алмат|almaty|алма-?ат", re.I), "almaty"),
}


def city_of(text: str) -> str | None:
    """Первый из наших городов, упомянутый в тексте, или None."""
    for name, (rx, _) in CITIES.items():
        if rx.search(text or ""):
            return name
    return None

# Признаки того, что продаётся целый компьютер
PC_RE = re.compile(
    r"(?<![a-zа-я])(пк|pc|комп|компь?ю?т|системн|сис\.?\s*блок|сборк|системник)",
    re.I,
)

# Любое упоминание конкретного железа в заголовке (чужого в т.ч.)
HW_IN_TITLE_RE = re.compile(
    r"(?<!\d)\d{4}\s*[xхfф]\b|ryzen|\bi[3579]\b|\bi[3579]-|(?:rtx|gtx|rx)\s*\d{3,4}|core\s*ultra",
    re.I,
)

# Заголовки, которые точно не про готовый ПК
TITLE_EXCLUDE_RE = re.compile(
    r"^\s*(видеокарт|процессор|материнск|мат\.?\s*плат|оперативн|блок\s+питани)"
    r"|ноутбук|laptop|legion|predator|helios|rog\s*strix|zephyrus|\btuf\b|nitro|victus|"
    r"\bomen\b|katana|vector|thinkpad|macbook|galaxy|iphone|xiaomi|redmi|"
    r"на\s+заказ|под\s+заказ|аренд|прокат|куплю|\bищу\b|скупк|ремонт|"
    r"хочу\s+купить|хочу\s+взять|подскажите|посоветуйте|кто\s+продает|кто\s+продаёт|"
    # казахский: «кім сатады», «сататындар бар ма», «сатып аламын», «керек»
    r"сата?т[ыі]?ндар|бар\s*ма\b|барма\b|аламын|алам\b|кім\s+сат|керек\b",
    re.I,
)

SEEN_FILE = Path(__file__).with_name("seen.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

MAX_PRICE = int(os.getenv("MAX_PRICE", "0") or 0)
# По умолчанию: нужны И процессор, И видеокарта из списка. REQUIRE_BOTH=0 — хватит одного.
REQUIRE_BOTH = os.getenv("REQUIRE_BOTH", "1") == "1"


# ---------------------------------------------------------------- утилиты

def load_seen() -> dict:
    """id объявления → последняя известная цена (None, если неизвестна)."""
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if isinstance(data, list):         # старый формат — только список id, без цен
        return dict.fromkeys(data)
    return data


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(dict(sorted(seen.items())), ensure_ascii=False, indent=0),
                         encoding="utf-8")


def detect_models(text: str) -> tuple[list, list]:
    cpus = [name for name, rx in CPU_PATTERNS.items() if rx.search(text)]
    gpus = [name for name, rx in GPU_PATTERNS.items() if rx.search(text)]
    return cpus, gpus


def is_relevant(title: str, description: str, price: int | None) -> tuple[bool, list, list]:
    title = title or ""
    if TITLE_EXCLUDE_RE.search(title):
        return False, [], []

    title_is_pc = bool(PC_RE.search(title))
    t_cpus, t_gpus = detect_models(title)
    # Целый ПК: в заголовке слово «ПК/компьютер/системный блок»
    # или сразу и процессор, и видеокарта из нашего списка.
    if not title_is_pc and not (t_cpus and t_gpus):
        return False, [], []

    # Модели берём из заголовка. Описание учитываем, только если в заголовке
    # железо не указано вообще («Продам игровой системный блок»): иначе
    # «3300X ... ПК под сборку, можно поставить 5070» пролезет по описанию.
    if t_cpus or t_gpus:
        # наша модель есть в заголовке — недостающую деталь ищем в описании
        d_cpus, d_gpus = detect_models(description or "")
        cpus = t_cpus or d_cpus
        gpus = t_gpus or d_gpus
    elif HW_IN_TITLE_RE.search(title):
        cpus, gpus = [], []
    else:
        cpus, gpus = detect_models(f"{title}\n{description or ''}")
    if not cpus and not gpus:
        return False, cpus, gpus
    if REQUIRE_BOTH and not (cpus and gpus):
        return False, cpus, gpus
    if MAX_PRICE and price and price > MAX_PRICE:
        return False, cpus, gpus
    return True, cpus, gpus


def fmt_price(price: int | None) -> str:
    return f"{price:,} ₸".replace(",", " ") if price else "цена не указана"


# ---------------------------------------------------------------- OLX.kz

OLX_API = "https://www.olx.kz/api/v1/offers/"
OLX_HTML = "https://www.olx.kz/{city}/q-{q}/"
_olx_warmed = False
OLX_FAILS = 0


def olx_get(url: str, **kw):
    """GET с cookies главной страницы (как у обычного посетителя)."""
    global _olx_warmed
    if not _olx_warmed:
        try:
            HTTP.get("https://www.olx.kz/", headers={"Accept-Language": "ru-RU,ru;q=0.9"}, timeout=20)
        except Exception:  # noqa: BLE001
            pass
        _olx_warmed = True
    return HTTP.get(url, timeout=25, **kw)


def olx_price(offer: dict) -> int | None:
    # формат API
    for p in offer.get("params", []) or []:
        if p.get("key") == "price":
            val = (p.get("value") or {}).get("value")
            try:
                return int(float(val)) if val else None
            except (TypeError, ValueError):
                return None
    # формат HTML-страницы
    price = offer.get("price") or {}
    reg = price.get("regularPrice") or {}
    val = reg.get("value")
    if val:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            pass
    disp = price.get("displayValue") or ""
    digits = re.sub(r"\D", "", disp)
    return int(digits) if digits else None


def olx_state(offer: dict) -> str | None:
    """Состояние из параметров объявления: «Б/у» или «Новый»."""
    for p in offer.get("params", []) or []:
        if p.get("key") == "state":
            val = p.get("value")
            # API: {"key": "used", "label": "Б/у"}; HTML-страница: "Б/у"
            return (val.get("label") if isinstance(val, dict) else val) or None
    return None


def olx_city(offer: dict) -> str | None:
    loc = offer.get("location") or {}
    # сначала сам город, потом область
    city = " ".join([str(loc.get("cityName", "")),
                     *(str((loc.get("city") or {}).get(f, "")) for f in ("name", "normalized_name"))])
    region = " ".join([str(loc.get("regionName", "")),
                       *(str((loc.get("region") or {}).get(f, "")) for f in ("name", "normalized_name"))])
    return city_of(city) or city_of(region)


def olx_from_api(query: str) -> list[dict] | None:
    try:
        r = olx_get(OLX_API, params={"offset": 0, "limit": 40, "query": query},
                    headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        return r.json().get("data", [])
    except Exception:  # noqa: BLE001
        return None


def olx_from_html(query: str) -> list[dict] | None:
    ads, ok = [], False
    for name, (_, slug) in CITIES.items():
        part = olx_from_html_city(query, name, slug)
        if part is not None:
            ok = True
            ads.extend(part)
    return ads if ok else None


def olx_from_html_city(query: str, city: str, slug: str) -> list[dict] | None:
    url = OLX_HTML.format(city=slug, q=re.sub(r"\s+", "-", query.strip()))
    try:
        r = olx_get(url, headers={"Accept": "text/html", "Accept-Language": "ru-RU,ru;q=0.9"})
        if r.status_code != 200:
            print(f"[OLX] страница {url} ответила {r.status_code}", file=sys.stderr)
            return None
        m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:\\.|[^"\\])*")', r.text)
        if not m:
            print("[OLX] не нашёл данные на странице (возможно, изменилась вёрстка)", file=sys.stderr)
            return None
        state = json.loads(json.loads(m.group(1)))
        ads = (state.get("listing") or {}).get("listing", {}).get("ads", [])
        # на странице города (/astana/, /almaty/) все объявления уже из него
        for ad in ads:
            ad.setdefault("location", {}).setdefault("cityName", city)
        return ads
    except Exception as e:  # noqa: BLE001
        print(f"[OLX] ошибка разбора страницы: {e}", file=sys.stderr)
        return None


def search_olx(query: str) -> list[dict]:
    data = olx_from_api(query)
    if data is None:
        data = olx_from_html(query)
    if data is None:
        global OLX_FAILS
        OLX_FAILS += 1
        print(f"[OLX] не удалось получить результаты по «{query}»", file=sys.stderr)
        return []

    results = []
    for offer in data:
        city = olx_city(offer)
        if not city:
            continue
        price = olx_price(offer)
        ok, cpus, gpus = is_relevant(offer.get("title", ""), offer.get("description", ""), price)
        if not ok:
            continue
        results.append({
            "id": f"olx:{offer.get('id')}",
            "source": "OLX",
            "city": city,
            "state": olx_state(offer),
            "title": (offer.get("title") or "").strip(),
            "price": price,
            "url": offer.get("url"),
            "date": offer.get("created_time") or offer.get("createdTime"),
            "cpus": cpus,
            "gpus": gpus,
        })
    return results


# ---------------------------------------------------------------- уведомления

def notify(item: dict, old_price: int | None = None) -> None:
    parts = item["cpus"] + item["gpus"]
    if old_price:
        diff = item["price"] - old_price
        sign = "📉 подешевело" if diff < 0 else "📈 подорожало"
        price_line = (f"🔄 Цена изменилась: {fmt_price(old_price)} → {fmt_price(item['price'])}\n"
                      f"{sign} на {fmt_price(abs(diff))}")
    else:
        price_line = f"💰 {fmt_price(item['price'])}"
    msg = (
        f"🖥 {item['title']}\n"
        f"{price_line}\n"
        f"🏷 {item.get('state') or 'состояние не указано'}\n"
        f"🔧 {', '.join(parts)}\n"
        f"📍 {item['city']} · {item['source']}\n"
        f"{item['url']}"
    )
    print(msg, "\n" + "-" * 40)

    token, chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    if token and chat:
        # Telegram ограничивает частоту сообщений: при ответе 429 ждём и пробуем снова,
        # между сообщениями — пауза, чтобы порядок сохранился и ничего не потерялось.
        for _ in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": msg, "disable_web_page_preview": False},
                    timeout=15,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[Telegram] не отправилось: {e}", file=sys.stderr)
                break
            if r.status_code == 429:
                wait = (r.json().get("parameters") or {}).get("retry_after", 5)
                time.sleep(wait + 1)
                continue
            if r.status_code != 200:
                print(f"[Telegram] ответ {r.status_code}: {r.text[:200]}", file=sys.stderr)
            break
        time.sleep(1.1)


# ---------------------------------------------------------------- main

def run_once(first_run_silent: bool) -> None:
    seen = load_seen()
    is_first = not seen
    found: dict[str, dict] = {}

    for q in SEARCH_QUERIES:
        for item in search_olx(q):
            found.setdefault(item["id"], item)
        time.sleep(1)

    new = [it for it in found.values() if it["id"] not in seen]
    # Уже знакомые объявления, у которых поменялась цена (старая цена должна быть известна)
    changed = [it for it in found.values()
               if seen.get(it["id"]) and it["price"] and it["price"] != seen[it["id"]]]

    # Новый город в CITIES — его текущие объявления тоже запоминаем без спама.
    # В seen хранится метка «city:<название>» для уже знакомых городов.
    if not is_first and not any(s.startswith("city:") for s in seen):
        seen["city:Астана"] = None     # старый seen.json — Астана уже знакома
    if not is_first and first_run_silent:
        fresh = [c for c in CITIES if f"city:{c}" not in seen]
        silent = [it for it in new if it.get("city") in fresh]
        if silent:
            print(f"Первый запуск для {', '.join(fresh)}: найденные объявления (без уведомлений):")
            for it in silent:
                print(f"  {fmt_price(it['price']):>15} | {it['city']} | {it['title'][:60]} | {it['url']}")
        new = [it for it in new if it.get("city") not in fresh]
    for c in CITIES:
        seen.setdefault(f"city:{c}", None)
    # Сначала города в порядке CITIES (Астана, потом Алматы), внутри — от дешёвых к дорогим
    city_order = {c: i for i, c in enumerate(CITIES)}
    order = lambda it: (city_order.get(it.get("city"), len(CITIES)), it["price"] or 10**12)  # noqa: E731
    new.sort(key=order)
    changed.sort(key=order)

    print(f"Найдено подходящих: {len(found)}, новых: {len(new)}, с новой ценой: {len(changed)}")
    if is_first and first_run_silent:
        print("Первый запуск: запоминаю текущие объявления без уведомлений в Telegram.")
        for it in new:
            print(f"  {fmt_price(it['price']):>15} | {it['title'][:70]} | {it['url']}")
    else:
        for it in new:
            notify(it)
    for it in changed:
        notify(it, old_price=seen[it["id"]])

    # Запоминаем цены; если в этот раз цена не распознана — оставляем прежнюю
    for id_, it in found.items():
        if it["price"] or id_ not in seen:
            seen[id_] = it["price"]
    save_seen(seen)


def main() -> None:
    # консоль Windows не умеет эмодзи — не падаем, а заменяем их на «?»
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(description="Поиск ПК в Астане и Алматы на OLX")
    ap.add_argument("--loop", type=int, default=0, help="повторять каждые N минут")
    ap.add_argument("--notify-all", action="store_true",
                    help="на первом запуске тоже слать всё в Telegram")
    args = ap.parse_args()

    while True:
        global OLX_FAILS
        OLX_FAILS = 0
        run_once(first_run_silent=not args.notify_all)
        print(time.strftime("%H:%M"), "— проверка завершена"
              + (f", следующая через {args.loop} мин" if args.loop else ""))
        if not args.loop:
            if OLX_FAILS == len(SEARCH_QUERIES):
                print("OLX не ответил ни на один запрос — вероятно, блокирует этот IP.",
                      file=sys.stderr)
                sys.exit(1)
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
