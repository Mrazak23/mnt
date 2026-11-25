import os
import re
import json
import base64
import requests
from urllib.parse import quote, quote_plus
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Konfigurace
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]
GH_REPO  = os.environ["GITHUB_REPOSITORY"]

API_URL    = os.environ["SERVICE_API_URL"]     # https://api.padelos.co
COMPANY_ID = os.environ["SERVICE_COMPANY_ID"]  # 217
CLUB_ID    = os.environ["SERVICE_CLUB_ID"]     # 216927
DOMAIN     = os.environ["SERVICE_DOMAIN"]      # PADELOSCO

TZ_PRAHA = ZoneInfo("Europe/Prague")

# Odkazy na detail (id + nazev se doplni za behu)
TURNAJ_URL = f"https://player.padelos.co/club/{CLUB_ID}/tournaments/tournament-detail"
AKCE_URL   = f"https://player.padelos.co/club/{CLUB_ID}/events/event-detail"

STATE_FILE  = "tour_state.json"
DNI_DOPREDU = 180

DNY = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]

# ---------------------------------------------------------------------------
# PRAVIDLA (plati pro turnaje i akce)
#  1) muj rating 3.0 musi byt v rozsahu: minRating <= 3.0 < maxRating
#     -> akce, kde je 3.00 horni hranice (Beginners 1.0-3.0 apod.), NEhlasime
#  2) jen pro zeny / LADIES -> ne
#  3) ranni akce ve vsedni dny (start pred RANNI_DO) -> ne; o vikendu
#     a ve statni svatky ano
#  4) kdyz akce rating nema vyplneny, rozhodne nazev:
#     Beginners/Starters bez Intermediate -> ne, uroven pismenem jen s C
#     (C, B-C) -> ano, C-D / D / E -> ne, jinak ano
# ---------------------------------------------------------------------------
MUJ_RATING = 3.0
RANNI_DO   = 10          # vsedni den se startem pred 10:00 = ranni akce
LEVEL_PATTERN = re.compile(r'\b([A-E])(?:\s*[-–]\s*([A-E]))?\b')


