#!/usr/bin/env python3
"""Maak snel een aparte iCalendar-feed voor elk VV SCP-team via een API-aanvraag."""

from __future__ import annotations

import hashlib
import html
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = (
    "https://site-api.voetbalassi.st/"
    "SCP/front/programmaenuitslagen/PostWedstrijden"
)
PUBLIC_BASE_URL = "https://vv-scp.github.io/wedstrijdagenda"
TZ = ZoneInfo("Europe/Amsterdam")

HEADERS = {
    "User-Agent": (
        "VV-SCP-wedstrijdagenda/6.0 "
        "(+https://github.com/VV-SCP/wedstrijdagenda)"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.vv-scp.nl",
    "Referer": "https://www.vv-scp.nl/521/programma/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Houd bestaande openbare links stabiel en geef bekende teams nette bestandsnamen.
OUTPUT_OVERRIDES = {
    "SCP 1": "scp-1.ics",
    "SCP 2": "scp-2.ics",
    "SCP 3": "scp-3.ics",
    "SCP Zaterdag 2": "scp-zaterdag-2.ics",
    "SCP Dames 1": "scp-dames-1.ics",
    "SCP 35+1": "scp-35-plus-1.ics",
    "SCP 35+2": "scp-35-plus-2.ics",
    "SJO SCP/DSZ-DIO'30 O19-2": "o19-2.ics",
}


def season_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Geef het begin en einde van het actuele voetbalseizoen."""
    now = now or datetime.now(TZ)
    start_year = now.year if now.month >= 7 else now.year - 1
    start = datetime(start_year, 7, 1, tzinfo=TZ)
    end = datetime(start_year + 1, 7, 1, tzinfo=TZ)
    return start, end


def api_timestamp(value: datetime) -> str:
    """Zet een datum om naar het formaat dat VoetbalAssist verwacht."""
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def fetch_season_matches() -> list[dict]:
    """Haal het volledige actuele seizoen op via de VoetbalAssist-API."""
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
        raise RuntimeError("De VoetbalAssist-API gaf geen lijst met wedstrijden terug.")

    return data


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def clean_team_name(value: object) -> str:
    """Normaliseer een teamnaam voor weergave in de agenda."""
    name = clean_text(value)
    name = re.sub(r"\s*\((?:zon|zat)\)\s*$", "", name, flags=re.IGNORECASE)
    name = name.replace("SJO SCP/DSZ -DIO'3...", "SJO SCP/DSZ-DIO'30")
    return name


def is_scp_team(name: str) -> bool:
    """Controleer of een teamnaam bij SCP of het SCP-samenwerkingsverband hoort."""
    upper = clean_text(name).upper()
    return upper.startswith("SCP ") or upper.startswith("SJO SCP/DSZ")


def canonical_scp_team(match: dict) -> str:
    """Bepaal een korte, vaste SCP-teamnaam voor bestandsnaam en agenda."""
    home_raw = clean_text(match.get("thuisClubEnTeamNaamFriendly"))
    away_raw = clean_text(match.get("uitClubEnTeamNaamFriendly"))

    if is_scp_team(home_raw):
        raw = home_raw
    elif is_scp_team(away_raw):
        raw = away_raw
    else:
        return ""

    category = clean_text(match.get("categorie")).lower()
    age = clean_text(match.get("leeftijd"))
    team_number = clean_text(match.get("team"))

    # Voor jeugd zijn leeftijd en teamnummer betrouwbaarder dan soms afgekorte labels.
    if category in {"junioren", "pupillen"} and age.isdigit() and team_number:
        age_number = int(age)
        if age_number == 19 and team_number == "2":
            return "SJO SCP/DSZ-DIO'30 O19-2"
        return f"SJO SCP/DSZ O{age_number}-{team_number}"

    cleaned = clean_team_name(raw)

    # Houd zaterdag- en zondagteams uit elkaar, ook als beide hetzelfde nummer hebben.
    if "(zat)" in raw.lower() or bool(match.get("bevatZaterdagteam")):
        number = re.search(r"\b(\d+)\b", cleaned)
        if number:
            return f"SCP Zaterdag {number.group(1)}"

    if "(zon)" in raw.lower() or bool(match.get("bevatZondagteam")):
        number = re.search(r"\b(\d+)\b", cleaned)
        if number:
            return f"SCP {number.group(1)}"

    return cleaned


def slugify(value: str) -> str:
    value = value.lower().replace("+", "-plus-").replace("'", "")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def output_name(team_name: str) -> str:
    if team_name in OUTPUT_OVERRIDES:
        return OUTPUT_OVERRIDES[team_name]

    youth = re.search(r"\bO(\d+)-(\d+)\b", team_name, flags=re.IGNORECASE)
    if youth:
        return f"o{youth.group(1)}-{youth.group(2)}.ics"

    return f"{slugify(team_name)}.ics"


def parse_api_datetime(value: object) -> datetime:
    text = clean_text(value)
    parsed = datetime.fromisoformat(text)
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


def extract_url(value: object) -> str:
    """Haal een URL uit gewone tekst of uit per ongeluk meegegeven HTML."""
    text = html.unescape(str(value or ""))
    href = re.search(r'href=["\']([^"\']+)', text, flags=re.IGNORECASE)
    if href:
        return href.group(1)
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return ""


def detail_location(url: str) -> tuple[str, str]:
    """Geen extra detailverzoeken: locatie blijft leeg en veld komt uit de API."""
    return "", ""


def create_uid(match: dict) -> str:
    """Gebruik de stabiele API-ID en val anders terug op een inhoudelijke hash."""
    api_id = clean_text(match.get("id"))
    if api_id:
        return f"wedstrijd-{api_id}@vv-scp.nl"

    raw = "|".join(
        [
            clean_text(match.get("wedstrijdnummer")),
            clean_text(match.get("thuisClubEnTeamNaamFriendly")),
            clean_text(match.get("uitClubEnTeamNaamFriendly")),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{digest}@vv-scp.nl"


def convert_match(raw: dict) -> tuple[str, dict] | None:
    """Zet een API-wedstrijd om naar een agenda-item en koppel deze aan het SCP-team."""
    team_name = canonical_scp_team(raw)
    if not team_name:
        return None

    home = clean_team_name(raw.get("thuisClubEnTeamNaamFriendly"))
    away = clean_team_name(raw.get("uitClubEnTeamNaamFriendly"))
    detail_url = extract_url(raw.get("wedstrijdDetailsUrl"))
    address = "Hosterd 65, 6655 AE Puiflijk" if raw.get("isThuiswedstrijd") else ""
    accommodation = "Sportpark Schravenhof" if raw.get("isThuiswedstrijd") else ""

    status = ""
    if raw.get("statusAfgelast"):
        status = "afgelast"
    elif raw.get("statusHerkeuring"):
        status = "herkeuring"

    item = {
        "start": parse_api_datetime(raw.get("datum")),
        "home": home,
        "away": away,
        "kind": match_type(raw),
        "result": normalise_result(raw.get("uitslag")),
        "status": status,
        "url": detail_url or API_URL,
        "address": address,
        "accommodation": accommodation,
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


def build_index(results: list[dict]) -> str:
    """Maak een mobiele overzichtspagina met echte agenda-abonnementen."""
    groups = {"Senioren": [], "Junioren": [], "Pupillen": []}
    for result in results:
        groups[category(result["name"])].append(result)

    sections = []
    for group_name, teams in groups.items():
        if not teams:
            continue

        cards = []
        for team in sorted(teams, key=lambda item: item["name"]):
            name = html.escape(team["name"])
            filename = html.escape(team["output"])
            https_url = f"{PUBLIC_BASE_URL}/{filename}"
            webcal_url = https_url.replace("https://", "webcal://", 1)

            cards.append(
                f'<article class="card">'
                f'<h3>{name}</h3>'
                f'<p class="count">{team["count"]} wedstrijden</p>'
                f'<div class="actions">'
                f'<a class="button apple" href="{webcal_url}">Abonneren op iPhone</a>'
                f'<button class="button android copy-link" type="button" '
                f'data-url="{https_url}">Kopieer link voor Android</button>'
                f'</div>'
                f'<p class="help">Kies een abonnement, niet een eenmalige import.</p>'
                f'<details><summary>Android-instructie</summary>'
                f'<ol><li>Kopieer de link hierboven.</li>'
                f'<li>Open Google Agenda in een browser en kies '
                f'<strong>Andere agenda\'s &gt; + &gt; Via URL</strong>.</li>'
                f'<li>Plak de link en kies <strong>Agenda toevoegen</strong>.</li></ol>'
                f'<p class="small">Dit werkt het betrouwbaarst via de webversie van Google Agenda. '
                f'Daarna verschijnt de agenda ook op de Android-telefoon.</p></details>'
                f'<p class="url">{https_url}</p>'
                f'</article>'
            )

        sections.append(
            f'<section><h2>{group_name}</h2><div class="grid">'
            f'{"".join(cards)}</div></section>'
        )

    return f"""<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#116735">
  <title>Wedstrijdagenda's VV SCP</title>
  <style>
    :root{{--green:#116735;--blue:#1565c0;--ink:#172033;--muted:#5d6675;--line:#d8dee9;--bg:#f5f7fa}}
    *{{box-sizing:border-box}}
    body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1120px;margin:0 auto;padding:28px 18px 48px;line-height:1.5;color:var(--ink);background:var(--bg)}}
    h1{{margin-bottom:6px}} h2{{margin-top:34px}} h3{{margin:0 0 4px}}
    .intro{{max-width:780px}} .notice{{background:#fff7dc;border-left:4px solid #e0a800;padding:12px 14px;border-radius:8px;margin:20px 0}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}}
    .card{{border:1px solid var(--line);border-radius:14px;padding:18px;background:white;box-shadow:0 2px 8px rgba(21,32,51,.05)}}
    .count,.help,.small{{color:var(--muted)}} .count{{margin-top:0}}
    .actions{{display:grid;gap:10px;margin:14px 0}}
    .button{{display:block;width:100%;border:0;border-radius:9px;padding:11px 13px;color:white;text-align:center;text-decoration:none;font-weight:700;font-size:1rem;cursor:pointer}}
    .apple{{background:var(--green)}} .android{{background:var(--blue)}}
    .button:focus{{outline:3px solid #ffbf47;outline-offset:2px}}
    details{{border-top:1px solid var(--line);padding-top:10px;margin-top:12px}}
    summary{{cursor:pointer;font-weight:700}} .url{{font-size:.76rem;overflow-wrap:anywhere;color:var(--muted)}}
    .toast{{position:sticky;bottom:16px;margin:22px auto 0;max-width:520px;background:#172033;color:white;border-radius:10px;padding:12px 14px;text-align:center;display:none}}
    footer{{margin-top:34px;color:var(--muted)}}
  </style>
</head>
<body>
  <h1>Wedstrijdagenda's VV SCP</h1>
  <p class="intro">Kies jouw team. Op een iPhone opent de groene knop rechtstreeks een agenda-abonnement. Voor Android kopieert de blauwe knop de abonnementslink.</p>
  <div class="notice"><strong>Belangrijk:</strong> voeg de agenda toe als abonnement. Download of importeer het ICS-bestand niet eenmalig, want dan worden latere wijzigingen niet automatisch verwerkt.</div>
  {"".join(sections)}
  <div id="toast" class="toast" role="status" aria-live="polite">De agenda-link is gekopieerd.</div>
  <footer>De agenda's worden ieder uur bijgewerkt. Controleer bij last-minute wijzigingen altijd Voetbal.nl of de teamcommunicatie.</footer>
  <script>
    (() => {{
      const toast = document.getElementById('toast');
      let timer;
      const showMessage = (message) => {{
        toast.textContent = message;
        toast.style.display = 'block';
        clearTimeout(timer);
        timer = setTimeout(() => {{ toast.style.display = 'none'; }}, 3500);
      }};

      document.querySelectorAll('.copy-link').forEach((button) => {{
        button.addEventListener('click', async () => {{
          const url = button.dataset.url;
          try {{
            await navigator.clipboard.writeText(url);
            showMessage('De agenda-link is gekopieerd. Plak deze bij Google Agenda > Andere agenda\'s > Via URL.');
          }} catch (error) {{
            window.prompt('Kopieer deze agenda-link:', url);
          }}
        }});
      }});
    }})();
  </script>
</body>
</html>"""


def main() -> None:
    raw_matches = fetch_season_matches()
    calendars: dict[str, list[dict]] = defaultdict(list)

    for raw in raw_matches:
        converted = convert_match(raw)
        if converted:
            team_name, item = converted
            calendars[team_name].append(item)

    if not calendars:
        raise RuntimeError("Er zijn geen SCP-teamagenda's uit de API opgebouwd.")

    expected_files = {output_name(name) for name in calendars}
    for old_file in Path(".").glob("*.ics"):
        if old_file.name not in expected_files:
            old_file.unlink()
            print(f"Verouderd agenda-bestand verwijderd: {old_file.name}")

    results = []
    for team_name, matches in sorted(calendars.items()):
        filename = output_name(team_name)
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
