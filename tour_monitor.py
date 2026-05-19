import os
import re
import json
import requests
import base64
from datetime import datetime, timezone, timedelta

# ── Konfigurace ──────────────────────────────────────────────────────────────

EMAIL       = os.environ["SERVICE_EMAIL"]
HESLO       = os.environ["SERVICE_PASSWORD"]
TG_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TG_CHAT     = os.environ["TELEGRAM_CHAT_ID"]
GH_TOKEN    = os.environ["GITHUB_TOKEN"]
GH_REPO     = os.environ["GITHUB_REPOSITORY"]

ORG_ID      = os.environ["SERVICE_ORG_ID"]
FED_ID      = os.environ["SERVICE_FED_ID"]
CLUB_ID     = os.environ["SERVICE_CLUB_ID"]
LOCATION_ID = os.environ["SERVICE_LOCATION_ID"]
BASE_URL    = os.environ["SERVICE_API_URL"] + "/foys/api"
EVENT_URL   = os.environ["SERVICE_EVENT_URL"]
STATE_FILE  = "tour_state.json"
DNI_DOPREDU = 60

DNY = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]

# ── Filtr názvů ──────────────────────────────────────────────────────────────

VYLOUCENA_SLOVA = ["MORNING", "LUNCH", "LADIES", "B-C", "B–C", "C-B", "C–B"]

LEVEL_PATTERN   = re.compile(r'\b([A-E])(?:-([A-E]))?\b')
ZAJIMAVE_UROVNE = {"C", "D"}


def zajima_me(nazev):
    n = nazev.upper()
    if any(s in n for s in VYLOUCENA_SLOVA):
        return False
    for m in LEVEL_PATTERN.finditer(n):
        urovne = {m.group(1)}
        if m.group(2):
            urovne.add(m.group(2))
        if urovne & ZAJIMAVE_UROVNE:
            return True
    return False


def formatovat_datum(zacatek):
    datum_raw = zacatek[:10] if zacatek else "?"
    cas       = zacatek[11:16] if len(zacatek) > 10 else "?"
    try:
        datum_dt  = datetime.strptime(datum_raw, "%Y-%m-%d")
        den_nazev = DNY[datum_dt.weekday()]
        datum     = f"{den_nazev} {datum_dt.strftime('%d.%m.%Y')}"
    except:
        datum = datum_raw
    return datum, cas

# ─────────────────────────────────────────────────────────────────────────────

session = requests.Session()
session.headers.update({
    "X-OrganisationId": ORG_ID,
    "X-FederationID":   FED_ID,
    "Accept":           "application/json",
})


def pripojit():
    r = session.post(
        f"{BASE_URL}/v1/token",
        headers={
            "accept":           "application/json",
            "content-type":     "application/x-www-form-urlencoded",
            "x-federationid":   FED_ID,
            "x-organisationid": ORG_ID,
        },
        data={
            "grant_type": "password",
            "username":   EMAIL,
            "password":   HESLO,
            "clubId":     CLUB_ID,
        },
        timeout=15,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    session.headers["Authorization"] = f"Bearer {token}"
    print("Připojení OK")


def ziskat_udalosti():
    start    = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
    udalosti = []
    skip     = 0
    batch    = 50

    while True:
        r = session.get(f"{BASE_URL}/v2/pub/public-calendar", params={
            "organisationId":      ORG_ID,
            "locationIds[]":       LOCATION_ID,
            "start":               start,
            "calendarItemTypes[]": ["Training", "Event"],
            "skipCount":           skip,
            "maxResultCount":      batch,
        })
        r.raise_for_status()
        data  = r.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        if not items:
            break
        udalosti.extend(items)
        if len(items) < batch:
            break
        skip += batch

    return udalosti


def poslat_zpravu(zprava):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": zprava, "parse_mode": "HTML"},
    )


