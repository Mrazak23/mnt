import os
import re
import json
import base64
import requests
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

# Konfigurace
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]
GH_REPO  = os.environ["GITHUB_REPOSITORY"]

API_URL    = os.environ["SERVICE_API_URL"]     # https://api.padelos.co
COMPANY_ID = os.environ["SERVICE_COMPANY_ID"]  # 217
CLUB_ID    = os.environ["SERVICE_CLUB_ID"]     # 216927
DOMAIN     = os.environ["SERVICE_DOMAIN"]      # PADELOSCO

# Odkaz na detail konkretniho turnaje (id + nazev se doplni za behu)
DETAIL_URL = f"https://player.padelos.co/club/{CLUB_ID}/tournaments/tournament-detail"

STATE_FILE  = "tour_state.json"
DNI_DOPREDU = 60

DNY = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]

# Filtr nazvu
#  - uroven C/D zapsana pismenem (puvodni chovani)
#  - ratingove americana: Beginners / Intermediates
#  - vylouceno (ma prednost): rana/obedy/ladies, smisene B-C/C-B,
#    a dale Starters a High Intermediates (i v kombinaci typu
#    "Starters & Beginners" nebo "Intermediates + High Intermediates")
VYLOUCENA_SLOVA = ["MORNING", "LUNCH", "LADIES", "B-C", "B\u2013C", "C-B", "C\u2013B",
                   "STARTER", "HIGH INTERMEDIATE"]
LEVEL_PATTERN   = re.compile(r'\b([A-E])(?:-([A-E]))?\b')
ZAJIMAVE_UROVNE = {"C", "D"}
ZAJIMAVA_SLOVA  = ["BEGINNER", "INTERMEDIATE"]


def zajima_me(nazev):
    n = nazev.upper()
    # vyloucene slovo kdekoli v nazvu -> ihned pryc (ma prednost pred zajimavymi)
    if any(s in n for s in VYLOUCENA_SLOVA):
        return False
    # uroven C/D zapsana pismenem
    for m in LEVEL_PATTERN.finditer(n):
        urovne = {m.group(1)}
        if m.group(2):
            urovne.add(m.group(2))
        if urovne & ZAJIMAVE_UROVNE:
            return True
    # ratingove americana (Starters a High Intermediates uz jsou vyloucene vyse)
    if any(s in n for s in ZAJIMAVA_SLOVA):
        return True
    return False


def hlavicky():
    return {
        "accept": "application/json, text/plain, */*",
        "origin": "https://player.padelos.co",
        "referer": "https://player.padelos.co/",
        "x-clubos-channel": "CLUBOS-WEB",
        "x-clubos-company": COMPANY_ID,
        "x-clubos-club-info": CLUB_ID,
        "x-clubos-domain": DOMAIN,
    }


def ziskat_turnaje():
    r = requests.get(
        f"{API_URL}/customers/tournament/listing",
        headers=hlavicky(),
        params={
            "clubId": "",
            "limit": 200,
            "sport": "Padel",
            "format": "",
            "category": "",
            "hideFilters": "registration_closed",
            "page": "",
        },
        timeout=20,
    )
    if r.status_code != 200:
        print(f"Chyba API: {r.status_code} {r.text[:150]}")
        return []
    data = r.json().get("data", {})
    return data.get("rows", []) if isinstance(data, dict) else []


def formatovat(t):
    datum_raw = t.get("startDate", "")
    cas_raw   = t.get("startTime", "")
    try:
        d = datetime.strptime(datum_raw, "%Y-%m-%d")
        datum = f"{DNY[d.weekday()]} {d.strftime('%d.%m.%Y')}"
    except ValueError:
        datum = datum_raw or "?"
    cas = cas_raw[:5] if cas_raw else "?"
    return datum, cas


def poslat_zpravu(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=10,
    )
    print(f"Zprava odeslana: {r.status_code}")


def nacist_stav():
    r = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}",
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if r.status_code == 404:
        # uplne prvni beh -> seed bez notifikaci
        return {"turnaje": {}, "prvni_beh": True}, None
    data  = r.json()
    obsah = json.loads(base64.b64decode(data["content"]).decode())
    if isinstance(obsah.get("turnaje"), dict):
        # novy format {tid: zbyva}
        return {"turnaje": obsah["turnaje"], "prvni_beh": False}, data["sha"]
    # stary format {"videno": [ids]} -> migrace, tento beh jen nastype stav, neflooduje
    return {"turnaje": {}, "prvni_beh": True}, data["sha"]


