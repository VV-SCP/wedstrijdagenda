#!/usr/bin/env python3
"""Genereer per VV SCP-team een automatisch bijgewerkte iCalendar-feed."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

API_URL = "https://site-api.voetbalassi.st/SCP/front/programmaenuitslagen/PostWedstrijden"
PUBLIC_BASE_URL = "https://vv-scp.github.io/wedstrijdagenda"
CACHE_FILE = Path("uitslagen_cache.json")
TZ = ZoneInfo("Europe/Amsterdam")

HEADERS = {
    "User-Agent": "VV-SCP-wedstrijdagenda/12.0 (+https://github.com/VV-SCP/wedstrijdagenda)",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.vv-scp.nl",
    "Referer": "https://www.vv-scp.nl/521/programma/",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# De volgorde in deze dictionary is ook de volgorde op de website.
TEAM_FILES = {
    "SCP 1": "scp-1.ics",
    "SCP 2": "scp-2.ics",
    "SCP 3": "scp-3.ics",
    "SCP Zaterdag 2": "scp-zaterdag-2.ics",
    "SCP Dames 1": "scp-dames-1.ics",
    "SCP 35+1": "scp-35-plus-1.ics",
    "SCP 35+2": "scp-35-plus-2.ics",
    "SJO SCP/DSZ O19-1": "o19-1.ics",
    "SJO SCP/DSZ-DIO'30 O19-2": "o19-2.ics",
    "SJO SCP/DSZ O17-1": "o17-1.ics",
    "SJO SCP/DSZ O15-1": "o15-1.ics",
    "SJO SCP/DSZ O15-2": "o15-2.ics",
    "SJO SCP/DSZ O14-1": "o14-1.ics",
    "SJO SCP/DSZ O13-1": "o13-1.ics",
    "SJO SCP/DSZ O12-1": "o12-1.ics",
    "SJO SCP/DSZ O12-2": "o12-2.ics",
    "SJO SCP/DSZ O11-1": "o11-1.ics",
    "SJO SCP/DSZ O11-2": "o11-2.ics",
    "SJO SCP/DSZ O10-1": "o10-1.ics",
    "SJO SCP/DSZ O10-2": "o10-2.ics",
    "SJO SCP/DSZ O8-1": "o8-1.ics",
    "SJO SCP/DSZ O8-2": "o8-2.ics",
    "SJO SCP/DSZ O7-1": "o7-1.ics",
    "SJO SCP/DSZ O7-2": "o7-2.ics",
}
TEAM_ORDER = {name: index for index, name in enumerate(TEAM_FILES)}


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def season_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(TZ)
    start_year = now.year if now.month >= 7 else now.year - 1
    return datetime(start_year, 7, 1, tzinfo=TZ), datetime(start_year + 1, 7, 1, tzinfo=TZ)


def fetch_season_matches() -> list[dict]:
    """Haal het volledige actuele seizoen in een API-aanvraag op."""
    now = datetime.now(TZ)
    start, end = season_bounds(now)
    stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%S")
    payload = {
        "clubWedstrijdenStandaardSorterenOp": "team",
        "datumBeginSeizoen": stamp(start),
        "datumEindeSeizoen": stamp(end),
        "datumVan": stamp(start),
        "datumTot": stamp(end),
        "klantAfkorting": "SCP",
        "lang": "nl",
        "programmaEnUitslagenType": 1,
        "vandaag": now.strftime("%Y-%m-%dT%H:%M:%S.%f"),
    }
    response = SESSION.post(API_URL, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("De VoetbalAssist-API gaf geen wedstrijdenlijst terug.")
    print(f"API: {len(data)} programmarecords opgehaald.")
    return data


def result_cache_key(match: dict) -> str:
    """Gebruik bij voorkeur het officiële wedstrijdnummer als cachesleutel."""
    wedstrijdnummer = clean_text(match.get("wedstrijdnummer"))
    if wedstrijdnummer:
        return f"wedstrijdnummer:{wedstrijdnummer}"
    api_id = clean_text(match.get("id"))
    if api_id:
        return f"id:{api_id}"
    return "|".join(
        [
            clean_text(match.get("datum")),
            clean_text(match.get("thuisClubEnTeamNaamFriendly")).upper(),
            clean_text(match.get("uitClubEnTeamNaamFriendly")).upper(),
        ]
    )


def load_result_cache() -> dict[str, str]:
    """Lees eerder gevonden eindstanden; een ontbrekende cache is normaal."""
    if not CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WAARSCHUWING: uitslagencache kon niet worden gelezen: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        clean_text(key): normalise_result(value)
        for key, value in data.items()
        if clean_text(key) and normalise_result(value)
    }


def save_result_cache(cache: dict[str, str]) -> None:
    """Sla alleen geldige eindstanden in een stabiele volgorde op."""
    cleaned = {
        key: result
        for key, result in sorted(cache.items())
        if normalise_result(result)
    }
    CACHE_FILE.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def result_from_detail_page(match: dict) -> str:
    """Lees een eindstand eenmalig van de openbare wedstrijddetailpagina."""
    url = extract_url(match.get("wedstrijdDetailsUrl"))
    if not url.startswith("https://www.vv-scp.nl/"):
        return ""

    try:
        response = SESSION.get(url, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"WAARSCHUWING: uitslag niet opgehaald voor {url}: {exc}")
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    candidates = [
        clean_text(element.get_text(" ", strip=True))
        for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "div"])
    ]
    page_text = clean_text(soup.get_text(" ", strip=True))

    home_names = {
        clean_text(match.get("thuisClubEnTeamNaamFriendly")),
        display_team_name(match.get("thuisClubEnTeamNaamFriendly")),
    }
    away_names = {
        clean_text(match.get("uitClubEnTeamNaamFriendly")),
        display_team_name(match.get("uitClubEnTeamNaamFriendly")),
    }

    for home in sorted(home_names, key=len, reverse=True):
        for away in sorted(away_names, key=len, reverse=True):
            if not home or not away:
                continue
            pattern = re.compile(
                rf"{re.escape(home)}\s+(\d+)\s*-\s*(\d+)\s+{re.escape(away)}",
                flags=re.IGNORECASE,
            )
            for text in candidates + [page_text]:
                found = pattern.search(text)
                if found:
                    return f"{found.group(1)} - {found.group(2)}"

    return ""


def enrich_results(matches: list[dict]) -> tuple[list[dict], int]:
    """Gebruik API/cache en bezoek alleen nieuwe gespeelde wedstrijden zonder uitslag."""
    cache = load_result_cache()
    now = datetime.now(TZ)
    detail_requests = 0

    for match in matches:
        key = result_cache_key(match)
        api_result = normalise_result(match.get("uitslag"))

        if api_result:
            match["uitslag"] = api_result
            cache[key] = api_result
            continue

        cached_result = normalise_result(cache.get(key))
        if cached_result:
            match["uitslag"] = cached_result
            continue

        if match.get("statusAfgelast"):
            continue

        try:
            start = parse_datetime(match.get("datum"))
        except (TypeError, ValueError):
            continue

        # Pas twee uur na de aftrap controleren, zodat geen tussenstand wordt opgeslagen.
        if start + timedelta(hours=2) > now:
            continue

        detail_requests += 1
        detail_result = result_from_detail_page(match)
        if detail_result:
            match["uitslag"] = detail_result
            cache[key] = detail_result
            print(
                "Nieuwe uitslag opgeslagen: "
                f"{display_team_name(match.get('thuisClubEnTeamNaamFriendly'))} "
                f"{detail_result} "
                f"{display_team_name(match.get('uitClubEnTeamNaamFriendly'))}"
            )

    save_result_cache(cache)
    print(
        f"Uitslagencache: {len(cache)} uitslagen; "
        f"{detail_requests} detailpagina's gecontroleerd."
    )
    return matches, detail_requests


def display_team_name(value: object) -> str:
    name = clean_text(value)
    name = re.sub(r"\s*\((?:zon|zat)\)\s*$", "", name, flags=re.IGNORECASE)
    return name.replace("SJO SCP/DSZ -DIO'3...", "SJO SCP/DSZ-DIO'30")


def choose_raw_scp_name(match: dict) -> str:
    for key in ("thuisClubEnTeamNaamFriendly", "uitClubEnTeamNaamFriendly"):
        name = clean_text(match.get(key))
        upper = name.upper()
        if upper.startswith("SCP ") or upper.startswith("SJO SCP/DSZ"):
            return name
    return ""


def canonical_scp_team(match: dict) -> str:
    """Koppel uitsluitend aan een vaste, geldige SCP-teamnaam."""
    raw = choose_raw_scp_name(match)
    if not raw:
        return ""
    upper = raw.upper()

    # SCP 2 zondag en zaterdag hebben in de API soms allebei de naam SCP 2.
    if re.fullmatch(r"SCP(?:\s+(?:ZON|ZAT|ZATERDAG))?\s+2(?:\s*\((?:ZON|ZAT)\))?", upper):
        if bool(match.get("bevatZaterdagteam")) or "ZAT" in upper:
            return "SCP Zaterdag 2"
        if bool(match.get("bevatZondagteam")) or "ZON" in upper:
            return "SCP 2"
        try:
            weekday = datetime.fromisoformat(clean_text(match.get("datum"))).weekday()
        except ValueError:
            weekday = None
        if weekday == 6:
            return "SCP 2"
        if weekday is not None:
            return "SCP Zaterdag 2"

    aliases = {
        "SCP ZON 1": "SCP 1", "SCP 1 (ZON)": "SCP 1",
        "SCP ZON 2": "SCP 2", "SCP 2 (ZON)": "SCP 2",
        "SCP ZON 3": "SCP 3", "SCP 3 (ZON)": "SCP 3",
        "SCP ZAT 2": "SCP Zaterdag 2", "SCP 2 (ZAT)": "SCP Zaterdag 2",
        "SCP ZATERDAG 2": "SCP Zaterdag 2",
        "SCP VR1 (ZON)": "SCP Dames 1", "SCP VR1": "SCP Dames 1",
        "SCP VR 1": "SCP Dames 1", "SCP VROUWEN 1": "SCP Dames 1",
        "SCP DAMES 1": "SCP Dames 1", "SCP 35+1": "SCP 35+1",
        "SCP 35+2": "SCP 35+2",
        "SJO SCP/DSZ O13-EWIJK 1": "SJO SCP/DSZ O13-1",
        "SJO SCP/DSZ O14-SC WOEZIK 2": "SJO SCP/DSZ O14-1",
        "SJO SCP/DSZ O15-BLAUW WIT 1": "SJO SCP/DSZ O15-1",
    }
    if upper in aliases:
        return aliases[upper]

    youth = re.search(r"\bO(\d+)\b", upper)
    if youth:
        age = int(youth.group(1))
        tail = raw[youth.end():]
        number = re.search(r"(\d+)\s*$", tail)
        if not number:
            print(f"WAARSCHUWING: geen teamnummer herkend in: {raw}")
            return ""
        team_number = int(number.group(1))
        name = "SJO SCP/DSZ-DIO'30 O19-2" if (age, team_number) == (19, 2) else f"SJO SCP/DSZ O{age}-{team_number}"
        return name if name in TEAM_FILES else ""

    if re.search(r"\bSCP\s+(?:VR|VROUWEN|DAMES)\s*1\b", upper):
        return "SCP Dames 1"
    if "35+1" in upper:
        return "SCP 35+1"
    if "35+2" in upper:
        return "SCP 35+2"
    number = re.search(r"\bSCP\s+(\d+)\b", upper)
    name = f"SCP {number.group(1)}" if number else ""
    return name if name in TEAM_FILES else ""


def parse_datetime(value: object) -> datetime:
    result = datetime.fromisoformat(clean_text(value))
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def normalise_result(value: object) -> str:
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", clean_text(value))
    return f"{match.group(1)} - {match.group(2)}" if match else ""


def extract_url(value: object) -> str:
    text = html.unescape(str(value or ""))
    href = re.search(r'href=["\']([^"\']+)', text, flags=re.IGNORECASE)
    if href:
        return href.group(1)
    return text if text.startswith(("https://", "http://")) else ""


def create_uid(match: dict) -> str:
    api_id = clean_text(match.get("id"))
    if api_id:
        return f"wedstrijd-{api_id}@vv-scp.nl"
    raw = "|".join(clean_text(match.get(key)) for key in ("wedstrijdnummer", "thuisClubEnTeamNaamFriendly", "uitClubEnTeamNaamFriendly"))
    return f"{hashlib.sha256(raw.encode()).hexdigest()[:20]}@vv-scp.nl"


def convert_match(raw: dict) -> tuple[str, dict] | None:
    team = canonical_scp_team(raw)
    if not team:
        return None
    status = "afgelast" if raw.get("statusAfgelast") else "herkeuring" if raw.get("statusHerkeuring") else ""
    kind = "Vriendschappelijk" if raw.get("typeVriendschappelijk") else "Beker" if raw.get("typeBeker") else "Competitie"
    is_home = bool(raw.get("isThuiswedstrijd"))
    return team, {
        "start": parse_datetime(raw.get("datum")),
        "home": display_team_name(raw.get("thuisClubEnTeamNaamFriendly")),
        "away": display_team_name(raw.get("uitClubEnTeamNaamFriendly")),
        "kind": kind,
        "result": normalise_result(raw.get("uitslag")),
        "status": status,
        "url": extract_url(raw.get("wedstrijdDetailsUrl")) or API_URL,
        "address": "Hosterd 65, 6655 AE Puiflijk" if is_home else "",
        "accommodation": "Sportpark Schravenhof" if is_home else "",
        "field": clean_text(raw.get("verkrijgVeldtekst")),
        "referee": clean_text(raw.get("scheidsrechter")),
        "remark": clean_text(raw.get("opmerking")),
        "uid": create_uid(raw),
    }


def escape_ical(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold_ical(line: str, limit: int = 73) -> str:
    parts = []
    while len(line.encode()) > limit:
        cut = min(limit, len(line))
        while len(line[:cut].encode()) > limit:
            cut -= 1
        parts.append(line[:cut])
        line = " " + line[cut:]
    return "\r\n".join(parts + [line])


def build_calendar(team: str, matches: list[dict]) -> str:
    timestamp = datetime.now(TZ).strftime("%Y%m%dT000000Z")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//VV SCP//Wedstrijdagenda {team}//NL", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{escape_ical(team)}", "X-WR-TIMEZONE:Europe/Amsterdam", "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for match in sorted(matches, key=lambda item: item["start"]):
        start, status = match["start"], match["status"]
        title = f"{match['home']} - {match['away']}"
        title = f"{status.upper()}: {title}" if status else title + (f" ({match['result']})" if match["result"] else "")
        description = [match["kind"]]
        if status: description.insert(0, f"Status: {status}")
        if match["result"]: description.append(f"Eindstand: {match['result']}")
        if match["accommodation"]: description.append(f"Accommodatie: {match['accommodation']}")
        if match["field"]: description.append(f"Veld: {match['field']}")
        if match["referee"]: description.append(f"Scheidsrechter: {match['referee']}")
        if match["remark"]: description.append(f"Opmerking: {match['remark']}")
        description.append("Controleer bij last-minute wijzigingen altijd Voetbal.nl.")
        lines.extend(["BEGIN:VEVENT", f"UID:{match['uid']}", f"DTSTAMP:{timestamp}", f"DTSTART;TZID=Europe/Amsterdam:{start.strftime('%Y%m%dT%H%M%S')}", f"DTEND;TZID=Europe/Amsterdam:{(start + timedelta(hours=2)).strftime('%Y%m%dT%H%M%S')}", f"SUMMARY:{escape_ical(title)}", f"DESCRIPTION:{escape_ical(chr(10).join(description))}", f"URL:{match['url']}", f"STATUS:{'CANCELLED' if status == 'afgelast' else 'CONFIRMED'}", "SEQUENCE:0"])
        if match["address"]: lines.append(f"LOCATION:{escape_ical(match['address'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical(line) for line in lines) + "\r\n"


def category(team: str) -> str:
    if team.startswith("SCP "): return "Senioren"
    age = re.search(r"\bO(\d+)-", team)
    return "Junioren" if age and int(age.group(1)) >= 12 else "Pupillen"


def build_index(results: list[dict]) -> str:
    groups = {name: [] for name in ("Senioren", "Junioren", "Pupillen")}
    for result in results: groups[category(result["name"])].append(result)
    sections = []
    for group, teams in groups.items():
        if not teams: continue
        cards = []
        for team in sorted(teams, key=lambda item: TEAM_ORDER.get(item["name"], 9999)):
            name, filename = html.escape(team["name"]), html.escape(team["output"])
            url = f"{PUBLIC_BASE_URL}/{filename}"
            webcal = url.replace("https://", "webcal://", 1)
            cards.append(f'<article class="card"><h3>{name}</h3><p>{team["count"]} wedstrijden</p><div class="actions"><a class="button apple" href="{webcal}">Abonneren op iPhone</a><button class="button android copy-link" type="button" data-url="{url}">Kopieer link voor Android</button></div><p class="help">Kies een abonnement, niet een eenmalige import.</p><details><summary>Android-instructie</summary><ol><li>Kopieer de link hierboven.</li><li>Open Google Agenda in een browser en kies <strong>Andere agenda\'s &gt; + &gt; Via URL</strong>.</li><li>Plak de link en kies <strong>Agenda toevoegen</strong>.</li></ol></details><p class="url">{url}</p></article>')
        sections.append(f'<section><h2>{group}</h2><div class="grid">{"".join(cards)}</div></section>')
    return f'''<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#116735"><title>Wedstrijdagenda's VV SCP</title><style>:root{{--green:#116735;--blue:#1565c0;--ink:#172033;--muted:#5d6675;--line:#d8dee9;--bg:#f5f7fa}}*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1120px;margin:0 auto;padding:28px 18px 48px;line-height:1.5;color:var(--ink);background:var(--bg)}}.notice{{background:#fff7dc;border-left:4px solid #e0a800;padding:12px 14px;border-radius:8px;margin:20px 0}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}}.card{{border:1px solid var(--line);border-radius:14px;padding:18px;background:#fff}}.actions{{display:grid;gap:10px;margin:14px 0}}.button{{display:block;width:100%;border:0;border-radius:9px;padding:11px;color:#fff;text-align:center;text-decoration:none;font-weight:700;font-size:1rem;cursor:pointer}}.apple{{background:var(--green)}}.android{{background:var(--blue)}}.help,.url,footer{{color:var(--muted)}}.url{{font-size:.76rem;overflow-wrap:anywhere}}footer{{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);font-size:.88rem}}.toast{{position:sticky;bottom:16px;display:none;background:#172033;color:#fff;padding:12px;border-radius:10px;text-align:center}}</style></head><body><h1>Wedstrijdagenda's VV SCP</h1><p>Kies jouw team. De groene knop opent op een iPhone een agenda-abonnement. De blauwe knop kopieert de link voor Android.</p><div class="notice"><strong>Belangrijk:</strong> voeg de agenda toe als abonnement. Importeer het ICS-bestand niet eenmalig, want dan worden wijzigingen niet automatisch verwerkt.</div>{"".join(sections)}<div id="toast" class="toast" role="status" aria-live="polite"></div><footer><strong>Let op:</strong> de agenda's worden ieder uur bijgewerkt. Je agenda-app bepaalt zelf wanneer wijzigingen zichtbaar worden. Controleer bij last-minute wijzigingen of afgelastingen daarom altijd ook Voetbal.nl of de teamcommunicatie.</footer><script>(()=>{{const t=document.getElementById('toast');document.querySelectorAll('.copy-link').forEach(b=>b.addEventListener('click',async()=>{{try{{await navigator.clipboard.writeText(b.dataset.url);t.textContent='De agenda-link is gekopieerd.';t.style.display='block';setTimeout(()=>t.style.display='none',3500)}}catch(e){{window.prompt('Kopieer deze agenda-link:',b.dataset.url)}}}}))}})();</script></body></html>'''


def main() -> None:
    raw_matches = fetch_season_matches()
    raw_matches, _ = enrich_results(raw_matches)
    calendars: dict[str, dict[str, dict]] = defaultdict(dict)
    for raw in raw_matches:
        converted = convert_match(raw)
        if converted:
            team, item = converted
            calendars[team][item["uid"]] = item
    if not calendars: raise RuntimeError("Er zijn geen geldige SCP-teamagenda's opgebouwd.")
    expected = {TEAM_FILES[name] for name in calendars}
    for old in Path(".").glob("*.ics"):
        if old.name not in expected:
            old.unlink()
            print(f"Verouderd agenda-bestand verwijderd: {old.name}")
    results = []
    for team in sorted(calendars, key=lambda name: TEAM_ORDER.get(name, 9999)):
        matches = list(calendars[team].values())
        filename = TEAM_FILES[team]
        Path(filename).write_text(build_calendar(team, matches), encoding="utf-8", newline="")
        results.append({"name": team, "output": filename, "count": len(matches)})
        print(f"{team}: {len(matches)} wedstrijden -> {filename}")
    Path("index.html").write_text(build_index(results), encoding="utf-8")
    print(f"Klaar: {len(results)} teamagenda's en {len(raw_matches)} API-records verwerkt.")


if __name__ == "__main__":
    main()
