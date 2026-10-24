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
TIMEZONE = ZoneInfo("Europe/Amsterdam")

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

MATCH_TYPES = {
    "co": "Competitie",
    "be": "Beker",
    "vr": "Vriendschappelijk",
}

HOME_ADDRESS = "Hosterd 65, 6655 AE Puiflijk"
HOME_ACCOMMODATION = "Sportpark Schravenhof"

HEADERS = {
    "User-Agent": (
        "VV-SCP-wedstrijdagenda/1.0 "
        "(+https://github.com/VV-SCP/wedstrijdagenda)"
    )
}


def clean_team_name(value: str) -> str:
    """Verwijder extra spaties en de toevoeging '(zon)'."""

    value = " ".join(value.split())

    return re.sub(
        r"\s*\(zon\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )


def escape_ical_text(value: str) -> str:
    """Maak tekst geschikt voor een iCalendar-bestand."""

    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_ical_line(line: str, limit: int = 73) -> str:
    """Breek lange regels af volgens de iCalendar-standaard."""

    parts = []

    while len(line.encode("utf-8")) > limit:
        cut = min(limit, len(line))

        while len(line[:cut].encode("utf-8")) > limit:
            cut -= 1

        parts.append(line[:cut])
        line = " " + line[cut:]

    parts.append(line)

    return "\r\n".join(parts)


def get_season_start_year() -> int:
    """Bepaal het beginjaar van het actuele voetbalseizoen."""

    now = datetime.now(TIMEZONE)

    if now.month >= 7:
        return now.year

    return now.year - 1


def get_match_year(month: int) -> int:
    """Bepaal het juiste kalenderjaar bij een wedstrijdmaand."""

    season_start_year = get_season_start_year()

    if month >= 7:
        return season_start_year

    return season_start_year + 1


def normalise_postcode(postcode: str) -> str:
    """Zet een postcode om naar de vorm 1234 AB."""

    postcode = postcode.upper().replace(" ", "")

    if len(postcode) == 6:
        return f"{postcode[:4]} {postcode[4:]}"

    return postcode


def normalise_result(value: str) -> str:
    """
    Verwerk alleen een echte numerieke uitslag.

    Voorbeelden:
    3 - 1 wordt 3 - 1
    3-1 wordt 3 - 1
    - wordt leeg
    """

    value = " ".join(value.split())

    result_match = re.fullmatch(
        r"(\d+)\s*-\s*(\d+)",
        value,
    )

    if not result_match:
        return ""

    home_score = result_match.group(1)
    away_score = result_match.group(2)

    return f"{home_score} - {away_score}"


def get_detail_location(url: str) -> tuple[str, str]:
    """
    Haal indien mogelijk het adres en de accommodatie op
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

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    page_text = soup.get_text(
        " ",
        strip=True,
    )

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
        return "", ""

    accommodation = " ".join(
        location_match.group(1).split()
    )

    street = " ".join(
        location_match.group(2).split()
    )

    postcode = normalise_postcode(
        location_match.group(3)
    )

    place = " ".join(
        location_match.group(4).split()
    ).title()

    address = f"{street}, {postcode} {place}"

    return address, accommodation


def create_match_uid(
    home_team: str,
    away_team: str,
    match_type: str,
) -> str:
    """
    Maak een vaste identificatie voor de wedstrijd.

    Datum en tijd maken bewust geen onderdeel uit van de
    identificatie. Daardoor blijft bij een verplaatsing
    dezelfde agenda-afspraak bestaan.
    """

    season_start_year = get_season_start_year()

    raw_identifier = (
        f"{season_start_year}|"
        f"{home_team}|"
        f"{away_team}|"
        f"{match_type}"
    )

    match_hash = hashlib.sha256(
        raw_identifier.encode("utf-8")
    ).hexdigest()[:20]

    return f"{match_hash}@vv-scp.nl"


def find_program_table(
    soup: BeautifulSoup,
):
    """Zoek de programmatabel op de SCP-pagina."""

    for candidate in soup.find_all("table"):
        table_headers = [
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

        if required_headers.issubset(
            set(table_headers)
        ):
            return candidate

    return None


def get_match_detail_url(row) -> str:
    """Zoek de detailpagina van een wedstrijd."""

    for link in row.find_all(
        "a",
        href=True,
    ):
        href = link.get(
            "href",
            "",
        )

        if "wedstrijd" in href.lower():
            return urljoin(
                PROGRAM_URL,
                href,
            )

    return ""


def fetch_matches() -> list"""Lees alle wedstrijden en uitslagen van SCP 1."""

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

    program_table = find_program_table(
        soup
    )

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
            r"(\d{1,2})\d{2}",
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

        year = get_match_year(
            month
        )

        hour = int(
            time_match.group(1)
        )

        minute = int(
            time_match.group(2)
        )

        start_time = datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=TIMEZONE,
        )

        home_team = clean_team_name(
            values[2]
        )

        away_team = clean_team_name(
            values[3]
        )

        match_type_code = values[4].lower()

        match_type = MATCH_TYPES.get(
            match_type_code,
            values[4] or "Wedstrijd",
        )

        result = normalise_result(
            values[5]
        )

        detail_url = get_match_detail_url(
            row
        )

        address = ""
        accommodation = ""

        if detail_url:
            address, accommodation = (
                get_detail_location(
                    detail_url
                )
            )

        if home_team.upper().startswith("SCP 1"):
            if not address:
                address = HOME_ADDRESS

            if not accommodation:
                accommodation = (
                    HOME_ACCOMMODATION
                )

        uid = create_match_uid(
            home_team,
            away_team,
            match_type,
        )

        matches.append(
            {
                "start": start_time,
                "home": home_team,
                "away": away_team,
                "match_type": match_type,
                "result": result,
                "detail_url": (
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


def build_event_title(
    match: dict,
) -> str:
    """Maak de titel van de agenda-afspraak."""

    title = (
        f"{match['home']} - "
        f"{match['away']}"
    )

    if match["result"]:
        title += (
            f" ({match['result']})"
        )

    return title


def build_event_description(
    match: dict,
) -> str:
    """Maak de beschrijving van de agenda-afspraak."""

    description_parts = [
        match["match_type"],
    ]

    if match["result"]:
        description_parts.append(
            f"Eindstand: {match['result']}"
        )

    if match["accommodation"]:
        description_parts.append(
            f"Accommodatie: "
            f"{match['accommodation']}"
        )

    description_parts.append(
        "Controleer bij last-minute wijzigingen "
        "altijd Voetbal.nl."
    )

    return "\n".join(
        description_parts
    )


def build_calendar(
    matches: list[dict],
) -> str:
    """
    Zet de wedstrijden en uitslagen om
    naar een iCalendar-bestand.
    """

    timestamp = datetime.now(
        TIMEZONE
    ).strftime(
        "%Y%m%dT000000Z"
    )

    calendar_lines = [
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
        start_time = match["start"]

        end_time = (
            start_time
            + timedelta(hours=2)
        )

        event_title = build_event_title(
            match
        )

        event_description = (
            build_event_description(
                match
            )
        )

        calendar_lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{match['uid']}",
                f"DTSTAMP:{timestamp}",
                (
                    "DTSTART;"
                    "TZID=Europe/Amsterdam:"
                    f"{start_time.strftime('%Y%m%dT%H%M%S')}"
                ),
                (
                    "DTEND;"
                    "TZID=Europe/Amsterdam:"
                    f"{end_time.strftime('%Y%m%dT%H%M%S')}"
                ),
                (
                    "SUMMARY:"
                    f"{escape_ical_text(event_title)}"
                ),
                (
                    "DESCRIPTION:"
                    f"{escape_ical_text(event_description)}"
                ),
                (
                    "URL:"
                    f"{match['detail_url']}"
                ),
                "STATUS:CONFIRMED",
                "SEQUENCE:0",
            ]
        )

        if match["address"]:
            calendar_lines.append(
                "LOCATION:"
                f"{escape_ical_text(match['address'])}"
            )

        calendar_lines.append(
            "END:VEVENT"
        )

    calendar_lines.append(
        "END:VCALENDAR"
    )

    folded_lines = [
        fold_ical_line(line)
        for line in calendar_lines
    ]

    return (
        "\r\n".join(folded_lines)
        + "\r\n"
    )


def main() -> None:
    """Haal de wedstrijden op en schrijf de agenda."""

    matches = fetch_matches()

    calendar_content = build_calendar(
        matches
    )

    OUTPUT.write_text(
        calendar_content,
        encoding="utf-8",
        newline="",
    )

    print(
        f"{len(matches)} wedstrijden "
        f"geschreven naar {OUTPUT}"
    )


if __name__ == "__main__":
    main()
