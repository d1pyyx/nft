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
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
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
CREDIT = "Данные: api.changes.tg и t.me/nft, спасибо @GiftChanges"
DEFAULT_BLOCKED = "mrktbank,mrkt,tonnel,tonnelnetwork,portals,portalsmarket,fragment,getgems,tonnelmarket"
BLOCKED_OWNERS = {
    part.strip().lower().lstrip("@")
    for part in (os.getenv("BLOCKED_OWNERS") or DEFAULT_BLOCKED).split(",")
    if part.strip()
}
PROBE_NUMBERS = [1, 2, 3, 4]
FEED_SIZE = 20
CATALOG_TTL = 900
INSTANCE_TTL = 300
MAX_NUMBER = 5000000


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
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
DATE_RE = re.compile(r"\bon\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
USERNAME_RE = re.compile(r"t\.me/([A-Za-z0-9_]{4,32})")
TGID_RE = re.compile(r"user\?id=(\d+)")
QUANTITY_RE = re.compile(r"([\d\s,]+)/([\d\s,]+)")
MONTHS = {
    "january": "янв",
    "february": "фев",
    "march": "мар",
    "april": "апр",
    "may": "мая",
    "june": "июн",
    "july": "июл",
    "august": "авг",
    "september": "сен",
    "october": "окт",
    "november": "ноя",
    "december": "дек",
}


def clean_text(value: str) -> str:
    return " ".join(unescape(TAG_RE.sub(" ", value or "")).replace("\xa0", " ").split())


def format_date(page: str) -> str:
    match = DATE_RE.search(page or "")
    if not match:
        return ""
    day, month, year = match.group(1), match.group(2), match.group(3)
    return day.lstrip("0") + " " + MONTHS.get(month.lower(), month.lower()) + " " + year


def parse_rows(page: str) -> Dict[str, Dict[str, str]]:
    rows: Dict[str, Dict[str, str]] = {}
    for chunk in ROW_RE.findall(page or ""):
        cells = CELL_RE.findall(chunk)
        if len(cells) < 2:
            continue
        label = clean_text(cells[0]).lower()
        if not label:
            continue
        link = HREF_RE.search(cells[1])
        rows[label] = {
            "value": clean_text(cells[1]),
            "href": link.group(1) if link else "",
        }
    return rows


def parse_owner(cell: Dict[str, str]) -> Dict[str, str]:
    link = cell.get("href") or ""
    name = cell.get("value") or ""
    username = ""
    user_id = ""
    match = USERNAME_RE.search(link)
    if match:
        username = match.group(1)
    match = TGID_RE.search(link)
    if match:
        user_id = match.group(1)
    if not username and name.startswith("@"):
        username = name[1:]
    if username:
        profile = "https://t.me/" + username
    elif user_id:
        profile = "tg://user?id=" + user_id
    else:
        profile = ""
    return {
        "name": name,
        "username": username,
        "userId": user_id,
        "link": profile,
    }


def owner_ok(owner: Dict[str, str]) -> bool:
    username = str(owner.get("username") or "").lower()
    if not username:
        return False
    if username in BLOCKED_OWNERS:
        return False
    if username.endswith("bot") or username.endswith("bank"):
        return False
    return True


async def load_instance(slug: str, number: int) -> Optional[Dict[str, Any]]:
    page = await fetch_text(TME_NFT_BASE + "/" + slug + "-" + str(number))
    if not page:
        return None
    rows = parse_rows(page)
    owner = parse_owner(rows.get("owner") or {})
    quantity = (rows.get("quantity") or {}).get("value") or ""
    issued = ""
    total = ""
    match = QUANTITY_RE.search(quantity)
    if match:
        issued = " ".join(match.group(1).replace(",", " ").split())
        total = " ".join(match.group(2).replace(",", " ").split())
    return {
        "slug": slug,
        "number": number,
        "owner": owner,
        "date": format_date(page),
        "issued": issued,
        "issuedTotal": total,
        "pageUrl": TME_NFT_BASE + "/" + slug + "-" + str(number),
    }


async def get_instance(slug: str, number: int) -> Optional[Dict[str, Any]]:
    async def loader() -> Optional[Dict[str, Any]]:
        try:
            return await load_instance(slug, number)
        except Exception:
            return None

    return await CACHE.get("nft:" + slug + ":" + str(number), INSTANCE_TTL, loader)


async def pick_instance(slug: str) -> Optional[Dict[str, Any]]:
    found = await asyncio.gather(*[get_instance(slug, number) for number in PROBE_NUMBERS])
    for item in found:
        if item is not None and owner_ok(item["owner"]):
            return item
    return None


def feed_card(gift: Dict[str, Any], instance: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slug": gift["slug"],
        "name": gift["name"],
        "icon": gift["icon"],
        "number": instance["number"],
        "owner": instance["owner"]["username"],
        "stars": gift["stars"],
    }


async def load_feed(page: int) -> Dict[str, Any]:
    gifts = await get_catalog()
    start = max(0, (page - 1) * FEED_SIZE)
    window = gifts[start : start + FEED_SIZE]
    limiter = asyncio.Semaphore(5)

    async def one(gift: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with limiter:
            instance = await pick_instance(gift["slug"])
        if instance is None:
            return None
        return feed_card(gift, instance)

    found = await asyncio.gather(*[one(gift) for gift in window])
    return {
        "items": [item for item in found if item is not None],
        "page": page,
        "hasMore": start + FEED_SIZE < len(gifts),
        "total": len(gifts),
    }


async def get_feed(page: int) -> Dict[str, Any]:
    return await CACHE.get("feed:" + str(page), INSTANCE_TTL, lambda: load_feed(page))


async def gift_payload(gift: Dict[str, Any], number: Optional[int]) -> Dict[str, Any]:
    if number is None:
        instance = await pick_instance(gift["slug"])
    else:
        instance = await get_instance(gift["slug"], number)
    owner = (instance or {}).get("owner") or {}
    return {
        "slug": gift["slug"],
        "name": gift["name"],
        "icon": gift["iconLarge"],
        "stars": gift["stars"],
        "upgradeStars": gift["upgradeStars"],
        "total": gift["total"],
        "remaining": gift["remaining"],
        "onSale": gift["onSale"],
        "number": (instance or {}).get("number"),
        "owner": owner.get("username") or "",
        "ownerLink": owner.get("link") or "",
        "date": (instance or {}).get("date") or "",
        "issued": (instance or {}).get("issued") or "",
        "issuedTotal": (instance or {}).get("issuedTotal") or "",
        "pageUrl": (instance or {}).get("pageUrl") or "",
        "visible": bool(owner.get("username")) and owner_ok(owner),
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
    raw = re.sub(r"[^0-9]", "", request.query.get("number", ""))
    number = int(raw) if raw and 0 < int(raw) <= MAX_NUMBER else None
    try:
        return json_response(await gift_payload(gift, number))
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
<title>Подарки</title>
<script src='https://telegram.org/js/telegram-web-app.js'></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#17212b);--text:var(--tg-theme-text-color,#ffffff);--muted:var(--tg-theme-hint-color,#7d8b99);--card:var(--tg-theme-secondary-bg-color,#232e3c);--line:rgba(128,128,128,.2);--link:var(--tg-theme-link-color,#4aa8e8);--btn:var(--tg-theme-button-color,#2f80c2);--btntext:var(--tg-theme-button-text-color,#ffffff)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif}
.wrap{max-width:520px;margin:0 auto;padding:12px 12px 24px}
input{width:100%;padding:10px 12px;border:0;border-radius:10px;background:var(--card);color:var(--text);font-size:15px;outline:none}
.hint{color:var(--muted);font-size:13px;margin:10px 2px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cell{background:var(--card);border-radius:14px;padding:12px 10px;text-align:center}
.cell img{width:86px;height:86px;object-fit:contain;display:block;margin:0 auto 8px}
.cell b{display:block;font-size:14px;font-weight:600}
.cell u{display:block;margin-top:3px;color:var(--link);font-size:13px;text-decoration:none}
.more{width:100%;margin:12px 0 0;padding:11px;border:0;border-radius:12px;background:var(--card);color:var(--text);font-size:14px}
.hide{display:none}
.back{display:inline-block;padding:2px 0 6px;color:var(--link);font-size:15px}
.hero{text-align:center;padding:8px 0 2px}
.hero img{width:132px;height:132px;object-fit:contain}
.title{margin:6px 0 14px;text-align:center;font-size:17px;font-weight:600}
.card{background:var(--card);border-radius:12px;overflow:hidden}
.kv{display:flex;gap:12px;padding:11px 14px;border-bottom:1px solid var(--line);font-size:14px}
.kv:last-child{border-bottom:0}
.kv i{flex:0 0 92px;font-style:normal;color:var(--muted)}
.kv span{flex:1}
.kv a{color:var(--link);text-decoration:none}
.btn{display:block;margin-top:14px;padding:13px;border:0;border-radius:12px;background:var(--btn);color:var(--btntext);font-size:15px;text-align:center;text-decoration:none}
.foot{margin:14px 2px 0;color:var(--muted);font-size:12px;text-align:center}
.foot a{color:var(--link);text-decoration:none}
</style>
</head>
<body>
<div class='wrap'>
<div id='listScreen'>
<input id='search' placeholder='Поиск' autocomplete='off'>
<div class='hint' id='count'>Загружаю</div>
<div class='grid' id='grid'></div>
<button class='more hide' id='more'>Показать ещё</button>
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

function openLink(link) {
  if (!link) { return; }
  if (tg && link.indexOf('https://t.me/') === 0) { tg.openTelegramLink(link); return; }
  if (tg && link.indexOf('tg://') === 0) { tg.openLink(link); return; }
  window.open(link, '_blank');
}

function kv(label, value, link) {
  var row = el('div', 'kv');
  row.appendChild(el('i', null, label));
  if (link) {
    var box = el('span');
    var a = el('a', null, value);
    a.href = '#';
    a.onclick = function (event) { event.preventDefault(); openLink(link); };
    box.appendChild(a);
    row.appendChild(box);
  } else {
    row.appendChild(el('span', null, value));
  }
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
    return item.name.toLowerCase().indexOf(needle) >= 0 || String(item.owner).toLowerCase().indexOf(needle) >= 0;
  });
  shown.forEach(function (item) {
    var cell = el('div', 'cell');
    var img = el('img');
    img.src = item.icon;
    img.alt = '';
    img.loading = 'lazy';
    cell.appendChild(img);
    cell.appendChild(el('b', null, item.name));
    cell.appendChild(el('u', null, '@' + item.owner));
    cell.onclick = function () { openGift(item.slug, item.number); };
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
  if (data.owner) {
    card.appendChild(kv('Владелец', '@' + data.owner, data.ownerLink));
  }
  if (data.number) {
    card.appendChild(kv('Номер', '#' + fmt(data.number)));
  }
  if (data.date) {
    card.appendChild(kv('Дата', data.date));
  }
  if (data.stars !== null && data.stars !== undefined) {
    card.appendChild(kv('Стоимость', stars(data.stars)));
  }
  if (data.total) {
    card.appendChild(kv('Наличие', fmt(data.remaining || 0) + ' из ' + fmt(data.total)));
  } else if (!data.onSale) {
    card.appendChild(kv('Наличие', 'уже не продаётся'));
  }
  box.appendChild(card);
  if (data.ownerLink) {
    var btn = el('a', 'btn', 'Профиль владельца');
    btn.href = '#';
    btn.onclick = function (event) { event.preventDefault(); openLink(data.ownerLink); };
    box.appendChild(btn);
  }
  if (data.pageUrl) {
    var foot = el('div', 'foot');
    var link = el('a', null, 'Посмотреть на t.me');
    link.href = '#';
    link.onclick = function (event) { event.preventDefault(); openLink(data.pageUrl); };
    foot.appendChild(link);
    box.appendChild(foot);
  }
}

function openGift(slug, number) {
  showDetail();
  var box = document.getElementById('detail');
  box.textContent = '';
  box.appendChild(el('div', 'foot', 'Секунду'));
  var url = '/api/gift/' + slug + (number ? '?number=' + number : '');
  fetch(url).then(function (response) { return response.json(); }).then(function (data) {
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
if (wanted) { openGift(String(wanted).replace(/[^a-z0-9]/gi, '').toLowerCase(), ''); }
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


def webapp_available() -> bool:
    return WEBAPP_URL.startswith("https://")


def webapp_link(slug: str = "") -> str:
    if not webapp_available():
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
    if data.get("owner"):
        lines.append("Владелец: @" + escape(str(data["owner"])))
    if data.get("number"):
        lines.append("Номер: #" + group(data["number"]))
    if data.get("date"):
        lines.append("Дата: " + escape(str(data["date"])))
    if data.get("stars") is not None:
        lines.append("Стоимость: ⭐ " + group(data["stars"]))
    if data.get("total"):
        lines.append("Наличие: " + group(data.get("remaining") or 0) + " из " + group(data["total"]))
    elif not data.get("onSale"):
        lines.append("Наличие: уже не продаётся")
    if not data.get("owner"):
        lines.append("Владельца с юзернеймом у этого подарка не нашёл.")
    return "\n".join(lines)


async def reply_gift(message: Message, query: str) -> None:
    gift = await find_gift(query)
    if gift is None:
        await message.answer(
            "Такого подарка не нашёл. Проверь название или открой список.",
            reply_markup=app_markup(),
        )
        return
    try:
        data = await gift_payload(gift, None)
    except Exception:
        await message.answer("Источник молчит, попробуй через минуту.")
        return
    await message.answer(card_text(data), reply_markup=app_markup(gift["slug"], "Посмотреть в приложении"))


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    text = (
        "Привет. Здесь подарки Telegram в обычном, не улучшенном виде — такими, какими их дарят.\n\n"
        "Открой список: иконки идут по две в ряд. Тапни по любой — покажу юзернейм владельца, дату, цену в звёздах и наличие. Маркеты и боты пропускаю, только живые владельцы с юзернеймом."
    )
    await message.answer(text, reply_markup=app_markup("", "Открыть подарки"))


@router.message(Command("app"))
async def on_app(message: Message) -> None:
    markup = app_markup("", "Открыть подарки")
    if markup is None:
        await message.answer("Адрес приложения не задан, поставь WEBAPP_URL.")
        return
    await message.answer("Список подарков:", reply_markup=markup)


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
    while not stop_event.is_set():
        try:
            me = await bot.get_me()
            print("telegram authorized as @" + (me.username or "unknown"), flush=True)
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать"),
                    BotCommand(command="app", description="Список подарков"),
                    BotCommand(command="gift", description="Найти подарок"),
                    BotCommand(command="about", description="Откуда данные"),
                ]
            )
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


BOT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="app", description="Список подарков"),
    BotCommand(command="gift", description="Найти подарок"),
    BotCommand(command="about", description="Откуда данные"),
]
