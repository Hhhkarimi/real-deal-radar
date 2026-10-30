import io
import json
import unittest
from dealradar.health import check_telegram


class HealthTests(unittest.TestCase):
    def test_channel_permission_is_checked_without_sending_messages(self):
        calls=[]
        responses=[{'id':123}, {'type':'channel'}, {'status':'administrator','can_post_messages':True}]
        def opener(request, timeout):
            calls.append(request.full_url.rsplit('/',1)[1])
            return io.BytesIO(json.dumps(dict(ok=True,result=responses.pop(0))).encode())
        result=check_telegram(env={'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'channel'},opener=opener)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(calls,['getMe','getChat','getChatMember'])

    def test_administrator_without_post_permission_fails(self):
        responses=[{'id':123}, {'type':'channel'}, {'status':'administrator','can_post_messages':False}]
        result=check_telegram(env={'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'channel'},
            opener=lambda *a,**kw:io.BytesIO(json.dumps(dict(ok=True,result=responses.pop(0))).encode()))
        self.assertEqual(result['reason'],'posting_permission_missing')

    def test_transport_errors_never_expose_token_bearing_urls(self):
        def opener(*a,**kw):raise RuntimeError('botsecret/private-channel')
        result=check_telegram(env={'TELEGRAM_BOT_TOKEN':'secret','TELEGRAM_CHAT_ID':'channel'},opener=opener)
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('secret',str(result))

    def test_missing_credentials_make_no_request(self):
        self.assertEqual(check_telegram(env={})['reason'],'missing_credentials')
