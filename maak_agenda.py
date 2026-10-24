#!/usr/bin/env python3
"""Maak automatisch een iCalendar-feed voor SCP 1 vanaf vv-scp.nl."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

PROGRAM_URL = "https://www.vv-scp.nl/539/uitslagen--programma/"
OUTPUT = Path("scp-1.ics")
TZ = ZoneInfo("Europe/Amsterdam")

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mrt": 3,
    "apr": 4,
    "mei": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dec": 12,
}

TYPE_NAMES = {
    "co": "Competitie",
    "be": "Beker",
    "vr": "Vriendschappelijk",
}

HOME_LOCATION = "Hosterd 65, 6655 AE Puiflijk"
HOME_ACCOMMODATION = "Sportpark Schravenhof"

HEADERS = {
    "User-Agent": (
        "VV-SCP-wedstrijdagenda/1.0 "
        "(+https://github.com/VV-SCP/wedstrijdagenda)"
    )
}


def clean_team(value: str) -> str:
    """Verwijder overbodige spaties en de toevoeging '(zon)'."""
    value = " ".join(value.split())
    return re.sub(r"\s*\(zon\)\s*$", "", value, flags=re.IGNORECASE)


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
    """Zet een postcode om naar de vorm 1234 AB."""
    postcode = postcode.upper().replace(" ", "")

    if len(postcode) == 6:
        return f"{postcode[:4]} {postcode[4:]}"

    return postcode


def normalise_result(value: str) -> str:
    """Geef alleen een echte numerieke uitslag terug."""
    value = " ".join(value.split())
    result_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", value)

    if not result_match:
        return ""

    return f"{result_match.group(1)} - {result_match.group(2)}"


def detect_match_status(page_text: str) -> str:
    """Herken een bijzondere wedstrijdstatus op de detailpagina."""
    text = " ".join(page_text.lower().split())

    if "deze wedstrijd is afgelast" in text or re.search(r"\bafgelast\b", text):
        return "afgelast"

    if "deze wedstrijd is uitgesteld" in text or re.search(r"\buitgesteld\b", text):
        return "uitgesteld"

    if "deze wedstrijd is gestaakt" in text or re.search(r"\bgestaakt\b", text):
        return "gestaakt"

    if "deze wedstrijd is vervallen" in text or re.search(r"\bvervallen\b", text):
        return "vervallen"

    return ""


def detail_data(url: str) -> tuple[str, str, str]:
    """Haal adres, accommodatie en wedstrijdstatus van de detailpagina."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
    except requests.RequestException:
        return "", "", ""

    soup = BeautifulSoup(response.text, "html.parser")
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

    accommodation = " ".join(location_match.group(1).split())
    street = " ".join(location_match.group(2).split())
    postcode = normalise_postcode(location_match.group(3))
    place = " ".join(location_match.group(4).split()).title()
    address = f"{street}, {postcode} {place}"

    return address, accommodation, match_status


def create_uid(season_start: int, home: str, away: str, kind: str) -> str:
    """Maak een vaste wedstrijd-ID die gelijk blijft bij datumwijzigingen."""
    raw_id = f"{season_start}|{home}|{away}|{kind}"
    match_hash = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:20]
    return f"{match_hash}@vv-scp.nl"


