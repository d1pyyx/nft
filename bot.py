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
DEFAULT_WEBAPP_URL = "https://nft-production-6950.up.railway.app"
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
                    text="🎁 Открыть каталог",
                    web_app=WebAppInfo(url=webapp_link(slug)),
                )
            ]
        ]
    )


def app_reply_markup() -> Optional[ReplyKeyboardMarkup]:
    if not webapp_available():
        return None
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎁 Каталог подарков", web_app=WebAppInfo(url=webapp_link()))]],
        resize_keyboard=True,
        is_persistent=True,
    )


router = Router()


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    text = (
        "🎁 <b>Gift Changes Explorer</b>\n\n"
        "Каталог всех подарков Telegram в непрокачанном виде: оригинальная иконка, ID, "
        "модели, фоны и узоры. Открой мини-приложение, найди подарок в списке и укажи номер "
        "коллекционного — увидишь дату выпуска, тираж и владельца с переходом в профиль.\n\n"
        "<b>Команды</b>\n"
        "/app — открыть мини-приложение\n"
        "/gifts — статистика каталога\n"
        "/gift Scared Cat — карточка подарка\n"
        "/nft scaredcat 1 — владелец коллекционного\n"
        "/about — источник данных\n\n"
        "<i>" + escape(CREDIT) + "</i>"
    )
    if not webapp_available():
        text += "\n\n⚠️ Задай переменную <code>WEBAPP_URL</code> (https), чтобы включить кнопку приложения."
    reply_markup = app_reply_markup()
    if reply_markup is not None:
        await message.answer(text, reply_markup=reply_markup)
        markup = app_inline_markup()
        if markup is not None:
            await message.answer("Каталог открывается здесь 👇", reply_markup=markup)
        return
    await message.answer(text)


@router.message(Command("app"))
async def on_app(message: Message) -> None:
    markup = app_inline_markup()
    if markup is None:
        await message.answer("WEBAPP_URL не задан, мини-приложение недоступно.")
        return
    await message.answer("Каталог непрокачанных подарков 👇", reply_markup=markup)


@router.message(Command("about"))
async def on_about(message: Message) -> None:
    await message.answer(
        "Данные о подарках, моделях, фонах и узорах: <b>api.changes.tg</b>\n"
        "Владельцы и тиражи коллекционных: <b>t.me/nft</b> и <b>nft.fragment.com</b>\n\n"
        "<i>" + escape(CREDIT) + "</i>"
    )


@router.message(Command("gifts"))
async def on_gifts(message: Message) -> None:
    try:
        catalog = await get_catalog()
    except UpstreamError as error:
        await message.answer("API недоступен: " + escape(error.message))
        return
    totals = catalog.get("totals") or {}
    gifts_total = totals.get("gifts") or {}
    lines = [
        "📊 <b>Каталог Gift Changes</b>",
        "Подарков всего: <b>" + str(gifts_total.get("total", "—")) + "</b>",
        "Прокачиваемых: <b>" + str(gifts_total.get("upgradable", "—")) + "</b>",
        "Лимитированных: <b>" + str(gifts_total.get("limited", "—")) + "</b>",
        "Безлимитных: <b>" + str(gifts_total.get("unlimited", "—")) + "</b>",
        "Моделей: <b>" + str(totals.get("models", "—")) + "</b>",
        "Фонов: <b>" + str(totals.get("backdrops", "—")) + "</b>",
        "Узоров: <b>" + str(totals.get("patterns", "—")) + "</b>",
        "В приложении доступно карточек: <b>" + str(len(catalog.get("gifts") or [])) + "</b>",
        "",
        "<i>" + escape(CREDIT) + "</i>",
    ]
    await message.answer("\n".join(lines), reply_markup=app_inline_markup())


