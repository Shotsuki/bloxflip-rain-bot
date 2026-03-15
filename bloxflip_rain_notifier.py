import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_ROLE_ID = os.getenv("DISCORD_ROLE_ID", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
STATE_FILE = os.getenv("STATE_FILE", "rain_state.json")
DEBUG_LOG = os.getenv("DEBUG_LOG", "true").strip().lower() in {"1", "true", "yes", "on"}
STARTUP_TEST = os.getenv("STARTUP_TEST", "false").strip().lower() in {"1", "true", "yes", "on"}
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
BLOXFLIP_URL = os.getenv("BLOXFLIP_URL", "https://bloxflip.com").strip()
CHAT_WAIT_MS = int(os.getenv("CHAT_WAIT_MS", "25000"))


class ConfigError(Exception):
    pass


class RainWatcher:
    def __init__(self) -> None:
        self.state_path = Path(STATE_FILE)
        self.last_signature = self._load_state()

    def log(self, message: str) -> None:
        print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

    def debug(self, message: str) -> None:
        if DEBUG_LOG:
            self.log(message)

    def _load_state(self) -> Optional[str]:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data.get("last_signature")
        except Exception:
            return None

    def _save_state(self, signature: str) -> None:
        self.state_path.write_text(
            json.dumps({"last_signature": signature}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def validate_config(self) -> None:
        if not DISCORD_WEBHOOK_URL:
            raise ConfigError("Missing DISCORD_WEBHOOK_URL environment variable.")
        if not DISCORD_ROLE_ID:
            raise ConfigError("Missing DISCORD_ROLE_ID environment variable.")

    def send_discord_message(self, content: str) -> None:
        payload = {
            "content": content,
            "allowed_mentions": {"parse": [], "roles": [DISCORD_ROLE_ID]},
            "username": "Bloxflip APP",
        }
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=HTTP_TIMEOUT)
        self.debug(f"Discord webhook -> HTTP {response.status_code}")
        response.raise_for_status()

    def format_amount(self, amount: Any) -> str:
        try:
            if isinstance(amount, str):
                amount = amount.replace(",", "").strip()
            value = float(amount)
            if value.is_integer():
                return f"{int(value):,}"
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(amount)

    def currency_name(self, currency_hint: Optional[str]) -> str:
        if not currency_hint:
            return "coins"
        hint = str(currency_hint).strip().lower()
        if hint in {"f", "flipcoin", "flipcoins", "fc"}:
            return "Flipcoins"
        if hint in {"r", "rocoin", "rocoins", "rc", "robux", "robucks"}:
            return "Rocoins"
        return str(currency_hint)

    def build_signature(self, rain: dict[str, Any]) -> str:
        parts = [
            str(rain.get("amount", "")),
            str(rain.get("currency_hint", "")),
            str(rain.get("host", "")),
            str(rain.get("participants", "")),
            str(rain.get("title", "")),
        ]
        return "|".join(parts)

    async def extract_active_rain(self, page) -> Optional[dict[str, Any]]:
        self.debug(f"Ouverture de {BLOXFLIP_URL}")
        await page.goto(BLOXFLIP_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        title_selectors = [
            "text=It’s about to rain!",
            "text=It's about to rain!",
            "text=/about to rain/i",
        ]

        title_found = False
        for selector in title_selectors:
            try:
                await page.locator(selector).first.wait_for(state="visible", timeout=CHAT_WAIT_MS)
                title_found = True
                self.debug(f"Bloc rain trouvé avec le sélecteur: {selector}")
                break
            except PlaywrightTimeoutError:
                continue

        if not title_found:
            self.debug("Aucun rain visible sur la page.")
            return None

        card = page.locator("text=/about to rain/i").first.locator("xpath=ancestor::*[self::div or self::section][1]")
        card_text = await card.inner_text()
        compact = " ".join(card_text.split())
        self.debug(f"Texte brut du bloc rain: {compact}")

        parsed = await page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('*'));
              const rainNode = nodes.find(n => /about to rain/i.test((n.innerText || '').trim()));
              if (!rainNode) return null;

              let card = rainNode;
              for (let i = 0; i < 6 && card.parentElement; i++) {
                if ((card.innerText || '').includes('Join For Free')) break;
                card = card.parentElement;
              }

              const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
              const amountMatch = text.match(/about to rain!?\s*([\d,.]+)/i);
              const participantsMatch = text.match(/([\d,.]+)\s+participants/i);
              const hostMatch = text.match(/by\s+([A-Za-z0-9_]+)/i);

              let currencyHint = null;
              const html = card.innerHTML || '';
              if (/flip/i.test(html) || /flipcoin/i.test(html)) currencyHint = 'f';
              if (/rocoin|robux|robucks/i.test(html)) currencyHint = 'r';

              if (!currencyHint) {
                const texts = Array.from(card.querySelectorAll('*')).map(el => (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('alt') || '') + ' ' + (el.className || '')).join(' ');
                if (/flip/i.test(texts)) currencyHint = 'f';
                if (/rocoin|robux|robucks/i.test(texts)) currencyHint = 'r';
              }

              return {
                title: 'It\'s about to rain!',
                amount: amountMatch ? amountMatch[1] : null,
                participants: participantsMatch ? participantsMatch[1] : null,
                host: hostMatch ? hostMatch[1] : null,
                currency_hint: currencyHint,
                raw_text: text,
                raw_html: html.slice(0, 2000)
              };
            }
            """
        )

        if not parsed or not parsed.get("amount"):
            self.debug("Rain trouvé visuellement, mais parsing incomplet.")
            self.debug(f"Fallback texte: {compact}")
            return None

        self.debug(f"Rain parsé: {json.dumps(parsed, ensure_ascii=False)}")
        return parsed

    async def run_once(self) -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1400, "height": 1600})
            try:
                rain = await self.extract_active_rain(page)
            finally:
                await browser.close()

        if not rain:
            self.debug("Pas de rain à notifier.")
            return

        signature = self.build_signature(rain)
        self.debug(f"Signature actuelle: {signature}")
        self.debug(f"Signature précédente: {self.last_signature}")

        if signature == self.last_signature:
            self.debug("Rain déjà notifié, rien à envoyer.")
            return

        amount = self.format_amount(rain.get("amount"))
        currency = self.currency_name(rain.get("currency_hint"))
        message = f"<@&{DISCORD_ROLE_ID}> Un Rain de {amount} {currency} est disponible"
        self.send_discord_message(message)
        self.log(f"Notification envoyée: {message}")
        self.last_signature = signature
        self._save_state(signature)

    async def run_forever(self) -> None:
        self.validate_config()
        self.log(f"Rain notifier Playwright lancé. Vérification toutes les {POLL_SECONDS}s")
        self.log(f"Signature précédente: {self.last_signature}")

        if STARTUP_TEST:
            self.send_discord_message("🟡 Test webhook : le bot a démarré correctement.")
            self.log("Message de test envoyé au démarrage.")

        while True:
            try:
                await self.run_once()
            except Exception as exc:
                self.log(f"Erreur: {exc}")
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    watcher = RainWatcher()
    try:
        asyncio.run(watcher.run_forever())
    except ConfigError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
