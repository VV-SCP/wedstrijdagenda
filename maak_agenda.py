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

    return re.sub(
        r"\s*\(zon\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )


def escape(value: str) -> str:
    """Maak tekst geschikt voor iCalendar."""

    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line: str, limit: int = 73) -> str:
    """Breek lange iCalendar-regels af."""

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

    season_start = (
        now.year
        if now.month >= 7
        else now.year - 1
    )

    if month >= 7:
        return season_start

    return season_start + 1


def normalise_postcode(postcode: str) -> str:
    """Zet een postcode om naar de vorm 1234 AB."""

    postcode = postcode.upper().replace(" ", "")

    if len(postcode) == 6:
        return f"{postcode[:4]} {postcode[4:]}"

    return postcode


def normalise_result(value: str) -> str:
    """
    Geef alleen een echte numerieke uitslag terug.

    Voorbeelden:
    3 - 1 wordt 3 - 1
    3-1 wordt 3 - 1
    - wordt leeg
    """

    value = " ".join(value.split())

    match = re.fullmatch(
        r"(\d+)\s*-\s*(\d+)",
        value,
    )

    if not match:
        return ""

    return (
        f"{match.group(1)} - "
        f"{match.group(2)}"
    )


def detail_location(
    url: str,
) -> tuple[str, str]:
    """
    Haal indien mogelijk het adres en de accommodatie
    van de detailpagina van de wedstrijd.
    """

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20,
        )
        response.raise_for_status()

    except requests.RequestException:
        return "", ""

    text = BeautifulSoup(
        response.text,
        "html.parser",
    ).get_text(" ", strip=True)

    match = re.search(
        r"Accommodatie\s+(.+?)\s+"
        r"([A-Za-zÀ-ÿ' .-]+\s+\d+[A-Za-z]?)\s+"
        r"(\d{4}\s?[A-Z]{2})\s+"
        r"([A-Za-zÀ-ÿ' .-]+?)"
        r"(?:\s+tel\.|\s+Toon routebeschrijving)",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return "", ""

    accommodation = " ".join(
        match.group(1).split()
    )

    street = " ".join(
        match.group(2).split()
    )

    postcode = normalise_postcode(
        match.group(3)
    )

    place = " ".join(
        match.group(4).split()
    ).title()

    address = (
        f"{street}, "
        f"{postcode} "
        f"{place}"
    )

    return address, accommodation


def create_uid(
    season_start: int,
    home: str,
    away: str,
    kind: str,
) -> str:
    """
    Maak een vaste wedstrijd-ID.

    De datum en tijd zitten bewust niet in de ID.
    Daardoor blijft bij een verplaatsing dezelfde
    agenda-afspraak bestaan.
    """

    raw_id = (
        f"{season_start}|"
        f"{home}|"
        f"{away}|"
        f"{kind}"
    )

    match_hash = hashlib.sha256(
        raw_id.encode("utf-8")
    ).hexdigest()[:20]

    return f"{match_hash}@vv-scp.nl"


def fetch_matches() -> list[dict]:
    """Lees het programma en de uitslagen van SCP 1."""

    response = requests.get(
        PROGRAM_URL,
        headers=HEADERS,
        timeout=30,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    program_table = None

    for candidate in soup.find_all("table"):
        headers = [
            heading.get_text(
                " ",
                strip=True,
            ).lower()
            for heading in candidate.find_all("th")
        ]

        required_headers = {
            "datum",
            "tijd",
            "thuis",
            "uit",
            "uitslag",
        }

        if required_headers.issubset(set(headers)):
            program_table = candidate
            break

    if program_table is None:
        raise RuntimeError(
            "Geen programmatabel gevonden. "
            "De bronpagina is mogelijk gewijzigd."
        )

    matches = []

    for row in program_table.find_all("tr"):
        cells = row.find_all(
            ["td", "th"]
        )

        if len(cells) < 6:
            continue

        if cells[0].name == "th":
            continue

        values = [
            cell.get_text(
                " ",
                strip=True,
            )
            for cell in cells
        ]

        date_match = re.fullmatch(
            r"(\d{1,2})\s+([A-Za-z]{3})",
            values[0].lower(),
        )

        time_match = re.fullmatch(
            r"(\d{1,2})[.:](\d{2})",
            values[1],
        )

            if not date_match or not time_match:
            continue

        day = int(
            date_match.group(1)
        )

        month_name = date_match.group(2)

        month = MONTHS.get(
            month_name
        )

        if not month:
            continue

        year = season_year(
            month
        )

        start = datetime(
            year,
            month,
            day,
            int(time_match.group(1)),
            int(time_match.group(2)),
            tzinfo=TZ,
        )

        home = clean_team(
            values[2]
        )

        away = clean_team(
            values[3]
        )

        kind_code = values[4].lower()

        kind = TYPE_NAMES.get(
            kind_code,
            values[4] or "Wedstrijd",
        )

        result = normalise_result(
            values[5]
        )

        detail_url = ""

        for link in row.find_all(
            "a",
            href=True,
        ):
            href = link.get(
                "href",
                "",
            )

            if "wedstrijd" in href.lower():
                detail_url = urljoin(
                    PROGRAM_URL,
                    href,
                )
                break

        address = ""
        accommodation = ""

        if detail_url:
            address, accommodation = (
                detail_location(
                    detail_url
                )
            )

        if home.upper().startswith("SCP 1"):
            if not address:
                address = HOME_LOCATION

            if not accommodation:
                accommodation = (
                    HOME_ACCOMMODATION
                )

        season_start = (
            year
            if month >= 7
            else year - 1
        )

        uid = create_uid(
            season_start,
            home,
            away,
            kind,
        )

        matches.append(
            {
                "start": start,
                "home": home,
                "away": away,
                "kind": kind,
                "result": result,
                "url": (
                    detail_url
                    or PROGRAM_URL
                ),
                "address": address,
                "accommodation": accommodation,
                "uid": uid,
            }
        )

    if not matches:
        raise RuntimeError(
            "Geen wedstrijden gevonden. "
            "Het bestaande kalenderbestand "
            "blijft behouden."
        )

    return matches


def build_calendar(
    matches: list[dict],
) -> str:
    """
    Zet de wedstrijden en bekende uitslagen
    om naar een iCalendar-bestand.
    """

    timestamp = datetime.now(
        TZ
    ).strftime(
        "%Y%m%dT000000Z"
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        (
            "PRODID:-//VV SCP//"
            "Wedstrijdagenda SCP 1//NL"
        ),
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:VV SCP 1",
        "X-WR-TIMEZONE:Europe/Amsterdam",
        (
            "REFRESH-INTERVAL;"
            "VALUE=DURATION:PT1H"
        ),
        "X-PUBLISHED-TTL:PT1H",
    ]

    sorted_matches = sorted(
        matches,
        key=lambda item: item["start"],
    )

    for match in sorted_matches:
        start = match["start"]

        end = start + timedelta(
            hours=2
        )

        title = (
            f"{match['home']} - "
            f"{match['away']}"
        )

        if match["result"]:
            title += (
                f" ({match['result']})"
            )

        description = [
            match["kind"],
        ]

        if match["result"]:
            description.append(
                f"Eindstand: "
                f"{match['result']}"
            )

        if match["accommodation"]:
            description.append(
                f"Accommodatie: "
                f"{match['accommodation']}"
            )

        description.append(
            "Controleer bij last-minute "
            "wijzigingen altijd Voetbal.nl."
        )

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{match['uid']}",
                f"DTSTAMP:{timestamp}",
                (
                    "DTSTART;"
                    "TZID=Europe/Amsterdam:"
                    f"{start.strftime(
                        '%Y%m%dT%H%M%S'
                    )}"
                ),
                (
                    "DTEND;"
                    "TZID=Europe/Amsterdam:"
                    f"{end.strftime(
                        '%Y%m%dT%H%M%S'
                    )}"
                ),
                (
                    "SUMMARY:"
                    f"{escape(title)}"
                ),
                (
                    "DESCRIPTION:"
                    f"{escape(
                        chr(10).join(
                            description
                        )
                    )}"
                ),
                f"URL:{match['url']}",
                "STATUS:CONFIRMED",
                "SEQUENCE:0",
            ]
        )

        if match["address"]:
            lines.append(
                "LOCATION:"
                f"{escape(
                    match['address']
                )}"
            )

        lines.append(
            "END:VEVENT"
        )

    lines.append(
        "END:VCALENDAR"
    )

    return (
        "\r\n".join(
            fold(line)
            for line in lines
        )
        + "\r\n"
    )


def main() -> None:
    """Maak en bewaar de agenda."""

    matches = fetch_matches()

    calendar = build_calendar(
        matches
    )

    OUTPUT.write_text(
        calendar,
        encoding="utf-8",
        newline="",
    )

    print(
        f"{len(matches)} wedstrijden "
        f"geschreven naar {OUTPUT}"
    )


if __name__ == "__main__":
    main()
