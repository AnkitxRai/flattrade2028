#!/usr/bin/env python3
import argparse
"""Login once and retain an in-memory API session for 2028 strategies."""
from credentials import USER_ID, PASSWORD, TOTP_SECRET, APP_KEY

from datetime import datetime
from zoneinfo import ZoneInfo

TELEGRAM = True
last_notifications = {}


def notification_allowed(message, category):
    # Login failure and success are different states of the same login attempt.
    key = 'login' if category in ('login_failed', 'login_success') else category
    if category == 'trade':
        return True  # Trade/order IDs are already checked in wt_cr.py.
    current = (category, message)
    if last_notifications.get(key) == current:
        return False
    last_notifications[key] = current
    return True


def reset_notification(category):
    last_notifications.pop(category, None)


def send_telegram_message(msg, imp=True, category="error"):
    if not TELEGRAM:
        return False
    try:
        import requests
        BOT_TOKEN = '8331147432:AAGSG4mI8d87sWEBsY0qtarAtwWbpa4viq0'
        CHANNEL_ID = '-1003494200670'
        CHANNEL_ID_IMP = '-1003448158591'
        # Never expose authentication values in an alert.
        for secret in (USER_ID, PASSWORD, TOTP_SECRET, APP_KEY, BOT_TOKEN):
            if secret:
                msg = msg.replace(str(secret), '[REDACTED]')
        if not notification_allowed(msg, category):
            return True
        stamp = datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%d-%m %H:%M:%S IST')
        msg = f'{stamp}\n{msg}'
        response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                 data={'chat_id': CHANNEL_ID_IMP if imp else CHANNEL_ID,
                                       'text': msg[:4000]}, timeout=10)
        response.raise_for_status()
        return response.json().get('ok') is True
    except Exception as error:
        print('Telegram failed:', type(error).__name__, flush=True)
        return False


def login():
    """Return the authenticated API object to share with strategy functions."""
    values = dict(userid=USER_ID, password=PASSWORD,
                  totp_secret=TOTP_SECRET, app_key=APP_KEY)
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        raise ValueError('Fill USER_ID, PASSWORD, TOTP_SECRET and APP_KEY in credentials.py')
    from NorenWebApi import NorenWebApi, BrokerLoginError
    api = NorenWebApi()
    try:
        result = api.login(**values)
        if not isinstance(result, dict) or result.get('stat') != 'Ok' or not api.jkey:
            raise RuntimeError('Broker did not confirm an authenticated session')
    except BrokerLoginError:
        api.session.close()
        raise
    except Exception as error:
        api.session.close()
        # Broker exception text can contain authentication data.
        raise RuntimeError(f'Broker login failed ({type(error).__name__})') from None
    api.notify = send_telegram_message
    api.reset_notification = reset_notification
    return api


def start_session(check=False):
    from NorenWebApi import BrokerLoginError
    try:
        api = login()
    except Exception as error:
        detail = str(error) if isinstance(error, BrokerLoginError) else type(error).__name__
        message = f'❌ 2028 login failed: {detail}'
        print(message, flush=True)
        send_telegram_message(message, category='login_failed')
        return 1
    try:
        print('Login successful. Read-only check.' if check else 'Login successful. Starting wt_cr with the shared session.', flush=True)
        send_telegram_message('✅ 2028 login successful', category='login_success')
        import wt_cr
        if check:
            wt_cr.check_connection(api)
        else:
            wt_cr.run(api)
    except KeyboardInterrupt:
        print('Session runner stopped.', flush=True)
    except Exception as error:
        message = f'❌ 2028 wt_cr stopped ({type(error).__name__}); check broker positions and saved state'
        print(message, flush=True)
        send_telegram_message(message)
        return 1
    finally:
        api.session.close()
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Read-only login, candles and option check')
    args = parser.parse_args()
    return start_session(check=args.check)


if __name__ == '__main__':
    raise SystemExit(main())