def fetch_matches() -> list[dict]:
    """Lees programma, uitslagen en statussen van SCP 1."""
    response = requests.get(PROGRAM_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    program_table = None

    for candidate in soup.find_all("table"):
        table_headers = [
            heading.get_text(" ", strip=True).lower()
            for heading in candidate.find_all("th")
        ]

        required_headers = {"datum", "tijd", "thuis", "uit", "uitslag"}

        if required_headers.issubset(set(table_headers)):
            program_table = candidate
            break

    if program_table is None:
        raise RuntimeError(
            "Geen programmatabel gevonden. De bronpagina is mogelijk gewijzigd."
        )

    matches = []

    for row in program_table.find_all("tr"):
        cells = row.find_all(["td", "th"])

        if len(cells) < 6 or cells[0].name == "th":
            continue

        values = [cell.get_text(" ", strip=True) for cell in cells]

        date_match = re.fullmatch(
            r"(\d{1,2})\s+([A-Za-z]{3})",
            values[0].lower(),
        )
        time_match = re.fullmatch(r"(\d{1,2})[.:](\d{2})", values[1])

        if not date_match or not time_match:
            continue

        day = int(date_match.group(1))
        month = MONTHS.get(date_match.group(2))

        if not month:
            continue

        year = season_year(month)
        start = datetime(
            year,
            month,
            day,
            int(time_match.group(1)),
            int(time_match.group(2)),
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
                detail_url = urljoin(PROGRAM_URL, href)
                break

        address = ""
        accommodation = ""
        match_status = ""

        if detail_url:
            address, accommodation, match_status = detail_data(detail_url)

        if home.upper().startswith("SCP 1"):
            if not address:
                address = HOME_LOCATION

            if not accommodation:
                accommodation = HOME_ACCOMMODATION

        season_start = year if month >= 7 else year - 1
        uid = create_uid(season_start, home, away, kind)

        matches.append(
            {
                "start": start,
                "home": home,
                "away": away,
                "kind": kind,
                "result": result,
                "match_status": match_status,
                "url": detail_url or PROGRAM_URL,
                "address": address,
                "accommodation": accommodation,
                "uid": uid,
            }
        )

    if not matches:
        raise RuntimeError(
            "Geen wedstrijden gevonden. Het bestaande kalenderbestand blijft behouden."
        )

    return matches


def build_calendar(matches: list[dict]) -> str:
    """Zet wedstrijden, uitslagen en statussen om naar iCalendar."""
    timestamp = datetime.now(TZ).strftime("%Y%m%dT000000Z")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//VV SCP//Wedstrijdagenda SCP 1//NL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:VV SCP 1",
        "X-WR-TIMEZONE:Europe/Amsterdam",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]

    for match in sorted(matches, key=lambda item: item["start"]):
        start = match["start"]
        end = start + timedelta(hours=2)
        match_status = match["match_status"]
        title = f"{match['home']} - {match['away']}"

        if match_status:
            title = f"{match_status.upper()}: {title}"
        elif match["result"]:
            title += f" ({match['result']})"

        description = [match["kind"]]

        if match_status:
            description.insert(0, f"Status: {match_status}")
        elif match["result"]:
            description.append(f"Eindstand: {match['result']}")

        if match["accommodation"]:
            description.append(f"Accommodatie: {match['accommodation']}")

        description.append(
            "Controleer bij last-minute wijzigingen altijd Voetbal.nl."
        )

        ical_status = "CANCELLED" if match_status in {"afgelast", "vervallen"} else "CONFIRMED"

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{match['uid']}",
                f"DTSTAMP:{timestamp}",
                (
                    "DTSTART;TZID=Europe/Amsterdam:"
                    f"{start.strftime('%Y%m%dT%H%M%S')}"
                ),
                (
                    "DTEND;TZID=Europe/Amsterdam:"
                    f"{end.strftime('%Y%m%dT%H%M%S')}"
                ),
                f"SUMMARY:{escape(title)}",
                f"DESCRIPTION:{escape(chr(10).join(description))}",
                f"URL:{match['url']}",
                f"STATUS:{ical_status}",
                "SEQUENCE:0",
            ]
        )

        if match["address"]:
            lines.append(f"LOCATION:{escape(match['address'])}")

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def main() -> None:
    """Haal wedstrijden op en schrijf het kalenderbestand."""
    matches = fetch_matches()
    calendar = build_calendar(matches)
    OUTPUT.write_text(calendar, encoding="utf-8", newline="")
    print(f"{len(matches)} wedstrijden geschreven naar {OUTPUT}")


if __name__ == "__main__":
    main()
