#!/usr/bin/env python3
"""Generate the static 13F monitoring page from SEC EDGAR filings."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import multiprocessing as mp
import re
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


USER_AGENT = "13f-report-2026-05-12 alicedingwow@example.com"
CACHE_DIR = Path(".sec-cache")
COMPANY_CACHE_DIR = Path(".company-cache")
COMPANY_PROFILE_CACHE = COMPANY_CACHE_DIR / "profiles.json"

MANAGERS = [
    ("Appaloosa Management", "0001656456", "appaloosa-management"),
    ("Berkshire Hathaway", "0001067983", "berkshire-hathaway"),
    ("Bridgewater Associates", "0001350694", "bridgewater-associates"),
    ("Citadel Advisors", "0001423053", "citadel-advisors"),
    ("Duquesne Family Office", "0001536411", "duquesne-family-office"),
    ("Lone Pine Capital", "0001061165", "lone-pine-capital"),
    ("Pershing Square Capital", "0001336528", "pershing-square-capital"),
    ("Point72 Asset Management", "0001603466", "point72-asset-management"),
    ("Renaissance Technologies", "0001037389", "renaissance-technologies"),
    ("Tiger Global Management", "0001167483", "tiger-global-management"),
    ("Two Sigma Investments", "0001179392", "two-sigma-investments"),
]

NAME_EXPANSIONS = {
    "ADR": "ADR",
    "AG": "AG",
    "AIRLS": "AIRLINES",
    "AMERN": "AMERICAN",
    "BK": "BANK",
    "BANCSHARES": "BANCSHARES",
    "CDA": "CANADA",
    "CL": "CLASS",
    "CMNTYS": "COMMUNITIES",
    "CO": "COMPANY",
    "COM": "COMMON",
    "COS": "COMPANIES",
    "CORP": "CORPORATION",
    "DEL": "",
    "ELEC": "ELECTRIC",
    "ENTMT": "ENTERTAINMENT",
    "FD": "FUND",
    "FINL": "FINANCIAL",
    "HLDG": "HOLDING",
    "HLDGS": "HOLDINGS",
    "INDS": "INDUSTRIES",
    "INTL": "INTERNATIONAL",
    "INVT": "INVESTMENT",
    "LTD": "LIMITED",
    "MASS": "",
    "MATLS": "MATERIALS",
    "MLS": "MILLS",
    "MNG": "MINING",
    "MTR": "MOTOR",
    "MTRS": "MOTORS",
    "NATL": "NATIONAL",
    "ONT": "",
    "PARE": "PARENT",
    "PETE": "PETROLEUM",
    "PMTS": "PAYMENTS",
    "PPTY": "PROPERTY",
    "PWR": "POWER",
    "RLTY": "REALTY",
    "RUBR": "RUBBER",
    "SHS": "SHARES",
    "STL": "STEEL",
    "SVCS": "SERVICES",
    "SV": "SERVICES",
    "SWITZ": "",
    "TECH": "TECHNOLOGY",
    "TR": "TRUST",
    "WKS": "WORKS",
}

FUND_ISSUER_HINTS = (
    "EXCHANGE TRADED",
    "ISHARES",
    "SPDR",
    "VANECK",
    "INVESCO",
    "DIMENSIONAL ETF",
    "FIRST TR",
    "AMPLIFY ETF",
    "PROSHARES",
    "WISDOMTREE",
)

MANUAL_PROFILES = {
    "00123Q104": "Real Estate / REIT - Mortgage",
    "00215W100": "Technology / Semiconductors",
    "05370A108": "Healthcare / Biotechnology",
    "060505682": "Financial Services / Banks - Diversified",
    "07782B104": "Healthcare / Biotechnology",
    "12503M108": "Financial Services / Financial Data & Stock Exchanges",
    "16119P108": "Communication Services / Telecom Services",
    "171757206": "Healthcare / Biotechnology",
    "20717M103": "Technology / Software - Infrastructure",
    "254687106": "Communication Services / Entertainment",
    "29445S100": "Industrials / Rental & Leasing Services",
    "30063P105": "Healthcare / Diagnostics & Research",
    "31946M103": "Financial Services / Banks - Regional",
    "35909D109": "Communication Services / Telecom Services",
    "413216300": "Basic Materials / Gold",
    "436440101": "Healthcare / Medical Instruments & Supplies",
    "59522J103": "Real Estate / REIT - Residential",
    "644535106": "Basic Materials / Gold",
    "650111107": "Communication Services / Publishing",
    "697900108": "Basic Materials / Silver",
    "758849103": "Real Estate / REIT - Retail",
    "832696405": "Consumer Defensive / Packaged Foods",
    "882508104": "Technology / Semiconductors",
    "912008109": "Consumer Defensive / Food Distribution",
    "G4766E116": "Healthcare / Drug Manufacturers - Specialty & Generic",
    "G7997R103": "Technology / Computer Hardware",
    "M2682V108": "Technology / Software - Infrastructure",
    "M7S64H106": "Technology / Software - Application",
}

COMPANY_TOKEN_DROP = {
    "ADR",
    "AG",
    "CLASS",
    "COMMON",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LIMITED",
    "LTD",
    "NEW",
    "PLC",
    "SHARES",
    "THE",
}


@dataclass
class Filing:
    form: str
    accession: str
    filed: str
    report: str


@dataclass
class Holding:
    issuer: str
    title: str
    cusip: str
    value: int
    shares: int
    put_call: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.cusip, self.title, self.put_call)


def fetch_cached_text(url: str, cache_dir: Path, user_agent: str, pause: float) -> str:
    cache_dir.mkdir(exist_ok=True)
    cache_path = cache_dir / hashlib.sha256(url.encode("utf-8")).hexdigest()
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()
            text = data.decode("utf-8", errors="replace")
            cache_path.write_text(text, encoding="utf-8")
            time.sleep(pause)
            return text
        except (OSError, socket.timeout, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def fetch_text(url: str) -> str:
    return fetch_cached_text(url, CACHE_DIR, USER_AGENT, 0.2)


def sec_json(url: str) -> dict:
    return json.loads(fetch_text(url))


def child_text(node: ET.Element, name: str) -> str:
    for child in list(node):
        if child.tag.split("}")[-1] == name:
            return (child.text or "").strip()
    return ""


def nested_text(node: ET.Element, path: tuple[str, ...]) -> str:
    current = node
    for part in path:
        found = None
        for child in list(current):
            if child.tag.split("}")[-1] == part:
                found = child
                break
        if found is None:
            return ""
        current = found
    return (current.text or "").strip()


def int_text(value: str) -> int:
    if not value:
        return 0
    return int(float(value.replace(",", "")))


def get_recent_13f(cik: str) -> list[Filing]:
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    data = sec_json(url)
    recent = data["filings"]["recent"]
    filings: list[Filing] = []
    for idx, form in enumerate(recent["form"]):
        if form not in {"13F-HR", "13F-HR/A"}:
            continue
        filings.append(
            Filing(
                form=form,
                accession=recent["accessionNumber"][idx],
                filed=recent["filingDate"][idx],
                report=recent["reportDate"][idx],
            )
        )
        if len(filings) == 2:
            break
    if len(filings) < 2:
        raise RuntimeError(f"Could not find two 13F filings for CIK {cik}")
    return filings


def archive_base(cik: str, accession: str) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}"
    )


def get_info_table_url(cik: str, accession: str) -> str:
    base = archive_base(cik, accession)
    data = sec_json(f"{base}/index.json")
    xml_names = [
        item["name"]
        for item in data["directory"]["item"]
        if item["name"].lower().endswith(".xml")
        and item["name"].lower() != "primary_doc.xml"
    ]
    if not xml_names:
        raise RuntimeError(f"No information table XML found for {cik} {accession}")
    return f"{base}/{xml_names[0]}"


def parse_holdings(xml_text: str) -> dict[tuple[str, str, str], Holding]:
    root = ET.fromstring(xml_text.encode("utf-8"))
    holdings: dict[tuple[str, str, str], Holding] = {}
    for info_table in root.iter():
        if info_table.tag.split("}")[-1] != "infoTable":
            continue
        issuer = child_text(info_table, "nameOfIssuer")
        title = child_text(info_table, "titleOfClass")
        cusip = child_text(info_table, "cusip")
        value = int_text(child_text(info_table, "value"))
        shares = int_text(nested_text(info_table, ("shrsOrPrnAmt", "sshPrnamt")))
        put_call = child_text(info_table, "putCall")
        holding = Holding(
            issuer=issuer,
            title=title,
            cusip=cusip,
            value=value,
            shares=shares,
            put_call=put_call,
        )
        if holding.key in holdings:
            current = holdings[holding.key]
            current.value += holding.value
            current.shares += holding.shares
        else:
            holdings[holding.key] = holding
    return holdings


def format_money(value: int) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value}"


def format_int(value: int) -> str:
    return f"{value:,}"


def pct_change(current: int, previous: int) -> int:
    if previous == 0:
        return 0
    return round((current - previous) / previous * 100)


def e(value: str) -> str:
    return html.escape(value, quote=True)


def industry_fallback(holding: Holding) -> str:
    issuer = holding.issuer.upper()
    title = holding.title.upper()
    combined = f"{issuer} {title}"
    is_etf = re.search(r"\bETF\b", combined) is not None
    is_fund_family = any(hint in combined for hint in FUND_ISSUER_HINTS)
    if is_etf or is_fund_family:
        return "ETF / fund"
    if "NOTE" in title:
        return "Convertible or corporate note"
    if holding.put_call:
        return f"{holding.put_call} option on underlying security"
    return ""


def clean_issuer_query(issuer: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9&. ]+", " ", issuer.upper())
    tokens = []
    for token in cleaned.split():
        replacement = NAME_EXPANSIONS.get(token, token)
        if replacement:
            tokens.extend(replacement.split())
    cleaned = " ".join(tokens)
    cleaned = re.sub(r"\b(FORMERLY|NEW|OLD)\b.*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title()


def normalized_company_name(name: str) -> str:
    return re.sub(r"\s+", " ", clean_issuer_query(name).upper()).strip()


def company_tokens(name: str) -> set[str]:
    tokens = {
        token
        for token in re.sub(r"[^A-Z0-9 ]+", " ", normalized_company_name(name)).split()
        if len(token) > 1
    }
    return tokens - COMPANY_TOKEN_DROP


def yfinance_quote_score(quote: dict, query: str) -> int:
    quote_type = quote.get("quoteType") or ""
    if quote_type not in {"EQUITY", "ETF", "MUTUALFUND"}:
        return -100
    score = 0
    if quote.get("sector") or quote.get("industry"):
        score += 50
    symbol = quote.get("symbol") or ""
    if symbol and "." not in symbol:
        score += 12
    name = " ".join(
        str(quote.get(key) or "")
        for key in ("shortname", "longname", "displayName")
    ).upper()
    query_tokens = company_tokens(query)
    name_tokens = company_tokens(name)
    score += len(query_tokens & name_tokens) * 8
    return score


def clean_profile_label(label: str) -> str:
    return (
        label.replace("\u2014", " - ")
        .replace("\u2013", "-")
        .replace("  ", " ")
        .strip()
    )


def profile_for_holding(holding: Holding) -> str:
    fallback = industry_fallback(holding)
    if fallback == "ETF / fund":
        return fallback
    if holding.cusip in MANUAL_PROFILES:
        return MANUAL_PROFILES[holding.cusip]
    try:
        import yfinance as yf
    except ImportError:
        return fallback or "Industry unavailable"

    queries = []
    for query in (holding.issuer, clean_issuer_query(holding.issuer)):
        query = query.strip()
        if query and query not in queries:
            queries.append(query)

    best_quote = None
    best_score = -100
    for query in queries:
        try:
            search = yf.Search(query, max_results=6)
        except Exception:
            continue
        for quote in search.quotes:
            score = yfinance_quote_score(quote, query)
            if score > best_score:
                best_score = score
                best_quote = quote

    if best_quote:
        quote_type = best_quote.get("quoteType") or ""
        sector = best_quote.get("sector") or ""
        industry = best_quote.get("industry") or ""
        if sector and industry:
            return clean_profile_label(f"{sector} / {industry}")
        if industry:
            return clean_profile_label(industry)
        if sector:
            return clean_profile_label(sector)
        if quote_type in {"ETF", "MUTUALFUND"}:
            return "ETF / fund"
    return fallback or "Industry unavailable"


def profile_lookup_worker(holding: Holding, queue: mp.Queue) -> None:
    try:
        queue.put(profile_for_holding(holding))
    except Exception:
        queue.put("")


def profile_for_holding_with_timeout(holding: Holding, timeout: int = 12) -> str:
    queue: mp.Queue = mp.Queue()
    process = mp.Process(target=profile_lookup_worker, args=(holding, queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(2)
        return industry_fallback(holding) or "Industry unavailable"
    if not queue.empty():
        return queue.get() or industry_fallback(holding) or "Industry unavailable"
    return industry_fallback(holding) or "Industry unavailable"


def collect_displayed_holdings(results: list[dict]) -> list[Holding]:
    displayed: dict[tuple[str, str, str], Holding] = {}
    for result in results:
        for bucket_name in ("new", "sold", "add", "reduce"):
            for holding in result[bucket_name][:25]:
                displayed.setdefault(holding.key, holding)
    crowd: dict[str, Holding] = {}
    counts: dict[str, int] = {}
    for result in results:
        for holding in result["new"]:
            if holding.put_call:
                continue
            crowd.setdefault(holding.cusip, holding)
            counts[holding.cusip] = counts.get(holding.cusip, 0) + 1
    for cusip, count in counts.items():
        if count >= 3:
            holding = crowd[cusip]
            displayed.setdefault(holding.key, holding)
    return list(displayed.values())


def build_profiles(holdings: list[Holding]) -> dict[tuple[str, str, str], str]:
    profiles: dict[tuple[str, str, str], str] = {}
    COMPANY_CACHE_DIR.mkdir(exist_ok=True)
    cached_profiles = {}
    if COMPANY_PROFILE_CACHE.exists():
        cached_profiles = json.loads(COMPANY_PROFILE_CACHE.read_text(encoding="utf-8"))
    total = len(holdings)
    for index, holding in enumerate(holdings, 1):
        cache_key = "|".join(holding.key)
        if cache_key not in cached_profiles:
            cached_profiles[cache_key] = profile_for_holding_with_timeout(holding)
        profiles[holding.key] = cached_profiles[cache_key]
        if index == 1 or index % 25 == 0 or index == total:
            COMPANY_PROFILE_CACHE.write_text(
                json.dumps(cached_profiles, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"Enriched company profiles: {index}/{total}", flush=True)
    return profiles


def security_cell(holding: Holding, profile: str = "") -> str:
    option = f" · {e(holding.put_call)} option" if holding.put_call else ""
    meta = f"{e(holding.title)}{option}"
    profile_html = (
        f'<div class="security-profile">{e(profile)}</div>'
        if profile
        else ""
    )
    return (
        '<div class="security-name">'
        f'<div class="security-en">{e(holding.issuer)}</div>'
        f'<div class="security-meta">{meta}</div>'
        f"{profile_html}"
        "</div>"
    )


def holding_row(
    holding: Holding,
    value_label: str,
    share_or_change: str,
    cls: str = "",
    profile: str = "",
) -> str:
    change_class = f' class="{cls}"' if cls else ""
    return (
        "<tr>"
        f"<td>{security_cell(holding, profile)}</td>"
        f'<td><span class="cusip">{e(holding.cusip)}</span></td>'
        f'<td class="num">{value_label}</td>'
        f'<td class="num"><span{change_class}>{share_or_change}</span></td>'
        "</tr>"
    )


def table(headers: list[str], rows: list[str], empty: str) -> str:
    if not rows:
        return f'<div class="empty">{e(empty)}</div>'
    head = "".join(
        f'<th class="{"num" if idx >= 2 else ""}">{e(header)}</th>'
        for idx, header in enumerate(headers)
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def bucket(title: str, cls: str, count: int, body: str) -> str:
    return (
        '<div class="bucket">'
        f'<span class="bucket-title {cls}">{e(title)} ({count})</span>'
        f"{body}</div>"
    )


def section_for_manager(result: dict, profiles: dict[tuple[str, str, str], str]) -> str:
    current = result["current"]
    previous = result["previous"]
    latest = result["latest"]
    previous_filing = result["previous_filing"]
    total_value = sum(h.value for h in current.values())
    section_meta = (
        f"报告期 {latest.report} · {len(current)} 个持仓 · "
        f"{format_money(total_value)} · filed {latest.filed} · 对比 {previous_filing.report}"
    )

    def rows_for(items: list[Holding], mode: str) -> list[str]:
        rows = []
        for holding in items:
            profile = profiles.get(holding.key, "")
            if mode == "new":
                rows.append(holding_row(holding, format_money(holding.value), format_int(holding.shares), profile=profile))
            elif mode == "sold":
                rows.append(holding_row(holding, format_money(holding.value), format_int(holding.shares), profile=profile))
            elif mode == "add":
                previous_holding = previous[holding.key]
                change = pct_change(holding.shares, previous_holding.shares)
                rows.append(
                    holding_row(
                        holding,
                        format_money(holding.value),
                        f"+{change}%",
                        "pct-up",
                        profile,
                    )
                )
            elif mode == "reduce":
                previous_holding = previous[holding.key]
                change = pct_change(holding.shares, previous_holding.shares)
                rows.append(
                    holding_row(
                        holding,
                        format_money(holding.value),
                        f"{change}%",
                        "pct-down",
                        profile,
                    )
                )
        return rows

    new_table = table(["标的", "CUSIP", "市值", "股数"], rows_for(result["new"][:25], "new"), "无新建仓")
    sold_table = table(["标的", "CUSIP", "原市值", "原股数"], rows_for(result["sold"][:25], "sold"), "无清仓")
    add_table = table(["标的", "CUSIP", "市值", "变化"], rows_for(result["add"][:25], "add"), "无超过 50% 的加仓")
    reduce_table = table(["标的", "CUSIP", "市值", "变化"], rows_for(result["reduce"][:25], "reduce"), "无超过 50% 的减仓")

    return (
        f'<section id="{result["slug"]}" class="section">'
        '<div class="section-header">'
        f'<div class="section-title">{e(result["name"])}</div>'
        f'<div class="section-meta">{e(section_meta)}</div>'
        "</div>"
        f'{bucket("新建仓", "bucket-new", len(result["new"]), new_table)}'
        f'{bucket("清仓", "bucket-sold", len(result["sold"]), sold_table)}'
        f'{bucket("加仓 >50%", "bucket-add", len(result["add"]), add_table)}'
        f'{bucket("减仓 >50%", "bucket-reduce", len(result["reduce"]), reduce_table)}'
        "</section>"
    )


def build_report() -> list[dict]:
    results = []
    for name, cik, slug in MANAGERS:
        filings = get_recent_13f(cik)
        latest, previous_filing = filings[0], filings[1]
        current_url = get_info_table_url(cik, latest.accession)
        previous_url = get_info_table_url(cik, previous_filing.accession)
        current = parse_holdings(fetch_text(current_url))
        previous = parse_holdings(fetch_text(previous_url))

        new = [h for key, h in current.items() if key not in previous]
        sold = [h for key, h in previous.items() if key not in current]
        add = [
            h
            for key, h in current.items()
            if key in previous and previous[key].shares > 0 and h.shares >= previous[key].shares * 1.5
        ]
        reduce = [
            h
            for key, h in current.items()
            if key in previous
            and previous[key].shares > 0
            and h.shares <= previous[key].shares * 0.5
            and h.shares > 0
        ]

        results.append(
            {
                "name": name,
                "cik": cik,
                "slug": slug,
                "latest": latest,
                "previous_filing": previous_filing,
                "current": current,
                "previous": previous,
                "new": sorted(new, key=lambda h: h.value, reverse=True),
                "sold": sorted(sold, key=lambda h: h.value, reverse=True),
                "add": sorted(add, key=lambda h: h.value, reverse=True),
                "reduce": sorted(reduce, key=lambda h: h.value, reverse=True),
            }
        )
        print(f"Fetched {name}: {latest.report} vs {previous_filing.report}")
    return results


def crowd_section(results: list[dict], profiles: dict[tuple[str, str, str], str]) -> tuple[str, int]:
    crowd: dict[str, dict] = {}
    for result in results:
        for holding in result["new"]:
            if holding.put_call:
                continue
            entry = crowd.setdefault(
                holding.cusip,
                {"holding": holding, "count": 0, "value": 0, "managers": []},
            )
            entry["count"] += 1
            entry["value"] += holding.value
            entry["managers"].append(result["name"])
    signals = [entry for entry in crowd.values() if entry["count"] >= 3]
    signals.sort(key=lambda item: (item["count"], item["value"]), reverse=True)
    rows = []
    for entry in signals:
        holding = entry["holding"]
        rows.append(
            "<tr>"
            f"<td>{security_cell(holding, profiles.get(holding.key, ''))}</td>"
            f'<td><span class="cusip">{e(holding.cusip)}</span></td>'
            f'<td class="num">{entry["count"]}</td>'
            f'<td class="num">{format_money(entry["value"])}</td>'
            f'<td><span class="institutions">{e(", ".join(entry["managers"]))}</span></td>'
            "</tr>"
        )
    body = table(
        ["标的", "CUSIP", "机构数", "合计市值", "关联机构"],
        rows,
        "本期没有 3 家及以上机构同时新建仓的标的",
    )
    latest_period = max(result["latest"].report for result in results)
    section = (
        '<section id="crowd" class="section">'
        '<div class="section-header">'
        '<div class="section-title">集中新建仓</div>'
        f'<div class="section-meta">≥3 家机构在 {e(latest_period)} 同季新建仓的标的</div>'
        "</div>"
        f'{bucket("多家机构同期新建仓", "bucket-crowd", len(signals), body)}'
        "</section>"
    )
    return section, len(signals)


def render(results: list[dict]) -> str:
    generated = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    latest_period = max(result["latest"].report for result in results)
    profiles = build_profiles(collect_displayed_holdings(results))
    significant_count = sum(
        len(result["new"]) + len(result["sold"]) + len(result["add"]) + len(result["reduce"])
        for result in results
    )
    crowd_html, crowd_count = crowd_section(results, profiles)
    nav_links = [
        f'<a href="#crowd">集中新建仓 <span class="pill">{crowd_count}</span></a>'
    ] + [f'<a href="#{result["slug"]}">{e(result["name"])}</a>' for result in results]
    sections = crowd_html + "".join(section_for_manager(result, profiles) for result in results)
    style = """
