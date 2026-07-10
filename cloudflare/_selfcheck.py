"""Offline runnable check for the cookie-extraction logic: python -m cloudflare._selfcheck"""
from cloudflare.solve import _clearance

if __name__ == "__main__":
    assert _clearance([{"name": "cf_clearance", "value": "abc", "domain": ".ex.com"}])["value"] == "abc"
    assert _clearance([{"name": "__cf_bm", "value": "x"}]) is None   # bot-mgmt cookie is NOT clearance
    assert _clearance([]) is None
    print("ok")
