#!/usr/bin/env python3
"""First-pass project-name availability checker.

This is a lightweight screening tool, not legal advice. It checks public
registry and namespace signals that are useful before committing to a name:

- PyPI, npm, crates.io, RubyGems
- GitHub user/org, a repo under a chosen owner, and exact-ish repo search
- Docker Hub repository plus a manual namespace link
- Common domain TLDs via RDAP

Examples:

    python scripts/name_check.py glassrail checkrail proofmark waymark
    python scripts/name_check.py --github-owner andrew-ellis-engineering --json glassrail
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_DOMAINS = ("com", "dev", "io", "ai")
DEFAULT_TIMEOUT_S = 12.0
USER_AGENT = "glassrail-name-check/0.1 (+https://github.com/andrew-ellis-engineering/glassrail)"
PRESETS = {
    "finalists": ("Glassrail", "Checkrail", "Proofmark", "Waymark", "Chartwright"),
    "favorites": (
        "Glassrail",
        "Checkrail",
        "Proofmark",
        "Waymark",
        "Chartwright",
        "Mica",
        "Stemma",
        "Pyxis",
        "Orrery",
        "Mortise",
        "Vellum",
        "Caliper",
        "Fiducial",
        "Witness",
    ),
    "broad": (
        "Glassrail",
        "Checkrail",
        "Proofmark",
        "Waymark",
        "Chartwright",
        "Mica",
        "Stemma",
        "Pyxis",
        "Orrery",
        "Mortise",
        "Vellum",
        "Caliper",
        "Fiducial",
        "Witness",
        "Stemma",
        "Indicia",
        "Cambium",
        "Rootnote",
        "Azimuth",
        "Rhumb",
        "Fathom",
        "Signalbox",
        "Airlock",
        "Keel",
        "Strata",
    ),
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    surface: str
    candidate: str
    status: str
    detail: str
    url: str


def slugify(name: str) -> str:
    """Return a conservative package/repo/domain slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug


def compact_slug(name: str) -> str:
    """Return an unhyphenated slug for names like Glassrail."""
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def variants(name: str) -> list[str]:
    slug = slugify(name)
    compact = compact_slug(name)
    seen: set[str] = set()
    out: list[str] = []
    for value in (slug, compact):
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def fetch_json(
    url: str,
    *,
    timeout_s: float,
    token: str | None = None,
    insecure: bool = False,
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urlopen(req, timeout=timeout_s, context=context) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body) if body else None
            except json.JSONDecodeError:
                return resp.status, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return exc.code, body
    except URLError as exc:
        return 0, {"error": str(exc.reason)}
    except TimeoutError:
        return 0, {"error": "timeout"}


def registry_result(
    *,
    name: str,
    surface: str,
    candidate: str,
    url: str,
    timeout_s: float,
    insecure: bool,
    taken_statuses: set[int] | None = None,
    free_statuses: set[int] | None = None,
) -> CheckResult:
    taken_statuses = taken_statuses or {200}
    free_statuses = free_statuses or {404}
    code, payload = fetch_json(url, timeout_s=timeout_s, insecure=insecure)
    if code in taken_statuses:
        status = "taken"
        detail = summarize_payload(payload)
    elif code in free_statuses:
        status = "available"
        detail = f"HTTP {code}"
    elif code == 0:
        status = "unknown"
        detail = summarize_payload(payload)
    else:
        status = "unknown"
        detail = f"HTTP {code}: {summarize_payload(payload)}"
    return CheckResult(name, surface, candidate, status, detail, url)


def summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        if "error" in payload:
            return str(payload["error"])
        if "message" in payload:
            return str(payload["message"])
        parts: list[str] = []
        if isinstance(payload.get("name"), str):
            parts.append(f"name={payload['name']}")
        if isinstance(payload.get("description"), str):
            parts.append(payload["description"][:120])
        if isinstance(payload.get("version"), str):
            parts.append(f"version={payload['version']}")
        dist_tags = payload.get("dist-tags")
        if isinstance(dist_tags, dict) and isinstance(dist_tags.get("latest"), str):
            parts.append(f"latest={dist_tags['latest']}")
        if isinstance(payload.get("time"), dict) and isinstance(
            payload["time"].get("modified"), str
        ):
            parts.append(f"modified={payload['time']['modified']}")
        if parts:
            return "; ".join(parts)
    if isinstance(payload, list):
        return f"{len(payload)} item(s)"
    if payload is None:
        return ""
    return str(payload)[:180]