def _cislo(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


# Ceske statni svatky (MM-DD) - ranni akce se v nich hlasi jako o vikendu
SVATKY = {"01-01", "05-01", "05-08", "07-05", "07-06", "09-28",
          "10-28", "11-17", "12-24", "12-25", "12-26"}


def velikonoce(rok):
    """Velikonocni nedele (gregoriansky kalendar)."""
    a = rok % 19
    b, c = divmod(rok, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (2 * e + 2 * i - h - k + 32) % 7
    m = (a + 11 * h + 19 * l) // 433
    mesic = (h + l - 7 * m + 90) // 25
    den = (h + l - 7 * m + 33 * mesic + 19) % 32
    return datetime(rok, mesic, den)


def je_volno(d):
    """Vikend nebo statni svatek (vcetne Velkeho patku a Velikonocniho pondeli)."""
    if d.weekday() >= 5 or d.strftime("%m-%d") in SVATKY:
        return True
    nedele = velikonoce(d.year)
    return d.date() in ((nedele - timedelta(days=2)).date(),
                        (nedele + timedelta(days=1)).date())


def zajima_me(nazev, min_r, max_r, gender, datum_str, cas_str):
    n = (nazev or "").upper()

    # 2) jen pro zeny
    if (gender or "").lower() == "female" or "LADIES" in n:
        return False

    # 3) ranni akce ve vsedni den
    try:
        d = datetime.strptime(datum_str, "%Y-%m-%d")
        hodina = int((cas_str or "99")[:2])
        if not je_volno(d) and hodina < RANNI_DO:
            return False
    except ValueError:
        pass

    # 1) rating
    lo, hi = _cislo(min_r), _cislo(max_r)
    if lo is not None and hi is not None:
        return lo <= MUJ_RATING < hi

    # 4) rating chybi -> podle nazvu
    if ("BEGINNER" in n or "STARTER" in n) and "INTERMEDIATE" not in n:
        return False
    urovne = set()
    for m in LEVEL_PATTERN.finditer(n):
        urovne.add(m.group(1))
        if m.group(2):
            urovne.add(m.group(2))
    if urovne:
        return "C" in urovne and not (urovne & {"D", "E"})
    return True


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


# ------------------------------- API ---------------------------------------

def ziskat_turnaje():
    """Vrati seznam polozek ve spolecnem formatu, nebo None pri chybe."""
    r = requests.get(
        f"{API_URL}/customers/tournament/listing",
        headers=hlavicky(),
        params={"clubId": "", "limit": 200, "sport": "Padel", "format": "",
                "category": "", "hideFilters": "registration_closed", "page": ""},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"Chyba API turnaje: {r.status_code} {r.text[:150]}")
        return None
    data = r.json().get("data", {})
    rows = data.get("rows", []) if isinstance(data, dict) else []
    vysledek = []
    for t in rows:
        try:
            zbyva = int(t.get("remainingSlots", 0))
        except (ValueError, TypeError):
            zbyva = 1
        vysledek.append({
            "id": str(t.get("id", "")), "nazev": t.get("name", ""),
            "datum": t.get("startDate", ""), "cas": t.get("startTime", ""),
            "min": t.get("minRating"), "max": t.get("maxRating"),
            "gender": t.get("gender"), "zbyva": zbyva,
            "otevreno": t.get("isRegistrationOpen", True),
        })
    return vysledek


def ziskat_akce():
    """Sekce Akce (eventsBooking). Vrati seznam, nebo None pri chybe."""
    rows = []
    for strana in range(1, 11):
        r = requests.get(
            f"{API_URL}/customers/club/eventsBooking",
            headers=hlavicky(),
            params={"eventType": "", "clubId": CLUB_ID, "status": "", "type": "",
                    "limit": 50, "availability": "all", "page": strana},
            timeout=20,
        )
        if r.status_code != 200:
            print(f"Chyba API akce: {r.status_code} {r.text[:150]}")
            return None
        data = r.json().get("data", {})
        if not isinstance(data, dict):
            break
        rows.extend(data.get("rows", []))
        if not data.get("hasMore"):
            break
    vysledek = []
    for e in rows:
        if e.get("status") != "Published":
            continue
        kapacita = e.get("maxParticipants")
        prihlaseno = e.get("participantCount") or 0
        try:
            zbyva = max(0, int(kapacita) - int(prihlaseno))
        except (TypeError, ValueError):
            zbyva = 1
        vysledek.append({
            "id": str(e.get("id", "")), "nazev": e.get("name", ""),
            "datum": e.get("startDate", ""), "cas": e.get("startTime", ""),
            "min": e.get("minRating"), "max": e.get("maxRating"),
            "gender": e.get("gender"), "zbyva": zbyva,
            "otevreno": e.get("isRegistrationOpen", True),
        })
    return vysledek


# ----------------------------- Telegram ------------------------------------

def formatovat(datum_raw, cas_raw):
    try:
        d = datetime.strptime(datum_raw, "%Y-%m-%d")
        datum = f"{DNY[d.weekday()]} {d.strftime('%d.%m.%Y')}"
    except ValueError:
        datum = datum_raw or "?"
    cas = cas_raw[:5] if cas_raw else "?"
    return datum, cas


def poslat_zpravu(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        print(f"Zprava odeslana: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"Chyba Telegramu: {e}")
        return False


# ------------------------------- Stav --------------------------------------

def _nacist_raw():
    r = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}",
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if r.status_code == 404:
        return {}, None
    data = r.json()
    return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]


def _normalizovat(sekce):
    """{id: {"z": volna_mista, "d": datum}}; stary format {id: cislo} prevede."""
    if not isinstance(sekce, dict):
        return None
    out = {}
    for k, v in sekce.items():
        if isinstance(v, dict):
            out[k] = {"z": v.get("z", 0), "d": v.get("d")}
        else:
            out[k] = {"z": v, "d": None}
    return out


def nacist_stav():
    obsah, sha = _nacist_raw()
    return _normalizovat(obsah.get("turnaje")), _normalizovat(obsah.get("akce")), sha


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
        _, novy_sha = _nacist_raw()
        return ulozit_stav(stav, novy_sha, pokus + 1)
    print(f"Chyba ukladani stavu: {r.status_code} {r.text[:200]}")


# ------------------------------ Logika -------------------------------------

def zpracovat(polozky, zname, druh, dnes, konec):
    """druh = "turnaj" / "akce". zname = None -> prvni beh, jen nastype stav."""
    prvni_beh = zname is None
    zname = zname or {}
    novy = {}
    preskoceno = 0

    if prvni_beh:
        print(f"[{druh}] PRVNI BEH -> jen ulozim stav, notifikace neposilam.")

    for p in polozky:
        pid, nazev = p["id"], p["nazev"]
        if not pid:
            continue
        try:
            d = datetime.strptime(p["datum"], "%Y-%m-%d").date()
            if not (dnes <= d <= konec):
                continue
        except ValueError:
            pass
        if not p["otevreno"]:
            continue
        if not zajima_me(nazev, p["min"], p["max"], p["gender"], p["datum"], p["cas"]):
            preskoceno += 1
            continue

        zbyva = p["zbyva"]
        drive = zname.get(pid)          # None = nikdy nevideno
        datum, cas = formatovat(p["datum"], p["cas"])
        print(f"  [{druh}] {nazev}: zbyva {zbyva} ({datum} {cas})")

        if prvni_beh:
            novy[pid] = {"z": zbyva, "d": p["datum"]}
            continue

        if druh == "turnaj":
            odkaz = f"{TURNAJ_URL}?tournamentId={pid}&name={quote(nazev)}"
            t_novy, t_misto = "Nový turnaj se vypsal!", "Volné místo na turnaji!"
        else:
            odkaz = f"{AKCE_URL}?eventId={pid}&type=active&name={quote_plus(nazev)}"
            t_novy, t_misto = "Nová akce!", "Volné místo na akci!"

        lo, hi = _cislo(p["min"]), _cislo(p["max"])
        rating = f"\U0001F4CA Rating {lo:.1f}–{hi:.1f}\n" if lo is not None and hi is not None else ""

        if drive is None:
            volno = f"\U0001F465 Volných míst: {zbyva}" if zbyva > 0 else "⛔ Aktuálně plno"
            zprava = (f"\U0001F195 <b>{t_novy}</b>\n\n<b>{nazev}</b>\n"
                      f"\U0001F4C5 {datum}  ⏰ {cas}\n{rating}{volno}\n\n\U0001F449 {odkaz}")
            if poslat_zpravu(zprava):
                novy[pid] = {"z": zbyva, "d": p["datum"]}
                print("  -> NOVE, notifikace odeslana!")
            # kdyz se zprava neodeslala, nezapamatuju si to -> zkusi se pristi beh
        elif drive["z"] <= 0 and zbyva > 0:
            zprava = (f"\U0001F3BE <b>{t_misto}</b>\n\n<b>{nazev}</b>\n"
                      f"\U0001F4C5 {datum}  ⏰ {cas}\n{rating}"
                      f"\U0001F465 Volných míst: {zbyva}\n\n\U0001F449 {odkaz}")
            if poslat_zpravu(zprava):
                novy[pid] = {"z": zbyva, "d": p["datum"]}
                print("  -> UVOLNENE MISTO, notifikace odeslana!")
            else:
                novy[pid] = drive
        else:
            novy[pid] = {"z": zbyva, "d": p["datum"]}

    # Co tentokrat v API nebylo, si pamatuju dal (dokud neprobehlo),
    # aby se pri "blikani" API neposilala znovu notifikace "Nove".
    for pid, v in zname.items():
        if pid not in novy and v.get("d") and v["d"] >= dnes.isoformat():
            novy[pid] = v

    print(f"[{druh}] preskoceno {preskoceno} nezajimavych, sledovano {len(novy)}")
    return novy


def spustit():
    print(f"Kontrola: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    turnaje = ziskat_turnaje()
    akce    = ziskat_akce()
    if turnaje is None and akce is None:
        print("Obe API selhala, koncim bez zmeny stavu.")
        return

    dnes  = datetime.now(TZ_PRAHA).date()
    konec = dnes + timedelta(days=DNI_DOPREDU)

    zname_t, zname_a, sha = nacist_stav()

    # pri chybe API sekci nechavam beze zmeny
    novy_t = zpracovat(turnaje, zname_t, "turnaj", dnes, konec) if turnaje is not None else zname_t
    novy_a = zpracovat(akce, zname_a, "akce", dnes, konec) if akce is not None else zname_a

    ulozit_stav({"turnaje": novy_t, "akce": novy_a}, sha)
    print("Hotovo.")


if __name__ == "__main__":
    spustit()
