"""Offline runnable check for the cookie/block logic: python -m awswaf._selfcheck"""
from awswaf.solve import _waf_token, _title_is_block

if __name__ == "__main__":
    assert _waf_token([{"name": "aws-waf-token", "value": "abc", "domain": ".ex.com"}])["value"] == "abc"
    assert _waf_token([{"name": "cf_clearance", "value": "x"}]) is None   # wrong cookie
    assert _waf_token([]) is None
    # genuine block titles still detected (incl. surrounding text)
    assert _title_is_block("403 forbidden")
    assert _title_is_block("the request could not be satisfied.")
    assert _title_is_block("ERROR: The request could not be satisfied")
    # benign titles NOT blocked (regression + the tightened "error" cases)
    assert not _title_is_block("welcome to the app")
    assert not _title_is_block("terror")
    assert not _title_is_block("0 errors found")
    assert not _title_is_block("live error console")
    print("ok")
