"""One read-only request from the Actions host; never sends Telegram messages."""
import json
from dealradar.emalls import read_search

try:
    items=read_search('انکر R50i')
    print(json.dumps({'source':'emalls','status':'available','results':len(items)}))
except Exception as error:
    print(json.dumps({'source':'emalls','status':'unavailable','error':type(error).__name__}))
    raise SystemExit(1) from None
