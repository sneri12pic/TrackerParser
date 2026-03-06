from bs4 import BeautifulSoup
import re
import json
import sys
import os
import sqlite3
from datetime import datetime

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


# Database functions ---------------------------------------------------------------------------------------------
def init_db(db_path: str):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                source_json TEXT,
                agent TEXT,
                time_ago TEXT,
                map TEXT,
                placement TEXT,
                score TEXT,
                trs INTEGER,
                rank TEXT,
                kd REAL,
                kills INTEGER,
                deaths INTEGER,
                assists INTEGER,
                dd_delta INTEGER,
                hs_pct REAL,
                acs INTEGER
            )
            """
        )
        conn.commit()

def save_matches_to_db(matches, db_path: str, captured_at: str, source_json: str | None = None):
    if not matches:
        return 0

    rows = []
    for match in matches:
        rows.append(
            (
                captured_at,
                source_json,
                match.get("agent"),
                match.get("time_ago"),
                match.get("map"),
                match.get("placement"),
                match.get("score"),
                match.get("trs"),
                match.get("rank"),
                match.get("kd"),
                match.get("kills"),
                match.get("deaths"),
                match.get("assists"),
                match.get("dd_delta"),
                match.get("hs_pct"),
                match.get("acs"),
            )
        )

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO matches (
                captured_at, source_json, agent, time_ago, map, placement, score, trs, rank, kd,
                kills, deaths, assists, dd_delta, hs_pct, acs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    return len(rows)
# ----------------------------------------------------------------------------------------------------------------------------


if __name__ == "__main__":
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
    db_path = os.path.join(script_dir, "matches.db")
    json_written = False
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(matches, f, indent=2)
        print(f"Wrote JSON to {out_path}")
        json_written = True
    except Exception as e:
        print(f"Failed to write JSON file: {e}")

    try:
        init_db(db_path)
        inserted = save_matches_to_db(
            matches,
            db_path=db_path,
            captured_at=timestamp,
            source_json=out_path if json_written else None,
        )
        print(f"Stored {inserted} match rows in SQLite DB: {db_path}")
    except Exception as e:
        print(f"Failed to write to SQLite DB: {e}")
