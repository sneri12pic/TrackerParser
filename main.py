from bs4 import BeautifulSoup
import re
import json
import sys
import os
import argparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from datetime import datetime

TARGET_COMPONENT_SELECTOR = ".col-span-full.grid.grid-cols-subgrid.gap-y-2"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def extract_stat(row, label):
    """
    Finds a stat block by its label text (e.g. 'TRS', 'K/D', 'DDΔ', 'HS%', 'ACS')
    and returns the value text next to it.
    """
    for name in row.select(".stat-name .truncate"):
        if clean_spaces(name.get_text()) == label:
            name_value = name.find_parent(class_=re.compile(r"\bname-value\b"))
            if not name_value:
                continue
            val = name_value.select_one(".stat-value .truncate, .value .truncate, .value")
            return clean_spaces(val.get_text()) if val else None
    return None

def extract_kda(row):
    # First try the old format: "12 K", "7 D", "10 A"
    kills = deaths = assists = None
    substats = [clean_spaces(x.get_text()) for x in row.select(".substats .value")]
    for s in substats:
        m = re.match(r"(\d+)\s*K\b", s)
        if m:
            kills = int(m.group(1))
        m = re.match(r"(\d+)\s*D\b", s)
        if m:
            deaths = int(m.group(1))
        m = re.match(r"(\d+)\s*A\b", s)
        if m:
            assists = int(m.group(1))

    if kills is not None and deaths is not None and assists is not None:
        return kills, deaths, assists

    # New format fallback: three numeric spans inside K/D stat list (K / D / A).
    for name in row.select(".stat-name .truncate"):
        if clean_spaces(name.get_text()) != "K/D":
            continue
        name_value = name.find_parent(class_=re.compile(r"\bname-value\b"))
        search_root = name_value if name_value else row
        values = []
        for sp in search_root.select(
            ".v3-separate-slash span.value, .v3-separate-slash span.truncate, .stat-list span.value"
        ):
            t = clean_spaces(sp.get_text())
            if re.match(r"^\d+$", t):
                values.append(int(t))
        if len(values) >= 3:
            return values[0], values[1], values[2]

    # Final fallback: any slash-separated stat list with exactly three numbers.
    for stat_list in row.select(".stat-list.v3-separate-slash, .v3-separate-slash"):
        values = []
        for sp in stat_list.select("span.value, span.truncate"):
            t = clean_spaces(sp.get_text())
            if re.match(r"^\d+$", t):
                values.append(int(t))
        if len(values) == 3:
            return values[0], values[1], values[2]

    return kills, deaths, assists

def parse_matches(html: str):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(".v3-match-row")

    results = []
    for row in rows:
        # agent (first image alt in the row)
        agent_img = row.select_one("img[alt]")
        agent = agent_img.get("alt") if agent_img else None

        # time ago: usually a span containing 'h ago' etc
        time_span = None
        for sp in row.select("span"):
            t = clean_spaces(sp.get_text())
            if re.search(r"\b(ago|min|h|d)\b", t) and "Score" not in t:
                # pick first plausible "11h ago" / "12h ago"
                if re.search(r"\b\d+\s*(s|m|h|d)\s*ago\b|\b\d+(s|m|h|d)\s*ago\b|\b\d+(s|m|h|d)\b\s*ago\b|\b\d+h ago\b", t):
                    time_span = t
                    break
                if re.search(r"\b\d+\s*(h|m|d)\s*ago\b|\b\d+(h|m|d)\s*ago\b", t):
                    time_span = t
                    break
        time_ago = time_span

        # map name + placement chip live in the big bold line:
        # <span ...>Abyss <span class="v3-chip ...">3rd</span></span>
        map_container = row.select_one("span.inline-flex.items-center.gap-2.text-16")
        if not map_container:
            # fallback for responsive variants
            map_container = row.select_one("span.inline-flex.items-center.gap-2")

        placement = None
        map_name = None
        if map_container:
            chip = map_container.select_one(".v3-chip")
            placement = clean_spaces(chip.get_text()) if chip else None

            # map name is the text node before chip
            # easiest: take full text and remove placement if present
            full = clean_spaces(map_container.get_text(" ", strip=True))
            if placement and full.endswith(placement):
                map_name = clean_spaces(full[: -len(placement)])
            else:
                map_name = full

        # score "13 : 3"
        score_el = row.select_one(".value.inline-flex")
        score = clean_spaces(score_el.get_text()) if score_el else None
        if score:
            score = score.replace(" : ", ":").replace(" :", ":").replace(": ", ":")

        # rank icon text from <img alt="Ascendant 2" ...> within the rank area
        # there are multiple imgs (agent, TRS badge, rank icon) so target tier icons:
        rank_img = None
        for img in row.select("img[alt]"):
            alt = clean_spaces(img.get("alt", ""))
            if re.search(r"\b(Iron|Bronze|Silver|Gold|Platinum|Diamond|Ascendant|Immortal|Radiant)\b", alt):
                rank_img = img
                break
        rank = clean_spaces(rank_img.get("alt")) if rank_img else None

        # stats
        trs = extract_stat(row, "TRS")
        kd = extract_stat(row, "K/D")
        dd_delta = extract_stat(row, "DDΔ")
        hs_pct = extract_stat(row, "HS%")
        acs = extract_stat(row, "ACS")

        kills, deaths, assists = extract_kda(row)

        results.append({
            "agent": agent,
            "time_ago": time_ago,
            "map": map_name,
            "placement": placement,
            "score": score,
            "trs": int(trs) if (trs and trs.isdigit()) else trs,
            "rank": rank,
            "kd": float(kd) if kd and re.match(r"^\d+(\.\d+)?$", kd) else kd,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "dd_delta": int(dd_delta) if dd_delta and re.match(r"^-?\d+$", dd_delta) else dd_delta,
            "hs_pct": float(hs_pct.replace("%","")) if hs_pct and "%" in hs_pct else hs_pct,
            "acs": int(acs) if acs and re.match(r"^\d+$", acs) else acs,
        })

    return results

