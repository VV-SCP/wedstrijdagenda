#!/usr/bin/env python3
"""Ontdek VV SCP-teams en maak per team een actuele iCalendar-feed."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SITE_URL = "https://www.vv-scp.nl/"
OUTPUT_DIR = Path(".")
TZ = ZoneInfo("Europe/Amsterdam")

# Bestaande openbare link van SCP 1 blijft hierdoor ongewijzigd.
OUTPUT_OVERRIDES = {
    "SCP 1": "scp-1.ics",
    "SCP 2": "scp-2.ics",
    "SCP ZON 1": "scp-1.ics",
    "SCP ZON 2": "scp-2.ics",
}

MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4,
    "mei": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

TYPE_NAMES = {
    "co": "Competitie",
    "be": "Beker",
    "vr": "Vriendschappelijk",
}

HEADERS = {
    "User-Agent": (
        "VV-SCP-wedstrijdagenda/3.0 "
        "(+https://github.com/VV-SCP/wedstrijdagenda)"
    )
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_soup(url: str, timeout: int = 30) -> BeautifulSoup:
    """Download een pagina en geef BeautifulSoup terug."""
    response = SESSION.get(url, timeout=timeout)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def clean_text(value: str) -> str:
    return " ".join(value.split())


def clean_team(value: str) -> str:
    """Verwijder extra spaties en toevoegingen als '(zon)' en '(zat)'."""
    value = clean_text(value)
    return re.sub(r"\s*\((?:zon|zat)\)\s*$", "", value, flags=re.IGNORECASE)


def slugify(value: str) -> str:
    """Maak een veilige bestandsnaam van een teamnaam."""
    value = value.lower().replace("'", "")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def output_name(team_name: str) -> str:
    """Bepaal de openbare bestandsnaam van een teamagenda."""
    if team_name in OUTPUT_OVERRIDES:
        return OUTPUT_OVERRIDES[team_name]

    short_name = re.sub(r"^SJO\s+", "", team_name, flags=re.IGNORECASE)
    short_name = re.sub(r"^SCP/DSZ(?:-DIO'?30)?\s+", "", short_name, flags=re.IGNORECASE)
    return f"{slugify(short_name)}.ics"


def escape(value: str) -> str:
    """Maak tekst geschikt voor iCalendar."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line: str, limit: int = 73) -> str:
    """Breek lange iCalendar-regels correct af."""
    parts = []
    while len(line.encode("utf-8")) > limit:
        cut = min(limit, len(line))
        while len(line[:cut].encode("utf-8")) > limit:
            cut -= 1
        parts.append(line[:cut])
        line = " " + line[cut:]
    parts.append(line)
    return "\r\n".join(parts)


def season_year(month: int) -> int:
    """Bepaal het kalenderjaar binnen het actuele voetbalseizoen."""
    now = datetime.now(TZ)
    season_start = now.year if now.month >= 7 else now.year - 1
    return season_start if month >= 7 else season_start + 1


def normalise_postcode(postcode: str) -> str:
    postcode = postcode.upper().replace(" ", "")
    return f"{postcode[:4]} {postcode[4:]}" if len(postcode) == 6 else postcode


def normalise_result(value: str) -> str:
    """Geef alleen een numerieke uitslag terug."""
    result_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", clean_text(value))
    if not result_match:
        return ""
    return f"{result_match.group(1)} - {result_match.group(2)}"


def detect_match_status(page_text: str) -> str:
    """Herken afgelast, uitgesteld, gestaakt of vervallen."""
    text = clean_text(page_text.lower())
    for status in ("afgelast", "uitgesteld", "gestaakt", "vervallen"):
        if f"deze wedstrijd is {status}" in text or re.search(rf"\b{status}\b", text):
            return status
    return ""


