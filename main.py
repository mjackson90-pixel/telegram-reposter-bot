#!/usr/bin/env python3
import asyncio
import logging
import re
from urllib.parse import urlparse
from pathlib import Path
import os
import requests
import pyperclip
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage
from playwright.async_api import async_playwright
import html
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION = os.getenv("SESSION", "main_user")

RAW_SOURCES = os.getenv("RAW_SOURCES", "").split(",")
RAW_TARGET = os.getenv("RAW_TARGET", "")

STORAGE = Path(os.getenv("STORAGE_PATH", "storage_state.json"))
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

log = logging.getLogger("ref_reposter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

URL_RE = re.compile(r'https?://[^\s)]+')

def safe_markdown_to_html(text: str) -> str:
    if not text:
        return ""

    text = html.escape(text)
    rules = [
        (r'\*\*(.+?)\*\*', r'<b>\1</b>'),
        (r'__(.+?)__', r'<u>\1</u>'),
        (r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', r'<i>\1</i>'),
        (r'~~(.+?)~~', r'<s>\1</s>'),
        (r'`([^`\n]+?)`', r'<code>\1</code>'),
        (r'\[(.+?)\]\((https?://[^\s)]+)\)', r'<a href="\2">\1</a>'),
    ]
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text, flags=re.DOTALL)
    text = text.replace("\r\n", "\n").replace("\n", "<br>")
    return text


async def ensure_yandex_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        if STORAGE.exists():
            context = await browser.new_context(storage_state=str(STORAGE))
            page = await context.new_page()
            await page.goto("https://passport.yandex.ru/profile")
            await asyncio.sleep(2)
        else:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://passport.yandex.ru/auth")
            input("Login to Yandex and press Enter to save session...")
            await context.storage_state(path=str(STORAGE))
        await context.close()
        await browser.close()


async def generate_short_link_gui_async(product_url: str) -> str | None:
    async with async_playwright() as p:
        if not STORAGE.exists():
            await ensure_yandex_login()
            return None

        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(storage_state=str(STORAGE))
        page = await context.new_page()
        try:
            await page.goto(product_url, timeout=30000)
            await asyncio.sleep(3)
            share_btn = await page.wait_for_selector("button:has-text('Поделиться')", timeout=8000)
            await share_btn.click()
            await asyncio.sleep(2)

            selectors = [
                "button:has-text('Копировать')",
                "text=Копировать ссылку",
                "button[aria-label*='Копировать']",
            ]
            copy_btn = None
            for sel in selectors:
                try:
                    copy_btn = await page.wait_for_selector(sel, timeout=4000)
                    if copy_btn:
                        break
                except:
                    continue

            if not copy_btn:
                await page.evaluate("""
                    const btn = [...document.querySelectorAll('button, div, span')]
                      .find(b => b.innerText && b.innerText.includes('Копировать'));
                    if (btn) btn.click();
                """)
            else:
                await copy_btn.click()

            await asyncio.sleep(2)
            link = pyperclip.paste().strip()
            if "market.yandex.ru/cc/" in link:
                return link

            candidate = await page.evaluate("""
                [...document.querySelectorAll('input, textarea')]
                    .map(el => el.value)
                    .find(v => v && v.includes('market.yandex.ru/cc/')) || null
            """)
            return candidate
        finally:
            await context.close()
            await browser.close()


async def patch_yandex_in_text(text: str):
    if not text:
        return text, None
    m = URL_RE.search(text)
    if not m:
        return text, None
    url = m.group(0)
    parsed = urlparse(url)
    if "yandex.ru" not in parsed.netloc:
        return text, None
    try:
        r = requests.get(url, allow_redirects=True, timeout=8)
        full_url = r.url
    except Exception:
        full_url = url
    short = await generate_short_link_gui_async(full_url)
    if not short:
        return text, full_url
    new_text = re.sub(re.escape(url), f'<a href="{short}">Посмотреть на Маркете</a>', text)
    return new_text, short


async def resolve_entities(client: TelegramClient, raws):
    resolved = []
    for it in raws:
        try:
            ent = await client.get_entity(it)
            resolved.append(ent)
            log.info("Resolved %s", it)
        except Exception as e:
            log.warning("Cannot resolve %s: %s", it, e)
    return resolved


async def main():
    if not STORAGE.exists():
        await ensure_yandex_login()

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    sources = await resolve_entities(client, RAW_SOURCES)
    targets = await resolve_entities(client, [RAW_TARGET])
    if not targets:
        log.error("Target not resolved — exit")
        return
    TARGET_ENTITY = targets[0]

    @client.on(events.NewMessage(chats=sources))
    async def handler(event):
        try:
            msg = event.message
            text = msg.message or msg.text or ""
            patched_text, primary_url = await patch_yandex_in_text(text)
            buttons = [Button.url("Посмотреть на Маркете", primary_url)] if primary_url else None

            if msg.media and not isinstance(msg.media, MessageMediaWebPage):
                await client.send_file(
                    TARGET_ENTITY,
                    msg.media,
                    caption=patched_text or None,
                    buttons=buttons,
                    parse_mode="html",
                )
            else:
                await client.send_message(
                    TARGET_ENTITY,
                    patched_text or "",
                    buttons=buttons,
                    parse_mode="html",
                    link_preview=True,
                )
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            log.exception("Error in handler: %s", e)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