async def send_gift_card(message: Message, query: str) -> None:
    try:
        data = await get_gift(query)
    except UpstreamError as error:
        if error.status == 404:
            await message.answer("Подарок не найден. Попробуй, например: <code>/gift plush pepe</code>")
        else:
            await message.answer("API недоступен: " + escape(error.message))
        return
    counts = data["counts"]
    rarest = data["rarest"]
    lines = [
        "🎁 <b>" + escape(data["name"]) + "</b>",
        "Состояние: <b>непрокачанный оригинал</b>",
        "NFT-версия: <b>" + ("есть" if data["upgradable"] else "нет") + "</b>",
        "",
        "Моделей: <b>" + str(counts["models"]) + "</b> · Фонов: <b>" + str(counts["backdrops"]) + "</b> · Узоров: <b>" + str(counts["symbols"]) + "</b>",
    ]
    if rarest["model"]:
        lines.append("Редчайшая модель: <b>" + escape(str(rarest["model"])) + "</b>")
    if rarest["backdrop"]:
        lines.append("Редчайший фон: <b>" + escape(str(rarest["backdrop"])) + "</b>")
    if rarest["symbol"]:
        lines.append("Редчайший узор: <b>" + escape(str(rarest["symbol"])) + "</b>")
    lines.append("")
    lines.append("Gift ID: <code>" + escape(data["id"]) + "</code>")
    if data["customEmojiId"]:
        lines.append("Custom emoji ID: <code>" + escape(data["customEmojiId"]) + "</code>")
    lines.append("Владелец экземпляра: <code>/nft " + escape(data["slug"]) + " 1</code>")
    lines.append("")
    lines.append("<i>" + escape(CREDIT) + "</i>")
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
        await message.answer("Укажи название: <code>/gift scared cat</code>")
        return
    await send_gift_card(message, query)


