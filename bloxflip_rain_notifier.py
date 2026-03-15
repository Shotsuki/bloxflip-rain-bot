import json
import os
import re
import time
from typing import Any, Dict, Iterable, Optional

import requests

API_URL = os.getenv('BLOXFLIP_CHAT_HISTORY_URL', 'https://api.bloxflip.com/chat/history')
WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
ROLE_ID = os.getenv('DISCORD_ROLE_ID', '').strip()
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '30'))
STATE_FILE = os.getenv('STATE_FILE', 'rain_state.json')
TIMEOUT = int(os.getenv('HTTP_TIMEOUT', '20'))
USER_AGENT = os.getenv('USER_AGENT', 'Mozilla/5.0 (compatible; RainNotifier/2.0)')
STARTUP_TEST = os.getenv('STARTUP_TEST', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
DEBUG_LOG = os.getenv('DEBUG_LOG', 'true').strip().lower() in {'1', 'true', 'yes', 'on'}

if not WEBHOOK_URL:
    raise SystemExit('Missing DISCORD_WEBHOOK_URL environment variable.')
if not ROLE_ID:
    raise SystemExit('Missing DISCORD_ROLE_ID environment variable.')

session = requests.Session()
session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})

CURRENCY_MAP = {
    'f': 'Flipcoins',
    'flip': 'Flipcoins',
    'flipcoin': 'Flipcoins',
    'flipcoins': 'Flipcoins',
    'r': 'Rocoins',
    'ro': 'Rocoins',
    'rocoin': 'Rocoins',
    'rocoins': 'Rocoins',
    'robux': 'Rocoins',
    'r$': 'Rocoins',
}


def log(msg: str) -> None:
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


def load_previous_signature() -> Optional[str]:
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('last_signature')
    except FileNotFoundError:
        return None
    except Exception as exc:
        log(f'Impossible de lire {STATE_FILE}: {exc}')
        return None



def save_previous_signature(signature: Optional[str]) -> None:
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'last_signature': signature}, f)



def fetch_history() -> Dict[str, Any]:
    response = session.get(API_URL, timeout=TIMEOUT)
    log(f'GET {API_URL} -> HTTP {response.status_code}')
    response.raise_for_status()
    data = response.json()
    if DEBUG_LOG:
        keys = list(data.keys()) if isinstance(data, dict) else []
        log(f'Clés racine API: {keys}')
    return data



def extract_rain_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    candidates = [
        data.get('rain'),
        (data.get('chat') or {}).get('rain') if isinstance(data.get('chat'), dict) else None,
        (data.get('data') or {}).get('rain') if isinstance(data.get('data'), dict) else None,
    ]
    for item in candidates:
        if isinstance(item, dict):
            return item
    return {}



def is_active_rain(rain: Dict[str, Any]) -> bool:
    for key in ('active', 'isActive', 'enabled'):
        active = rain.get(key)
        if isinstance(active, bool):
            return active
        if active is not None:
            return bool(active)
    return False



def iter_scalar_values(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from iter_scalar_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_scalar_values(item)
    elif obj is not None:
        yield str(obj)



def detect_currency_label(rain: Dict[str, Any]) -> str:
    for key in ('currency', 'coinType', 'coin', 'currencyType', 'wallet', 'prizeType', 'icon', 'iconType', 'token', 'unit'):
        value = rain.get(key)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in CURRENCY_MAP:
                return CURRENCY_MAP[normalized]

    for raw in iter_scalar_values(rain):
        text = raw.strip().lower()
        if text in CURRENCY_MAP:
            return CURRENCY_MAP[text]
        match = re.search(r'(?:currency|coin|icon|type)[\s:=_-]*([fr])\b', text)
        if match:
            return CURRENCY_MAP[match.group(1)]

    blob = json.dumps(rain, ensure_ascii=False).lower()
    if '"icon":"f"' in blob or '"currency":"f"' in blob:
        return 'Flipcoins'
    if '"icon":"r"' in blob or '"currency":"r"' in blob:
        return 'Rocoins'
    if 'flipcoin' in blob or 'flipcoins' in blob:
        return 'Flipcoins'
    if 'rocoin' in blob or 'rocoins' in blob or 'robux' in blob or 'r$' in blob:
        return 'Rocoins'
    return 'coins'



def detect_amount(rain: Dict[str, Any]) -> str:
    for key in ('prize', 'amount', 'value', 'pot', 'rainAmount'):
        value = rain.get(key)
        if isinstance(value, (int, float)):
            if float(value).is_integer():
                return f'{int(value):,}'
            return f'{value:,}'
        if isinstance(value, str) and value.strip():
            return value.strip()
    return '?'



def detect_host(rain: Dict[str, Any]) -> Optional[str]:
    for key in ('host', 'createdBy', 'username', 'user'):
        value = rain.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested in ('username', 'name', 'displayName'):
                nested_value = value.get(nested)
                if isinstance(nested_value, str) and nested_value.strip():
                    return nested_value.strip()
    return None



def build_signature(rain: Dict[str, Any]) -> str:
    candidates = [rain.get('_id'), rain.get('id'), rain.get('createdAt'), rain.get('expiresAt'), rain.get('prize'), rain.get('amount'), rain.get('host')]
    if all(v is None for v in candidates):
        return json.dumps(rain, sort_keys=True, ensure_ascii=False)
    return '|'.join('' if v is None else str(v) for v in candidates)



def send_discord_notification(message: str) -> None:
    payload = {
        'content': message,
        'allowed_mentions': {'parse': [], 'roles': [ROLE_ID]},
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    log(f'Discord webhook -> HTTP {response.status_code}')
    response.raise_for_status()



def main() -> None:
    log(f'Rain notifier lancé. Vérification toutes les {POLL_SECONDS}s')
    previous_signature = load_previous_signature()
    log(f'Signature précédente: {previous_signature!r}')

    if STARTUP_TEST:
        send_discord_notification(f'<@&{ROLE_ID}> Test webhook : le bot a démarré correctement.')
        log('Message de test envoyé au démarrage.')

    while True:
        try:
            data = fetch_history()
            rain = extract_rain_payload(data)
            if DEBUG_LOG:
                log(f'Rain brut: {json.dumps(rain, ensure_ascii=False)[:800]}')
            active = is_active_rain(rain)
            log(f'Rain actif ? {active}')

            if active:
                signature = build_signature(rain)
                log(f'Signature rain actuelle: {signature}')
                if signature != previous_signature:
                    amount = detect_amount(rain)
                    currency = detect_currency_label(rain)
                    host = detect_host(rain)
                    message = f'<@&{ROLE_ID}> Un Rain de {amount} {currency} est disponible'
                    if host:
                        message += f' (par {host})'
                    send_discord_notification(message)
                    previous_signature = signature
                    save_previous_signature(previous_signature)
                    log(f'Nouveau rain détecté et envoyé: {amount} {currency}')
                else:
                    log('Rain actif déjà signalé.')
            else:
                if previous_signature is not None:
                    previous_signature = None
                    save_previous_signature(None)
                    log('Rain terminé, état réinitialisé.')
                else:
                    log('Aucun rain actif.')

        except requests.HTTPError as exc:
            log(f'HTTP error: {exc}')
        except requests.RequestException as exc:
            log(f'Request error: {exc}')
        except Exception as exc:
            log(f'Unexpected error: {exc}')

        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
