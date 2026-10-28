#!/usr/bin/env python3
"""VV SCP wedstrijdagenda's: API, uitslagencache, ICS-feeds en overzichtspagina."""

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
LOCATION_CACHE_FILE = Path("locaties_cache.json")
MAX_LOCATION_REQUESTS_PER_RUN = 12
TZ = ZoneInfo("Europe/Amsterdam")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "VV-SCP-wedstrijdagenda/14.0 (+https://github.com/VV-SCP/wedstrijdagenda)",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.vv-scp.nl",
    "Referer": "https://www.vv-scp.nl/521/programma/",
})

# Deze volgorde wordt ook op de website gebruikt.
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
    year = now.year if now.month >= 7 else now.year - 1
    return datetime(year, 7, 1, tzinfo=TZ), datetime(year + 1, 7, 1, tzinfo=TZ)


def fetch_matches() -> list[dict]:
    now = datetime.now(TZ)
    start, end = season_bounds(now)
    stamp = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S")
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
        raise RuntimeError("De API gaf geen wedstrijdenlijst terug.")
    print(f"API: {len(data)} programmarecords opgehaald.")
    return data


def display_team_name(value: object) -> str:
    name = clean_text(value)
    name = re.sub(r"\s*\((?:zon|zat)\)\s*$", "", name, flags=re.I)
    return name.replace("SJO SCP/DSZ -DIO'3...", "SJO SCP/DSZ-DIO'30")


def raw_scp_name(match: dict) -> str:
    for key in ("thuisClubEnTeamNaamFriendly", "uitClubEnTeamNaamFriendly"):
        name = clean_text(match.get(key))
        upper = name.upper()
        if upper.startswith("SCP ") or upper.startswith("SJO SCP/DSZ"):
            return name
    return ""


def canonical_team(match: dict) -> str:
    raw = raw_scp_name(match)
    if not raw:
        return ""
    upper = raw.upper()

    # De API noemt zaterdag en zondag soms allebei SCP 2.
    if re.fullmatch(r"SCP(?:\s+(?:ZON|ZAT|ZATERDAG))?\s+2(?:\s*\((?:ZON|ZAT)\))?", upper):
        if match.get("bevatZaterdagteam") or "ZAT" in upper:
            return "SCP Zaterdag 2"
        if match.get("bevatZondagteam") or "ZON" in upper:
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
        number = re.search(r"(\d+)\s*$", raw[youth.end():])
        if not number:
            print(f"WAARSCHUWING: geen teamnummer herkend in {raw}")
            return ""
        team_no = int(number.group(1))
        name = "SJO SCP/DSZ-DIO'30 O19-2" if (age, team_no) == (19, 2) else f"SJO SCP/DSZ O{age}-{team_no}"
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
    found = re.fullmatch(r"(\d+)\s*-\s*(\d+)", clean_text(value))
    return f"{found.group(1)} - {found.group(2)}" if found else ""


def extract_url(value: object) -> str:
    text = html.unescape(str(value or ""))
    found = re.search(r'href=["\']([^"\']+)', text, flags=re.I)
    if found:
        return found.group(1)
    return text if text.startswith(("https://", "http://")) else ""


def cache_key(match: dict) -> str:
    number = clean_text(match.get("wedstrijdnummer"))
    if number:
        return f"wedstrijdnummer:{number}"
    api_id = clean_text(match.get("id"))
    return f"id:{api_id}" if api_id else hashlib.sha256(str(match).encode()).hexdigest()


def load_cache() -> dict[str, str]:
    if not CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): normalise_result(v) for k, v in data.items() if normalise_result(v)} if isinstance(data, dict) else {}


def save_cache(cache: dict[str, str]) -> None:
    CACHE_FILE.write_text(json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def result_from_detail(match: dict) -> str:
    url = extract_url(match.get("wedstrijdDetailsUrl"))
    if not url.startswith("https://www.vv-scp.nl/"):
        return ""
    try:
        response = SESSION.get(url, timeout=20)
        response.raise_for_status()
    except requests.RequestException:
        return ""
    text = clean_text(BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True))
    homes = {clean_text(match.get("thuisClubEnTeamNaamFriendly")), display_team_name(match.get("thuisClubEnTeamNaamFriendly"))}
    aways = {clean_text(match.get("uitClubEnTeamNaamFriendly")), display_team_name(match.get("uitClubEnTeamNaamFriendly"))}
    for home in homes:
        for away in aways:
            found = re.search(rf"{re.escape(home)}\s+(\d+)\s*-\s*(\d+)\s+{re.escape(away)}", text, flags=re.I)
            if found:
                return f"{found.group(1)} - {found.group(2)}"
    return ""