def check_package_registries(name: str, *, timeout_s: float, insecure: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    for candidate in variants(name):
        quoted = quote(candidate)
        results.extend(
            [
                registry_result(
                    name=name,
                    surface="PyPI",
                    candidate=candidate,
                    url=f"https://pypi.org/pypi/{quoted}/json",
                    timeout_s=timeout_s,
                    insecure=insecure,
                ),
                registry_result(
                    name=name,
                    surface="npm",
                    candidate=candidate,
                    url=f"https://registry.npmjs.org/{quoted}",
                    timeout_s=timeout_s,
                    insecure=insecure,
                ),
                registry_result(
                    name=name,
                    surface="RubyGems",
                    candidate=candidate,
                    url=f"https://rubygems.org/api/v1/gems/{quoted}.json",
                    timeout_s=timeout_s,
                    insecure=insecure,
                ),
                registry_result(
                    name=name,
                    surface="crates.io",
                    candidate=candidate,
                    url=f"https://crates.io/api/v1/crates/{quoted}",
                    timeout_s=timeout_s,
                    insecure=insecure,
                ),
            ]
        )
    return results


def check_github(
    name: str,
    *,
    owner: str,
    token: str | None,
    timeout_s: float,
    insecure: bool,
) -> list[CheckResult]:
    candidate = slugify(name)
    quoted = quote(candidate)
    results: list[CheckResult] = []

    for surface, url in (
        ("GitHub user", f"https://api.github.com/users/{quoted}"),
        ("GitHub org", f"https://api.github.com/orgs/{quoted}"),
        ("GitHub repo", f"https://api.github.com/repos/{quote(owner)}/{quoted}"),
    ):
        code, payload = fetch_json(url, timeout_s=timeout_s, token=token, insecure=insecure)
        if code == 200:
            status = "taken"
            detail = github_detail(payload)
        elif code == 404:
            status = "available"
            detail = "HTTP 404"
        elif code == 0:
            status = "unknown"
            detail = summarize_payload(payload)
        else:
            status = "unknown"
            detail = f"HTTP {code}: {summarize_payload(payload)}"
        results.append(CheckResult(name, surface, candidate, status, detail, url))

    query = f"{candidate} in:name"
    url = "https://api.github.com/search/repositories?" + urlencode({"q": query, "per_page": "10"})
    code, payload = fetch_json(url, timeout_s=timeout_s, token=token, insecure=insecure)
    if code == 200 and isinstance(payload, dict):
        items = payload.get("items") or []
        hits = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            full_name = item.get("full_name", "?")
            stars = item.get("stargazers_count", 0)
            pushed = item.get("pushed_at", "?")
            hits.append(f"{full_name} ({stars} stars, pushed {pushed})")
        status = "taken" if hits else "available"
        detail = "; ".join(hits) if hits else "no repository-name hits"
    elif code == 0:
        status = "unknown"
        detail = summarize_payload(payload)
    else:
        status = "unknown"
        detail = f"HTTP {code}: {summarize_payload(payload)}"
    results.append(CheckResult(name, "GitHub repo search", candidate, status, detail, url))

    return results


def github_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return summarize_payload(payload)
    if "full_name" in payload:
        stars = payload.get("stargazers_count", 0)
        pushed = payload.get("pushed_at", "?")
        desc = payload.get("description") or ""
        return f"{payload['full_name']} ({stars} stars, pushed {pushed}) {desc}"[:180]
    login = payload.get("login")
    account_type = payload.get("type")
    repos = payload.get("public_repos")
    created = payload.get("created_at")
    return f"{login} ({account_type}, public_repos={repos}, created={created})"


def check_dockerhub(name: str, *, timeout_s: float, insecure: bool) -> list[CheckResult]:
    candidate = slugify(name)
    repo_url = f"https://hub.docker.com/v2/repositories/{quote(candidate)}/{quote(candidate)}/"
    return [
        CheckResult(
            name=name,
            surface="Docker Hub namespace",
            candidate=candidate,
            status="unknown",
            detail="Docker Hub does not expose a definitive unauthenticated namespace check",
            url=f"https://hub.docker.com/u/{quote(candidate)}",
        ),
        registry_result(
            name=name,
            surface="Docker Hub repo",
            candidate=f"{candidate}/{candidate}",
            url=repo_url,
            timeout_s=timeout_s,
            insecure=insecure,
        ),
    ]


def domain_rdap_url(domain: str) -> str | None:
    if domain.endswith(".com"):
        return f"https://rdap.verisign.com/com/v1/domain/{quote(domain.upper())}"
    if domain.endswith(".net"):
        return f"https://rdap.verisign.com/net/v1/domain/{quote(domain.upper())}"
    if domain.endswith(".dev") or domain.endswith(".app"):
        return f"https://rdap.nic.google/domain/{quote(domain)}"
    if domain.endswith(".io"):
        return f"https://rdap.nic.io/domain/{quote(domain.upper())}"
    if domain.endswith(".ai"):
        return f"https://rdap.whois.ai/domain/{quote(domain)}"
    return None


def check_domains(
    name: str, *, tlds: list[str], timeout_s: float, insecure: bool
) -> list[CheckResult]:
    candidate = slugify(name)
    results: list[CheckResult] = []
    for tld in tlds:
        domain = f"{candidate}.{tld.removeprefix('.')}"
        url = domain_rdap_url(domain)
        if url is None:
            results.append(
                CheckResult(
                    name,
                    "Domain RDAP",
                    domain,
                    "unknown",
                    "no built-in RDAP endpoint for this TLD",
                    "",
                )
            )
            continue
        code, payload = fetch_json(url, timeout_s=timeout_s, insecure=insecure)
        if code == 200:
            detail = domain_detail(payload)
            status = "taken"
        elif code == 404:
            detail = "RDAP 404"
            status = "available"
        elif code == 0:
            detail = summarize_payload(payload)
            status = "unknown"
        else:
            detail = f"HTTP {code}: {summarize_payload(payload)}"
            status = "unknown"
        results.append(CheckResult(name, "Domain RDAP", domain, status, detail, url))
    return results


def domain_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return summarize_payload(payload)
    events = payload.get("events") or []
    bits: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        action = event.get("eventAction")
        date = event.get("eventDate")
        if action in {"registration", "expiration"} and date:
            bits.append(f"{action}={date}")
    nameservers = payload.get("nameservers") or []
    ns = []
    for nameserver in nameservers[:2]:
        if isinstance(nameserver, dict) and nameserver.get("ldhName"):
            ns.append(nameserver["ldhName"])
    if ns:
        bits.append("ns=" + ",".join(ns))
    return "; ".join(bits) or "registered"


def manual_links(name: str) -> list[CheckResult]:
    candidate = slugify(name)
    quoted_phrase = quote(f'"{candidate}"')
    tm_query = quote(candidate)
    links = [
        (
            "USPTO manual",
            f"https://tmsearch.uspto.gov/search/search-results?query={tm_query}",
        ),
        (
            "Google exact search",
            f"https://www.google.com/search?q={quoted_phrase}",
        ),
        (
            "GitHub web search",
            f"https://github.com/search?q={quote(candidate)}&type=repositories",
        ),
    ]
    return [
        CheckResult(name, surface, candidate, "manual", "open and review", url)
        for surface, url in links
    ]


def check_name(
    name: str,
    *,
    github_owner: str,
    github_token: str | None,
    domains: list[str],
    timeout_s: float,
    include_manual: bool,
    insecure: bool,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.extend(check_package_registries(name, timeout_s=timeout_s, insecure=insecure))
    results.extend(
        check_github(
            name,
            owner=github_owner,
            token=github_token,
            timeout_s=timeout_s,
            insecure=insecure,
        )
    )
    results.extend(check_dockerhub(name, timeout_s=timeout_s, insecure=insecure))
    results.extend(check_domains(name, tlds=domains, timeout_s=timeout_s, insecure=insecure))
    if include_manual:
        results.extend(manual_links(name))
    return results


def print_markdown(results: list[CheckResult]) -> None:
    by_name: dict[str, list[CheckResult]] = {}
    for result in results:
        by_name.setdefault(result.name, []).append(result)

    for name, rows in by_name.items():
        print(f"## {name}")
        print()
        print("| Surface | Candidate | Status | Detail | URL |")
        print("|---|---|---|---|---|")
        for row in rows:
            print(
                "| "
                + " | ".join(
                    [
                        escape_md(row.surface),
                        f"`{escape_md(row.candidate)}`",
                        status_label(row.status),
                        escape_md(row.detail),
                        f"[link]({row.url})" if row.url else "",
                    ]
                )
                + " |"
            )
        print()


def escape_md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def status_label(status: str) -> str:
    return {
        "available": "available",
        "taken": "taken",
        "unknown": "unknown",
        "manual": "manual review",
    }.get(status, status)


def print_summary(results: list[CheckResult]) -> None:
    by_name: dict[str, list[CheckResult]] = {}
    for result in results:
        by_name.setdefault(result.name, []).append(result)

    print("| Name | Clear Signals | Taken Signals | Unknown | Notes |")
    print("|---|---:|---:|---:|---|")
    for name, rows in by_name.items():
        clear = sum(1 for row in rows if row.status == "available")
        taken = sum(1 for row in rows if row.status == "taken")
        unknown = sum(1 for row in rows if row.status == "unknown")
        notes = []
        for row in rows:
            if row.status == "taken" and row.surface in {
                "PyPI",
                "npm",
                "GitHub user",
                "GitHub org",
                "GitHub repo",
                "GitHub repo search",
                "Domain RDAP",
            }:
                notes.append(f"{row.surface}: {row.candidate}")
        print(
            f"| {escape_md(name)} | {clear} | {taken} | {unknown} | "
            f"{escape_md('; '.join(notes[:4]))} |"
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Names to check, e.g. Glassrail Checkrail")
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="Named candidate set to check",
    )
    parser.add_argument(
        "--github-owner",
        default="andrew-ellis-engineering",
        help=("GitHub owner to test repo availability under (default: andrew-ellis-engineering)"),
    )
    parser.add_argument(
        "--github-token",
        default=None,
        help="Optional GitHub token. If omitted, GITHUB_TOKEN is used when set.",
    )
    parser.add_argument(
        "--domains",
        default=",".join(DEFAULT_DOMAINS),
        help="Comma-separated TLDs to check via RDAP (default: com,dev,io,ai)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification for local machines with broken Python CA roots",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")
    parser.add_argument("--summary", action="store_true", help="Emit one summary row per name")
    parser.add_argument(
        "--no-manual-links",
        action="store_true",
        help="Omit manual trademark/search links",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Optional delay between names to be gentle with public APIs",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    names = list(args.names)
    if args.preset:
        names = [*PRESETS[args.preset], *names]
    names = list(dict.fromkeys(names))
    if not names:
        raise SystemExit("provide at least one name or --preset")

    domains = [part.strip().removeprefix(".") for part in args.domains.split(",") if part.strip()]
    token = args.github_token
    if token is None:
        token = os.environ.get("GITHUB_TOKEN")

    all_results: list[CheckResult] = []
    for i, name in enumerate(names):
        if i and args.sleep:
            time.sleep(args.sleep)
        all_results.extend(
            check_name(
                name,
                github_owner=args.github_owner,
                github_token=token,
                domains=domains,
                timeout_s=args.timeout,
                include_manual=not args.no_manual_links,
                insecure=args.insecure,
            )
        )

    if args.json:
        print(json.dumps([asdict(result) for result in all_results], indent=2))
    elif args.summary:
        print_summary(all_results)
    else:
        print_markdown(all_results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