def ulozit_stav(stav, sha, pokus=0):
    payload = {"message": "update tour state",
               "content": base64.b64encode(json.dumps(stav).encode()).decode()}
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}",
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        json=payload,
        timeout=10,
    )
    if r.status_code in (200, 201):
        print("Stav ulozen")
        return
    if r.status_code == 409 and pokus < 3:
        print(f"  Konflikt pri ukladani, zkousim znovu (pokus {pokus + 1})")
        _, novy_sha = nacist_stav()
        return ulozit_stav(stav, novy_sha, pokus + 1)
    print(f"Chyba ukladani stavu: {r.status_code} {r.text[:200]}")


def spustit():
    print(f"Kontrola: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    turnaje = ziskat_turnaje()
    if not turnaje:
        print("Zadne turnaje (nebo chyba), koncim bez zmeny stavu.")
        return

    dnes  = datetime.now(timezone.utc).date()
    konec = dnes + timedelta(days=DNI_DOPREDU)

    stav, sha = nacist_stav()
    zname     = stav["turnaje"]      # {tid: posledni znamy pocet volnych mist}
    prvni_beh = stav["prvni_beh"]    # True = jen nastype stav, neposila notifikace
    novy_stav  = {}
    preskoceno = 0

    if prvni_beh:
        print("PRVNI BEH / migrace formatu -> jen nastype stav, notifikace se neposilaji.")

    for t in turnaje:
        tid   = str(t.get("id", ""))
        nazev = t.get("name", "")
        if not tid:
            continue
        if not zajima_me(nazev):
            preskoceno += 1
            continue
        try:
            d = datetime.strptime(t.get("startDate", ""), "%Y-%m-%d").date()
            if not (dnes <= d <= konec):
                continue
        except ValueError:
            pass
        if not t.get("isRegistrationOpen", True):
            continue
        try:
            zbyva = int(t.get("remainingSlots", 0))
        except (ValueError, TypeError):
            zbyva = 1

        # zapamatuj si aktualni stav turnaje (i plneho)
        drive = zname.get(tid)            # None = nikdy nevideny turnaj
        novy_stav[tid] = zbyva

        datum, cas = formatovat(t)
        print(f"  {nazev}: zbyva {zbyva} mist ({datum} {cas})")

        if prvni_beh:
            continue

        odkaz = f"{DETAIL_URL}?tournamentId={tid}&name={quote(nazev)}"

        if drive is None:
            # uplne novy turnaj v listingu
            volno = f"\U0001F465 Volných míst: {zbyva}" if zbyva > 0 else "\u26D4 Aktuálně plno"
            zprava = (f"\U0001F195 <b>Nový turnaj se vypsal!</b>\n\n"
                      f"<b>{nazev}</b>\n"
                      f"\U0001F4C5 {datum}  \u23F0 {cas}\n"
                      f"{volno}\n\n"
                      f"\U0001F449 {odkaz}")
            poslat_zpravu(zprava)
            print("  -> NOVY TURNAJ, notifikace odeslana!")
        elif drive <= 0 and zbyva > 0:
            # znamy turnaj, ktery byl plny a ted se uvolnilo misto
            zprava = (f"\U0001F3BE <b>Volné místo na turnaji!</b>\n\n"
                      f"<b>{nazev}</b>\n"
                      f"\U0001F4C5 {datum}  \u23F0 {cas}\n"
                      f"\U0001F465 Volných míst: {zbyva}\n\n"
                      f"\U0001F449 {odkaz}")
            poslat_zpravu(zprava)
            print("  -> UVOLNENE MISTO, notifikace odeslana!")
        # jinak: znamy turnaj beze zmeny -> nic

    print(f"Preskoceno {preskoceno} nezajimavych polozek")
    ulozit_stav({"turnaje": novy_stav}, sha)
    print(f"Hotovo. Sledovano {len(novy_stav)} turnaju.")


if __name__ == "__main__":
    spustit()
