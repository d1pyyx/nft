import asyncio
import json
import os
import re
import signal
import time
from html import escape, unescape
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

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
FRAGMENT_BASE = "https://nft.fragment.com/gift"
TME_NFT_BASE = "https://t.me/nft"
CREDIT = "Powered by @GiftChanges (api.changes.tg) - thanks to @GiftChanges for this API"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
CATALOG_TTL = 900.0
GIFT_TTL = 900.0
NFT_TTL = 180.0
MAX_NUMBER = 5000000


class UpstreamError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class TtlCache:
    def __init__(self) -> None:
        self._values: Dict[str, Tuple[float, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get(self, key: str, ttl: float, loader: Callable[[], Awaitable[Any]]) -> Any:
        cached = self._values.get(key)
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._values.get(key)
            if cached is not None and time.monotonic() - cached[0] < ttl:
                return cached[1]
            value = await loader()
            self._values[key] = (time.monotonic(), value)
            return value


CACHE = TtlCache()
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=25),
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept-Language": "en",
                    },
                )
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def fetch_json(url: str) -> Any:
    session = await get_session()
    try:
        async with session.get(url) as response:
            if response.status == 404:
                raise UpstreamError(404, "not found")
            if response.status >= 400:
                raise UpstreamError(502, "upstream status " + str(response.status))
            return await response.json(content_type=None)
    except asyncio.TimeoutError:
        raise UpstreamError(504, "upstream timeout")
    except aiohttp.ClientError:
        raise UpstreamError(502, "upstream unavailable")


async def fetch_text(url: str) -> str:
    session = await get_session()
    try:
        async with session.get(url) as response:
            if response.status == 404:
                raise UpstreamError(404, "not found")
            if response.status >= 400:
                raise UpstreamError(502, "upstream status " + str(response.status))
            return await response.text()
    except asyncio.TimeoutError:
        raise UpstreamError(504, "upstream timeout")
    except aiohttp.ClientError:
        raise UpstreamError(502, "upstream unavailable")


async def fetch_json_optional(url: str) -> Optional[Any]:
    try:
        return await fetch_json(url)
    except UpstreamError:
        return None


async def fetch_text_optional(url: str) -> Optional[str]:
    try:
        return await fetch_text(url)
    except UpstreamError:
        return None


def to_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def icon_url(gift_id: str, size: int) -> str:
    return API_BASE + "/original/" + str(gift_id) + ".png?size=" + str(size)