def fetch_html(url: str, cookie: str | None = None, referer: str | None = None) -> str:
    headers = dict(DEFAULT_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer

    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as response:
        content_type = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(content_type, errors="replace")

def parse_cookie_header(cookie: str | None) -> list[tuple[str, str]]:
    if not cookie:
        return []
    parsed = []
    for part in cookie.split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            parsed.append((name, value))
    return parsed

def fetch_html_rendered(
    url: str,
    selector: str | None = None,
    ready_selector: str | None = ".v3-match-row",
    loading_text: str | None = "loading profile",
    loading_selector: str | None = ".v3-card.mx-4.w-full.max-w-80.items-center",
    cookie: str | None = None,
    referer: str | None = None,
    wait_ms: int = 4000,
    selector_timeout_ms: int = 15000,
    headless: bool = True,
    debug: bool = False,
) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Install with:\n"
            "pip install playwright\n"
            "python -m playwright install chromium"
        ) from e

    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer

    parsed_url = urlparse(url)
    domain = parsed_url.hostname
    cookies = parse_cookie_header(cookie)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            extra_http_headers=headers,
        )
        if domain and cookies:
            context.add_cookies(
                [
                    {"name": name, "value": value, "domain": domain, "path": "/"}
                    for name, value in cookies
                ]
            )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        if loading_selector:
            try:
                page.wait_for_function(
                    """([selector]) => {
                        if (!selector) return true;
                        return !document.querySelector(selector);
                    }""",
                    [loading_selector],
                    timeout=selector_timeout_ms,
                )
            except Exception:
                pass
        if loading_text:
            try:
                page.wait_for_function(
                    """([text]) => {
                        const body = document.body;
                        if (!body) return false;
                        return !body.innerText.toLowerCase().includes((text || "").toLowerCase());
                    }""",
                    [loading_text],
                    timeout=selector_timeout_ms,
                )
            except Exception:
                pass
        if ready_selector:
            try:
                page.wait_for_selector(ready_selector, timeout=selector_timeout_ms)
            except Exception:
                pass
        if selector:
            try:
                page.wait_for_selector(selector, timeout=selector_timeout_ms)
            except Exception:
                # Some pages lazy-load; continue and try after a scroll + extra wait.
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
                try:
                    page.wait_for_selector(selector, timeout=3000)
                except Exception:
                    pass
        page.wait_for_timeout(wait_ms)
        html = page.content()
        if debug:
            title = page.title()
            print(f"[debug] Rendered title: {title}")
            print(f"[debug] Rendered HTML length: {len(html)}")
            if loading_selector:
                loading_selector_still_present = False
                try:
                    loading_selector_still_present = (
                        BeautifulSoup(html, "html.parser").select_one(loading_selector) is not None
                    )
                except Exception:
                    pass
                print(
                    f"[debug] Loading selector present after render: "
                    f"{loading_selector_still_present}"
                )
            if loading_text:
                print(
                    f"[debug] Loading text present after render: "
                    f"{loading_text.lower() in html.lower()}"
                )
            if "Just a moment" in title or "cf-challenge" in html.lower():
                print("[debug] Potential anti-bot challenge detected.")
        browser.close()
        return html