@router.message(Command("nft"))
async def on_nft(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    parsed = re.match(r"^(.*?)[\s\-_#]+(\d+)$", raw)
    if not parsed:
        await message.answer("Формат: <code>/nft scaredcat 1</code>")
        return
    query = parsed.group(1)
    number = int(parsed.group(2))
    if number < 1 or number > MAX_NUMBER:
        await message.answer("Номер вне диапазона.")
        return
    gift = await find_gift(query)
    if gift is None:
        await message.answer("Подарок не найден.")
        return
    try:
        data = await get_nft(gift["slug"], number)
    except UpstreamError as error:
        if error.status == 404:
            await message.answer("Экземпляр #" + str(number) + " не найден.")
        else:
            await message.answer("Источник недоступен: " + escape(error.message))
        return
    owner = data["owner"]
    lines = [
        "🖼 <b>" + escape(data["title"]) + "</b>",
        "Номер: <b>#" + str(data["number"]) + "</b>",
    ]
    if data["issued"]:
        lines.append("Выпущен: <b>" + escape(str(data["issued"])) + "</b>")
    if data["quantity"]:
        lines.append("Тираж: <b>" + escape(str(data["quantity"])) + "</b>")
    lines.append("Владелец: <b>" + escape(owner_display(owner)) + "</b>")
    if data["model"]:
        lines.append("Модель: <b>" + escape(str(data["model"])) + "</b>")
    if data["backdrop"]:
        lines.append("Фон: <b>" + escape(str(data["backdrop"])) + "</b>")
    if data["symbol"]:
        lines.append("Узор: <b>" + escape(str(data["symbol"])) + "</b>")
    lines.append("")
    lines.append("<i>" + escape(CREDIT) + "</i>")
    buttons: List[List[InlineKeyboardButton]] = []
    if owner.get("username"):
        buttons.append([InlineKeyboardButton(text="👤 Профиль владельца", url="https://t.me/" + str(owner["username"]))])
    elif owner.get("userId"):
        buttons.append([InlineKeyboardButton(text="👤 Профиль владельца", url="tg://user?id=" + str(owner["userId"]))])
    buttons.append([InlineKeyboardButton(text="🔗 Открыть NFT", url=data["pageUrl"])])
    if webapp_available():
        buttons.append([InlineKeyboardButton(text="🎁 Открыть каталог", web_app=WebAppInfo(url=webapp_link(data["slug"])))])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    raw = (message.text or "").strip()
    if not raw:
        return
    parsed = re.match(r"^(.*?)[\s\-_#]+(\d+)$", raw)
    if parsed and to_slug(parsed.group(1)):
        await on_nft(message, CommandObject(command="nft", args=raw))
        return
    await send_gift_card(message, raw)


MINI_APP_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>Подарки Telegram</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box}
:root{
--bg:var(--tg-theme-bg-color,#ffffff);
--surface:var(--tg-theme-secondary-bg-color,#f9f8f7);
--raised:#f0efed;
--text:var(--tg-theme-text-color,#2c2c2b);
--muted:var(--tg-theme-hint-color,#7d7a75);
--accent:var(--tg-theme-link-color,#2783de);
--accent-soft:#e5f2fc;
--border:#e6e5e3;
--good:#46a171;
--warn:#d5803b;
}
@media (prefers-color-scheme:dark){
:root{
--bg:var(--tg-theme-bg-color,#191919);
--surface:var(--tg-theme-secondary-bg-color,#202020);
--raised:#383836;
--text:var(--tg-theme-text-color,#ffffff);
--muted:var(--tg-theme-hint-color,rgba(255,255,255,.65));
--accent:var(--tg-theme-link-color,#5e9fe8);
--accent-soft:rgba(94,159,232,.14);
--border:rgba(255,255,255,.2);
--good:#72bc8f;
--warn:#de9255;
}
}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
button,input{font:inherit;color:inherit}
.wrap{max-width:720px;margin:0 auto;padding:0 16px calc(32px + env(safe-area-inset-bottom))}
.topbar{position:sticky;top:0;z-index:20;background:var(--bg);padding:16px 0 12px;border-bottom:1px solid var(--border)}
.title{margin:0;font-size:22px;font-weight:600;letter-spacing:-.01em}
.sub{margin-top:4px;color:var(--muted);font-size:14px}
.search{margin-top:16px;position:relative}
.search input{width:100%;min-height:44px;padding:10px 14px;border-radius:12px;border:1px solid var(--border);background:var(--surface);outline:none}
.search input:focus{border-color:var(--accent)}
.chips{display:flex;gap:8px;margin-top:12px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:0 0 auto;min-height:40px;padding:8px 14px;border-radius:10px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-size:14px}
.chip.on{background:var(--accent-soft);border-color:transparent;color:var(--accent);font-weight:600}
.countline{margin-top:12px;color:var(--muted);font-size:14px}
.list{display:grid;gap:8px;padding-top:16px}
.card{display:flex;align-items:center;gap:8px;border:1px solid var(--border);border-radius:12px;background:var(--surface);overflow:hidden}
.card-main{flex:1 1 auto;display:flex;align-items:center;gap:12px;min-height:76px;padding:12px;border:0;background:transparent;text-align:left;cursor:pointer}
.thumb{flex:0 0 auto;width:56px;height:56px;border-radius:12px;background:var(--raised);display:flex;align-items:center;justify-content:center;overflow:hidden}
.thumb img{width:52px;height:52px;object-fit:contain;display:block}
.thumb-empty::after{content:"🎁";font-size:24px}
.card-body{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:2px}
.card-title{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card-meta{color:var(--muted);font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chev{flex:0 0 auto;color:var(--muted);font-size:22px;line-height:1}
.fav{flex:0 0 auto;width:44px;height:44px;margin-right:8px;border:0;border-radius:10px;background:transparent;color:var(--muted);font-size:20px;cursor:pointer}
.fav.on{color:var(--warn)}
.skel{height:76px;border-radius:12px;border:1px solid var(--border);background:var(--surface)}
.empty,.loading,.error,.notice{padding:24px 16px;text-align:center;color:var(--muted);font-size:14px;border:1px solid var(--border);border-radius:12px;background:var(--surface)}
.error{color:#e56458}
.notice{text-align:left}
.hidden{display:none}
.backrow{position:sticky;top:0;z-index:20;background:var(--bg);padding:12px 0;border-bottom:1px solid var(--border)}
.back{min-height:44px;padding:8px 12px 8px 4px;border:0;background:transparent;color:var(--accent);font-size:16px;cursor:pointer}
.hero{padding:24px 0 8px;text-align:center}
.hero-art{width:128px;height:128px;margin:0 auto;border-radius:16px;background:var(--surface);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;overflow:hidden}
.hero-art img{width:112px;height:112px;object-fit:contain}
.hero-title{margin:16px 0 4px;font-size:26px;font-weight:600;letter-spacing:-.02em}
.hero-sub{color:var(--muted);font-size:14px}
.badges{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:12px}
.badge{padding:4px 10px;border-radius:8px;background:var(--surface);border:1px solid var(--border);color:var(--muted);font-size:13px}
.badge.ok{background:var(--accent-soft);border-color:transparent;color:var(--accent)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:24px}
.stat{padding:12px 8px;border:1px solid var(--border);border-radius:12px;background:var(--surface);text-align:center}
.stat-num{font-size:20px;font-weight:600}
.stat-label{margin-top:2px;color:var(--muted);font-size:14px}
.block{margin-top:24px}
.block-title{font-size:14px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}
.kvs{border:1px solid var(--border);border-radius:12px;background:var(--surface);overflow:hidden}
.kv{display:flex;align-items:center;gap:12px;padding:12px;border-top:1px solid var(--border);width:100%;background:transparent;border-left:0;border-right:0;border-bottom:0;text-align:left}
.kv:first-child{border-top:0}
.k{flex:0 0 40%;color:var(--muted);font-size:14px}
.v{flex:1 1 auto;min-width:0;font-size:15px;word-break:break-word}
.kv.tap{cursor:pointer}
.kv.tap .v{color:var(--accent);font-weight:600}
.lookup{border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:12px}
.lookup-row{display:flex;gap:8px;margin-top:8px}
.lookup-row input{flex:1 1 auto;min-width:0;min-height:44px;padding:10px 12px;border-radius:10px;border:1px solid var(--border);background:var(--bg);outline:none}
.lookup-row input:focus{border-color:var(--accent)}
.lab{color:var(--muted);font-size:14px}
.btn{min-height:44px;padding:10px 16px;border-radius:10px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}
.btn.primary{background:var(--accent);border-color:transparent;color:#fff}
.btn.wide{width:100%}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.nft{margin-top:12px;border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:12px}
.nft-art{width:100%;max-width:220px;margin:0 auto 12px;aspect-ratio:1/1;border-radius:12px;overflow:hidden;background:var(--raised);display:flex;align-items:center;justify-content:center}
.nft-art img{width:100%;height:100%;object-fit:cover;display:block}
.nft-title{font-size:18px;font-weight:600;margin-bottom:12px;text-align:center}
.hint{margin-top:8px;color:var(--muted);font-size:14px}
.credit{margin-top:32px;padding-top:16px;border-top:1px solid var(--border);color:var(--muted);font-size:14px;text-align:center}
.credit a{color:var(--accent);text-decoration:none}
@media (min-width:560px){
.list{grid-template-columns:1fr 1fr}
.stats{gap:12px}
}
</style>
</head>
<body>
<div class="wrap">
<div id="listScreen">
<header class="topbar">
<h1 class="title">Подарки Telegram</h1>
<div class="sub" id="summary">загрузка каталога…</div>
<div class="search"><input id="search" type="search" placeholder="Поиск: plush pepe, ID, scaredcat" autocomplete="off" /></div>
<div class="chips" id="filters">
<button class="chip on" type="button" data-filter="all">Все</button>
<button class="chip" type="button" data-filter="fav">★ Избранные</button>
<button class="chip" type="button" data-filter="nft">С NFT</button>
</div>
<div class="chips" id="sorts">
<button class="chip on" type="button" data-sort="catalog">По каталогу</button>
<button class="chip" type="button" data-sort="name">А-Я</button>
<button class="chip" type="button" data-sort="new">Новые</button>
</div>
<div class="countline" id="countline">&nbsp;</div>
</header>
<main class="list" id="list">
<div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div>
</main>
</div>
<section id="detailScreen" class="hidden">
<div class="backrow"><button class="back" id="backBtn" type="button">‹ Назад к списку</button></div>
<div id="detail"></div>
</section>
<footer class="credit">Powered by <a href="https://t.me/GiftChanges" target="_blank" rel="noopener">@GiftChanges</a> · api.changes.tg<br />Владельцы и тиражи — t.me/nft</footer>
</div>
<script>
var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
var state = {gifts:[], totals:null, filter:"all", sort:"catalog", query:"", gift:null, favs:{}};

function esc(value){
  return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, function(ch){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
  });
}

function num(value){
  var parsed = Number(value);
  if (!isFinite(parsed)) { return String(value); }
  return parsed.toLocaleString("ru-RU");
}

function haptic(kind){
  try {
    if (tg && tg.HapticFeedback) {
      if (kind === "select") { tg.HapticFeedback.selectionChanged(); }
      else { tg.HapticFeedback.impactOccurred("light"); }
    }
  } catch (err) {}
}

function loadFavs(){
  try {
    var raw = window.localStorage.getItem("gc_favs");
    if (raw) { state.favs = JSON.parse(raw) || {}; }
  } catch (err) { state.favs = {}; }
}

function saveFavs(){
  try { window.localStorage.setItem("gc_favs", JSON.stringify(state.favs)); } catch (err) {}
}

async function api(path){
  var response = await fetch(path, {headers:{"Accept":"application/json"}});
  var data = null;
  try { data = await response.json(); } catch (err) { data = null; }
  if (!response.ok || !data) {
    throw new Error(data && data.error ? data.error : "источник недоступен");
  }
  return data;
}

function openLink(url){
  if (!url) { return; }
  haptic("tap");
  if (tg && url.indexOf("https://t.me/") === 0) { tg.openTelegramLink(url); return; }
  if (url.indexOf("tg://") === 0) { window.location.href = url; return; }
  if (tg && tg.openLink) { tg.openLink(url); return; }
  window.open(url, "_blank");
}

function visibleGifts(){
  var query = state.query.trim().toLowerCase();
  var slugQuery = query.replace(/[^a-z0-9]/g, "");
  var items = state.gifts.filter(function(gift){
    if (state.filter === "fav" && !state.favs[gift.slug]) { return false; }
    if (state.filter === "nft" && !gift.upgradable) { return false; }
    if (!query) { return true; }
    if (gift.name.toLowerCase().indexOf(query) >= 0) { return true; }
    if (slugQuery && gift.slug.indexOf(slugQuery) >= 0) { return true; }
    return gift.id.indexOf(query) >= 0;
  });
  if (state.sort === "name") {
    items = items.slice().sort(function(a, b){ return a.name.localeCompare(b.name, "ru"); });
  } else if (state.sort === "new") {
    items = items.slice().sort(function(a, b){
      if (a.id.length !== b.id.length) { return b.id.length - a.id.length; }
      return b.id.localeCompare(a.id);
    });
  } else {
    items = items.slice().sort(function(a, b){ return a.order - b.order; });
  }
  return items;
}

function toggleFav(slug){
  if (state.favs[slug]) { delete state.favs[slug]; } else { state.favs[slug] = 1; }
  saveFavs();
  haptic("select");
  renderList();
}

function giftCard(gift){
  var row = document.createElement("div");
  row.className = "card";

  var main = document.createElement("button");
  main.type = "button";
  main.className = "card-main";

  var thumb = document.createElement("span");
  thumb.className = "thumb";
  var image = document.createElement("img");
  image.src = gift.icon;
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("error", function(){
    image.remove();
    thumb.classList.add("thumb-empty");
  });
  thumb.appendChild(image);

  var body = document.createElement("span");
  body.className = "card-body";
  var title = document.createElement("span");
  title.className = "card-title";
  title.textContent = gift.name;
  var meta = document.createElement("span");
  meta.className = "card-meta";
  meta.textContent = (gift.upgradable ? "есть NFT" : "без NFT") + " · ID " + gift.id;
  body.appendChild(title);
  body.appendChild(meta);

  var chevron = document.createElement("span");
  chevron.className = "chev";
  chevron.textContent = "›";

  main.appendChild(thumb);
  main.appendChild(body);
  main.appendChild(chevron);
  main.addEventListener("click", function(){ openGift(gift.slug); });

  var fav = document.createElement("button");
  fav.type = "button";
  fav.className = "fav" + (state.favs[gift.slug] ? " on" : "");
  fav.textContent = state.favs[gift.slug] ? "★" : "☆";
  fav.setAttribute("aria-label", "Избранное");
  fav.addEventListener("click", function(event){
    event.stopPropagation();
    toggleFav(gift.slug);
  });

  row.appendChild(main);
  row.appendChild(fav);
  return row;
}

function renderSummary(){
  var totals = state.totals || {};
  var gifts = totals.gifts || {};
  var parts = [];
  if (gifts.total) { parts.push(num(gifts.total) + " подарков"); }
  if (totals.models) { parts.push(num(totals.models) + " моделей"); }
  if (totals.backdrops) { parts.push(num(totals.backdrops) + " фонов"); }
  document.getElementById("summary").textContent = parts.length ? parts.join(" · ") : "каталог загружен";
}

function renderList(){
  var host = document.getElementById("list");
  var items = visibleGifts();
  document.getElementById("countline").textContent = "Показано " + items.length + " из " + state.gifts.length;
  host.innerHTML = "";
  if (!items.length) {
    host.innerHTML = '<div class="empty">Ничего не найдено</div>';
    return;
  }
  var frag = document.createDocumentFragment();
  items.forEach(function(gift){ frag.appendChild(giftCard(gift)); });
  host.appendChild(frag);
}

function showScreen(name){
  document.getElementById("listScreen").classList.toggle("hidden", name !== "list");
  document.getElementById("detailScreen").classList.toggle("hidden", name !== "detail");
  try {
    if (tg && tg.BackButton) {
      if (name === "detail") { tg.BackButton.show(); } else { tg.BackButton.hide(); }
    }
  } catch (err) {}
  window.scrollTo(0, 0);
}

function kvRow(label, value){
  return '<div class="kv"><span class="k">' + esc(label) + '</span><span class="v">' + esc(value) + '</span></div>';
}

function renderDetail(data){
  var host = document.getElementById("detail");
  var counts = data.counts || {};
  var rarest = data.rarest || {};
  var rarestRows = "";
  if (rarest.model) { rarestRows += kvRow("Модель", rarest.model); }
  if (rarest.backdrop) { rarestRows += kvRow("Фон", rarest.backdrop); }
  if (rarest.symbol) { rarestRows += kvRow("Узор", rarest.symbol); }
  var metaRows = kvRow("Gift ID", data.id);
  if (data.customEmojiId) { metaRows += kvRow("Custom emoji ID", data.customEmojiId); }
  metaRows += kvRow("Слаг", data.slug);

  host.innerHTML =
    '<div class="hero">' +
      '<div class="hero-art"><img id="heroImg" src="' + esc(data.iconLarge) + '" alt="" /></div>' +
      '<h2 class="hero-title">' + esc(data.name) + '</h2>' +
      '<div class="hero-sub">непрокачанный оригинал подарка</div>' +
      '<div class="badges">' +
        '<span class="badge' + (data.upgradable ? ' ok' : '') + '">' + (data.upgradable ? 'NFT-версия есть' : 'NFT-версии нет') + '</span>' +
        '<span class="badge">' + esc(data.slug) + '</span>' +
      '</div>' +
    '</div>' +
    '<div class="stats">' +
      '<div class="stat"><div class="stat-num">' + esc(counts.models || 0) + '</div><div class="stat-label">модели</div></div>' +
      '<div class="stat"><div class="stat-num">' + esc(counts.backdrops || 0) + '</div><div class="stat-label">фоны</div></div>' +
      '<div class="stat"><div class="stat-num">' + esc(counts.symbols || 0) + '</div><div class="stat-label">узоры</div></div>' +
    '</div>' +
    (rarestRows ? '<section class="block"><div class="block-title">Редчайшие атрибуты</div><div class="kvs">' + rarestRows + '</div></section>' : '') +
    '<section class="block">' +
      '<div class="block-title">Коллекционный экземпляр</div>' +
      '<div class="lookup">' +
        '<div class="lab">Номер экземпляра</div>' +
        '<div class="lookup-row">' +
          '<input id="numInput" type="text" inputmode="numeric" value="1" />' +
          '<button class="btn primary" type="button" id="numBtn">Показать</button>' +
        '</div>' +
        '<div class="hint">Дата, тираж и владелец берутся со страницы коллекционного в Telegram.</div>' +
      '</div>' +
      '<div id="nftBox"></div>' +
    '</section>' +
    '<section class="block"><div class="block-title">Данные подарка</div><div class="kvs">' + metaRows + '</div></section>' +
    '<section class="block"><a class="btn wide" id="stickerBtn" href="' + esc(data.sticker) + '" target="_blank" rel="noopener">Скачать оригинальный стикер (.tgs)</a></section>';

  var heroImg = document.getElementById("heroImg");
  if (heroImg) {
    heroImg.addEventListener("error", function(){
      heroImg.remove();
      var art = document.querySelector(".hero-art");
      if (art) { art.classList.add("thumb-empty"); }
    });
  }
  var input = document.getElementById("numInput");
  var button = document.getElementById("numBtn");
  function runLookup(){
    var raw = (input.value || "").replace(/[^0-9]/g, "");
    if (!raw) { raw = "1"; }
    input.value = raw;
    lookupNft(data.slug, raw);
  }
  button.addEventListener("click", runLookup);
  input.addEventListener("keydown", function(event){
    if (event.key === "Enter") { event.preventDefault(); runLookup(); }
  });
}

function renderNft(box, data){
  var owner = data.owner || {};
  var ownerText = owner.username ? ("@" + owner.username) : (owner.userId ? ("ID " + owner.userId) : (owner.name || "скрыт"));
  var ownerExtra = (owner.name && (owner.username || owner.userId)) ? (owner.name + " · " + ownerText) : ownerText;
  var rows = kvRow("Номер", "#" + data.number);
  rows += kvRow("Дата выпуска", data.issued || "нет данных");
  if (data.quantity) { rows += kvRow("Тираж", data.quantity); }
  if (owner.link) {
    rows += '<button class="kv tap" type="button" id="ownerRow"><span class="k">Владелец</span><span class="v">' + esc(ownerExtra) + ' ›</span></button>';
  } else {
    rows += kvRow("Владелец", ownerExtra);
  }
  if (data.model) { rows += kvRow("Модель", data.model); }
  if (data.backdrop) { rows += kvRow("Фон", data.backdrop); }
  if (data.symbol) { rows += kvRow("Узор", data.symbol); }

  box.innerHTML =
    '<div class="nft">' +
      '<div class="nft-art"><img id="nftImg" src="' + esc(data.image) + '" alt="" /></div>' +
      '<div class="nft-title">' + esc(data.title) + '</div>' +
      '<div class="kvs">' + rows + '</div>' +
      '<div class="actions">' +
        (owner.link ? '<button class="btn primary" type="button" id="ownerBtn">Профиль владельца</button>' : '') +
        '<button class="btn" type="button" id="nftBtn">Открыть в Telegram</button>' +
      '</div>' +
    '</div>';

  var nftImg = document.getElementById("nftImg");
  if (nftImg) {
    nftImg.addEventListener("error", function(){
      var art = nftImg.parentNode;
      nftImg.remove();
      if (art) { art.classList.add("thumb-empty"); }
    });
  }
  var ownerRow = document.getElementById("ownerRow");
  if (ownerRow) { ownerRow.addEventListener("click", function(){ openLink(owner.link); }); }
  var ownerBtn = document.getElementById("ownerBtn");
  if (ownerBtn) { ownerBtn.addEventListener("click", function(){ openLink(owner.link); }); }
  var nftBtn = document.getElementById("nftBtn");
  if (nftBtn) { nftBtn.addEventListener("click", function(){ openLink(data.pageUrl); }); }
}

async function lookupNft(slug, number){
  var box = document.getElementById("nftBox");
  if (!box) { return; }
  box.innerHTML = '<div class="loading">Ищем экземпляр #' + esc(number) + '…</div>';
  try {
    var data = await api("api/nft/" + encodeURIComponent(slug) + "/" + encodeURIComponent(number));
    renderNft(box, data);
  } catch (err) {
    box.innerHTML = '<div class="notice">Экземпляр #' + esc(number) + ' не найден. У подарка может не быть NFT-версии, либо номер больше тиража.</div>';
  }
}

async function openGift(slug){
  haptic("select");
  showScreen("detail");
  var host = document.getElementById("detail");
  host.innerHTML = '<div class="loading">Загружаем карточку…</div>';
  try {
    var data = await api("api/gift/" + encodeURIComponent(slug));
    state.gift = data;
    renderDetail(data);
    lookupNft(data.slug, 1);
  } catch (err) {
    host.innerHTML = '<div class="error">Не удалось открыть подарок: ' + esc(err.message) + '</div>';
  }
}

function bindChips(hostId, key){
  var host = document.getElementById(hostId);
  host.addEventListener("click", function(event){
    var chip = event.target.closest(".chip");
    if (!chip) { return; }
    var buttons = host.querySelectorAll(".chip");
    for (var i = 0; i < buttons.length; i += 1) { buttons[i].classList.remove("on"); }
    chip.classList.add("on");
    state[key] = chip.getAttribute(key === "filter" ? "data-filter" : "data-sort");
    haptic("select");
    renderList();
  });
}

async function init(){
  if (tg) {
    try { tg.ready(); tg.expand(); } catch (err) {}
    try {
      if (tg.BackButton) { tg.BackButton.onClick(function(){ showScreen("list"); }); }
    } catch (err) {}
  }
  loadFavs();
  document.getElementById("backBtn").addEventListener("click", function(){ showScreen("list"); });
  document.getElementById("search").addEventListener("input", function(event){
    state.query = event.target.value || "";
    renderList();
  });
  bindChips("filters", "filter");
  bindChips("sorts", "sort");
  try {
    var data = await api("api/catalog");
    state.gifts = data.gifts || [];
    state.totals = data.totals || null;
    renderSummary();
    renderList();
  } catch (err) {
    document.getElementById("list").innerHTML = '<div class="error">Каталог недоступен: ' + esc(err.message) + '</div>';
    document.getElementById("summary").textContent = "ошибка загрузки";
  }
  var deep = "";
  try { deep = new URLSearchParams(window.location.search).get("gift") || ""; } catch (err) { deep = ""; }
  if (!deep && tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) { deep = tg.initDataUnsafe.start_param; }
  if (deep) { openGift(String(deep).toLowerCase().replace(/[^a-z0-9]/g, "")); }
}

document.addEventListener("DOMContentLoaded", init);
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
