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
    return "СЃРєСЂС‹С‚"


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
                    text="рџЋЃ РћС‚РєСЂС‹С‚СЊ РєР°С‚Р°Р»РѕРі",
                    web_app=WebAppInfo(url=webapp_link(slug)),
                )
            ]
        ]
    )


def app_reply_markup() -> Optional[ReplyKeyboardMarkup]:
    if not webapp_available():
        return None
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="рџЋЃ РљР°С‚Р°Р»РѕРі РїРѕРґР°СЂРєРѕРІ", web_app=WebAppInfo(url=webapp_link()))]],
        resize_keyboard=True,
        is_persistent=True,
    )


router = Router()


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    text = (
        "рџЋЃ <b>Gift Changes Explorer</b>\n\n"
        "РљР°С‚Р°Р»РѕРі РІСЃРµС… РїРѕРґР°СЂРєРѕРІ Telegram РІ РЅРµРїСЂРѕРєР°С‡Р°РЅРЅРѕРј РІРёРґРµ: РѕСЂРёРіРёРЅР°Р»СЊРЅР°СЏ РёРєРѕРЅРєР°, ID, "
        "РјРѕРґРµР»Рё, С„РѕРЅС‹ Рё СѓР·РѕСЂС‹. РћС‚РєСЂРѕР№ РјРёРЅРё-РїСЂРёР»РѕР¶РµРЅРёРµ, РЅР°Р№РґРё РїРѕРґР°СЂРѕРє РІ СЃРїРёСЃРєРµ Рё СѓРєР°Р¶Рё РЅРѕРјРµСЂ "
        "РєРѕР»Р»РµРєС†РёРѕРЅРЅРѕРіРѕ вЂ” СѓРІРёРґРёС€СЊ РґР°С‚Сѓ РІС‹РїСѓСЃРєР°, С‚РёСЂР°Р¶ Рё РІР»Р°РґРµР»СЊС†Р° СЃ РїРµСЂРµС…РѕРґРѕРј РІ РїСЂРѕС„РёР»СЊ.\n\n"
        "<b>РљРѕРјР°РЅРґС‹</b>\n"
        "/app вЂ” РѕС‚РєСЂС‹С‚СЊ РјРёРЅРё-РїСЂРёР»РѕР¶РµРЅРёРµ\n"
        "/gifts вЂ” СЃС‚Р°С‚РёСЃС‚РёРєР° РєР°С‚Р°Р»РѕРіР°\n"
        "/gift Scared Cat вЂ” РєР°СЂС‚РѕС‡РєР° РїРѕРґР°СЂРєР°\n"
        "/nft scaredcat 1 вЂ” РІР»Р°РґРµР»РµС† РєРѕР»Р»РµРєС†РёРѕРЅРЅРѕРіРѕ\n"
        "/about вЂ” РёСЃС‚РѕС‡РЅРёРє РґР°РЅРЅС‹С…\n\n"
        "<i>" + escape(CREDIT) + "</i>"
    )
    if not webapp_available():
        text += "\n\nвљ пёЏ Р—Р°РґР°Р№ РїРµСЂРµРјРµРЅРЅСѓСЋ <code>WEBAPP_URL</code> (https), С‡С‚РѕР±С‹ РІРєР»СЋС‡РёС‚СЊ РєРЅРѕРїРєСѓ РїСЂРёР»РѕР¶РµРЅРёСЏ."
    reply_markup = app_reply_markup()
    if reply_markup is not None:
        await message.answer(text, reply_markup=reply_markup)
        markup = app_inline_markup()
        if markup is not None:
            await message.answer("РљР°С‚Р°Р»РѕРі РѕС‚РєСЂС‹РІР°РµС‚СЃСЏ Р·РґРµСЃСЊ рџ‘‡", reply_markup=markup)
        return
    await message.answer(text)


@router.message(Command("app"))
async def on_app(message: Message) -> None:
    markup = app_inline_markup()
    if markup is None:
        await message.answer("WEBAPP_URL РЅРµ Р·Р°РґР°РЅ, РјРёРЅРё-РїСЂРёР»РѕР¶РµРЅРёРµ РЅРµРґРѕСЃС‚СѓРїРЅРѕ.")
        return
    await message.answer("РљР°С‚Р°Р»РѕРі РЅРµРїСЂРѕРєР°С‡Р°РЅРЅС‹С… РїРѕРґР°СЂРєРѕРІ рџ‘‡", reply_markup=markup)