def extract_component_html(html: str, selector: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    components = soup.select(selector)

    # Fallback for the default class target: match by required class set.
    if not components and selector == TARGET_COMPONENT_SELECTOR:
        required_classes = set(TARGET_COMPONENT_SELECTOR.strip(".").split("."))
        for tag in soup.find_all(True):
            tag_classes = set(tag.get("class", []))
            if required_classes.issubset(tag_classes):
                components.append(tag)

    if not components:
        return None
    return "\n".join(str(component) for component in components)

if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        description=(
            "Parse match rows either from pasted HTML or from a fetched URL. "
            "When using --url, the parser first extracts a specific component."
        )
    )
    arg_parser.add_argument(
        "-u",
        "--url",
        help="Page URL to fetch before parsing.",
    )
    arg_parser.add_argument(
        "--cookie",
        help="Optional raw Cookie header value copied from your browser.",
    )
    arg_parser.add_argument(
        "--referer",
        help="Optional Referer header. Useful for sites that block direct requests.",
    )
    arg_parser.add_argument(
        "--render-js",
        action="store_true",
        help="Use Playwright to render JavaScript before extracting selector.",
    )
    arg_parser.add_argument(
        "--wait-ms",
        type=int,
        default=4000,
        help="Milliseconds to wait after page load when using --render-js (default: 4000).",
    )
    arg_parser.add_argument(
        "--selector-timeout-ms",
        type=int,
        default=15000,
        help="Timeout waiting for selector in JS-render mode (default: 15000).",
    )
    arg_parser.add_argument(
        "--ready-selector",
        default=".v3-match-row",
        help="Selector to wait for in JS-render mode before extraction (default: .v3-match-row).",
    )
    arg_parser.add_argument(
        "--loading-text",
        default="loading profile",
        help="Text to wait to disappear in JS-render mode (default: loading profile).",
    )
    arg_parser.add_argument(
        "--loading-selector",
        default=".v3-card.mx-4.w-full.max-w-80.items-center",
        help=(
            "Selector to wait to disappear in JS-render mode "
            "(default: .v3-card.mx-4.w-full.max-w-80.items-center)."
        ),
    )
    arg_parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Playwright with a visible browser window (can help with anti-bot checks).",
    )
    arg_parser.add_argument(
        "--debug-fetch",
        action="store_true",
        help="Print fetch diagnostics (title/HTML length/challenge hints).",
    )
    arg_parser.add_argument(
        "-s",
        "--selector",
        default=TARGET_COMPONENT_SELECTOR,
        help=(
            "CSS selector for component extraction from fetched page "
            f"(default: {TARGET_COMPONENT_SELECTOR})"
        ),
    )
    args = arg_parser.parse_args()

    html = ""
    if args.url:
        page_html = ""
        extracted_html = None
        try:
            page_html = fetch_html(args.url, cookie=args.cookie, referer=args.referer)
            extracted_html = extract_component_html(page_html, args.selector)
        except HTTPError as e:
            if e.code == 403 and not args.render_js:
                print(
                    "Failed to fetch URL with HTTP 403 (Forbidden). "
                    "This site is blocking non-browser requests.\n"
                    "Try one of these:\n"
                    "1) Use browser cookie + referer\n"
                    "2) Use JS rendering: --render-js\n"
                    "Example:\n"
                    "python main.py --url \"<url>\" --render-js"
                )
                sys.exit(1)
            else:
                print(f"Failed to fetch URL '{args.url}': {e}")
                if not args.render_js:
                    sys.exit(1)
        except (URLError, TimeoutError, ValueError) as e:
            print(f"Failed to fetch URL '{args.url}': {e}")
            if not args.render_js:
                sys.exit(1)

        if not extracted_html and args.render_js:
            print("Trying JS-rendered fetch for selector extraction...")
            try:
                rendered_html = fetch_html_rendered(
                    args.url,
                    selector=args.selector,
                    ready_selector=args.ready_selector,
                    loading_text=args.loading_text,
                    loading_selector=args.loading_selector,
                    cookie=args.cookie,
                    referer=args.referer,
                    wait_ms=args.wait_ms,
                    selector_timeout_ms=args.selector_timeout_ms,
                    headless=not args.headed,
                    debug=args.debug_fetch,
                )
                rendered_extracted = extract_component_html(rendered_html, args.selector)
                if rendered_extracted:
                    extracted_html = rendered_extracted
                    page_html = rendered_html
                    print(f"Extracted selector from JS-rendered page: {args.selector}")
                else:
                    page_html = rendered_html
            except RuntimeError as e:
                print(str(e))
                if not page_html:
                    sys.exit(1)
            except Exception as e:
                print(f"JS-rendered fetch failed: {e}")
                if not page_html:
                    sys.exit(1)

        if extracted_html:
            html = extracted_html
            print(f"Fetched URL and extracted component via selector: {args.selector}")
        else:
            html = page_html
            print(
                f"Selector not found ({args.selector}). "
                "Parsing full page HTML instead."
            )
    else:
        # Accept HTML from stdin so you can paste directly into the terminal.
        # On Windows: paste, then press Ctrl+Z and Enter. On Unix: Ctrl+D.
        try:
            if not sys.stdin.isatty():
                html = sys.stdin.read()
            else:
                print("Paste HTML, then press Ctrl+Z (Windows) or Ctrl+D (Unix) and Enter:")
                html = sys.stdin.read()
        except Exception:
            html = ""

    if not (html and html.strip()):
        # fallback placeholder
        html = """PASTE_YOUR_HTML_HERE"""

    matches = parse_matches(html)
    print(json.dumps(matches, indent=2))

    # write output JSON file into the same folder as this script
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        script_dir = os.getcwd()

    timestamp = datetime.now().strftime("%d-%m-%Y_%H%M%S")
    out_name = f"matches_{timestamp}.json"
    out_path = os.path.join(script_dir, out_name)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(matches, f, indent=2)
        print(f"Wrote JSON to {out_path}")
    except Exception as e:
        print(f"Failed to write JSON file: {e}")