def sorted_by_rarity(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clean = [item for item in items if isinstance(item, dict) and item.get("name")]
    return sorted(clean, key=lambda item: (float(item.get("rarity") or 100.0), str(item.get("name"))))


def format_attribute(item: Optional[Dict[str, Any]]) -> Optional[str]:
    if not item:
        return None
    rarity = item.get("rarity")
    if isinstance(rarity, (int, float)):
        return str(item.get("name")) + " " + ("%g" % rarity) + "%"
    return str(item.get("name"))


async def load_catalog() -> Dict[str, Any]:
    ids, upgradable, totals = await asyncio.gather(
        fetch_json(API_BASE + "/ids"),
        fetch_json(API_BASE + "/gifts"),
        fetch_json(API_BASE + "/total"),
    )
    upgradable_slugs = {to_slug(name) for name in (upgradable or [])}
    gifts: List[Dict[str, Any]] = []
    for index, (gift_id, name) in enumerate(dict(ids or {}).items()):
        slug = to_slug(name)
        gifts.append(
            {
                "name": str(name),
                "slug": slug,
                "id": str(gift_id),
                "order": index,
                "upgradable": slug in upgradable_slugs,
                "icon": icon_url(gift_id, 256),
                "iconLarge": icon_url(gift_id, 512),
                "sticker": API_BASE + "/original/" + str(gift_id) + ".tgs",
            }
        )
    return {"gifts": gifts, "totals": totals, "credit": CREDIT}


async def get_catalog() -> Dict[str, Any]:
    return await CACHE.get("catalog", CATALOG_TTL, load_catalog)


async def find_gift(query: str) -> Optional[Dict[str, Any]]:
    catalog = await get_catalog()
    gifts: List[Dict[str, Any]] = catalog["gifts"]
    raw = str(query).strip()
    needle = to_slug(raw)
    if not needle:
        return None
    for gift in gifts:
        if gift["id"] == raw or gift["slug"] == needle:
            return gift
    starts = [gift for gift in gifts if gift["slug"].startswith(needle)]
    if starts:
        return starts[0]
    contains = [gift for gift in gifts if needle in gift["slug"]]
    if contains:
        return contains[0]
    return None


async def load_gift(slug: str) -> Dict[str, Any]:
    entry = await find_gift(slug)
    if entry is None:
        raise UpstreamError(404, "gift not found")
    data = await fetch_json(API_BASE + "/gift/" + entry["slug"])
    gift = data.get("gift") or {}
    models = sorted_by_rarity(data.get("models") or [])
    backdrops = sorted_by_rarity(data.get("backdrops") or [])
    symbols = sorted_by_rarity(data.get("symbols") or [])
    return {
        "name": str(gift.get("name") or entry["name"]),
        "slug": entry["slug"],
        "id": str(gift.get("id") or entry["id"]),
        "customEmojiId": str(gift.get("customEmojiId") or ""),
        "upgradable": bool(entry["upgradable"]),
        "icon": entry["icon"],
        "iconLarge": entry["iconLarge"],
        "sticker": entry["sticker"],
        "counts": {
            "models": len(models),
            "backdrops": len(backdrops),
            "symbols": len(symbols),
        },
        "rarest": {
            "model": format_attribute(models[0] if models else None),
            "backdrop": format_attribute(backdrops[0] if backdrops else None),
            "symbol": format_attribute(symbols[0] if symbols else None),
        },
        "topModels": [format_attribute(item) for item in models[:8]],
        "pageUrl": TME_NFT_BASE + "/" + entry["slug"] + "-1",
        "credit": CREDIT,
    }


async def get_gift(slug: str) -> Dict[str, Any]:
    key = "gift:" + to_slug(slug)
    return await CACHE.get(key, GIFT_TTL, lambda: load_gift(slug))


ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<(th|td)[^>]*>(.*?)</\1>", re.S | re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(r"\bon\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})")
USERNAME_RE = re.compile(r"^https?://t\.me/([A-Za-z0-9_]{4,32})/?$", re.I)
TGID_RE = re.compile(r"^tg://user\?id=(\d+)", re.I)
RESERVED = {"nft", "share", "iv", "joinchat", "addstickers", "proxy", "socks", "setlanguage"}


