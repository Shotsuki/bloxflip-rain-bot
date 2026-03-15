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
USER_AGENT = os.getenv('USER_AGENT', 'Mozilla/5.0 (compatible; RainNotifier/1.1)')

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


def load_previous_signature() -> Optional[str]:
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('last_signature')
    except FileNotFoundError:
        return None
    except Exception:
        return None



def save_previous_signature(signature: Optional[str]) -> None:
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'last_signature': signature}, f)



def fetch_history() -> Dict[str, Any]:
    response = session.get(API_URL, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()



def extract_rain_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    rain = data.get('rain') or {}
    if not isinstance(rain, dict):
        return {}
    return rain



def is_active_rain(rain: Dict[str, Any]) -> bool:
    active = rain.get('active')
    if isinstance(active, bool):
        return active
    return bool(active)



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
    # 1) Check common explicit fields first.
    explicit_keys = (
        'currency', 'coinType', 'coin', 'currencyType', 'wallet',
        'prizeType', 'icon', 'iconType', 'token', 'unit'
    )
    for key in explicit_keys:
        value = rain.get(key)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in CURRENCY_MAP:
                return CURRENCY_MAP[normalized]

    # 2) Look for patterns like icon='f' or icon='r' anywhere in nested payload.
    for raw in iter_scalar_values(rain):
        text = raw.strip().lower()
        if text in CURRENCY_MAP:
            return CURRENCY_MAP[text]
        # Match phrases like 'currency:f' or 'icon=r'.
        match = re.search(r'(?:currency|coin|icon|type)[\s:=_-]*([fr])\b', text)
        if match:
            return CURRENCY_MAP[match.group(1)]

    # 3) Final fallback on the serialized payload.
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
    candidates = [
        rain.get('_id'),
        rain.get('id'),
        rain.get('createdAt'),
        rain.get('expiresAt'),
        rain.get('prize'),
        rain.get('amount'),
        rain.get('host'),
    ]
    if all(v is None for v in candidates):
        return json.dumps(rain, sort_keys=True, ensure_ascii=False)
    return '|'.join('' if v is None else str(v) for v in candidates)



def send_discord_notification(amount: str, currency: str, host: Optional[str]) -> None:
    mention = f'<@&{ROLE_ID}>'
    message = f'{mention} Un Rain de {amount} {currency} est disponible'
    if host:
        message += f' (par {host})'

    payload = {
        'content': message,
        'allowed_mentions': {'parse': [], 'roles': [ROLE_ID]},
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    response.raise_for_status()



def main() -> None:
    print(f'Rain notifier lancé. Vérification toutes les {POLL_SECONDS}s')
    previous_signature = load_previous_signature()

    while True:
        try:
            data = fetch_history()
            rain = extract_rain_payload(data)
            active = is_active_rain(rain)

            if active:
                signature = build_signature(rain)
                if signature != previous_signature:
                    amount = detect_amount(rain)
                    currency = detect_currency_label(rain)
                    host = detect_host(rain)
                    send_discord_notification(amount, currency, host)
                    previous_signature = signature
                    save_previous_signature(previous_signature)
                    print(f'[{time.strftime("%H:%M:%S")}] Nouveau rain détecté: {amount} {currency}')
                else:
                    print(f'[{time.strftime("%H:%M:%S")}] Rain actif déjà signalé.')
            else:
                if previous_signature is not None:
                    previous_signature = None
                    save_previous_signature(None)
                print(f'[{time.strftime("%H:%M:%S")}] Aucun rain actif.')

        except requests.HTTPError as exc:
            print(f'HTTP error: {exc}')
        except requests.RequestException as exc:
            print(f'Request error: {exc}')
        except Exception as exc:
            print(f'Unexpected error: {exc}')

        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