def enrich_results(matches: list[dict]) -> None:
    cache = load_cache()
    now = datetime.now(TZ)
    requests_made = 0
    for match in matches:
        key = cache_key(match)
        result = normalise_result(match.get("uitslag")) or cache.get(key, "")
        if result:
            match["uitslag"] = result
            cache[key] = result
            continue
        if match.get("statusAfgelast"):
            continue
        try:
            start = parse_datetime(match.get("datum"))
        except (TypeError, ValueError):
            continue
        if start + timedelta(hours=2) > now:
            continue
        requests_made += 1
        result = result_from_detail(match)
        if result:
            match["uitslag"] = result
            cache[key] = result
            print(f"Nieuwe uitslag: {display_team_name(match.get('thuisClubEnTeamNaamFriendly'))} {result} {display_team_name(match.get('uitClubEnTeamNaamFriendly'))}")
    save_cache(cache)
    print(f"Uitslagencache: {len(cache)} uitslagen; {requests_made} detailpagina's gecontroleerd.")


def location_signature(match: dict) -> str:
    """Herken wijzigingen die een nieuwe locatiecontrole noodzakelijk maken."""
    raw = "|".join(
        [
            clean_text(match.get("datum")),
            clean_text(match.get("thuisClubEnTeamNaamFriendly")),
            clean_text(match.get("uitClubEnTeamNaamFriendly")),
            clean_text(match.get("isThuiswedstrijd")),
            clean_text(match.get("verkrijgVeldtekst")),
            extract_url(match.get("wedstrijdDetailsUrl")),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def load_location_cache() -> dict[str, dict]:
    if not LOCATION_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(LOCATION_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WAARSCHUWING: locatiecache kon niet worden gelezen: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def save_location_cache(cache: dict[str, dict]) -> None:
    LOCATION_CACHE_FILE.write_text(
        json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def location_refresh_interval(start: datetime, now: datetime) -> timedelta:
    """Controleer locaties vaker naarmate de wedstrijd dichterbij komt."""
    remaining = start - now
    if remaining <= timedelta(hours=48):
        return timedelta(hours=1)
    if remaining <= timedelta(days=7):
        return timedelta(hours=6)
    if remaining <= timedelta(days=30):
        return timedelta(days=2)
    return timedelta(days=7)


def cached_location_is_due(entry: dict, match: dict, start: datetime, now: datetime) -> bool:
    if not entry.get("adres"):
        return True
    if entry.get("signature") != location_signature(match):
        return True
    try:
        checked = datetime.fromisoformat(clean_text(entry.get("laatst_gecontroleerd")))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=TZ)
    except ValueError:
        return True
    return now - checked >= location_refresh_interval(start, now)


def location_from_detail(match: dict) -> tuple[str, str]:
    """Lees accommodatie en volledig adres van de wedstrijddetailpagina."""
    url = extract_url(match.get("wedstrijdDetailsUrl"))
    if not url.startswith("https://www.vv-scp.nl/"):
        return "", ""
    try:
        response = SESSION.get(url, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"WAARSCHUWING: locatie niet opgehaald voor {url}: {exc}")
        return "", ""

    text = clean_text(BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True))
    found = re.search(
        r"Accommodatie\s+(.+?)\s+"
        r"([A-Za-zÀ-ÿ' .-]+\s+\d+[A-Za-z]?)\s+"
        r"(\d{4}\s?[A-Z]{2})\s+"
        r"([A-Za-zÀ-ÿ' .-]+?)"
        r"(?:\s+tel\.|\s+Toon routebeschrijving|\s+Uw adres)",
        text,
        flags=re.IGNORECASE,
    )
    if not found:
        return "", ""

    accommodation = clean_text(found.group(1))
    street = clean_text(found.group(2))
    postcode = clean_text(found.group(3)).upper().replace(" ", "")
    if len(postcode) == 6:
        postcode = f"{postcode[:4]} {postcode[4:]}"
    place = clean_text(found.group(4)).title()
    return accommodation, f"{street}, {postcode} {place}"


def enrich_locations(matches: list[dict]) -> None:
    """Verrijk toekomstige wedstrijden met een periodiek vernieuwde locatie per wedstrijd."""
    cache = load_location_cache()
    now = datetime.now(TZ)
    requests_made = 0

    future_matches = []
    for match in matches:
        try:
            start = parse_datetime(match.get("datum"))
        except (TypeError, ValueError):
            continue
        if start >= now - timedelta(hours=2):
            future_matches.append((start, match))

    for start, match in sorted(future_matches, key=lambda item: item[0]):
        key = cache_key(match)
        entry = cache.get(key, {})

        if entry.get("adres"):
            match["locatie_adres"] = clean_text(entry.get("adres"))
            match["locatie_accommodatie"] = clean_text(entry.get("accommodatie"))

        if not cached_location_is_due(entry, match, start, now):
            continue
        if requests_made >= MAX_LOCATION_REQUESTS_PER_RUN:
            continue

        requests_made += 1
        accommodation, address = location_from_detail(match)
        checked_at = now.isoformat(timespec="seconds")

        if address:
            match["locatie_adres"] = address
            match["locatie_accommodatie"] = accommodation
            cache[key] = {
                "accommodatie": accommodation,
                "adres": address,
                "laatst_gecontroleerd": checked_at,
                "wedstrijd_datum": start.isoformat(timespec="seconds"),
                "signature": location_signature(match),
            }
            print(
                "Locatie bijgewerkt: "
                f"{display_team_name(match.get('thuisClubEnTeamNaamFriendly'))} - "
                f"{display_team_name(match.get('uitClubEnTeamNaamFriendly'))}: "
                f"{accommodation}, {address}"
            )
        elif entry:
            entry["laatst_gecontroleerd"] = checked_at
            entry["signature"] = location_signature(match)
            cache[key] = entry

    save_location_cache(cache)
    print(
        f"Locatiecache: {sum(1 for value in cache.values() if value.get('adres'))} locaties; "
        f"{requests_made} detailpagina's gecontroleerd."
    )


def create_uid(match: dict) -> str:
    api_id = clean_text(match.get("id"))
    return f"wedstrijd-{api_id}@vv-scp.nl" if api_id else f"{hashlib.sha256(str(match).encode()).hexdigest()[:20]}@vv-scp.nl"


def convert_match(raw: dict) -> tuple[str, dict] | None:
    team = canonical_team(raw)
    if not team:
        return None
    status = "afgelast" if raw.get("statusAfgelast") else "herkeuring" if raw.get("statusHerkeuring") else ""
    kind = "Vriendschappelijk" if raw.get("typeVriendschappelijk") else "Beker" if raw.get("typeBeker") else "Competitie"
    return team, {
        "start": parse_datetime(raw.get("datum")),
        "home": display_team_name(raw.get("thuisClubEnTeamNaamFriendly")),
        "away": display_team_name(raw.get("uitClubEnTeamNaamFriendly")),
        "kind": kind, "result": normalise_result(raw.get("uitslag")), "status": status,
        "url": extract_url(raw.get("wedstrijdDetailsUrl")) or API_URL,
        "address": clean_text(raw.get("locatie_adres")),
        "accommodation": clean_text(raw.get("locatie_accommodatie")),
        "field": clean_text(raw.get("verkrijgVeldtekst")),
        "referee": clean_text(raw.get("scheidsrechter")),
        "remark": clean_text(raw.get("opmerking")), "uid": create_uid(raw),
    }


def escape_ical(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold_ical(line: str, limit: int = 73) -> str:
    parts = []
    while len(line.encode()) > limit:
        cut = min(limit, len(line))
        while len(line[:cut].encode()) > limit:
            cut -= 1
        parts.append(line[:cut]); line = " " + line[cut:]
    return "\r\n".join(parts + [line])


def build_calendar(team: str, matches: list[dict]) -> str:
    stamp = datetime.now(TZ).strftime("%Y%m%dT000000Z")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//VV SCP//Wedstrijdagenda {team}//NL", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{escape_ical(team)}", "X-WR-TIMEZONE:Europe/Amsterdam", "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for item in sorted(matches, key=lambda x: x["start"]):
        title = f"{item['home']} - {item['away']}"
        title = f"{item['status'].upper()}: {title}" if item["status"] else title + (f" ({item['result']})" if item["result"] else "")
        desc = [item["kind"]]
        if item["status"]: desc.insert(0, f"Status: {item['status']}")
        if item["result"]: desc.append(f"Eindstand: {item['result']}")
        if item["accommodation"]: desc.append(f"Accommodatie: {item['accommodation']}")
        if item["field"]: desc.append(f"Veld: {item['field']}")
        if item["referee"]: desc.append(f"Scheidsrechter: {item['referee']}")
        if item["remark"]: desc.append(f"Opmerking: {item['remark']}")
        desc.append("Controleer bij last-minute wijzigingen altijd Voetbal.nl.")
        start = item["start"]
        lines.extend(["BEGIN:VEVENT", f"UID:{item['uid']}", f"DTSTAMP:{stamp}", f"DTSTART;TZID=Europe/Amsterdam:{start:%Y%m%dT%H%M%S}", f"DTEND;TZID=Europe/Amsterdam:{start + timedelta(hours=2):%Y%m%dT%H%M%S}", f"SUMMARY:{escape_ical(title)}", f"DESCRIPTION:{escape_ical(chr(10).join(desc))}", f"URL:{item['url']}", f"STATUS:{'CANCELLED' if item['status'] == 'afgelast' else 'CONFIRMED'}", "SEQUENCE:0"])
        if item["address"]: lines.append(f"LOCATION:{escape_ical(item['address'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical(x) for x in lines) + "\r\n"


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
        for team in sorted(teams, key=lambda x: TEAM_ORDER.get(x["name"], 9999)):
            name, filename = html.escape(team["name"]), html.escape(team["output"])
            url, key = f"{PUBLIC_BASE_URL}/{filename}", group.lower()
            webcal = url.replace("https://", "webcal://", 1)
            search = html.escape(team["name"].lower())
            cards.append(f'<article class="card" data-category="{key}" data-search="{search}"><h3>{name}</h3><p>{team["count"]} wedstrijden</p><div class="actions"><a class="button apple" href="{webcal}">Abonneren op iPhone</a><button class="button android copy-link" type="button" data-url="{url}">Kopieer link voor Android</button></div><details><summary>Android-instructie</summary><ol><li>Kopieer de link hierboven.</li><li>Open Google Agenda in een browser en kies <strong>Andere agenda\'s &gt; + &gt; Via URL</strong>.</li><li>Plak de link en kies <strong>Agenda toevoegen</strong>.</li></ol></details></article>')
        sections.append(f'<section data-section="{group.lower()}"><h2>{group}</h2><div class="grid">{"".join(cards)}</div></section>')
    return f'''<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Wedstrijdagenda's VV SCP</title><style>:root{{--green:#116735;--blue:#1565c0;--ink:#172033;--muted:#5d6675;--line:#d8dee9;--bg:#f5f7fa}}*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1120px;margin:auto;padding:28px 18px 48px;line-height:1.5;color:var(--ink);background:var(--bg)}}.tools{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;margin:20px 0}}label{{font-weight:700;display:block;margin-bottom:6px}}#team-search{{width:100%;padding:12px;border:1px solid #aab3c0;border-radius:9px;font-size:1rem}}.filters{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}}.filter{{border:1px solid var(--green);background:#fff;color:var(--green);border-radius:999px;padding:8px 13px;font-weight:700;cursor:pointer}}.filter.active{{background:var(--green);color:#fff}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}}.card{{border:1px solid var(--line);border-radius:14px;padding:18px;background:#fff}}.actions{{display:grid;gap:10px;margin:14px 0}}.button{{display:block;width:100%;border:0;border-radius:9px;padding:11px;color:#fff;text-align:center;text-decoration:none;font-weight:700;font-size:1rem;cursor:pointer}}.apple{{background:var(--green)}}.android{{background:var(--blue)}}.notice{{background:#fff7dc;border-left:4px solid #e0a800;padding:12px 14px;border-radius:8px}}#no-results{{display:none;background:#fff;padding:18px;border-radius:12px}}footer{{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:.88rem}}.toast{{position:sticky;bottom:16px;display:none;background:#172033;color:#fff;padding:12px;border-radius:10px;text-align:center}}</style></head><body><h1>Wedstrijdagenda's VV SCP</h1><p>Kies jouw team en voeg de agenda gratis toe als abonnement.</p><div class="notice"><strong>Belangrijk:</strong> importeer het ICS-bestand niet eenmalig, want dan worden wijzigingen niet automatisch verwerkt.</div><div class="tools"><label for="team-search">Zoek je team</label><input id="team-search" type="search" placeholder="Bijvoorbeeld JO15-1, O11 of SCP 2" autocomplete="off"><div class="filters" aria-label="Filter op categorie"><button type="button" class="filter active" data-filter="all">Alle teams</button><button type="button" class="filter" data-filter="senioren">Senioren</button><button type="button" class="filter" data-filter="junioren">Junioren</button><button type="button" class="filter" data-filter="pupillen">Pupillen</button></div></div><p id="no-results">Geen team gevonden. Probeer bijvoorbeeld <strong>O15</strong>, <strong>JO11-1</strong> of <strong>SCP 2</strong>.</p>{"".join(sections)}<div id="toast" class="toast" role="status" aria-live="polite"></div><footer><strong>Let op:</strong> de agenda's worden ieder uur bijgewerkt. Je agenda-app bepaalt zelf wanneer wijzigingen zichtbaar worden. Controleer bij last-minute wijzigingen of afgelastingen daarom altijd ook Voetbal.nl of de teamcommunicatie.</footer><script>(()=>{{const q=document.getElementById('team-search'),cards=[...document.querySelectorAll('.card')],sections=[...document.querySelectorAll('section[data-section]')],empty=document.getElementById('no-results');let filter='all';const normalize=s=>s.toLowerCase().replace(/^jo/,'o').replace(/[^a-z0-9]+/g,'');function apply(){{const term=normalize(q.value);let visible=0;cards.forEach(c=>{{const categoryOk=filter==='all'||c.dataset.category===filter;const text=normalize(c.dataset.search);const show=categoryOk&&(!term||text.includes(term));c.hidden=!show;if(show)visible++;}});sections.forEach(s=>{{s.hidden=![...s.querySelectorAll('.card')].some(c=>!c.hidden);}});empty.style.display=visible?'none':'block';}}q.addEventListener('input',apply);document.querySelectorAll('.filter').forEach(b=>b.addEventListener('click',()=>{{document.querySelectorAll('.filter').forEach(x=>x.classList.remove('active'));b.classList.add('active');filter=b.dataset.filter;apply();}}));const toast=document.getElementById('toast');document.querySelectorAll('.copy-link').forEach(b=>b.addEventListener('click',async()=>{{try{{await navigator.clipboard.writeText(b.dataset.url);toast.textContent='De agenda-link is gekopieerd.';toast.style.display='block';setTimeout(()=>toast.style.display='none',3500)}}catch(e){{window.prompt('Kopieer deze agenda-link:',b.dataset.url)}}}}));}})();</script></body></html>'''


def main() -> None:
    raw = fetch_matches()
    enrich_results(raw)
    enrich_locations(raw)
    calendars: dict[str, dict[str, dict]] = defaultdict(dict)
    for match in raw:
        converted = convert_match(match)
        if converted:
            team, item = converted; calendars[team][item["uid"]] = item
    if not calendars: raise RuntimeError("Geen geldige teamagenda's opgebouwd.")
    expected = {TEAM_FILES[name] for name in calendars}
    for old in Path(".").glob("*.ics"):
        if old.name not in expected: old.unlink()
    results = []
    for team in sorted(calendars, key=lambda x: TEAM_ORDER.get(x, 9999)):
        matches = list(calendars[team].values()); filename = TEAM_FILES[team]
        Path(filename).write_text(build_calendar(team, matches), encoding="utf-8", newline="")
        results.append({"name": team, "output": filename, "count": len(matches)})
        print(f"{team}: {len(matches)} wedstrijden -> {filename}")
    Path("index.html").write_text(build_index(results), encoding="utf-8")


if __name__ == "__main__": main()