def clean_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    text = TAG_RE.sub(" ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_profile(cell_html: str) -> Dict[str, Optional[str]]:
    for raw in HREF_RE.findall(cell_html):
        link = unescape(raw).strip()
        tg_id = TGID_RE.match(link)
        if tg_id:
            return {"link": link, "username": None, "userId": tg_id.group(1)}
        username = USERNAME_RE.match(link)
        if username and username.group(1).lower() not in RESERVED:
            handle = username.group(1)
            return {"link": "https://t.me/" + handle, "username": handle, "userId": None}
    return {"link": None, "username": None, "userId": None}


def parse_nft_page(page: str) -> Dict[str, Any]:
    labels: Dict[str, str] = {}
    notes: List[str] = []
    owner: Dict[str, Optional[str]] = {"name": None, "link": None, "username": None, "userId": None}
    for row in ROW_RE.findall(page or ""):
        cells = CELL_RE.findall(row)
        if len(cells) >= 2:
            label = clean_text(cells[0][1]).rstrip(":").lower()
            value_html = cells[1][1]
            value = clean_text(value_html)
            if not label:
                if value:
                    notes.append(value)
                continue
            if label == "owner":
                profile = extract_profile(value_html)
                owner = {
                    "name": value or None,
                    "link": profile["link"],
                    "username": profile["username"],
                    "userId": profile["userId"],
                }
            else:
                labels[label] = value
        elif len(cells) == 1:
            value = clean_text(cells[0][1])
            if value:
                notes.append(value)
                if owner["link"] is None and "gifted" in value.lower():
                    profile = extract_profile(cells[0][1])
                    if profile["link"]:
                        owner["link"] = profile["link"]
                        owner["username"] = profile["username"]
                        owner["userId"] = profile["userId"]
    issued = None
    for note in notes:
        found = DATE_RE.search(note)
        if found:
            issued = found.group(1)
            break
    return {
        "owner": owner,
        "model": labels.get("model"),
        "backdrop": labels.get("backdrop"),
        "symbol": labels.get("symbol"),
        "quantity": labels.get("quantity"),
        "issued": issued,
        "notes": notes,
    }


async def load_nft(slug: str, number: int) -> Dict[str, Any]:
    entry = await find_gift(slug)
    if entry is None:
        raise UpstreamError(404, "gift not found")
    clean_slug = entry["slug"]
    meta_url = FRAGMENT_BASE + "/" + clean_slug + "-" + str(number) + ".json"
    page_url = TME_NFT_BASE + "/" + clean_slug + "-" + str(number)
    meta, page = await asyncio.gather(
        fetch_json_optional(meta_url),
        fetch_text_optional(page_url),
    )
    parsed = parse_nft_page(page or "")
    has_page = bool(parsed["owner"]["name"] or parsed["quantity"] or parsed["model"])
    if not isinstance(meta, dict) and not has_page:
        raise UpstreamError(404, "collectible not found")
    meta = meta if isinstance(meta, dict) else {}
    attributes = {}
    for item in meta.get("attributes") or []:
        if isinstance(item, dict) and item.get("trait_type"):
            attributes[str(item["trait_type"]).lower()] = str(item.get("value") or "")
    title = str(meta.get("name") or (entry["name"] + " #" + str(number)))
    return {
        "slug": clean_slug,
        "name": entry["name"],
        "number": number,
        "title": title,
        "description": meta.get("description"),
        "image": meta.get("image") or (FRAGMENT_BASE + "/" + clean_slug + "-" + str(number) + ".webp"),
        "lottie": meta.get("lottie"),
        "model": parsed["model"] or attributes.get("model"),
        "backdrop": parsed["backdrop"] or attributes.get("backdrop"),
        "symbol": parsed["symbol"] or attributes.get("symbol"),
        "quantity": parsed["quantity"],
        "issued": parsed["issued"],
        "history": parsed["notes"],
        "owner": parsed["owner"],
        "pageUrl": page_url,
        "credit": CREDIT,
    }


async def get_nft(slug: str, number: int) -> Dict[str, Any]:
    key = "nft:" + to_slug(slug) + ":" + str(number)
    return await CACHE.get(key, NFT_TTL, lambda: load_nft(slug, number))


def owner_display(owner: Dict[str, Any]) -> str:
    name = owner.get("name")
    username = owner.get("username")
    user_id = owner.get("userId")
    if name and username:
        return name + " (@" + username + ")"
    if username:
        return "@" + username
    if name and user_id:
        return name + " (ID " + str(user_id) + ")"
    if user_id:
        return "ID " + str(user_id)
    if name:
        return name
    return "скрыт"


def webapp_available() -> bool:
    return WEBAPP_URL.startswith("https://")


def webapp_link(slug: Optional[str] = None) -> str:
    if slug:
        return WEBAPP_URL + "?gift=" + slug
    return WEBAPP_URL


def app_inline_markup(slug: Optional[str] = None) -> Optional[InlineKeyboardMarkup]:
    if not webapp_available():
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть каталог",
                    web_app=WebAppInfo(url=webapp_link(slug)),
                )
            ]
        ]
    )


def app_reply_markup() -> Optional[ReplyKeyboardMarkup]:
    if not webapp_available():
        return None
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Каталог", web_app=WebAppInfo(url=webapp_link()))]],
        resize_keyboard=True,
        is_persistent=True,
    )


