"""Read-only Telegram checks; never send a test message or log credentials."""
import datetime as dt
import json
import os
import pathlib
import urllib.parse
import urllib.request


def check_telegram(*, env=None, opener=urllib.request.urlopen):
    env = os.environ if env is None else env
    token, channel = env.get('TELEGRAM_BOT_TOKEN'), env.get('TELEGRAM_CHAT_ID')
    if not token or not channel:
        return dict(status='failed', reason='missing_credentials')
    def call(method, **params):
        request = urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,
            data=urllib.parse.urlencode(params).encode())
        with opener(request, timeout=20) as response:
            answer = json.load(response)
        if answer.get('ok') is not True:
            raise ValueError('Telegram rejected read-only check')
        return answer['result']
    try:
        bot = call('getMe')
        chat = call('getChat', chat_id=channel)
        membership = call('getChatMember', chat_id=channel, user_id=bot['id'])
        role = membership.get('status')
        if chat.get('type') == 'channel':
            permitted = role == 'creator' or role == 'administrator' and membership.get('can_post_messages') is True
        else:
            permitted = role in {'creator', 'administrator', 'member'} or (
                role == 'restricted' and membership.get('is_member') is True and membership.get('can_send_messages') is True)
        return dict(status='ok' if permitted else 'failed',
                    reason='posting_permission_verified' if permitted else 'posting_permission_missing',
                    chat_type=chat.get('type'), bot_role=role)
    except Exception:
        return dict(status='failed', reason='telegram_access_unconfirmed')


def main():
    result = check_telegram()
    result['checked_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    destination = pathlib.Path('output')
    destination.mkdir(exist_ok=True)
    (destination/'telegram-health.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('Telegram read-only health: '+json.dumps(result))
    if result['status'] != 'ok':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
