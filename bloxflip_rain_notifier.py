import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_ROLE_ID = os.getenv("DISCORD_ROLE_ID", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
STATE_FILE = os.getenv("STATE_FILE", "rain_state.json")
DEBUG_LOG = os.getenv("DEBUG_LOG", "true").strip().lower() in {"1", "true", "yes", "on"}
STARTUP_TEST = os.getenv("STARTUP_TEST", "false").strip().lower() in {"1", "true", "yes", "on"}
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
BLOXFLIP_URL = os.getenv("BLOXFLIP_URL", "https://bloxflip.com").strip()
CHAT_WAIT_MS = int(os.getenv("CHAT_WAIT_MS", "25000"))
ABSENCE_RESET_POLLS = int(os.getenv("ABSENCE_RESET_POLLS", "2"))
RECENT_EVENT_TTL_SECONDS = int(os.getenv("RECENT_EVENT_TTL_SECONDS", "1800"))


class ConfigError(Exception):
    pass


class RainWatcher:
    def __init__(self) -> None:
        self.state_path = Path(STATE_FILE)
        state = self._load_state()
        self.current_visible_signature: Optional[str] = state.get("current_visible_signature")
        self.current_notified_event_id: Optional[str] = state.get("current_notified_event_id")
        self.no_rain_streak: int = int(state.get("no_rain_streak", 0))
        self.recent_event_ids: dict[str, float] = {
            str(k): float(v)
            for k, v in (state.get("recent_event_ids") or {}).items()
            if self._is_number(v)
        }
        self._prune_recent_event_ids()

    def log(self, message: str) -> None:
        print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

    def debug(self, message: str) -> None:
        if DEBUG_LOG:
            self.log(message)

    def _is_number(self, value: Any) -> bool:
        try:
            float(value)
            return True
        except Exception:
            return False

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        self._prune_recent_event_ids()
        payload = {
            "current_visible_signature": self.current_visible_signature,
            "current_notified_event_id": self.current_notified_event_id,
            "no_rain_streak": self.no_rain_streak,
            "recent_event_ids": self.recent_event_ids,
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _prune_recent_event_ids(self) -> None:
        now = time.time()
        self.recent_event_ids = {
            event_id: ts
            for event_id, ts in self.recent_event_ids.items()
            if now - ts <= RECENT_EVENT_TTL_SECONDS
        }

    def validate_config(self) -> None:
        if not DISCORD_WEBHOOK_URL:
            raise ConfigError("Missing DISCORD_WEBHOOK_URL environment variable.")
        if not DISCORD_ROLE_ID:
            raise ConfigError("Missing DISCORD_ROLE_ID environment variable.")

    def send_discord_message(self, content: str) -> None:
        payload = {
            "content": content,
            "allowed_mentions": {"parse": [], "roles": [DISCORD_ROLE_ID]},
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

    def normalize_spaces(self, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    def currency_name(self, currency_hint: Optional[str]) -> str:
        hint = (currency_hint or "").strip().lower()
        if hint in {"f", "flipcoin", "flipcoins", "fc"}:
            return "Flipcoins"
        if hint in {"r", "rocoin", "rocoins", "rc"}:
            return "Rocoins"
        return "coins"

    def build_visible_signature(self, rain: dict[str, Any]) -> str:
        raw_text = self.normalize_spaces(rain.get("raw_text"))
        icon_blob = self.normalize_spaces(rain.get("debug_icon_blob"))
        source = f"{raw_text} || {icon_blob}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]

    def build_event_id(self, rain: dict[str, Any]) -> str:
        """
        Event id intentionally uses as much visible information as possible.
        This makes two different rains produce two notifications whenever the
        page exposes at least one differing detail (participants, host, amount,
        text, icon metadata, etc.).
        """
        canonical = {
            "amount": re.sub(r"\s+", "", str(rain.get("amount") or "")),
            "participants": re.sub(r"\s+", "", str(rain.get("participants") or "")),
            "host": self.normalize_spaces(rain.get("host")).lower(),
            "currency": (rain.get("currency_hint") or "?").strip().lower(),
            "raw_text": self.normalize_spaces(rain.get("raw_text")),
            "icon_blob": self.normalize_spaces(rain.get("debug_icon_blob")),
        }
        blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def detect_currency_hint(self, text: str, html: str, snapshot: dict[str, Any]) -> Optional[str]:
        full_blob = " ".join(
            [
                text,
                html,
                str(snapshot.get("icon_text", "")),
                str(snapshot.get("icon_label", "")),
                str(snapshot.get("icon_class", "")),
                str(snapshot.get("icon_href", "")),
                str(snapshot.get("icon_src", "")),
                str(snapshot.get("icon_alt", "")),
            ]
        ).lower()

        ro_patterns = [
            "rocoin",
            "rocoins",
            "coin-r",
            "currency-r",
            "icon-r",
            " ro ",
            " rc ",
            "ro icon",
            "icon ro",
            "/r",
            "-r-",
            "alt=\"r\"",
            "aria-label=\"r\"",
            "title=\"r\"",
            "href=\"#r",
            "href='#r",
        ]
        flip_patterns = [
            "flipcoin",
            "flipcoins",
            "coin-f",
            "currency-f",
            "icon-f",
            " fc ",
            " flip ",
            "flip icon",
            "icon flip",
            "/f",
            "-f-",
            "alt=\"f\"",
            "aria-label=\"f\"",
            "title=\"f\"",
            "href=\"#f",
            "href='#f",
        ]

        ro_score = sum(1 for p in ro_patterns if p in full_blob)
        flip_score = sum(1 for p in flip_patterns if p in full_blob)

        amount_context = re.search(r"about to rain!?\s*[\d,.]+\s*([fr])\b", text, re.IGNORECASE)
        if amount_context:
            if amount_context.group(1).lower() == "r":
                ro_score += 3
            else:
                flip_score += 3

        single_letter_hints = re.findall(r"\b([fr])\b", full_blob)
        if single_letter_hints:
            ro_score += sum(1 for h in single_letter_hints if h == "r")
            flip_score += sum(1 for h in single_letter_hints if h == "f")

        self.debug(f"Score monnaie -> rocoins={ro_score} flipcoins={flip_score}")

        if ro_score > flip_score and ro_score > 0:
            return "r"
        if flip_score > ro_score and flip_score > 0:
            return "f"
        return None

    def parse_rain_from_snapshot(self, snap: dict[str, Any]) -> Optional[dict[str, Any]]:
        text = self.normalize_spaces(snap.get("text", ""))
        html = str(snap.get("html", ""))
        amount_match = re.search(r"about to rain!?\s*([\d,.]+)", text, re.IGNORECASE)
        participants_match = re.search(r"([\d,.]+)\s+participants", text, re.IGNORECASE)
        host_match = re.search(r"by\s+([A-Za-z0-9_]+)", text, re.IGNORECASE)

        if not amount_match:
            return None

        currency_hint = self.detect_currency_hint(text, html, snap)
        icon_blob = " ".join(
            [
                text,
                html,
                str(snap.get("icon_text", "")),
                str(snap.get("icon_label", "")),
                str(snap.get("icon_class", "")),
                str(snap.get("icon_href", "")),
                str(snap.get("icon_src", "")),
                str(snap.get("icon_alt", "")),
            ]
        )

        return {
            "title": "It's about to rain!",
            "amount": amount_match.group(1),
            "participants": participants_match.group(1) if participants_match else None,
            "host": host_match.group(1) if host_match else None,
            "currency_hint": currency_hint,
            "raw_text": text,
            "debug_icon_blob": icon_blob[:1500],
        }

    async def extract_active_rain(self, page) -> Optional[dict[str, Any]]:
        self.debug(f"Ouverture de {BLOXFLIP_URL}")
        await page.goto(BLOXFLIP_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        selectors = [
            "text=It’s about to rain!",
            "text=It's about to rain!",
            "text=/about to rain/i",
        ]

        target = None
        used_selector = None
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=CHAT_WAIT_MS)
                target = locator
                used_selector = selector
                break
            except PlaywrightTimeoutError:
                continue

        if target is None:
            self.debug("Aucun rain visible sur la page.")
            return None

        self.debug(f"Bloc rain trouvé avec le sélecteur: {used_selector}")
        card = target.locator("xpath=ancestor::*[self::div or self::section][1]")

        snapshot = await card.evaluate(
            """
            (el) => {
              const text = el.innerText || '';
              const html = el.outerHTML || '';
              const iconNode = el.querySelector('svg, img, use');
              const useNode = iconNode && iconNode.tagName === 'use' ? iconNode : iconNode?.querySelector?.('use');
              return {
                text,
                html,
                icon_text: iconNode?.textContent || '',
                icon_label: iconNode?.getAttribute?.('aria-label') || iconNode?.getAttribute?.('title') || '',
                icon_class: iconNode?.getAttribute?.('class') || '',
                icon_href: useNode?.getAttribute?.('href') || useNode?.getAttribute?.('xlink:href') || iconNode?.getAttribute?.('href') || '',
                icon_src: iconNode?.getAttribute?.('src') || '',
                icon_alt: iconNode?.getAttribute?.('alt') || ''
              };
            }
            """
        )

        compact = self.normalize_spaces(snapshot.get("text", ""))
        self.debug(f"Texte brut du bloc rain: {compact}")
        self.debug(
            "Indices monnaie: "
            + json.dumps(
                {
                    "icon_text": snapshot.get("icon_text"),
                    "icon_label": snapshot.get("icon_label"),
                    "icon_class": snapshot.get("icon_class"),
                    "icon_href": snapshot.get("icon_href"),
                    "icon_src": snapshot.get("icon_src"),
                    "icon_alt": snapshot.get("icon_alt"),
                },
                ensure_ascii=False,
            )
        )

        parsed = self.parse_rain_from_snapshot(snapshot)
        if not parsed:
            self.debug("Rain trouvé visuellement, mais parsing incomplet.")
            return None

        parsed["visible_signature"] = self.build_visible_signature(parsed)
        parsed["event_id"] = self.build_event_id(parsed)
        self.debug(f"Rain parsé: {json.dumps(parsed, ensure_ascii=False)}")
        return parsed

    async def run_once(self) -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = await browser.new_page(viewport={"width": 1400, "height": 1600})
            try:
                rain = await self.extract_active_rain(page)
            finally:
                await browser.close()

        if not rain:
            self.no_rain_streak += 1
            self.debug(f"Pas de rain à notifier. Streak sans rain: {self.no_rain_streak}")
            if self.no_rain_streak >= ABSENCE_RESET_POLLS:
                if self.current_visible_signature is not None or self.current_notified_event_id is not None:
                    self.debug("Rain absent assez longtemps, reset de l'état courant.")
                self.current_visible_signature = None
                self.current_notified_event_id = None
                self._save_state()
            return

        self.no_rain_streak = 0
        visible_signature = str(rain["visible_signature"])
        event_id = str(rain["event_id"])
        self._prune_recent_event_ids()

        self.debug(f"Signature visible actuelle: {visible_signature}")
        self.debug(f"Signature visible précédente: {self.current_visible_signature}")
        self.debug(f"Event id actuel: {event_id}")
        self.debug(f"Dernier event id courant: {self.current_notified_event_id}")

        # Same exact visible card still on screen => never notify again.
        if visible_signature == self.current_visible_signature:
            self.debug("Même carte rain encore visible, rien à envoyer.")
            self._save_state()
            return

        # New visible card. Notify if this event wasn't already sent recently.
        if event_id in self.recent_event_ids:
            self.debug("Nouvelle carte détectée mais event_id déjà vu récemment, pas de doublon.")
            self.current_visible_signature = visible_signature
            self.current_notified_event_id = event_id
            self._save_state()
            return

        amount = self.format_amount(rain.get("amount"))
        currency = self.currency_name(rain.get("currency_hint"))
        message = f"<@&{DISCORD_ROLE_ID}> Un Rain de {amount} {currency} est disponible"
        self.send_discord_message(message)
        self.log(f"Notification envoyée: {message}")

        now = time.time()
        self.recent_event_ids[event_id] = now
        self.current_visible_signature = visible_signature
        self.current_notified_event_id = event_id
        self._save_state()

    async def run_forever(self) -> None:
        self.validate_config()
        self.log(f"Rain notifier Playwright v3 lancé. Vérification toutes les {POLL_SECONDS}s")
        self.log(f"Signature visible au démarrage: {self.current_visible_signature}")
        self.log(f"Event id courant au démarrage: {self.current_notified_event_id}")

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
