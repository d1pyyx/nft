import asyncio
import json
import os
import re
import signal
import time
from html import escape, unescape
from typing import Any, Callable, Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiohttp import web

DEFAULT_BOT_TOKEN = "8788134150:AAFGBSSZlIaa0wMhZMgOx3_WeFPBILOR818"
BOT_TOKEN = (os.getenv("BOT_TOKEN") or DEFAULT_BOT_TOKEN).strip()
DEFAULT_WEBAPP_URL = "https://nft-production-f42b.up.railway.app"
WEBAPP_URL = (os.getenv("WEBAPP_URL") or DEFAULT_WEBAPP_URL).strip().rstrip("/")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

API_BASE = "https://api.changes.tg"
TELEGRAM_API = "https://api.telegram.org/bot" + BOT_TOKEN
TME_NFT_BASE = "https://t.me/nft"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
CREDIT = "Данные: api.changes.tg, спасибо @GiftChanges"
FEED_SIZE = 16
SORT_MODE = (os.getenv("SORT") or "left").strip().lower()
CATALOG_TTL = 900
STATS_TTL = 300


class UpstreamError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class TtlCache:
    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get(self, key: str, ttl: int, loader: Callable[[], Any]) -> Any:
        found = self._values.get(key)
        if found is not None and found[0] > time.time():
            return found[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            found = self._values.get(key)
            if found is not None and found[0] > time.time():
                return found[1]
            value = await loader()
            self._values[key] = (time.time() + ttl, value)
            return value


CACHE = TtlCache()
SESSION: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    global SESSION
    if SESSION is None or SESSION.closed:
        SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25),
            headers={"User-Agent": USER_AGENT},
        )
    return SESSION


async def close_session() -> None:
    global SESSION
    if SESSION is not None and not SESSION.closed:
        await SESSION.close()
    SESSION = None


async def fetch_json(url: str) -> Any:
    session = await get_session()
    async with session.get(url) as response:
        if response.status != 200:
            raise UpstreamError(response.status, "upstream " + str(response.status))
        return await response.json(content_type=None)


async def fetch_text(url: str) -> Optional[str]:
    session = await get_session()
    async with session.get(url) as response:
        if response.status == 404:
            return None
        if response.status != 200:
            raise UpstreamError(response.status, "upstream " + str(response.status))
        return await response.text()


def to_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def to_int(value: Any) -> int:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    return int(digits) if digits else 0


def icon_url(gift_id: Any, size: int) -> str:
    return API_BASE + "/original/" + str(gift_id) + ".png?size=" + str(size)


async def load_sale_info() -> Dict[str, Dict[str, Any]]:
    if not BOT_TOKEN:
        return {}
    try:
        data = await fetch_json(TELEGRAM_API + "/getAvailableGifts")
    except Exception:
        return {}
    info: Dict[str, Dict[str, Any]] = {}
    for item in ((data or {}).get("result") or {}).get("gifts") or []:
        info[str(item.get("id"))] = {
            "stars": item.get("star_count"),
            "upgradeStars": item.get("upgrade_star_count"),
            "total": item.get("total_count"),
            "remaining": item.get("remaining_count"),
        }
    return info


async def load_catalog() -> List[Dict[str, Any]]:
    ids, upgradable, sale = await asyncio.gather(
        fetch_json(API_BASE + "/ids"),
        fetch_json(API_BASE + "/gifts"),
        load_sale_info(),
    )
    allowed = {to_slug(name) for name in (upgradable or [])}
    gifts: List[Dict[str, Any]] = []
    for gift_id, name in dict(ids or {}).items():
        slug = to_slug(name)
        if not slug or slug not in allowed:
            continue
        extra = sale.get(str(gift_id)) or {}
        gifts.append(
            {
                "name": str(name),
                "slug": slug,
                "id": str(gift_id),
                "icon": icon_url(gift_id, 256),
                "iconLarge": icon_url(gift_id, 512),
                "stars": extra.get("stars"),
                "upgradeStars": extra.get("upgradeStars"),
                "total": extra.get("total"),
                "remaining": extra.get("remaining"),
                "onSale": str(gift_id) in sale,
            }
        )
    gifts.sort(key=lambda item: item["name"].lower())
    return gifts


async def get_catalog() -> List[Dict[str, Any]]:
    return await CACHE.get("catalog", CATALOG_TTL, load_catalog)