router = Router()


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    text = (
        "Привет. Здесь все подарки Telegram в обычном виде — такие, какими их присылают, без прокачки.\n\n"
        "Открой каталог: там список и поиск, тапни по подарку — увидишь его карточку. "
        "Знаешь номер экземпляра — впиши его там же, и получишь дату выпуска, тираж и владельца.\n\n"
        "Можно и текстом:\n"
        "/app — каталог\n"
        "/gifts — сколько всего подарков\n"
        "/gift plush pepe — карточка подарка\n"
        "/nft plushpepe 1 — владелец экземпляра\n\n"
        "<i>" + escape(CREDIT) + "</i>"
    )
    if not webapp_available():
        text += "\n\nКнопки каталога пока нет: нужен адрес приложения в WEBAPP_URL."
    reply_markup = app_reply_markup()
    if reply_markup is not None:
        await message.answer(text, reply_markup=reply_markup)
        return
    await message.answer(text)


@router.message(Command("app"))
async def on_app(message: Message) -> None:
    markup = app_inline_markup()
    if markup is None:
        await message.answer("Адрес приложения не задан, открывать нечего.")
        return
    await message.answer("Каталог здесь:", reply_markup=markup)


@router.message(Command("about"))
async def on_about(message: Message) -> None:
    await message.answer(
        "Подарки, модели, фоны и узоры беру с api.changes.tg. "
        "Владельцев и тиражи — со страниц t.me/nft и nft.fragment.com.\n\n"
        "<i>" + escape(CREDIT) + "</i>"
    )


@router.message(Command("gifts"))
async def on_gifts(message: Message) -> None:
    try:
        catalog = await get_catalog()
    except UpstreamError as error:
        await message.answer("API сейчас не отвечает: " + escape(error.message))
        return
    totals = catalog.get("totals") or {}
    gifts_total = totals.get("gifts") or {}
    lines = [
        "Всего подарков: <b>" + str(gifts_total.get("total", "—")) + "</b>",
        "Лимитированных: <b>" + str(gifts_total.get("limited", "—")) + "</b>",
        "Безлимитных: <b>" + str(gifts_total.get("unlimited", "—")) + "</b>",
        "В каталоге приложения: <b>" + str(len(catalog.get("gifts") or [])) + "</b>",
        "",
        "Моделей: <b>" + str(totals.get("models", "—")) + "</b>",
        "Фонов: <b>" + str(totals.get("backdrops", "—")) + "</b>",
        "Узоров: <b>" + str(totals.get("patterns", "—")) + "</b>",
    ]
    await message.answer("\n".join(lines), reply_markup=app_inline_markup())


async def send_gift_card(message: Message, query: str) -> None:
    try:
        data = await get_gift(query)
    except UpstreamError as error:
        if error.status == 404:
            await message.answer("Не нашёл такой подарок. Попробуй так: <code>/gift plush pepe</code>")
        else:
            await message.answer("API сейчас не отвечает: " + escape(error.message))
        return
    rarest = data["rarest"]
    lines = [
        "<b>" + escape(data["name"]) + "</b>",
        "Обычный вид, без прокачки.",
        "",
        "ID: <code>" + escape(data["id"]) + "</code>",
    ]
    if rarest["model"]:
        lines.append("Самая редкая модель: " + escape(str(rarest["model"])))
    lines.append("")
    lines.append("Владельца конкретного экземпляра узнаешь так: <code>/nft " + escape(data["slug"]) + " 1</code>")
    caption = "\n".join(lines)
    markup = app_inline_markup(data["slug"])
    try:
        await message.answer_photo(photo=data["iconLarge"], caption=caption, reply_markup=markup)
    except Exception:
        await message.answer(caption, reply_markup=markup)


