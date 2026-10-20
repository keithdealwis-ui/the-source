"""Local product detection: which catalogued SaaS product a URL belongs to (KEI-922).

One reference implementation of the detection rules, shared by the canonical build
(api/v1/domains.json and api/v1/catalogue.json), the MCP server's source_detect_saas and
the tests. extension/src/match.js implements the same semantics in JavaScript, and
extension/test/match.test.mjs checks the two agree URL by URL.

A rule is a host (or a host suffix for per-customer subdomains) and an optional path prefix:

    host         exact hostname, after lowercasing and stripping one leading "www."
    host_suffix  the hostname ends with this; it starts with "." or "-"
                 (".atlassian.net", "-my.sharepoint.com"), so "evilatlassian.net" never matches
    path_prefix  optional; matched on whole path segments: "/wiki" matches "/wiki" and
                 "/wiki/spaces/X", never "/wikipedia". A prefix ending in "/" is a plain prefix.

When several rules match, the most specific wins: a rule with a path prefix beats one
without, a longer path prefix beats a shorter one, an exact host beats a suffix, and a
longer suffix beats a shorter one. Remaining ties go to the first rule in order. This is
what lets one host carry several products: app.hubspot.com is HubSpot (CRM), but
app.hubspot.com/email is HubSpot Marketing Hub, and acme.atlassian.net/jira/servicedesk is
Jira Service Management while acme.atlassian.net/jira is Jira.

config/catalogue.yaml writes rules compactly, one string each, parsed by parse_rule():

    "slack.com"                    {"host": "slack.com"}
    "*.slack.com"                  {"host_suffix": ".slack.com"}
    "*-my.sharepoint.com"          {"host_suffix": "-my.sharepoint.com"}
    "atlassian.com/software/jira"  {"host": "atlassian.com", "path_prefix": "/software/jira"}
    "*.atlassian.net/wiki"         {"host_suffix": ".atlassian.net", "path_prefix": "/wiki"}

Standard library only: the hosted MCP server imports this module under python3 -S.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

MATCH_SEMANTICS = (
    "Lowercase the page hostname and strip one leading \"www.\". A rule matches when its host equals the "
    "hostname (or the hostname ends with its host_suffix) and, if it has a path_prefix, the URL path equals "
    "the prefix or continues it at a \"/\" (a prefix that ends in \"/\" is a plain prefix). When several rules "
    "match, the most specific wins: one with a path_prefix beats one without, a longer path_prefix beats a "
    "shorter one, an exact host beats a host_suffix, a longer host_suffix beats a shorter one; remaining ties "
    "go to the first rule in file order.")

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,}}$")
_SUFFIX = re.compile(rf"^[.-](?:{_LABEL}\.)+[a-z]{{2,}}$")
_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")


class RuleError(ValueError):
    pass


def parse_rule(text: str) -> dict:
    """Compact catalogue notation -> rule dict. Raises RuleError on anything malformed."""
    if not isinstance(text, str) or not text or text != text.strip():
        raise RuleError(f"rule {text!r} is not a plain string")
    if "://" in text or "?" in text or "#" in text:
        raise RuleError(f"rule {text!r}: write host[/path], without a scheme, query or fragment")
    hostpart, slash, path = text.partition("/")
    rule = {}
    if hostpart.startswith("*"):
        rule["host_suffix"] = hostpart[1:]
    else:
        rule["host"] = hostpart
    if slash:
        rule["path_prefix"] = "/" + path
    errors = rule_errors(rule)
    if errors:
        raise RuleError(f"rule {text!r}: {'; '.join(errors)}")
    return rule


def format_rule(rule: dict) -> str:
    host = rule["host"] if "host" in rule else "*" + rule["host_suffix"]
    return host + rule.get("path_prefix", "")


def rule_errors(rule: dict) -> list[str]:
    errs = []
    has_host, has_suffix = "host" in rule, "host_suffix" in rule
    if has_host == has_suffix:
        return ["needs exactly one of host / host_suffix"]
    if has_host:
        h = rule["host"]
        if h != h.lower() or not _HOST.match(h):
            errs.append(f"host {h!r} is not a lowercase hostname")
        elif h.startswith("www."):
            errs.append(f"host {h!r} starts with www. (pages are matched with one www. stripped)")
    else:
        s = rule["host_suffix"]
        if s != s.lower() or not _SUFFIX.match(s):
            errs.append(f"host_suffix {s!r} must start with '.' or '-' and end in a registrable domain")
    if "path_prefix" in rule:
        p = rule["path_prefix"]
        if not isinstance(p, str) or not _PATH.match(p) or p == "/":
            errs.append(f"path_prefix {p!r} must start with '/' and name at least one segment")
        elif "//" in p:
            errs.append(f"path_prefix {p!r} has an empty segment")
    extra = set(rule) - {"host", "host_suffix", "path_prefix", "saas_id"}
    if extra:
        errs.append(f"unknown keys {sorted(extra)}")
    return errs


def host_path(url: str):
    """The detection inputs: lowercase host without one leading www., and the path.
    Only http(s); a bare host gets https://. Never fetched."""
    u = str(url or "").strip()
    if "://" not in u:
        u = "https://" + u
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None, ""
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None, ""
    host = host[4:] if host.startswith("www.") else host
    return host, parts.path or "/"


def path_matches(prefix: str, path: str) -> bool:
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def rule_matches(rule: dict, host: str, path: str) -> bool:
    if "host" in rule:
        if host != rule["host"]:
            return False
    elif "host_suffix" in rule:
        if not host.endswith(rule["host_suffix"]):
            return False
    else:
        return False
    return "path_prefix" not in rule or path_matches(rule["path_prefix"], path)


def specificity(rule: dict) -> tuple:
    p = rule.get("path_prefix")
    return (p is not None, len(p or ""), "host" in rule, len(rule.get("host") or rule.get("host_suffix") or ""))


def match(host: str, path: str, rules: list[dict]) -> str | None:
    best, best_key = None, None
    for rule in rules:
        if not rule_matches(rule, host, path):
            continue
        key = specificity(rule)
        if best_key is None or key > best_key:
            best, best_key = rule, key
    return best["saas_id"] if best else None


def match_url(url: str, rules: list[dict]) -> str | None:
    host, path = host_path(url)
    return match(host, path, rules) if host else None


def example_urls(rule: dict) -> list[str]:
    """URLs a rule is written to catch, for detection tests: the bare rule and a deeper page."""
    host = rule["host"] if "host" in rule else "acme" + rule["host_suffix"]
    path = rule.get("path_prefix", "")
    base = f"https://{host}{path}"
    deep = base.rstrip("/") + "/example/page"
    return [base if path else base + "/", deep]