async def find_gift(query: str) -> Optional[Dict[str, Any]]:
    gifts = await get_catalog()
    raw = str(query or "").strip()
    needle = to_slug(raw)
    if not needle:
        return None
    for gift in gifts:
        if gift["slug"] == needle or gift["id"] == raw:
            return gift
    for gift in gifts:
        if gift["slug"].startswith(needle):
            return gift
    for gift in gifts:
        if needle in gift["slug"]:
            return gift
    return None


TAG_RE = re.compile(r"<[^>]+>")
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
QUANTITY_RE = re.compile(r"([\d\s,]+)/([\d\s,]+)")


def clean_text(value: str) -> str:
    return " ".join(unescape(TAG_RE.sub(" ", value or "")).replace("\xa0", " ").split())


def parse_rows(page: str) -> Dict[str, str]:
    rows: Dict[str, str] = {}
    for chunk in ROW_RE.findall(page or ""):
        cells = CELL_RE.findall(chunk)
        if len(cells) < 2:
            continue
        label = clean_text(cells[0]).lower()
        if label:
            rows[label] = clean_text(cells[1])
    return rows


async def load_stats(slug: str) -> Dict[str, int]:
    page = await fetch_text(TME_NFT_BASE + "/" + slug + "-1")
    if not page:
        return {"upgraded": 0, "supply": 0}
    match = QUANTITY_RE.search(parse_rows(page).get("quantity") or "")
    if not match:
        return {"upgraded": 0, "supply": 0}
    return {"upgraded": to_int(match.group(1)), "supply": to_int(match.group(2))}


async def get_stats(slug: str) -> Dict[str, int]:
    async def loader() -> Dict[str, int]:
        try:
            return await load_stats(slug)
        except Exception:
            return {"upgraded": 0, "supply": 0}

    return await CACHE.get("stats:" + slug, STATS_TTL, loader)


def supply_of(gift: Dict[str, Any], stats: Dict[str, int]) -> int:
    return int(stats.get("supply") or gift.get("total") or 0)


def left_of(gift: Dict[str, Any], stats: Dict[str, int]) -> int:
    supply = supply_of(gift, stats)
    if not supply:
        return 0
    return max(0, supply - int(stats.get("upgraded") or 0))


def feed_card(gift: Dict[str, Any], stats: Dict[str, int]) -> Dict[str, Any]:
    return {
        "slug": gift["slug"],
        "name": gift["name"],
        "icon": gift["icon"],
        "stars": gift["stars"],
        "left": left_of(gift, stats),
        "supply": supply_of(gift, stats),
    }