def detail_data(url: str) -> tuple[str, str, str]:
    """Haal adres, accommodatie en status van een wedstrijddetailpagina."""
    try:
        soup = get_soup(url, timeout=20)
    except requests.RequestException:
        return "", "", ""

    page_text = soup.get_text(" ", strip=True)
    match_status = detect_match_status(page_text)
    location_match = re.search(
        r"Accommodatie\s+(.+?)\s+"
        r"([A-Za-zÀ-ÿ' .-]+\s+\d+[A-Za-z]?)\s+"
        r"(\d{4}\s?[A-Z]{2})\s+"
        r"([A-Za-zÀ-ÿ' .-]+?)"
        r"(?:\s+tel\.|\s+Toon routebeschrijving)",
        page_text,
        flags=re.IGNORECASE,
    )
    if not location_match:
        return "", "", match_status

    accommodation = clean_text(location_match.group(1))
    street = clean_text(location_match.group(2))
    postcode = normalise_postcode(location_match.group(3))
    place = clean_text(location_match.group(4)).title()
    return f"{street}, {postcode} {place}", accommodation, match_status


def find_program_table(soup: BeautifulSoup):
    """Zoek de tabel met datum, tijd, thuis, uit en uitslag."""
    for candidate in soup.find_all("table"):
        headers = {
            clean_text(heading.get_text(" ", strip=True)).lower()
            for heading in candidate.find_all("th")
        }
        if {"datum", "tijd", "thuis", "uit", "uitslag"}.issubset(headers):
            return candidate
    return None


def team_name_from_page(soup: BeautifulSoup) -> str:
    """Haal de teamnaam uit het kruimelpad van een programmapagina."""
    text = clean_text(soup.get_text(" ", strip=True))
    breadcrumb_match = re.search(
        r"Teams\s+(?:Senioren|Junioren|Pupillen)\s+(.+?)\s+Uitslagen\s*&\s*programma",
        text,
        flags=re.IGNORECASE,
    )
    if breadcrumb_match:
        return clean_text(breadcrumb_match.group(1))

    headings = [clean_text(h.get_text(" ", strip=True)) for h in soup.find_all(["h1", "h2"])]
    for heading in headings:
        if heading.lower() != "uitslagen & programma":
            return heading
    return ""


def discover_team_pages() -> list[dict]:
    """Ontdek automatisch alle actuele links 'Uitslagen & programma'."""
    soup = get_soup(SITE_URL)
    urls = []
    seen = set()

    for link in soup.find_all("a", href=True):
        label = clean_text(link.get_text(" ", strip=True)).lower()
        if label != "uitslagen & programma":
            continue

        url = urljoin(SITE_URL, link["href"])
        parsed = urlparse(url)
        if parsed.netloc != urlparse(SITE_URL).netloc or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    teams = []
    used_outputs = set()
    for url in urls:
        try:
            page = get_soup(url)
        except requests.RequestException as exc:
            print(f"WAARSCHUWING: {url} kon niet worden opgehaald: {exc}")
            continue

        if find_program_table(page) is None:
            continue

        name = team_name_from_page(page)
        if not name:
            print(f"WAARSCHUWING: teamnaam niet gevonden voor {url}")
            continue

        filename = output_name(name)
        if filename in used_outputs:
            continue
        used_outputs.add(filename)
        teams.append({"name": name, "url": url, "output": filename})

    if not teams:
        raise RuntimeError("Geen actuele teampagina's met programma gevonden.")

    return sorted(teams, key=lambda item: item["name"].lower())


def create_uid(season_start: int, team_name: str, home: str, away: str, kind: str) -> str:
    """Maak per team en wedstrijd een vaste ID."""
    raw_id = f"{season_start}|{team_name}|{home}|{away}|{kind}"
    match_hash = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:20]
    return f"{match_hash}@vv-scp.nl"


def fetch_matches(team: dict, soup: BeautifulSoup | None = None) -> list[dict]:
    """Lees programma, uitslagen, locaties en statussen van een team."""
    soup = soup or get_soup(team["url"])
    program_table = find_program_table(soup)
    if program_table is None:
        raise RuntimeError(f"Geen programmatabel gevonden voor {team['name']}.")

    matches = []
    for row in program_table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 6 or cells[0].name == "th":
            continue

        values = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]
        date_match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3})", values[0].lower())
        time_match = re.fullmatch(r"(\d{1,2})[.:](\d{2})", values[1])
        if not date_match or not time_match:
            continue

        day = int(date_match.group(1))
        month = MONTHS.get(date_match.group(2))
        if not month:
            continue

        year = season_year(month)
        start = datetime(
            year, month, day,
            int(time_match.group(1)), int(time_match.group(2)),
            tzinfo=TZ,
        )
        home = clean_team(values[2])
        away = clean_team(values[3])
        kind = TYPE_NAMES.get(values[4].lower(), values[4] or "Wedstrijd")
        result = normalise_result(values[5])

        detail_url = ""
        for link in row.find_all("a", href=True):
            href = link.get("href", "")
            if "wedstrijd" in href.lower():
                detail_url = urljoin(team["url"], href)
                break

        address = ""
        accommodation = ""
        match_status = ""
        if detail_url:
            address, accommodation, match_status = detail_data(detail_url)

        season_start = year if month >= 7 else year - 1
        uid = create_uid(season_start, team["name"], home, away, kind)
        matches.append({
            "start": start,
            "home": home,
            "away": away,
            "kind": kind,
            "result": result,
            "match_status": match_status,
            "url": detail_url or team["url"],
            "address": address,
            "accommodation": accommodation,
            "uid": uid,
        })

    return matches