:root {
  --bg: #fafaf7;
  --card: #ffffff;
  --border: rgba(0,0,0,0.08);
  --text: #1a1a1a;
  --muted: #6b6b6b;
  --green: #0f6e56; --green-bg: #e1f5ee;
  --red: #a32d2d;   --red-bg: #fcebeb;
  --amber: #854f0b; --amber-bg: #faeeda;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1a1a18;
    --card: #2c2c2a;
    --border: rgba(255,255,255,0.1);
    --text: #f1efe8;
    --muted: #b4b2a9;
    --green: #5dcaa5; --green-bg: rgba(15,110,86,0.18);
    --red: #f09595;   --red-bg: rgba(163,45,45,0.18);
    --amber: #ef9f27; --amber-bg: rgba(133,79,11,0.20);
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; padding: 32px 20px;
  line-height: 1.55; font-size: 15px;
}
.container { max-width: 1100px; margin: 0 auto; }
header { margin-bottom: 24px; }
h1 { font-size: 26px; font-weight: 500; margin: 0 0 4px; }
.subtitle { color: var(--muted); font-size: 13px; }
.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin: 20px 0;
}
.stat {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 18px;
}
.stat-label {
  color: var(--muted); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px;
}
.stat-value { font-size: 24px; font-weight: 500; font-variant-numeric: tabular-nums; }
nav {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 12px 18px;
  margin-bottom: 24px; font-size: 14px; line-height: 2;
}
nav strong { font-weight: 500; margin-right: 8px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
nav a { color: var(--text); text-decoration: none; margin-right: 14px; }
nav a:hover { text-decoration: underline; }
nav .pill {
  display: inline-block; margin-left: 4px; padding: 0 6px;
  background: var(--amber-bg); color: var(--amber);
  border-radius: 4px; font-size: 11px; font-weight: 500;
}
.section {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 22px 24px;
  margin-bottom: 16px; scroll-margin-top: 16px;
}
.section-header {
  display: flex; justify-content: space-between; align-items: baseline;
  flex-wrap: wrap; gap: 8px;
  margin-bottom: 8px; padding-bottom: 12px; border-bottom: 1px solid var(--border);
}
.section-title { font-size: 18px; font-weight: 500; }
.section-meta { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.bucket { margin-top: 18px; }
.bucket-title {
  font-size: 11px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.06em;
  padding: 4px 10px; border-radius: 6px;
  display: inline-block; margin-bottom: 6px;
}
.bucket-new, .bucket-add { color: var(--green); background: var(--green-bg); }
.bucket-sold, .bucket-reduce { color: var(--red); background: var(--red-bg); }
.bucket-crowd { color: var(--amber); background: var(--amber-bg); }
table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 4px; }
th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 500; font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.05em; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.empty { color: var(--muted); font-size: 13px; padding: 8px 0; font-style: italic; }
.pct-up { color: var(--green); font-variant-numeric: tabular-nums; }
.pct-down { color: var(--red); font-variant-numeric: tabular-nums; }
.institutions { color: var(--muted); font-size: 12px; max-width: 320px; display: inline-block; }
.cusip { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; color: var(--muted); }
.security-name { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.security-en { font-weight: 500; }
.security-meta { color: var(--muted); font-size: 12px; line-height: 1.35; }
.security-profile { color: var(--amber); font-size: 12px; line-height: 1.35; }
footer {
  margin-top: 32px; padding-top: 16px;
  color: var(--muted); font-size: 12px; text-align: center;
  border-top: 1px solid var(--border);
}
footer a { color: var(--muted); }
@media (max-width: 640px) {
  body { padding: 16px 12px; }
  .section { padding: 16px; }
  th, td { padding: 6px 8px; font-size: 13px; }
  .stat-value { font-size: 22px; }
  .institutions { max-width: 180px; overflow: hidden;
    white-space: nowrap; text-overflow: ellipsis; }
}
""".strip()

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>13F 持仓监控 — {e(generated[:10])}</title>
<style>
{style}
</style>
</head>
<body>
<div class="container">
<header>
<h1>13F 持仓监控报告</h1>
<div class="subtitle">生成于 {e(generated)} · 最新报告期 {e(latest_period)}</div>
</header>
<div class="summary">
  <div class="stat"><div class="stat-label">监控机构</div><div class="stat-value">{len(results)}</div></div>
  <div class="stat"><div class="stat-label">最新报告期</div><div class="stat-value">{e(latest_period)}</div></div>
  <div class="stat"><div class="stat-label">重大变化</div><div class="stat-value">{significant_count}</div></div>
  <div class="stat"><div class="stat-label">集中信号</div><div class="stat-value">{crowd_count}</div></div>
</div>
<nav><strong>跳转至：</strong> {" ".join(nav_links)}</nav>
{sections}
<footer>
数据来源 <a href="https://www.sec.gov/edgar/searchedgar/companysearch">SEC EDGAR</a> ·
行业参考 Yahoo Finance search/profile ·
13F 披露存在 45 天延迟 ·
市值来自 13F information table 的 value 字段 ·
仅用于信息整理，不构成投资建议
</footer>
</div>
</body>
</html>
"""


def main() -> None:
    results = build_report()
    Path("index.html").write_text(render(results), encoding="utf-8")
    print("Wrote index.html")


if __name__ == "__main__":
    main()