async def load_feed(page: int) -> Dict[str, Any]:
    gifts = await get_catalog()
    start = max(0, (page - 1) * FEED_SIZE)
    window = gifts[start : start + FEED_SIZE]
    limiter = asyncio.Semaphore(6)

    async def one(gift: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with limiter:
            stats = await get_stats(gift["slug"])
        card = feed_card(gift, stats)
        if card["supply"] and card["left"] <= 0:
            return None
        return card

    found = await asyncio.gather(*[one(gift) for gift in window])
    items = [item for item in found if item is not None]
    if SORT_MODE == "name":
        items.sort(key=lambda item: item["name"].lower())
    else:
        items.sort(key=lambda item: (-item["left"], item["name"].lower()))
    return {
        "items": items,
        "page": page,
        "hasMore": start + FEED_SIZE < len(gifts),
        "total": len(gifts),
    }


async def get_feed(page: int) -> Dict[str, Any]:
    return await CACHE.get("feed:" + str(page), STATS_TTL, lambda: load_feed(page))


async def gift_payload(gift: Dict[str, Any]) -> Dict[str, Any]:
    stats = await get_stats(gift["slug"])
    return {
        "slug": gift["slug"],
        "name": gift["name"],
        "icon": gift["iconLarge"],
        "stars": gift["stars"],
        "upgradeStars": gift["upgradeStars"],
        "total": gift["total"],
        "remaining": gift["remaining"],
        "onSale": gift["onSale"],
        "left": left_of(gift, stats),
        "supply": supply_of(gift, stats),
        "upgraded": int(stats.get("upgraded") or 0),
    }


def json_response(payload: Any, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=MINI_APP_HTML, content_type="text/html")


async def handle_feed(request: web.Request) -> web.Response:
    raw = re.sub(r"[^0-9]", "", request.query.get("page", "1")) or "1"
    page = max(1, min(20, int(raw)))
    try:
        return json_response(await get_feed(page))
    except UpstreamError as error:
        return json_response({"error": error.message}, 502)
    except Exception:
        return json_response({"error": "failed"}, 502)


async def handle_gift(request: web.Request) -> web.Response:
    gift = await find_gift(request.match_info.get("slug", ""))
    if gift is None:
        return json_response({"error": "not found"}, 404)
    try:
        return json_response(await gift_payload(gift))
    except UpstreamError as error:
        return json_response({"error": error.message}, 502)
    except Exception:
        return json_response({"error": "failed"}, 502)


async def handle_health(request: web.Request) -> web.Response:
    return json_response({"ok": True, "credit": CREDIT})


MINI_APP_HTML = r'''<!doctype html>
<html lang='ru'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no'>
<title>Неулучшенные подарки</title>
<script src='https://telegram.org/js/telegram-web-app.js'></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#17212b);--text:var(--tg-theme-text-color,#ffffff);--muted:var(--tg-theme-hint-color,#7d8b99);--card:var(--tg-theme-secondary-bg-color,#232e3c);--line:rgba(128,128,128,.2);--link:var(--tg-theme-link-color,#4aa8e8)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif}
.wrap{max-width:520px;margin:0 auto;padding:12px 12px 24px}
input{width:100%;padding:10px 12px;border:0;border-radius:10px;background:var(--card);color:var(--text);font-size:15px;outline:none}
.hint{color:var(--muted);font-size:13px;margin:10px 2px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cell{background:var(--card);border-radius:14px;padding:12px 10px;text-align:center}
.cell img{width:86px;height:86px;object-fit:contain;display:block;margin:0 auto 8px}
.cell b{display:block;font-size:14px;font-weight:600}
.cell i{display:block;margin-top:4px;color:var(--muted);font-size:12px;font-style:normal}
.more{width:100%;margin:12px 0 0;padding:11px;border:0;border-radius:12px;background:var(--card);color:var(--text);font-size:14px}
.hide{display:none}
.back{display:inline-block;padding:2px 0 6px;color:var(--link);font-size:15px}
.hero{text-align:center;padding:8px 0 2px}
.hero img{width:132px;height:132px;object-fit:contain}
.title{margin:6px 0 14px;text-align:center;font-size:17px;font-weight:600}
.card{background:var(--card);border-radius:12px;overflow:hidden}
.kv{display:flex;gap:12px;padding:11px 14px;border-bottom:1px solid var(--line);font-size:14px}
.kv:last-child{border-bottom:0}
.kv i{flex:0 0 112px;font-style:normal;color:var(--muted)}
.kv span{flex:1}
.foot{margin:14px 2px 0;color:var(--muted);font-size:12px;text-align:center}
</style>
</head>
<body>
<div class='wrap'>
<div id='listScreen'>
<input id='search' placeholder='Поиск' autocomplete='off'>
<div class='hint' id='count'>Загружаю</div>
<div class='grid' id='grid'></div>
<button class='more hide' id='more'>Показать ещё</button>
<div class='foot'>Данные: api.changes.tg, спасибо @GiftChanges</div>
</div>
<div id='detailScreen' class='hide'>
<a class='back' id='back'>Назад</a>
<div id='detail'></div>
</div>
</div>
<script>
var tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }
var items = [];
var page = 1;
var loading = false;

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) { node.className = cls; }
  if (text !== undefined && text !== null) { node.textContent = text; }
  return node;
}

function fmt(value) {
  if (value === null || value === undefined || value === '') { return ''; }
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

function stars(value) {
  if (value === null || value === undefined) { return ''; }
  return '⭐ ' + fmt(value);
}

function plural(n, one, few, many) {
  var a = n % 100;
  var b = n % 10;
  if (a > 10 && a < 20) { return many; }
  if (b > 1 && b < 5) { return few; }
  if (b === 1) { return one; }
  return many;
}

function kv(label, value) {
  var row = el('div', 'kv');
  row.appendChild(el('i', null, label));
  row.appendChild(el('span', null, value));
  return row;
}

function showList() {
  document.getElementById('detailScreen').className = 'hide';
  document.getElementById('listScreen').className = '';
  if (tg && tg.BackButton) { tg.BackButton.hide(); }
}

function showDetail() {
  document.getElementById('listScreen').className = 'hide';
  document.getElementById('detailScreen').className = '';
  window.scrollTo(0, 0);
  if (tg && tg.BackButton) { tg.BackButton.show(); }
}

function renderGrid() {
  var needle = document.getElementById('search').value.trim().toLowerCase();
  var grid = document.getElementById('grid');
  grid.textContent = '';
  var shown = items.filter(function (item) {
    if (!needle) { return true; }
    return item.name.toLowerCase().indexOf(needle) >= 0;
  });
  shown.forEach(function (item) {
    var cell = el('div', 'cell');
    var img = el('img');
    img.src = item.icon;
    img.alt = '';
    img.loading = 'lazy';
    cell.appendChild(img);
    cell.appendChild(el('b', null, item.name));
    cell.appendChild(el('i', null, item.left ? 'не улучшено ' + fmt(item.left) : 'без лимита'));
    cell.onclick = function () { openGift(item.slug); };
    grid.appendChild(cell);
  });
  document.getElementById('count').textContent = shown.length
    ? shown.length + ' ' + plural(shown.length, 'подарок', 'подарка', 'подарков')
    : 'Ничего не нашёл';
}

function renderDetail(data) {
  var box = document.getElementById('detail');
  box.textContent = '';
  var hero = el('div', 'hero');
  var img = el('img');
  img.src = data.icon;
  img.alt = '';
  hero.appendChild(img);
  box.appendChild(hero);
  box.appendChild(el('div', 'title', data.name));
  var card = el('div', 'card');
  if (data.stars !== null && data.stars !== undefined) {
    card.appendChild(kv('Стоимость', stars(data.stars)));
  }
  if (data.upgradeStars !== null && data.upgradeStars !== undefined) {
    card.appendChild(kv('Улучшение', stars(data.upgradeStars)));
  }
  if (data.supply) {
    card.appendChild(kv('Не улучшено', fmt(data.left) + ' из ' + fmt(data.supply)));
    card.appendChild(kv('Уже улучшено', fmt(data.upgraded)));
  }
  if (data.total) {
    card.appendChild(kv('В продаже', fmt(data.remaining || 0) + ' из ' + fmt(data.total)));
  } else if (!data.onSale) {
    card.appendChild(kv('В продаже', 'нет'));
  }
  box.appendChild(card);
  box.appendChild(el('div', 'foot', 'Пока подарок не улучшен, у него нет ни номера, ни модели, ни публичного владельца.'));
}

function openGift(slug) {
  showDetail();
  var box = document.getElementById('detail');
  box.textContent = '';
  box.appendChild(el('div', 'foot', 'Секунду'));
  fetch('/api/gift/' + slug).then(function (response) { return response.json(); }).then(function (data) {
    if (!data || data.error) {
      box.textContent = '';
      box.appendChild(el('div', 'foot', 'Не получилось загрузить'));
      return;
    }
    renderDetail(data);
  }).catch(function () {
    box.textContent = '';
    box.appendChild(el('div', 'foot', 'Не получилось загрузить'));
  });
}

function loadPage() {
  if (loading) { return; }
  loading = true;
  var more = document.getElementById('more');
  more.textContent = 'Загружаю';
  fetch('/api/feed?page=' + page).then(function (response) { return response.json(); }).then(function (data) {
    loading = false;
    more.textContent = 'Показать ещё';
    if (!data || data.error) {
      document.getElementById('count').textContent = 'Не получилось загрузить';
      return;
    }
    items = items.concat(data.items || []);
    page = page + 1;
    more.className = data.hasMore ? 'more' : 'more hide';
    renderGrid();
  }).catch(function () {
    loading = false;
    more.textContent = 'Показать ещё';
    document.getElementById('count').textContent = 'Не получилось загрузить';
  });
}

document.getElementById('search').addEventListener('input', renderGrid);
document.getElementById('more').addEventListener('click', loadPage);
document.getElementById('back').addEventListener('click', showList);
if (tg && tg.BackButton) { tg.BackButton.onClick(showList); }
var params = new URLSearchParams(window.location.search);
var wanted = params.get('gift') || (tg && tg.initDataUnsafe ? tg.initDataUnsafe.start_param : '') || '';
loadPage();
if (wanted) { openGift(String(wanted).replace(/[^a-z0-9]/gi, '').toLowerCase()); }
</script>
</body>
</html>
'''


router = Router()


def group(value: Any) -> str:
    try:
        return format(int(value), ",d").replace(",", " ")
    except Exception:
        return str(value)


def webapp_link(slug: str = "") -> str:
    if not WEBAPP_URL.startswith("https://"):
        return ""
    if slug:
        return WEBAPP_URL + "/?gift=" + slug
    return WEBAPP_URL + "/"


def app_markup(slug: str = "", label: str = "Открыть") -> Optional[InlineKeyboardMarkup]:
    link = webapp_link(slug)
    if not link:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=link))]]
    )