def build_calendar(calendar_name: str, matches: list[dict]) -> str:
    """Zet wedstrijden, uitslagen en statussen om naar iCalendar."""
    timestamp = datetime.now(TZ).strftime("%Y%m%dT000000Z")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//VV SCP//Wedstrijdagenda {calendar_name}//NL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(calendar_name)}",
        "X-WR-TIMEZONE:Europe/Amsterdam",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]

    for match in sorted(matches, key=lambda item: item["start"]):
        start = match["start"]
        end = start + timedelta(hours=2)
        status = match["match_status"]
        title = f"{match['home']} - {match['away']}"
        if status:
            title = f"{status.upper()}: {title}"
        elif match["result"]:
            title += f" ({match['result']})"

        description = [match["kind"]]
        if status:
            description.insert(0, f"Status: {status}")
        elif match["result"]:
            description.append(f"Eindstand: {match['result']}")
        if match["accommodation"]:
            description.append(f"Accommodatie: {match['accommodation']}")
        description.append("Controleer bij last-minute wijzigingen altijd Voetbal.nl.")

        ical_status = "CANCELLED" if status in {"afgelast", "vervallen"} else "CONFIRMED"
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{match['uid']}",
            f"DTSTAMP:{timestamp}",
            f"DTSTART;TZID=Europe/Amsterdam:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Europe/Amsterdam:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{escape(title)}",
            f"DESCRIPTION:{escape(chr(10).join(description))}",
            f"URL:{match['url']}",
            f"STATUS:{ical_status}",
            "SEQUENCE:0",
        ])
        if match["address"]:
            lines.append(f"LOCATION:{escape(match['address'])}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def build_index(teams: list[dict]) -> str:
    """Maak een eenvoudige overzichtspagina met alle agenda-links."""
    items = []
    for team in teams:
        name = team["name"]
        filename = team["output"]
        items.append(
            f'<li><strong>{name}</strong><br>'
            f'<a href="{filename}">Agenda openen</a> · '
            f'<code>https://vv-scp.github.io/wedstrijdagenda/{filename}</code></li>'
        )

    return """<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Wedstrijdagenda's VV SCP</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:850px;margin:40px auto;padding:0 18px;line-height:1.5}
    li{margin:0 0 18px} code{overflow-wrap:anywhere} a{color:#0757b8}
  </style>
</head>
<body>
  <h1>Wedstrijdagenda's VV SCP</h1>
  <p>Kies het gewenste team en voeg de agenda toe als abonnement.</p>
  <ul>""" + "".join(items) + """</ul>
  <p><small>Controleer bij last-minute wijzigingen altijd Voetbal.nl.</small></p>
</body>
</html>
"""


def main() -> None:
    """Ontdek teams en maak alle afzonderlijke agenda's."""
    teams = discover_team_pages()
    generated = []

    for team in teams:
        matches = fetch_matches(team)
        calendar = build_calendar(team["name"], matches)
        output = OUTPUT_DIR / team["output"]
        output.write_text(calendar, encoding="utf-8", newline="")
        generated.append(team)
        print(f"{team['name']}: {len(matches)} wedstrijden -> {output}")

    (OUTPUT_DIR / "index.html").write_text(
        build_index(generated), encoding="utf-8"
    )
    print(f"Klaar: {len(generated)} teamagenda's en index.html gemaakt.")


if __name__ == "__main__":
    main()
