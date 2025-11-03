import json
import re
import urllib.parse
from typing import Dict, Any, Optional, Tuple


class UrlCleaner:
    """
    ClearURLs-like URL cleaner.

    Features:
    - Provider identification via urlPattern regex (case-insensitive)
    - Apply redirections if configured (extract final URL from wrapper links)
    - Remove tracking params via provider rules, referralMarketing and globalRules
    - Respect exceptions (skip cleaning when matched)
    - Fallback behavior: if no provider matches, remove all query parameters
    - Always normalize: scheme/netloc lowercase, remove fragment, tidy path

    Notes:
    - Rules can be provided directly as a dict (same shape as ClearURLs data),
      or loaded from a local JSON file path, or from a remote URL.
    - Only a safe subset of ClearURLs features are implemented for simplicity.
    """

    DEFAULT_RULES_MIN = {
        "providers": {
            # Minimal Google rules: handle /url?q= redirection and common tracking params
            "google": {
                "forceRedirection": True,
                "urlPattern": r"^https?:\\/\\/(?:[a-z0-9-]+\\.)*?google(?:\\\.[a-z]{2,}){1,}",
                "rules": [
                    r"ved",
                    r"gws_[a-z]+",
                    r"ei",
                    r"source",
                    r"gs_[a-z]+",
                    r"aqs",
                    r"uact",
                    r"sxsrf",
                ],
                "referralMarketing": ["referrer"],
                "redirections": [
                    r"^https?:\\/\\/(?:[a-z0-9-]+\\.)*?google(?:\\\.[a-z]{2,}){1,}\\/url\\?.*?(?:url|q)=([^&]+)",
                ],
                "exceptions": [
                    r"^https?:\\/\\/mail\\.google\\.com\\/",
                    r"^https?:\\/\\/accounts\\.google\\.com\\/",
                ],
            },
            # Minimal YouTube: remove known tracking, keep v
            "youtube": {
                "urlPattern": r"^https?:\\/\\/(?:[a-z0-9-]+\\.)*?(?:youtube\\.com|youtu\\.be)",
                "rules": [
                    r"ab_channel",
                    r"utm_[a-z_]+",
                    r"si",
                    r"pp",
                    r"feature",
                    r"list",
                    r"index",
                    r"t",
                ],
            },
            # Global rules (will be merged separately)
            "globalRules": {
                "urlPattern": ".*",
                "rules": [
                    r"(?:%3F)?utm(?:_[a-z_]+)?",
                    r"(?:%3F)?mtm(?:_[a-z_]+)?",
                    r"(?:%3F)?ga_[a-z_]+",
                    r"fbclid",
                    r"gclid",
                    r"igshid",
                    r"mc_eid",
                    r"mc_cid",
                    r"ref",
                ],
            },
        }
    }

    def __init__(
        self,
        rules: Optional[Dict[str, Any]] = None,
        rules_file: Optional[str] = None,
        rules_url: Optional[str] = None,
    ) -> None:
        self.rules: Dict[str, Any] = self._load_rules(rules, rules_file, rules_url)
        self._compiled_providers: Dict[str, Dict[str, Any]] = self._compile_providers(
            self.rules.get("providers", {})
        )
        self._global_rules = self._compiled_providers.get("globalRules", {})

    def _load_rules(
        self,
        rules: Optional[Dict[str, Any]],
        rules_file: Optional[str],
        rules_url: Optional[str],
    ) -> Dict[str, Any]:
        if rules is not None:
            return rules
        if rules_file:
            try:
                with open(rules_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        if rules_url:
            try:
                import requests

                resp = requests.get(rules_url, timeout=5)
                if resp.ok:
                    return resp.json()
            except Exception:
                pass
        # Fallback to minimal embedded rules
        return self.DEFAULT_RULES_MIN

    def _compile_providers(self, providers: Dict[str, Any]) -> Dict[str, Any]:
        compiled: Dict[str, Any] = {}
        for name, cfg in providers.items():
            compiled_cfg = dict(cfg)
            # Compile urlPattern
            pattern = cfg.get("urlPattern", ".*")
            compiled_cfg["_urlPattern"] = re.compile(pattern, re.IGNORECASE)
            # Compile param-rule regexes
            compiled_cfg["_rule_regexes"] = [
                re.compile(r, re.IGNORECASE) for r in cfg.get("rules", [])
            ]
            compiled_cfg["_referral_regexes"] = [
                re.compile(r, re.IGNORECASE) for r in cfg.get("referralMarketing", [])
            ]
            compiled_cfg["_raw_regexes"] = [
                re.compile(r, re.IGNORECASE) for r in cfg.get("rawRules", [])
            ]
            compiled_cfg["_exception_regexes"] = [
                re.compile(r, re.IGNORECASE) for r in cfg.get("exceptions", [])
            ]
            compiled_cfg["_redirection_regexes"] = [
                re.compile(r, re.IGNORECASE) for r in cfg.get("redirections", [])
            ]
            compiled[name] = compiled_cfg
        return compiled

    def _matches_exception(self, cfg: Dict[str, Any], url: str) -> bool:
        return any(rx.search(url) for rx in cfg.get("_exception_regexes", []))

    def _apply_redirections(self, cfg: Dict[str, Any], url: str) -> Tuple[str, bool]:
        # Provider-specific fast path: Google wrapper links /url?q=... or /url?url=...
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc.lower()
            if "google." in host and parsed.path.startswith("/url"):
                q = urllib.parse.parse_qs(parsed.query)
                target = q.get("url", q.get("q", [None]))[0]
                if target:
                    return urllib.parse.unquote(target), True
        except Exception:
            pass

        # Generic wrapper parameters often used by providers (duckduckgo, adservices, tumblr, skimresources, etc.)
        try:
            parsed = urllib.parse.urlparse(url)
            q = urllib.parse.parse_qs(parsed.query)
            for key in (
                "url",
                "q",
                "uddg",
                "adurl",
                "u",
                "z",
                "to",
                "r",
                "mpre",
                "wgtarget",
                "murl",
                "ulp",
                "remoteUrl",
                "trg",
                "dest",
                "deeplinkurl",
                "ckurl",
                "htmlurl",
                "redirect",
                "redirect_url",
            ):
                if key in q and q[key]:
                    target = q[key][0]
                    if target:
                        return urllib.parse.unquote(target), True
        except Exception:
            pass

        for rx in cfg.get("_redirection_regexes", []):
            m = rx.search(url)
            if m and m.group(1):
                target = m.group(1)
                # decode percent-encoding
                target = urllib.parse.unquote(target)
                return target, True
        return url, False

    def _provider_for(self, url: str) -> Optional[Dict[str, Any]]:
        # Prefer robust host-based detection over complex regex
        try:
            netloc = urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            netloc = ""
        # Quick host checks
        if "google." in netloc:
            return self._compiled_providers.get("google")
        if "youtube.com" in netloc or "youtu.be" in netloc:
            return self._compiled_providers.get("youtube")
        # Fallback to regex search for any other future providers
        for name, cfg in self._compiled_providers.items():
            if name == "globalRules":
                continue
            if cfg["_urlPattern"].search(url):
                return cfg
        return None

    def _filter_query(self, cfg: Optional[Dict[str, Any]], query: str) -> str:
        if not query:
            return ""
        params = urllib.parse.parse_qsl(query, keep_blank_values=True)

        def should_remove(key: str, value: str) -> bool:
            # Apply provider rules
            if cfg is not None:
                for rx in cfg.get("_rule_regexes", []):
                    if rx.fullmatch(key) or rx.search(key):
                        return True
                for rx in cfg.get("_raw_regexes", []):
                    if rx.search(f"{key}={value}"):
                        return True
                for rx in cfg.get("_referral_regexes", []):
                    if rx.fullmatch(key) or rx.search(key):
                        return True
            # Apply global rules
            for rx in self._global_rules.get("_rule_regexes", []):
                if rx.fullmatch(key) or rx.search(key):
                    return True
            return False

        # If no provider matched, drop all params by default
        if cfg is None:
            return ""

        kept = [(k, v) for (k, v) in params if not should_remove(k, v)]
        return urllib.parse.urlencode(kept, doseq=True)

    def _normalize(self, url: str) -> str:
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return url
        scheme = (parsed.scheme or "http").lower()
        netloc = parsed.netloc.lower()
        path = parsed.path or "/"
        query = parsed.query
        # do not carry fragment
        fragment = ""
        return urllib.parse.urlunparse((scheme, netloc, path, parsed.params, query, fragment))

    def clean(self, url: str) -> str:
        if not url:
            return url
        # Normalize basic shape first for matching
        normalized_once = self._normalize(url)
        provider = self._provider_for(normalized_once)

        # Respect exceptions
        if provider and self._matches_exception(provider, normalized_once):
            return normalized_once

        # Apply redirection if any
        if provider:
            redirected, changed = self._apply_redirections(provider, normalized_once)
            if changed:
                normalized_once = self._normalize(redirected)

        # Re-identify after potential redirect
        provider = self._provider_for(normalized_once)

        parsed = urllib.parse.urlparse(normalized_once)
        filtered_query = self._filter_query(provider, parsed.query)
        cleaned = urllib.parse.urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                filtered_query,
                "",
            )
        )
        # Final tidy
        return self._normalize(cleaned)