def card_text(data: Dict[str, Any]) -> str:
    lines = ["<b>" + escape(str(data.get("name") or "")) + "</b>"]
    if data.get("stars") is not None:
        lines.append("Стоимость: ⭐ " + group(data["stars"]))
    if data.get("upgradeStars") is not None:
        lines.append("Улучшение: ⭐ " + group(data["upgradeStars"]))
    if data.get("supply"):
        lines.append("Не улучшено: " + group(data["left"]) + " из " + group(data["supply"]))
        lines.append("Уже улучшено: " + group(data["upgraded"]))
    if data.get("total"):
        lines.append("В продаже: " + group(data.get("remaining") or 0) + " из " + group(data["total"]))
    elif not data.get("onSale"):
        lines.append("В продаже: нет")
    return "\n".join(lines)


async def reply_gift(message: Message, query: str) -> None:
    gift = await find_gift(query)
    if gift is None:
        await message.answer(
            "Такого подарка не нашёл. Проверь название или открой список.",
            reply_markup=app_markup("", "Открыть список"),
        )
        return
    try:
        data = await gift_payload(gift)
    except Exception:
        await message.answer("Источник молчит, попробуй через минуту.")
        return
    await message.answer(
        card_text(data),
        reply_markup=app_markup(gift["slug"], "Посмотреть в приложении"),
    )


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    text = (
        "Привет. Здесь подарки Telegram, которые ещё не улучшены — в обычном виде, без номера, модели и фона.\n\n"
        "По две иконки в ряд, сверху те, у кого больше всего неулучшенных копий. Тапни по любой — покажу цену, стоимость улучшения, сколько копий ещё не улучшено и сколько осталось в продаже."
    )
    await message.answer(text, reply_markup=app_markup("", "Открыть"))


