#!/usr/bin/env python3
"""Maak automatisch een aparte iCalendar-feed voor ieder VV SCP-team."""

from __future__ import annotations

import hashlib
import html
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = "https://site-api.voetbalassi.st/SCP/front/programmaenuitslagen/PostWedstrijden"
PUBLIC_BASE_URL = "https://vv-scp.github.io/wedstrijdagenda"
TZ = ZoneInfo("Europe/Amsterdam")

HEADERS = {
    "User-Agent": "VV-SCP-wedstrijdagenda/8.2 (+https://github.com/VV-SCP/wedstrijdagenda)",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.vv-scp.nl",
    "Referer": "https://www.vv-scp.nl/521/programma/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Alleen deze vaste teamnamen mogen als agenda worden gepubliceerd.
# Daardoor kunnen tegenstanders als Ewijk, SC Woezik en Blauw Wit nooit
# onderdeel worden van een agenda- of bestandsnaam.
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
    return (
        datetime(start_year, 7, 1, tzinfo=TZ),
        datetime(start_year + 1, 7, 1, tzinfo=TZ),
    )


def api_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def fetch_season_matches() -> list[dict]:
    """Doe exact een API-aanvraag voor het volledige actuele seizoen."""
    now = datetime.now(TZ)
    season_start, season_end = season_bounds(now)
    payload = {
        "clubWedstrijdenStandaardSorterenOp": "team",
        "datumBeginSeizoen": api_timestamp(season_start),
        "datumEindeSeizoen": api_timestamp(season_end),
        "datumVan": api_timestamp(season_start),
        "datumTot": api_timestamp(season_end),
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
    return data


def display_team_name(value: object) -> str:
    """Maak alleen de zichtbare wedstrijdnaam netjes."""
    name = clean_text(value)
    name = re.sub(r"\s*\((?:zon|zat)\)\s*$", "", name, flags=re.IGNORECASE)
    name = name.replace("SJO SCP/DSZ -DIO'3...", "SJO SCP/DSZ-DIO'30")
    return name


def choose_raw_scp_name(match: dict) -> str:
    """Kies de thuis- of uitnaam waarin SCP voorkomt."""
    home = clean_text(match.get("thuisClubEnTeamNaamFriendly"))
    away = clean_text(match.get("uitClubEnTeamNaamFriendly"))
    for name in (home, away):
        upper = name.upper()
        if upper.startswith("SCP ") or upper.startswith("SJO SCP/DSZ"):
            return name
    return ""


def canonical_scp_team(match: dict) -> str:
    """Koppel een API-wedstrijd uitsluitend aan een vaste geldige teamnaam."""
    raw = choose_raw_scp_name(match)
    if not raw:
        return ""

    upper = raw.upper()

    # Senioren zaterdag/zondag eerst scheiden met de expliciete API-vlaggen.
    # VoetbalAssist levert de zichtbare naam soms zonder '(zat)' of '(zon)'.
    # Daarom zijn bevatZaterdagteam en bevatZondagteam leidend.
    if not re.search(r"\bO\d+", upper):
        senior_number = re.search(r"\bSCP\s+(\d+)\b", upper)
        if senior_number and bool(match.get("bevatZaterdagteam")):
            canonical = f"SCP Zaterdag {senior_number.group(1)}"
            return canonical if canonical in TEAM_FILES else ""
        if senior_number and bool(match.get("bevatZondagteam")):
            canonical = f"SCP {senior_number.group(1)}"
            return canonical if canonical in TEAM_FILES else ""

    # Drie bekende foutieve API-labels. Dit zijn geen aparte SCP-teams.
    fixed_aliases = {
        "SJO SCP/DSZ O13-EWIJK 1": "SJO SCP/DSZ O13-1",
        "SJO SCP/DSZ O14-SC WOEZIK 2": "SJO SCP/DSZ O14-1",
        "SJO SCP/DSZ O15-BLAUW WIT 1": "SJO SCP/DSZ O15-1",
    }
    if upper in fixed_aliases:
        return fixed_aliases[upper]

    # Jeugd: lees O-leeftijd en het SCP-teamnummer uitsluitend uit de SCP-naam.
    # Werkt zowel voor O13-1 als afwijkingen zoals O13-Ewijk 1.
    age_match = re.search(r"\bO(\d+)\b", upper)
    if age_match:
        age = int(age_match.group(1))
        tail = raw[age_match.end():]
        number_match = re.search(r"(\d+)\s*$", tail)
        if not number_match:
            print(f"WAARSCHUWING: geen teamnummer herkend in: {raw}")
            return ""
        number = int(number_match.group(1))
        if age == 19 and number == 2:
            canonical = "SJO SCP/DSZ-DIO'30 O19-2"
        else:
            canonical = f"SJO SCP/DSZ O{age}-{number}"
        if canonical not in TEAM_FILES:
            print(f"WAARSCHUWING: onbekend jeugdteam overgeslagen: {canonical} (bron: {raw})")
            return ""
        return canonical

    # Senioren: herken zaterdag, zondag, dames en 35+ zonder te gokken.
    cleaned = display_team_name(raw)
    if re.search(r"\bSCP\s+(?:VR|VROUWEN|DAMES)\s*1\b", upper):
        canonical = "SCP Dames 1"
    elif "(ZAT)" in upper:
        number = re.search(r"\b(\d+)\b", upper)
        canonical = f"SCP Zaterdag {number.group(1)}" if number else ""
    elif "35+1" in upper:
        canonical = "SCP 35+1"
    elif "35+2" in upper:
        canonical = "SCP 35+2"
    else:
        number = re.search(r"\bSCP\s+(\d+)\b", upper)
        canonical = f"SCP {number.group(1)}" if number else cleaned

    if canonical not in TEAM_FILES:
        print(f"WAARSCHUWING: onbekend seniorenteam overgeslagen: {canonical} (bron: {raw})")
        return ""
    return canonical


def parse_api_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(clean_text(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def normalise_result(value: object) -> str:
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", clean_text(value))
    return f"{match.group(1)} - {match.group(2)}" if match else ""


def match_type(match: dict) -> str:
    if match.get("typeVriendschappelijk"):
        return "Vriendschappelijk"
    if match.get("typeBeker"):
        return "Beker"
    return "Competitie"


def create_uid(match: dict) -> str:
    api_id = clean_text(match.get("id"))
    if api_id:
        return f"wedstrijd-{api_id}@vv-scp.nl"
    raw = "|".join(
        clean_text(match.get(key))
        for key in (
            "wedstrijdnummer",
            "thuisClubEnTeamNaamFriendly",
            "uitClubEnTeamNaamFriendly",
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{digest}@vv-scp.nl"


def convert_match(raw: dict) -> tuple[str, dict] | None:
    team_name = canonical_scp_team(raw)
    if not team_name:
        return None

    status = ""
    if raw.get("statusAfgelast"):
        status = "afgelast"
    elif raw.get("statusHerkeuring"):
        status = "herkeuring"

    detail_url = clean_text(raw.get("wedstrijdDetailsUrl"))
    if not detail_url.startswith("http"):
        detail_url = API_URL

    is_home = bool(raw.get("isThuiswedstrijd"))
    item = {
        "start": parse_api_datetime(raw.get("datum")),
        "home": display_team_name(raw.get("thuisClubEnTeamNaamFriendly")),
        "away": display_team_name(raw.get("uitClubEnTeamNaamFriendly")),
        "kind": match_type(raw),
        "result": normalise_result(raw.get("uitslag")),
        "status": status,
        "url": detail_url,
        "address": "Hosterd 65, 6655 AE Puiflijk" if is_home else "",
        "accommodation": "Sportpark Schravenhof" if is_home else "",
        "field": clean_text(raw.get("verkrijgVeldtekst")),
        "referee": clean_text(raw.get("scheidsrechter")),
        "remark": clean_text(raw.get("opmerking")),
        "uid": create_uid(raw),
    }
    return team_name, item


def escape_ical(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_ical(line: str, limit: int = 73) -> str:
    parts = []
    while len(line.encode("utf-8")) > limit:
        cut = min(limit, len(line))
        while len(line[:cut].encode("utf-8")) > limit:
            cut -= 1
        parts.append(line[:cut])
        line = " " + line[cut:]
    parts.append(line)
    return "\r\n".join(parts)


def build_calendar(team_name: str, matches: list[dict]) -> str:
    timestamp = datetime.now(TZ).strftime("%Y%m%dT000000Z")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//VV SCP//Wedstrijdagenda {team_name}//NL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_ical(team_name)}",
        "X-WR-TIMEZONE:Europe/Amsterdam",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]

    for match in sorted(matches, key=lambda item: item["start"]):
        start = match["start"]
        end = start + timedelta(hours=2)
        status = match["status"]
        title = f"{match['home']} - {match['away']}"
        if status:
            title = f"{status.upper()}: {title}"
        elif match["result"]:
            title += f" ({match['result']})"

        description = [match["kind"]]
        if status:
            description.insert(0, f"Status: {status}")
        if match["result"]:
            description.append(f"Eindstand: {match['result']}")
        if match["accommodation"]:
            description.append(f"Accommodatie: {match['accommodation']}")
        if match["field"]:
            description.append(f"Veld: {match['field']}")
        if match["referee"]:
            description.append(f"Scheidsrechter: {match['referee']}")
        if match["remark"]:
            description.append(f"Opmerking: {match['remark']}")
        description.append("Controleer bij last-minute wijzigingen altijd Voetbal.nl.")

        ical_status = "CANCELLED" if status == "afgelast" else "CONFIRMED"
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{match['uid']}",
                f"DTSTAMP:{timestamp}",
                f"DTSTART;TZID=Europe/Amsterdam:{start.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID=Europe/Amsterdam:{end.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{escape_ical(title)}",
                f"DESCRIPTION:{escape_ical(chr(10).join(description))}",
                f"URL:{match['url']}",
                f"STATUS:{ical_status}",
                "SEQUENCE:0",
            ]
        )
        if match["address"]:
            lines.append(f"LOCATION:{escape_ical(match['address'])}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical(line) for line in lines) + "\r\n"


def category(team_name: str) -> str:
    if team_name.startswith("SCP "):
        return "Senioren"
    age = re.search(r"\bO(\d+)-", team_name)
    if age and int(age.group(1)) >= 12:
        return "Junioren"
    return "Pupillen"


def team_sort_key(team: dict) -> int:
    """Sorteer exact volgens TEAM_FILES: senioren, daarna jeugd van oud naar jong."""
    return TEAM_ORDER.get(team["name"], 9999)


def build_index(results: list[dict]) -> str:
    groups = {"Senioren": [], "Junioren": [], "Pupillen": []}
    for result in results:
        groups[category(result["name"])].append(result)

    sections = []
    for group_name, teams in groups.items():
        if not teams:
            continue
        cards = []
        for team in sorted(teams, key=team_sort_key):
            name = html.escape(team["name"])
            filename = html.escape(team["output"])
            https_url = f"{PUBLIC_BASE_URL}/{filename}"
            webcal_url = https_url.replace("https://", "webcal://", 1)
            cards.append(
                f'<article class="card"><h3>{name}</h3>'
                f'<p class="count">{team["count"]} wedstrijden</p>'
                f'<div class="actions">'
                f'<a class="button apple" href="{webcal_url}">Abonneren op iPhone</a>'
                f'<button class="button android copy-link" type="button" '
                f'data-url="{https_url}">Kopieer link voor Android</button></div>'
                f'<p class="help">Kies een abonnement, niet een eenmalige import.</p>'
                f'<details><summary>Android-instructie</summary><ol>'
                f'<li>Kopieer de link hierboven.</li>'
                f'<li>Open Google Agenda in een browser en kies '
                f'<strong>Andere agenda\'s &gt; + &gt; Via URL</strong>.</li>'
                f'<li>Plak de link en kies <strong>Agenda toevoegen</strong>.</li>'
                f'</ol></details><p class="url">{https_url}</p></article>'
            )
        sections.append(
            f'<section><h2>{group_name}</h2><div class="grid">'
            f'{"".join(cards)}</div></section>'
        )

    return f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#116735">
<title>Wedstrijdagenda's VV SCP</title>
<style>
:root{{--green:#116735;--blue:#1565c0;--ink:#172033;--muted:#5d6675;--line:#d8dee9;--bg:#f5f7fa}}
*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1120px;margin:0 auto;padding:28px 18px 48px;line-height:1.5;color:var(--ink);background:var(--bg)}}
h1{{margin-bottom:6px}}h2{{margin-top:34px}}h3{{margin:0 0 4px}}.intro{{max-width:800px}}
.notice{{background:#fff7dc;border-left:4px solid #e0a800;padding:12px 14px;border-radius:8px;margin:20px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}}
.card{{border:1px solid var(--line);border-radius:14px;padding:18px;background:#fff;box-shadow:0 2px 8px rgba(21,32,51,.05)}}
.count,.help{{color:var(--muted)}}.count{{margin-top:0}}.actions{{display:grid;gap:10px;margin:14px 0}}
.button{{display:block;width:100%;border:0;border-radius:9px;padding:11px 13px;color:#fff;text-align:center;text-decoration:none;font-weight:700;font-size:1rem;cursor:pointer}}
.apple{{background:var(--green)}}.android{{background:var(--blue)}}details{{border-top:1px solid var(--line);padding-top:10px;margin-top:12px}}
summary{{cursor:pointer;font-weight:700}}.url{{font-size:.76rem;overflow-wrap:anywhere;color:var(--muted)}}
.toast{{position:sticky;bottom:16px;margin:22px auto 0;max-width:560px;background:#172033;color:#fff;border-radius:10px;padding:12px 14px;text-align:center;display:none}}
footer{{margin-top:34px;color:var(--muted)}}
</style></head><body>
<h1>Wedstrijdagenda's VV SCP</h1>
<p class="intro">Kies jouw team. De groene knop opent op een iPhone rechtstreeks een agenda-abonnement. De blauwe knop kopieert de link voor Android.</p>
<div class="notice"><strong>Belangrijk:</strong> voeg de agenda toe als abonnement. Importeer het ICS-bestand niet eenmalig, want dan worden latere wijzigingen niet automatisch verwerkt.</div>
{"".join(sections)}
<div id="toast" class="toast" role="status" aria-live="polite">De agenda-link is gekopieerd.</div>
<footer>De agenda's worden ieder uur bijgewerkt. Controleer bij last-minute wijzigingen altijd Voetbal.nl of de teamcommunicatie.</footer>
<script>
(() => {{
  const toast = document.getElementById('toast');
  let timer;
  const showMessage = (message) => {{
    toast.textContent = message; toast.style.display = 'block';
    clearTimeout(timer); timer = setTimeout(() => toast.style.display = 'none', 3500);
  }};
  document.querySelectorAll('.copy-link').forEach((button) => {{
    button.addEventListener('click', async () => {{
      const url = button.dataset.url;
      try {{
        await navigator.clipboard.writeText(url);
        showMessage('De agenda-link is gekopieerd. Plak deze bij Google Agenda > Andere agenda\'s > Via URL.');
      }} catch (error) {{ window.prompt('Kopieer deze agenda-link:', url); }}
    }});
  }});
}})();
</script></body></html>"""


def main() -> None:
    raw_matches = fetch_season_matches()

    # Per team wordt de vaste wedstrijd-UID als sleutel gebruikt. Een API-record
    # kan daardoor nooit dubbel in dezelfde agenda terechtkomen.
    calendar_maps: dict[str, dict[str, dict]] = {
        team_name: {} for team_name in TEAM_FILES
    }

    for raw in raw_matches:
        converted = convert_match(raw)
        if converted:
            team_name, item = converted
            calendar_maps[team_name][item["uid"]] = item

    calendars = {
        team_name: list(matches_by_uid.values())
        for team_name, matches_by_uid in calendar_maps.items()
    }

    if not calendars:
        raise RuntimeError("Er zijn geen geldige SCP-teamagenda's opgebouwd.")

    expected_files = {TEAM_FILES[name] for name in calendars}
    for old_file in Path(".").glob("*.ics"):
        if old_file.name not in expected_files:
            old_file.unlink()
            print(f"Verouderd agenda-bestand verwijderd: {old_file.name}")

    results = []
    for team_name in sorted(calendars, key=lambda name: TEAM_ORDER.get(name, 9999)):
        matches = calendars[team_name]
        filename = TEAM_FILES[team_name]
        Path(filename).write_text(
            build_calendar(team_name, matches), encoding="utf-8", newline=""
        )
        results.append({"name": team_name, "output": filename, "count": len(matches)})
        print(f"{team_name}: {len(matches)} wedstrijden -> {filename}")

    Path("index.html").write_text(build_index(results), encoding="utf-8")
    print(
        f"Klaar: {len(results)} teamagenda's, "
        f"{len(raw_matches)} API-wedstrijden verwerkt en index.html gemaakt."
    )


if __name__ == "__main__":
    main()
