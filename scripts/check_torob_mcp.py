"""Read-only live check from the actual Actions host; never posts to Telegram."""
import datetime as dt
import json
from pathlib import Path

from dealradar.torob_mcp import TorobMCP, MCPError


def main():
    root=Path(__file__).resolve().parents[1]
    result=dict(checked_at=dt.datetime.now(dt.timezone.utc).isoformat(),status='failed')
    try:
        with TorobMCP({'_base':root}) as client:
            result['tools']=len(client.rpc('tools/list',{})['tools'])
            search=client.call('search_torob',{'query':'هدفون انکر R50i','limit':3})
            result.update(status='available',search_results=len(search['items']),fetched_at=search.get('fetched_at'))
            if search['items']:
                reference=search['items'][0]['id']
                detail=client.call('product_details',{'product':reference})
                sellers=client.call('product_sellers',{'product':reference,'limit':50,'available_only':True,'shop_type':'online'})
                result.update(product_id=reference,seller_offers=len(sellers['items']),
                              loaded_list_complete=sellers['loaded_list_complete'],truncated=sellers['truncated'])
    except Exception as error:
        result.update(status='failed',error_code=error.code if isinstance(error,MCPError) else 'LIVE_CHECK_FAILED')
    output=root/'output';output.mkdir(exist_ok=True)
    (output/'mcp-live-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='available' else 1


if __name__=='__main__':raise SystemExit(main())