def nacist_stav():
    gh_api = f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"
    r = requests.get(gh_api, headers={"Authorization": f"Bearer {GH_TOKEN}"})
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    data  = r.json()
    obsah = json.loads(base64.b64decode(data["content"]).decode())
    return obsah, data["sha"]


def ulozit_stav(stav, sha):
    gh_api = f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"
    if sha is None:
        r = requests.get(gh_api, headers={"Authorization": f"Bearer {GH_TOKEN}"})
        if r.status_code == 200:
            sha = r.json()["sha"]
    payload = {
        "message": "update state",
        "content": base64.b64encode(json.dumps(stav, indent=2).encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(gh_api, json=payload,
                     headers={"Authorization": f"Bearer {GH_TOKEN}"})
    if r.status_code == 409:
        r2 = requests.get(gh_api, headers={"Authorization": f"Bearer {GH_TOKEN}"})
        payload["sha"] = r2.json()["sha"]
        r = requests.put(gh_api, json=payload,
                         headers={"Authorization": f"Bearer {GH_TOKEN}"})
    r.raise_for_status()


def spustit():
    print(f"Kontrola: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    pripojit()

    print("Stahuji kalendář...")
    try:
        udalosti = ziskat_udalosti()
    except Exception as e:
        print(f"Chyba při stahování kalendáře: {e}")
        return

    ted        = datetime.now(timezone.utc)
    konec_okna = ted + timedelta(days=DNI_DOPREDU)

    predchozi, sha = nacist_stav()
    novy_stav  = {}
    preskocene = 0

    for e in udalosti:
        event_id = e.get("id") or e.get("guid")
        nazev    = e.get("title", "")
        zacatek  = e.get("startAt") or e.get("start", "")
        going    = e.get("countGoing") or 0
        max_cap  = e.get("maxAmountOfAttendances") or 0
        zrusen   = e.get("wasCancelled", False)

        if not event_id or zrusen:
            continue

        if not zajima_me(nazev):
            preskocene += 1
            continue

        if zacatek:
            try:
                start_dt = datetime.fromisoformat(zacatek).replace(tzinfo=timezone.utc)
                if not (ted <= start_dt <= konec_okna):
                    continue
            except ValueError:
                pass

        if max_cap == 0:
            continue

        volno      = going < max_cap
        datum, cas = formatovat_datum(zacatek)
        odkaz      = f"{EVENT_URL}?id={event_id}"

        print(f"  {nazev}: {going}/{max_cap} {'✅ volno' if volno else '🔴 plný'}")

        if event_id not in predchozi:
            stav   = f"{going}/{max_cap} – {'volno ✅' if volno else 'plný 🔴'}"
            zprava = (
                f"🆕 <b>Nová událost!</b>\n\n"
                f"<b>{nazev}</b>\n"
                f"📅 {datum}  ⏰ {cas}\n"
                f"👥 Obsazenost: {stav}\n\n"
                f"👉 {odkaz}"
            )
            poslat_zpravu(zprava)
            print(f"  → Nová událost, notifikace odeslána!")

        byl_plny = predchozi.get(event_id, {}).get("byl_plny", False)
        if volno and byl_plny:
            zprava = (
                f"🎾 <b>Uvolnilo se místo na turnaji!</b>\n\n"
                f"<b>{nazev}</b>\n"
                f"📅 {datum}  ⏰ {cas}\n"
                f"👥 Obsazenost: {going}/{max_cap}\n\n"
                f"👉 {odkaz}"
            )
            poslat_zpravu(zprava)
            print(f"  → Notifikace odeslána!")

        novy_stav[event_id] = {
            "nazev":    nazev,
            "byl_plny": not volno,
            "going":    going,
            "max_cap":  max_cap,
            "start":    zacatek,
        }

    print(f"Přeskočeno {preskocene} nezajímavých položek")
    ulozit_stav(novy_stav, sha)
    print(f"Hotovo. Sledováno {len(novy_stav)} položek.")


if __name__ == "__main__":
    spustit()