@router.message(Command("about"))
async def on_about(message: Message) -> None:
    await message.answer(
        "Р”Р°РЅРЅС‹Рµ Рѕ РїРѕРґР°СЂРєР°С…, РјРѕРґРµР»СЏС…, С„РѕРЅР°С… Рё СѓР·РѕСЂР°С…: <b>api.changes.tg</b>\n"
        "Р’Р»Р°РґРµР»СЊС†С‹ Рё С‚РёСЂР°Р¶Рё РєРѕР»Р»РµРєС†РёРѕРЅРЅС‹С…: <b>t.me/nft</b> Рё <b>nft.fragment.com</b>\n\n"
        "<i>" + escape(CREDIT) + "</i>"
    )


@router.message(Command("gifts"))
async def on_gifts(message: Message) -> None:
    try:
        catalog = await get_catalog()
    except UpstreamError as error:
        await message.answer("API РЅРµРґРѕСЃС‚СѓРїРµРЅ: " + escape(error.message))
        return
    totals = catalog.get("totals") or {}
    gifts_total = totals.get("gifts") or {}
    lines = [
        "рџ“Љ <b>РљР°С‚Р°Р»РѕРі Gift Changes</b>",
        "РџРѕРґР°СЂРєРѕРІ РІСЃРµРіРѕ: <b>" + str(gifts_total.get("total", "вЂ”")) + "</b>",
        "РџСЂРѕРєР°С‡РёРІР°РµРјС‹С…: <b>" + str(gifts_total.get("upgradable", "вЂ”")) + "</b>",
        "Р›РёРјРёС‚РёСЂРѕРІР°РЅРЅС‹С…: <b>" + str(gifts_total.get("limited", "вЂ”")) + "</b>",
        "Р‘РµР·Р»РёРјРёС‚РЅС‹С…: <b>" + str(gifts_total.get("unlimited", "вЂ”")) + "</b>",
        "РњРѕРґРµР»РµР№: <b>" + str(totals.get("models", "вЂ”")) + "</b>",
        "Р¤РѕРЅРѕРІ: <b>" + str(totals.get("backdrops", "вЂ”")) + "</b>",
        "РЈР·РѕСЂРѕРІ: <b>" + str(totals.get("patterns", "вЂ”")) + "</b>",
        "Р’ РїСЂРёР»РѕР¶РµРЅРёРё РґРѕСЃС‚СѓРїРЅРѕ РєР°СЂС‚РѕС‡РµРє: <b>" + str(len(catalog.get("gifts") or [])) + "</b>",
        "",
        "<i>" + escape(CREDIT) + "</i>",
    ]
    await message.answer("\n".join(lines), reply_markup=app_inline_markup())


async def send_gift_card(message: Message, query: str) -> None:
    try:
        data = await get_gift(query)
    except UpstreamError as error:
        if error.status == 404:
            await message.answer("РџРѕРґР°СЂРѕРє РЅРµ РЅР°Р№РґРµРЅ. РџРѕРїСЂРѕР±СѓР№, РЅР°РїСЂРёРјРµСЂ: <code>/gift plush pepe</code>")
        else:
            await message.answer("API РЅРµРґРѕСЃС‚СѓРїРµРЅ: " + escape(error.message))
        return
    counts = data["counts"]
    rarest = data["rarest"]
    lines = [
        "рџЋЃ <b>" + escape(data["name"]) + "</b>",
        "РЎРѕСЃС‚РѕСЏРЅРёРµ: <b>РЅРµРїСЂРѕРєР°С‡Р°РЅРЅС‹Р№ РѕСЂРёРіРёРЅР°Р»</b>",
        "NFT-РІРµСЂСЃРёСЏ: <b>" + ("РµСЃС‚СЊ" if data["upgradable"] else "РЅРµС‚") + "</b>",
        "",
        "РњРѕРґРµР»РµР№: <b>" + str(counts["models"]) + "</b> В· Р¤РѕРЅРѕРІ: <b>" + str(counts["backdrops"]) + "</b> В· РЈР·РѕСЂРѕРІ: <b>" + str(counts["symbols"]) + "</b>",
    ]
    if rarest["model"]:
        lines.append("Р РµРґС‡Р°Р№С€Р°СЏ РјРѕРґРµР»СЊ: <b>" + escape(str(rarest["model"])) + "</b>")
    if rarest["backdrop"]:
        lines.append("Р РµРґС‡Р°Р№С€РёР№ С„РѕРЅ: <b>" + escape(str(rarest["backdrop"])) + "</b>")
    if rarest["symbol"]:
        lines.append("Р РµРґС‡Р°Р№С€РёР№ СѓР·РѕСЂ: <b>" + escape(str(rarest["symbol"])) + "</b>")
    lines.append("")
    lines.append("Gift ID: <code>" + escape(data["id"]) + "</code>")
    if data["customEmojiId"]:
        lines.append("Custom emoji ID: <code>" + esca
