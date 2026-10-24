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
TEAM = "SCP 1"
TZ = ZoneInfo("Europe/Amsterdam")
MONTHS = {"jan":1,"feb":2,"mrt":3,"apr":4,"mei":5,"jun":6,"jul":7,"aug":8,"sep":9,"okt":10,"nov":11,"dec":12}
TYPE_NAMES = {"co":"Competitie", "be":"Beker", "vr":"Vriendschappelijk"}
HOME_LOCATION = "Hosterd 65, 6655 AE Puiflijk"

HEADERS = {"User-Agent": "VV-SCP-wedstrijdagenda/1.0 (+https://github.com/VV-SCP/wedstrijdagenda)"}


def clean_team(value: str) -> str:
    return re.sub(r"\s*\(zon\)\s*$", "", " ".join(value.split()), flags=re.I)


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str, limit: int = 73) -> str:
    # Houd regels ruim binnen de iCalendar-limiet van 75 octetten.
    parts=[]
    while len(line.encode("utf-8")) > limit:
        cut=min(limit, len(line))
        while len(line[:cut].encode("utf-8")) > limit:
            cut-=1
        parts.append(line[:cut])
        line=" " + line[cut:]
    parts.append(line)
    return "\r\n".join(parts)


def season_year(month: int, page_text: str) -> int:
    # Gebruik een zichtbaar seizoen, bijvoorbeeld 2026/2027. Anders: huidig voetbalseizoen.
    m=re.search(r"(20\d{2})\s*[/\-]\s*(20\d{2})", page_text)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
    else:
        now=datetime.now(TZ)
        start=now.year if now.month >= 7 else now.year-1
        end=start+1
    return start if month >= 7 else end


def detail_location(url: str) -> tuple[str, str]:
    """Geeft (adres, extra omschrijving) terug; leeg als de detailpagina niets bruikbaars bevat."""
    try:
        text=BeautifulSoup(requests.get(url, headers=HEADERS, timeout=20).text, "html.parser").get_text(" ", strip=True)
    except requests.RequestException:
        return "", ""
    # Veel VoetbalAssist-detailpagina's tonen: Accommodatie NAAM STRAAT NR POSTCODE PLAATS tel.
    m=re.search(r"Accommodatie\s+(.+?)\s+([A-ZÀ-ÿ][A-Za-zÀ-ÿ' .-]+\s+\d+[A-Za-z]?)\s+(\d{4}\s?[A-Z]{2})\s+([A-ZÀ-Ÿ][A-ZÀ-Ÿ '\-]+?)(?:\s+tel\.|\s+Toon routebeschrijving)", text, re.I)
    if not m:
        return "", ""
    sportpark, street, postcode, place=(" ".join(x.split()) for x in m.groups())
    address=f"{street}, {postcode.upper()}, {place.title()}"
    return address, f"Accommodatie: {sportpark}"


def fetch_matches() -> list[dict]:
    response=requests.get(PROGRAM_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup=BeautifulSoup(response.text, "html.parser")
    page_text=soup.get_text(" ", strip=True)
    table=None
    for candidate in soup.find_all("table"):
        headers=[th.get_text(" ", strip=True).lower() for th in candidate.find_all("th")]
        if {"datum","tijd","thuis","uit"}.issubset(set(headers)):
            table=candidate
            break
    if table is None:
        raise RuntimeError("Geen programmatabel gevonden; bronpagina is mogelijk gewijzigd.")

    rows=[]
    for tr in table.find_all("tr"):
        cells=tr.find_all(["td","th"])
        if len(cells) < 5 or cells[0].name == "th":
            continue
        values=[c.get_text(" ", strip=True) for c in cells]
        dm=re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3})", values[0].lower())
        tm=re.fullmatch(r"(\d{1,2})[.:](\d{2})", values[1])
        if not dm or not tm:
            continue
        day=int(dm.group(1)); month=MONTHS.get(dm.group(2))
        if not month:
            continue
        year=season_year(month, page_text)
        start=datetime(year, month, day, int(tm.group(1)), int(tm.group(2)), tzinfo=TZ)
        home, away=clean_team(values[2]), clean_team(values[3])
        kind=TYPE_NAMES.get(values[4].lower(), values[4] or "Wedstrijd")
        result=values[5] if len(values) > 5 else ""
        link=next((urljoin(PROGRAM_URL, a.get("href")) for a in tr.find_all("a", href=True) if "wedstrijd" in a.get("href", "").lower()), "")
        address, accommodation=(detail_location(link) if link else ("", ""))
        if not address and home.upper().startswith("SCP 1"):
            address=HOME_LOCATION
            accommodation="Accommodatie: Sportpark Schravenhof"
        season_start = year if month >= 7 else year - 1
        raw_id=f"{season_start}|{home}|{away}|{kind}"
        uid=hashlib.sha256(raw_id.encode()).hexdigest()[:20] + "@vv-scp.nl"
        rows.append({"start":start,"home":home,"away":away,"kind":kind,"result":result,"url":link or PROGRAM_URL,"address":address,"accommodation":accommodation,"uid":uid})
    if not rows:
        raise RuntimeError("Geen wedstrijden gevonden; bestaand kalenderbestand blijft behouden.")
    return rows


def build_calendar(matches: list[dict]) -> str:
    # Dagstempel verandert maximaal eenmaal per dag; zo ontstaan niet ieder uur commits.
    stamp=datetime.now(TZ).strftime("%Y%m%dT000000Z")
    lines=["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//VV SCP//Wedstrijdagenda SCP 1//NL","CALSCALE:GREGORIAN","METHOD:PUBLISH","X-WR-CALNAME:VV SCP 1","X-WR-TIMEZONE:Europe/Amsterdam","REFRESH-INTERVAL;VALUE=DURATION:PT1H","X-PUBLISHED-TTL:PT1H"]
    for m in sorted(matches, key=lambda x:x["start"]):
        end=m["start"]+timedelta(hours=2)
        description=[m["kind"]]
        if m["result"] and m["result"] != "-": description.append(f"Uitslag: {m['result']}")
        if m["accommodation"]: description.append(m["accommodation"])
        description.append("Controleer bij last-minute wijzigingen altijd Voetbal.nl.")
        lines += [
            "BEGIN:VEVENT", f"UID:{m['uid']}", f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Europe/Amsterdam:{m['start'].strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Europe/Amsterdam:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{escape(m['home'] + ' - ' + m['away'])}",
            f"DESCRIPTION:{escape(chr(10).join(description))}",
            f"URL:{m['url']}", "STATUS:CONFIRMED", "SEQUENCE:0"
        ]
        if m["address"]:
            lines.append(f"LOCATION:{escape(m['address'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


def main() -> None:
    content=build_calendar(fetch_matches())
    OUTPUT.write_text(content, encoding="utf-8", newline="")
    print(f"{len(content)} tekens geschreven naar {OUTPUT}")

if __name__ == "__main__":
    main()
