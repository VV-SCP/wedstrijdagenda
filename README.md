# Wedstrijdagenda VV SCP

Automatisch bijgewerkte iCalendar-feed voor **SCP 1**.

## Openbare agenda

`https://vv-scp.github.io/wedstrijdagenda/scp-1.ics`

## Werking

- `scripts/maak_agenda.py` leest het openbare programma van SCP 1 op vv-scp.nl.
- `.github/workflows/agenda-bijwerken.yml` draait elk uur en kan ook handmatig worden gestart.
- Alleen wanneer `scp-1.ics` verandert, wordt een nieuwe versie opgeslagen.
- Wedstrijden houden een vaste UID, zodat datum- of tijdwijzigingen geen dubbele afspraak horen te veroorzaken.

## Beheer

Controleer in GitHub onder **Actions** of de workflow groen is. Bij een wijziging van de bronwebsite kan de parser onderhoud nodig hebben.

## Belangrijk

De agenda is een extra service. Raadpleeg bij last-minute wijzigingen of afgelastingen altijd Voetbal.nl of de officiële clubcommunicatie.
