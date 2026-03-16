#!/usr/bin/env python3
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BLOXFLIP_URL = os.getenv("BLOXFLIP_URL", "https://bloxflip.com")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_ROLE_ID = os.getenv("DISCORD_ROLE_ID", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
CHAT_WAIT_MS = int(os.getenv("CHAT_WAIT_MS", "3000"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
STATE_FILE = os.getenv("STATE_FILE", "rain_state.json")
DEBUG_LOG = os.getenv("DEBUG_LOG", "true").lower() == "true"
STARTUP_TEST = os.getenv("STARTUP_TEST", "false").lower() == "true"
ABSENCE_RESET_POLLS = int(os.getenv("ABSENCE_RESET_POLLS", "2"))
PAGE_GOTO_TIMEOUT_MS = int(os.getenv("PAGE_GOTO_TIMEOUT_MS", "15000"))
RECENT_EVENT_TTL_SECONDS = int(os.getenv("RECENT_EVENT_TTL_SECONDS", "1800"))


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Impossible de lire {STATE_FILE}: {e}")
    return {
        "last_notified_key": None,
        "active_key": None,
        "no_rain_streak": 0,
        "recent_events": {},
    }


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Impossible d'écrire {STATE_FILE}: {e}")


def cleanup_recent_events(state: Dict[str, Any]) -> None:
    now = time.time()
    recent = state.get("recent_events", {})
    state["recent_events"] = {
        k: v for k, v in recent.items()
        if isinstance(v, (int, float)) and now - float(v) <= RECENT_EVENT_TTL_SECONDS
    }


def send_discord_message(content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles"]},
    }
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=HTTP_TIMEOUT)
    log(f"Discord webhook -> HTTP {r.status_code}")
    r.raise_for_status()


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def extract_amount(text: str) -> Optional[str]:
    m = re.search(r"It's about to rain!\s*([\d,]+)", text, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b([\d]{1,3}(?:,[\d]{3})+|[\d]+)\b", text)
    return m.group(1) if m else None


def extract_host(text: str) -> Optional[str]:
    m = re.search(r"\bby\s+([A-Za-z0-9_]+)", text, flags=re.I)
    return m.group(1) if m else None


def detect_currency_from_card(page, rain_locator) -> str:
    hints = []
    try:
        html = (rain_locator.inner_html(timeout=3000) or "").lower()
        hints.append(html)
    except Exception:
        pass
    try:
        aria = page.evaluate("""(el) => {
            const vals = [];
            const nodes = el.querySelectorAll('*');
            for (const n of nodes) {
                const a = (n.getAttribute && (n.getAttribute('aria-label') || n.getAttribute('alt') || n.getAttribute('title'))) || '';
                if (a) vals.push(a);
            }
            return vals.join(' | ');
        }""", rain_locator.element_handle(timeout=3000))
        if aria:
            hints.append(str(aria).lower())
    except Exception:
        pass

    joined = " || ".join(hints)
    if DEBUG_LOG and joined:
        log(f"Indices monnaie: {joined[:500]}")

    if any(x in joined for x in ["rocoin", ">r<", "coin-r", "currency-r"]):
        return "Rocoins"
    if any(x in joined for x in ["flipcoin", ">f<", "coin-f", "currency-f"]):
        return "Flipcoins"

    return "coins"


def extract_join_hint(page, rain_locator) -> str:
    try:
        hrefs = page.evaluate("""(el) => {
            const out = [];
            const nodes = el.querySelectorAll('a[href], button, [role="button"]');
            for (const n of nodes) {
                const h = n.getAttribute && n.getAttribute('href');
                const t = (n.textContent || '').trim();
                if (h) out.push(h);
                if (t) out.push(t);
            }
            return out.join(' | ');
        }""", rain_locator.element_handle(timeout=3000))
        return normalize_space(str(hrefs))[:200]
    except Exception:
        return ""


def make_stable_key(amount: str, currency: str, host: str, join_hint: str) -> str:
    return normalize_space(f"{amount}|{currency}|{host}|{join_hint}".lower())


def make_event_id(card_text: str) -> str:
    return normalize_space(card_text.lower())


def extract_rain(page) -> Optional[Tuple[str, str, str, str, str]]:
    selectors = [
        "text=It’s about to rain!",
        "text=It's about to rain!",
        ":text(\"It's about to rain!\")",
        ":text(\"It’s about to rain!\")",
    ]
    rain_locator = None
    used = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                rain_locator = loc.locator("xpath=ancestor::*[self::div or self::section][1]").first
                used = sel
                break
        except Exception:
            continue

    if rain_locator is None:
        return None

    try:
        card_text = normalize_space(rain_locator.inner_text(timeout=4000))
    except Exception:
        card_text = ""

    if not card_text or "about to rain" not in card_text.lower():
        return None

    if DEBUG_LOG:
        log(f"Bloc rain trouvé avec le sélecteur: {used}")
        log(f"Texte brut du bloc rain: {card_text}")

    amount = extract_amount(card_text) or "?"
    host = extract_host(card_text) or "unknown"
    currency = detect_currency_from_card(page, rain_locator)
    join_hint = extract_join_hint(page, rain_locator)

    return amount, currency, host, card_text, join_hint


def safe_goto(page) -> None:
    log(f"Ouverture de {BLOXFLIP_URL}")
    try:
        page.goto(BLOXFLIP_URL, wait_until="commit", timeout=PAGE_GOTO_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        log(f"Navigation timeout après {PAGE_GOTO_TIMEOUT_MS}ms, on continue quand même.")
    except Exception as e:
        log(f"Erreur navigation: {e}")
    try:
        page.wait_for_timeout(CHAT_WAIT_MS)
    except Exception:
        pass


def ensure_config() -> None:
    if not DISCORD_WEBHOOK_URL:
        log("Missing DISCORD_WEBHOOK_URL environment variable.")
        sys.exit(1)


def main() -> None:
    ensure_config()
    state = load_state()
    cleanup_recent_events(state)
    log(f"Rain notifier Playwright v5 lancé. Vérification toutes les {POLL_SECONDS}s")
    log(f"Dernière clé notifiée au démarrage: {state.get('last_notified_key')}")
    log(f"Clé active au démarrage: {state.get('active_key')}")

    if STARTUP_TEST:
        try:
            send_discord_message("🟡 Test webhook : le bot a démarré correctement.")
            log("Message de test envoyé au démarrage.")
        except Exception as e:
            log(f"Erreur envoi message de test: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"),
            locale="en-US",
        )
        page = context.new_page()

        while True:
            try:
                safe_goto(page)
                rain = extract_rain(page)

                if not rain:
                    state["no_rain_streak"] = int(state.get("no_rain_streak", 0)) + 1
                    log("Aucun rain visible sur la page.")
                    log(f"Pas de rain à notifier. Streak sans rain: {state['no_rain_streak']}")
                    if state["no_rain_streak"] >= ABSENCE_RESET_POLLS:
                        state["active_key"] = None
                    save_state(state)
                    time.sleep(POLL_SECONDS)
                    continue

                amount, currency, host, card_text, join_hint = rain
                state["no_rain_streak"] = 0

                stable_key = make_stable_key(amount, currency, host, join_hint)
                event_id = make_event_id(card_text)
                cleanup_recent_events(state)

                if DEBUG_LOG:
                    log(f"Clé stable rain: {stable_key}")
                    log(f"Event id rain: {event_id}")

                already_active = stable_key == state.get("active_key")
                recently_sent = event_id in state.get("recent_events", {})

                if already_active or recently_sent:
                    log("Rain déjà géré, aucune nouvelle notification.")
                    state["active_key"] = stable_key
                    save_state(state)
                    time.sleep(POLL_SECONDS)
                    continue

                content = f"<@&{DISCORD_ROLE_ID}> Un Rain de {amount} {currency} est disponible" if DISCORD_ROLE_ID else f"Un Rain de {amount} {currency} est disponible"
                send_discord_message(content)
                log(f"Notification envoyée: {content}")

                state["last_notified_key"] = stable_key
                state["active_key"] = stable_key
                state.setdefault("recent_events", {})[event_id] = time.time()
                save_state(state)

            except Exception as e:
                log(f"Erreur: {e}")

            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