@router.message(Command("app"))
async def on_app(message: Message) -> None:
    markup = app_markup("", "Открыть")
    if markup is None:
        await message.answer("Адрес приложения не задан, поставь WEBAPP_URL.")
        return
    await message.answer("Неулучшенные подарки:", reply_markup=markup)


@router.message(Command("gift"))
async def on_gift(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напиши название, например: /gift santa hat")
        return
    await reply_gift(message, query)


@router.message(Command("about"))
async def on_about(message: Message) -> None:
    await message.answer(CREDIT)


@router.message(F.text)
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    await reply_gift(message, text)


async def start_bot(stop_event: asyncio.Event) -> None:
    if not BOT_TOKEN:
        print("no BOT_TOKEN, running mini app only", flush=True)
        return
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    commands = [
        BotCommand(command="start", description="Начать"),
        BotCommand(command="app", description="Список подарков"),
        BotCommand(command="gift", description="Найти подарок"),
        BotCommand(command="about", description="Откуда данные"),
    ]
    while not stop_event.is_set():
        try:
            me = await bot.get_me()
            print("telegram authorized as @" + (me.username or "unknown"), flush=True)
            await bot.set_my_commands(commands)
            await bot.delete_webhook(drop_pending_updates=True)
            await dispatcher.start_polling(bot, handle_signals=False)
            break
        except TelegramUnauthorizedError:
            print("telegram rejected BOT_TOKEN, set a valid one in variables", flush=True)
            break
        except asyncio.CancelledError:
            break
        except Exception as error:
            print("bot error: " + str(error), flush=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
    try:
        await bot.session.close()
    except Exception:
        pass


async def run() -> None:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/feed", handle_feed)
    app.router.add_get("/api/gift/{slug}", handle_gift)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    print("mini app listening on http://" + HOST + ":" + str(PORT), flush=True)
    print("webapp url: " + (WEBAPP_URL or "not set"), flush=True)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop_event.set)
        except NotImplementedError:
            pass
    task = asyncio.create_task(start_bot(stop_event))
    await stop_event.wait()
    task.cancel()
    try:
        await task
    except Exception:
        pass
    await close_session()
    await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
