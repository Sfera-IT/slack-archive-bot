import os
import sys
import re

# Ensure project root is importable
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from url_cleaner import UrlCleaner


def _rules_with_providers(extra_providers):
    providers = {
        # minimal from our cleaner
        "google": {
            "urlPattern": r"^https?:\/\/(?:[a-z0-9-]+\.)*?google(?:\.[a-z]{2,}){1,}",
            "redirections": [r"^https?:\/\/(?:[a-z0-9-]+\.)*?google(?:\.[a-z]{2,}){1,}\/url\?.*?(?:url|q)=([^&]+)"],
            "rules": [r"ved", r"gws_[a-z]+", r"ei", r"uact", r"sxsrf"],
        },
        "youtube": {
            "urlPattern": r"^https?:\/\/(?:[a-z0-9-]+\.)*?(?:youtube\.com|youtu\.be)",
            "rules": [r"ab_channel", r"utm_[a-z_]+", r"si", r"pp", r"feature", r"list", r"index", r"t"],
        },
        "globalRules": {
            "urlPattern": ".*",
            "rules": [r"utm_[a-z_]+", r"fbclid", r"gclid", r"ref"],
        },
    }
    providers.update(extra_providers)
    return {"providers": providers}


def _load_full_rules():
    import json
    path = os.path.join(ROOT_DIR, "url_rules.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_unknown_domain_strips_all_params_and_fragment():
    url = "http://www.lombax.it/test?notUsefulParameter=xxx#frag"
    cleaned = UrlCleaner().clean(url)
    assert cleaned == "http://www.lombax.it/test"


def test_youtube_keeps_v_and_strips_trackers():
    url = "https://www.youtube.com/watch?v=7ts1vJLHrtc&utm_source=foo&feature=share"
    cleaned = UrlCleaner().clean(url)
    assert cleaned.startswith("https://www.youtube.com/watch")
    assert "v=7ts1vJLHrtc" in cleaned
    assert "utm_source" not in cleaned and "feature" not in cleaned


def test_google_redirect_extraction():
    src = (
        "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fpath%3Fa%3D1%26utm_source%3Dx&sa=D&source=hangouts&ust=123"
    )
    cleaned = UrlCleaner().clean(src)
    # should redirect to example.com and remove utm_source, keep a=1 if provider matched (none), so drop all params
    assert cleaned == "https://example.com/path"


def test_scheme_and_host_lowercased():
    url = "HTTPS://WWW.YOUTUBE.COM/watch?v=abcDEF"
    cleaned = UrlCleaner().clean(url)
    assert cleaned == "https://www.youtube.com/watch?v=abcDEF"


def test_duckduckgo_redirect_to_target_and_drop_params():
    rules = _rules_with_providers({
        "duckduckgo": {
            "urlPattern": r"^https?:\/\/(?:[a-z0-9-]+\.)*?duckduckgo\.com",
            "redirections": [r"^https?:\/\/duckduckgo\.com\/l\/.*?uddg=([^&]+)"],
        }
    })
    src = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falpha%3Fb%3D1%26utm_source%3Dx"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/alpha"


def test_googleadservices_adurl_redirect():
    rules = _rules_with_providers({
        "googleadservices": {
            "urlPattern": r"^https?:\/\/(?:[a-z0-9-]+\.)*?googleadservices\.com",
            "redirections": [r"^https?:\/\/(?:[a-z0-9-]+\.)*?googleadservices\.com\/.*?adurl=([^&]+)"],
        }
    })
    src = "https://www.googleadservices.com/pagead/aclk?adurl=https%3A%2F%2Fexample.com%2Ffoo%3Fx%3D1%26utm_campaign%3Dy"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/foo"


def test_tumblr_redirect_z_param():
    rules = _rules_with_providers({
        "t.umblr.com": {
            "urlPattern": r"^https?:\/\/(?:[a-z0-9-]+\.)*?umblr\.com",
            "redirections": [r"^https?:\/\/t\.umblr\.com\/redirect\?z=([^&]+)"],
        }
    })
    src = "https://t.umblr.com/redirect?z=https%3A%2F%2Fexample.com%2Fbar%2F"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/bar/"


def test_skimresources_redirect_url_param():
    rules = _rules_with_providers({
        "skimresources.com": {
            "urlPattern": r"^https?:\/\/(?:[a-z0-9-]+\.)*?skimresources\.com",
            "redirections": [r"^https?:\/\/go\.skimresources\.com\/.*?url=([^&]+)"],
        }
    })
    src = "https://go.skimresources.com/?id=123&url=https%3A%2F%2Fexample.com%2Fbaz%3Fa%3D1"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/baz"


def test_amazon_remove_affiliate_params_keep_path():
    rules = _rules_with_providers({
        "amazon": {
            "urlPattern": r"^https?:\/\/(?:[a-z0-9-]+\.)*?amazon(?:\.[a-z]{2,}){1,}",
            "rules": [r"tag", r"ref_?"],
        }
    })
    src = "https://www.amazon.com/dp/B000TEST?tag=affiliate-20&ref_=abc"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://www.amazon.com/dp/B000TEST"


# Additional cases (20+)

def test_facebook_l_redirect_to_target():
    rules = _load_full_rules()
    src = "https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Ffb"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/fb"


def test_reddit_out_redirect():
    rules = _load_full_rules()
    src = "https://out.reddit.com/?url=https%3A%2F%2Fexample.com%2Frd"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/rd"


def test_instagram_redirect_param_u():
    rules = _load_full_rules()
    src = "https://instagram.com/link?u=https%3A%2F%2Fexample.com%2Fig"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/ig"


def test_gate_sc_url_param():
    rules = _load_full_rules()
    src = "https://gate.sc/?url=https%3A%2F%2Fexample.com%2Fgate"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/gate"


def test_anonym_to_redirect():
    rules = _load_full_rules()
    src = "https://anonym.to/?https%3A%2F%2Fexample.com%2Fanonym"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/anonym"


def test_webgains_wgtarget():
    rules = _load_full_rules()
    src = "https://track.webgains.com/click.html?wgtarget=https%3A%2F%2Fexample.com%2Fwg"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/wg"


def test_effiliation_url():
    rules = _load_full_rules()
    src = "https://track.effiliation.com/redirect?url=https%3A%2F%2Fexample.com%2Feff"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/eff"


def test_steamcommunity_linkfilter():
    rules = _load_full_rules()
    src = "https://steamcommunity.com/linkfilter/?url=https%3A%2F%2Fexample.com%2Fsteam"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/steam"


def test_messenger_l_redirect():
    rules = _load_full_rules()
    src = "https://l.messenger.com/l.php?u=https%3A%2F%2Fexample.com%2Fmsgr"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/msgr"


def test_vk_away_to():
    rules = _load_full_rules()
    src = "https://vk.com/away.php?to=https%3A%2F%2Fexample.com%2Fvk"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/vk"


def test_ebay_rover_mpre():
    rules = _load_full_rules()
    src = "https://rover.ebay.com/rover/1/711-53200-19255-0/1?mpre=https%3A%2F%2Fexample.com%2Febay"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/ebay"


def test_digidip_url():
    rules = _load_full_rules()
    src = "https://redirect.digidip.net/?url=https%3A%2F%2Fexample.com%2Fdigidip"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/digidip"


def test_dpbolvw_url():
    rules = _load_full_rules()
    src = "https://dpbolvw.net/click?url=https%3A%2F%2Fexample.com%2Fdpb"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/dpb"


def test_flexlinkspro_url():
    rules = _load_full_rules()
    src = "https://track.flexlinkspro.com/click?url=https%3A%2F%2Fexample.com%2Fflp"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/flp"


def test_admitad_ulp():
    rules = _load_full_rules()
    src = "https://ad.admitad.com/g/redirect/?ulp=https%3A%2F%2Fexample.com%2Fadmitad"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/admitad"


def test_tradedoubler_deeplink():
    rules = _load_full_rules()
    src = "https://clk.tradedoubler.com/click?p=1&url=https%3A%2F%2Fexample.com%2Ftd"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/td"


def test_smartredirect_url():
    rules = _load_full_rules()
    src = "https://go.smartredirect.de/?url=https%3A%2F%2Fexample.com%2Fsr"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/sr"


def test_linksynergy_murl():
    rules = _load_full_rules()
    src = "https://click.linksynergy.com/deeplink?murl=https%3A%2F%2Fexample.com%2Fls"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/ls"


def test_facebook_strip_fbclid_ref():
    rules = _load_full_rules()
    src = "https://www.facebook.com/somepage?fbclid=abc&ref=foo"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://www.facebook.com/somepage"


def test_twitter_strip_src_and_params():
    rules = _load_full_rules()
    src = "https://twitter.com/some/status/1?src=hash&ref_url=https%3A%2F%2Fexample.com"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://twitter.com/some/status/1"


def test_global_rules_remove_utm():
    rules = _load_full_rules()
    src = "https://example.com/path?utm_source=abc&utm_campaign=def"
    cleaned = UrlCleaner(rules=rules).clean(src)
    assert cleaned == "https://example.com/path"