@router.message(Command("gift"))
async def on_gift(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напиши название, например: <code>/gift scared cat</code>")
        return
    await send_gift_card(message, query)


@router.message(Command("nft"))
async def on_nft(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    parsed = re.match(r"^(.*?)[\\s\\-_#]+(\\d+)$", raw)
    if not parsed:
        await message.answer("Нужны название и номер, например: <code>/nft scaredcat 1</code>")
        return
    query = parsed.group(1)
    number = int(parsed.group(2))
    if number < 1 or number > MAX_NUMBER:
        await message.answer("Такого номера не бывает.")
        return
    gift = await find_gift(query)
    if gift is None:
        await message.answer("Не нашёл такой подарок.")
        return
    try:
        data = await get_nft(gift["slug"], number)
    except UpstreamError as error:
        if error.status == 404:
            await message.answer("Экземпляра #" + str(number) + " нет.")
        else:
            await message.answer("Источник не отвечает: " + escape(error.message))
        return
    owner = data["owner"]
    lines = ["<b>" + escape(data["title"]) + "</b>"]
    if data["issued"]:
        lines.append("Выпущен: " + escape(str(data["issued"])))
    if data["quantity"]:
        lines.append("Тираж: " + escape(str(data["quantity"])))
    lines.append("Сейчас у: " + escape(owner_display(owner)))
    if data["model"]:
        lines.append("Модель: " + escape(str(data["model"])))
    if data["backdrop"]:
        lines.append("Фон: " + escape(str(data["backdrop"])))
    if data["symbol"]:
        lines.append("Узор: " + escape(str(data["symbol"])))
    buttons: List[List[InlineKeyboardButton]] = []
    if owner.get("username"):
        buttons.append([InlineKeyboardButton(text="Профиль владельца", url="https://t.me/" + str(owner["username"]))])
    elif owner.get("userId"):
        buttons.append([InlineKeyboardButton(text="Профиль владельца", url="tg://user?id=" + str(owner["userId"]))])
    buttons.append([InlineKeyboardButton(text="Страница на t.me", url=data["pageUrl"])])
    if webapp_available():
        buttons.append([InlineKeyboardButton(text="Открыть каталог", web_app=WebAppInfo(url=webapp_link(data["slug"])))])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    raw = (message.text or "").strip()
    if not raw:
        return
    parsed = re.match(r"^(.*?)[\\s\\-_#]+(\\d+)$", raw)
    if parsed and to_slug(parsed.group(1)):
        await on_nft(message, CommandObject(command="nft", args=raw))
        return
    await send_gift_card(message, raw)


MINI_APP_HTML = r"""<!doctype html>
<html lang='ru'>
<head>
<meta charset='utf-8' />
<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover' />
<title>Подарки</title>
<script src='https://telegram.org/js/telegram-web-app.js'></script>
<style>
*,*::before,*::after{box-sizing:border-box}
:root{
--bg:var(--tg-theme-bg-color,#ffffff);
--text:var(--tg-theme-text-color,#000000);
--muted:var(--tg-theme-hint-color,#8b8b8b);
--line:rgba(128,128,128,.22);
--link:var(--tg-theme-link-color,#2481cc);
}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
button,input{font:inherit;color:inherit;background:none;border:0;margin:0;padding:0}
.wrap{max-width:620px;margin:0 auto;padding:0 20px calc(28px + env(safe-area-inset-bottom))}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:14px 0 4px}
#search{width:100%;padding:10px 0;border-bottom:1px solid var(--line);outline:none}
#search::placeholder{color:var(--muted)}
#count{padding:12px 0;color:var(--muted);font-size:13px}
.row{display:flex;align-items:center;gap:14px;width:100%;padding:11px 0;border-top:1px solid var(--line);text-align:left;cursor:pointer}
.row img{flex:0 0 auto;width:44px;height:44px;object-fit:contain}
.row span{min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.hide{display:none}
.back{padding:14px 0;color:var(--muted);font-size:14px;cursor:pointer}
.hero{display:flex;flex-direction:column;align-items:center;gap:8px;padding:4px 0 20px}
.hero img{width:176px;height:176px;object-fit:contain}
.hero h1{margin:0;font-size:20px;font-weight:600;text-align:center}
.hero p{margin:0;color:var(--muted);font-size:13px}
.kv{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-top:1px solid var(--line);font-size:15px}
.kv b{font-weight:400;color:var(--muted)}
.kv span{min-width:0;text-align:right;overflow-wrap:anywhere}
.ask{padding:22px 0 0}
.ask p{margin:0 0 6px;color:var(--muted);font-size:13px}
.ask div{display:flex;align-items:center;gap:14px}
.ask input{flex:1 1 auto;padding:10px 0;border-bottom:1px solid var(--line);outline:none}
.ask button{flex:0 0 auto;color:var(--link);cursor:pointer}
.msg{padding:14px 0 0;color:var(--muted);font-size:14px}
a{color:var(--link);text-decoration:none}
.foot{padding:26px 0 0;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<div class='wrap'>
<div id='listScreen'>
<div class='bar'><input id='search' type='search' placeholder='Поиск' autocomplete='off' spellcheck='false' /></div>
<div id='count'>Загружаю…</div>
<div id='list'></div>
<div class='foot'>Данные: api.changes.tg (@GiftChanges)</div>
</div>
<div id='detailScreen' class='hide'>
<div class='back' id='back'>← Назад</div>
<div id='detail'></div>
</div>
</div>
<script>
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const listScreen = document.getElementById('listScreen');
const detailScreen = document.getElementById('detailScreen');
const listEl = document.getElementById('list');
const countEl = document.getElementById('count');
const searchEl = document.getElementById('search');
const detailEl = document.getElementById('detail');
const backEl = document.getElementById('back');
let gifts = [];
let current = null;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function kv(label, value) {
  const row = el('div', 'kv');
  row.appendChild(el('b', null, label));
  row.appendChild(el('span', null, value));
  return row;
}

function slugify(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function plural(n, one, few, many) {
  const d = n % 10;
  const h = n % 100;
  if (d === 1 && h !== 11) return one;
  if (d >= 2 && d <= 4 && (h < 10 || h >= 20)) return few;
  return many;
}

function openList() {
  current = null;
  detailScreen.classList.add('hide');
  listScreen.classList.remove('hide');
  if (tg && tg.BackButton) tg.BackButton.hide();
  window.scrollTo(0, 0);
}

function openDetailScreen() {
  listScreen.classList.add('hide');
  detailScreen.classList.remove('hide');
  if (tg && tg.BackButton) tg.BackButton.show();
  window.scrollTo(0, 0);
}

function renderList() {
  const raw = searchEl.value.trim();
  const needle = raw.toLowerCase();
  const slugNeedle = slugify(raw);
  const items = raw
    ? gifts.filter(function (gift) {
        return gift.name.toLowerCase().indexOf(needle) >= 0 || (slugNeedle && gift.slug.indexOf(slugNeedle) >= 0);
      })
    : gifts;
  if (!gifts.length) {
    countEl.textContent = '';
  } else if (!items.length) {
    countEl.textContent = 'Ничего не нашлось';
  } else {
    countEl.textContent = items.length + ' ' + plural(items.length, 'подарок', 'подарка', 'подарков');
  }
  const frag = document.createDocumentFragment();
  items.forEach(function (gift) {
    const row = el('button', 'row');
    row.type = 'button';
    row.dataset.slug = gift.slug;
    const img = el('img');
    img.src = gift.icon;
    img.alt = '';
    img.loading = 'lazy';
    row.appendChild(img);
    row.appendChild(el('span', null, gift.name));
    frag.appendChild(row);
  });
  listEl.textContent = '';
  listEl.appendChild(frag);
}

function renderDetail(gift) {
  detailEl.textContent = '';
  const hero = el('div', 'hero');
  const img = el('img');
  img.src = gift.iconLarge || gift.icon;
  img.alt = '';
  hero.appendChild(img);
  hero.appendChild(el('h1', null, gift.name));
  hero.appendChild(el('p', null, 'обычный вид, без прокачки'));
  detailEl.appendChild(hero);
  detailEl.appendChild(kv('ID', gift.id));
  const ask = el('div', 'ask');
  ask.appendChild(el('p', null, 'Знаешь номер экземпляра? Покажу, когда его выпустили и у кого он сейчас.'));
  const line = el('div');
  const input = el('input');
  input.id = 'num';
  input.type = 'number';
  input.min = '1';
  input.inputMode = 'numeric';
  input.placeholder = 'Номер';
  const button = el('button', null, 'Найти');
  button.type = 'button';
  line.appendChild(input);
  line.appendChild(button);
  ask.appendChild(line);
  detailEl.appendChild(ask);
  const box = el('div', 'msg');
  detailEl.appendChild(box);
  detailEl.appendChild(el('div', 'foot', 'Данные: api.changes.tg (@GiftChanges)'));
  button.addEventListener('click', function () {
    lookupNumber(input.value, box);
  });
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') lookupNumber(input.value, box);
  });
}

async function openGift(slug) {
  openDetailScreen();
  detailEl.textContent = '';
  detailEl.appendChild(el('div', 'msg', 'Открываю…'));
  try {
    const response = await fetch('/api/gift/' + encodeURIComponent(slug));
    const data = await response.json();
    if (!response.ok) throw new Error('fail');
    current = data;
    renderDetail(data);
  } catch (error) {
    detailEl.textContent = '';
    detailEl.appendChild(el('div', 'msg', 'Не получилось загрузить. Попробуй ещё раз.'));
  }
}

async function lookupNumber(value, box) {
  if (!current) return;
  const number = parseInt(String(value).replace(/[^0-9]+/g, ''), 10);
  if (!number || number < 1) {
    box.className = 'msg';
    box.textContent = 'Введи номер цифрами.';
    return;
  }
  box.className = 'msg';
  box.textContent = 'Смотрю…';
  try {
    const response = await fetch('/api/nft/' + encodeURIComponent(current.slug) + '/' + number);
    const data = await response.json();
    if (!response.ok) throw new Error('fail');
    box.className = '';
    box.textContent = '';
    box.appendChild(kv('Номер', '#' + data.number));
    if (data.issued) box.appendChild(kv('Выпущен', data.issued));
    if (data.quantity) box.appendChild(kv('Тираж', data.quantity));
    const owner = data.owner || {};
    const label = owner.username ? '@' + owner.username : owner.name || (owner.userId ? 'ID ' + owner.userId : 'скрыт');
    const link = owner.link || (owner.username ? 'https://t.me/' + owner.username : owner.userId ? 'tg://user?id=' + owner.userId : '');
    const row = el('div', 'kv');
    row.appendChild(el('b', null, 'Сейчас у'));
    const cell = el('span');
    if (link) {
      const anchor = el('a', null, label);
      anchor.href = link;
      anchor.addEventListener('click', function (event) {
        event.preventDefault();
        if (tg && tg.openTelegramLink && link.indexOf('https://t.me/') === 0) tg.openTelegramLink(link);
        else window.open(link, '_blank');
      });
      cell.appendChild(anchor);
    } else {
      cell.textContent = label;
    }
    row.appendChild(cell);
    box.appendChild(row);
    if (data.model) box.appendChild(kv('Модель', data.model));
    if (data.backdrop) box.appendChild(kv('Фон', data.backdrop));
    if (data.symbol) box.appendChild(kv('Узор', data.symbol));
  } catch (error) {
    box.className = 'msg';
    box.textContent = 'Такого экземпляра нет или Telegram его не отдал.';
  }
}

listEl.addEventListener('click', function (event) {
  const row = event.target.closest('.row');
  if (!row) return;
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  openGift(row.dataset.slug);
});

backEl.addEventListener('click', openList);
searchEl.addEventListener('input', renderList);

async function boot() {
  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.BackButton) tg.BackButton.onClick(openList);
  }
  try {
    const response = await fetch('/api/catalog');
    const data = await response.json();
    if (!response.ok) throw new Error('fail');
    gifts = (data.gifts || []).slice().sort(function (a, b) {
      return String(a.name).localeCompare(String(b.name), 'ru');
    });
    renderList();
  } catch (error) {
    countEl.textContent = '';
    listEl.textContent = '';
    listEl.appendChild(el('div', 'msg', 'Каталог не загрузился. Закрой и открой приложение заново.'));
    return;
  }
  const params = new URLSearchParams(window.location.search);
  let target = params.get('gift') || '';
  if (!target && tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) target = tg.initDataUnsafe.start_param;
  if (target) {
    const needle = slugify(target);
    const found = gifts.filter(function (gift) {
      return gift.slug === needle || gift.id === target;
    })[0];
    if (found) openGift(found.slug);
  }
}

boot();
</script>
</body>
</html>
"""


def json_response(payload: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False),
        status=status,
        content_type="application/json",
        headers={"X-Attribution": CREDIT, "Cache-Control": "no-store"},
    )


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(
        text=MINI_APP_HTML,
        content_type="text/html",
        charset="utf-8",
        headers={"X-Attribution": CREDIT},
    )


async def handle_catalog(request: web.Request) -> web.Response:
    try:
        catalog = await get_catalog()
    except UpstreamError as error:
        return json_response({"error": error.message}, error.status)
    return json_response(catalog)


async def handle_gift(request: web.Request) -> web.Response:
    slug = to_slug(request.match_info.get("slug", ""))
    if not slug:
        return json_response({"error": "empty slug"}, 400)
    try:
        data = await get_gift(slug)
    except UpstreamError as error:
        return json_response({"error": error.message}, error.status)
    return json_response(data)


async def handle_nft(request: web.Request) -> web.Response:
    slug = to_slug(request.match_info.get("slug", ""))
    raw_number = re.sub(r"[^0-9]", "", request.match_info.get("number", ""))
    if not slug or not raw_number:
        return json_response({"error": "bad request"}, 400)
    number = int(raw_number)
    if number < 1 or number > MAX_NUMBER:
        return json_response({"error": "number out of range"}, 400)
    try:
        data = await get_nft(slug, number)
    except UpstreamError as error:
        return json_response({"error": error.message}, error.status)
    return json_response(data)


async def handle_credits(request: web.Request) -> web.Response:
    return web.Response(text=CREDIT + "\n", content_type="text/plain", charset="utf-8")


async def handle_health(request: web.Request) -> web.Response:
    return json_response({"status": "ok", "webapp": WEBAPP_URL or None, "credit": CREDIT})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/catalog", handle_catalog)
    app.router.add_get("/api/gift/{slug}", handle_gift)
    app.router.add_get("/api/nft/{slug}/{number}", handle_nft)
    app.router.add_get("/credits", handle_credits)
    app.router.add_get("/health", handle_health)
    return app


async def start_bot(bot: Bot, dispatcher: Dispatcher) -> None:
    delay = 5
    while True:
        try:
            me = await bot.get_me()
            print("telegram authorized as @" + str(me.username))
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начало"),
                    BotCommand(command="app", description="Открыть каталог"),
                    BotCommand(command="gifts", description="Статистика"),
                    BotCommand(command="gift", description="Карточка подарка"),
                    BotCommand(command="nft", description="Владелец экземпляра"),
                    BotCommand(command="about", description="Источник данных"),
                ]
            )
            await bot.delete_webhook(drop_pending_updates=True)
            await dispatcher.start_polling(bot, handle_signals=False)
            return
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError:
            print("telegram rejected BOT_TOKEN, mini app keeps running without the bot")
            return
        except Exception as error:
            print("telegram error: " + repr(error) + ", retry in " + str(delay) + "s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def run() -> None:
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    print("mini app listening on http://" + HOST + ":" + str(PORT))
    print("webapp url: " + (WEBAPP_URL or "not set"))
    print(CREDIT)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        found = getattr(signal, name, None)
        if found is None:
            continue
        try:
            loop.add_signal_handler(found, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    task = asyncio.create_task(start_bot(bot, dispatcher))
    try:
        await stop_event.wait()
    finally:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        await runner.cleanup()
        await bot.session.close()
        await close_session()


def main() -> None:
    if not BOT_TOKEN:
        print("BOT_TOKEN is required")
        return
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
