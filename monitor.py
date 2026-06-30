import requests
import json
import os
import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Dny pocitame v pražském case, ne v UTC runneru – jinak nejvzdalenejsi den
# kolem pulnoci vypadne z okna (UTC je o den pozadu).
TZ_PRAHA = ZoneInfo("Europe/Prague")

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ["GITHUB_REPOSITORY"]
STATE_FILE   = "state.json"

# Okno: vsedni dny, cas startu 17:00-20:59
CAS_OD = 17
CAS_DO = 21
POCET_DNI = 15

# Hlidat jen tyto delky (minuty). Pro vsechny daej: {"60", "90", "120"}
POVOLENE_DELKY = {"90", "120"}

# Jak dlouho (hodiny) si pamatovat oznameny slot i kdyz docasne zmizi z API.
# Brani duplicitnim notifikacim, kdyz API "blika".
GRACE_HODIN = 6

API_URL     = os.environ["SERVICE_API_URL"]      # https://api.padelos.co
COMPANY_ID  = os.environ["SERVICE_COMPANY_ID"]   # 217
CLUB_ID     = os.environ["SERVICE_CLUB_ID"]      # 216927
DOMAIN      = os.environ["SERVICE_DOMAIN"]       # PADELOSCO
BOOKING_URL = os.environ["SERVICE_BOOKING_URL"]  # https://player.padelos.co/company/217?clubIds=216927&locale=cs


def hlavicky():
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://player.padelos.co",
        "referer": "https://player.padelos.co/",
        "x-clubos-channel": "CLUBOS-WEB",
        "x-clubos-company": COMPANY_ID,
        "x-clubos-club-info": CLUB_ID,
        "x-clubos-domain": DOMAIN,
    }


def dny_dopredu():
    dnes = datetime.now(TZ_PRAHA).date()
    vysledek = []
    for i in range(POCET_DNI):
        den = dnes + timedelta(days=i)
        if den.weekday() < 5:  # po-pa
            vysledek.append(den.strftime("%Y-%m-%d"))
    return vysledek


def ziskat_sloty(datum):
    r = requests.post(
        f"{API_URL}/customers/searchByDate",
        headers=hlavicky(),
        json={
            "date": datum,
            "sport": "padel",
            "courtType": "",
            "courtSize": "",
            "courtTurf": "",
            "courtFeature": "",
            "searchTerm": "",
            "limit": "",
            "offset": "",
            "type": "",
        },
        timeout=15,
    )
    if r.status_code != 200:
        print(f"Chyba API pro {datum}: {r.status_code} {r.text[:150]}")
        return []

    data = r.json().get("data", [])
    # Kazda kombinace kurt + cas + delka je samostatna moznost (vcetne ceny).
    sloty = []
    for lokace in data:
        for blok in lokace.get("availability", []):
            delka = str(blok.get("duration", ""))
            if delka not in POVOLENE_DELKY:
                continue
            for slot in blok.get("slots", []):
                start = slot.get("startTime", "")
                try:
                    hodina = int(start[:2])
                except (ValueError, IndexError):
                    continue
                if not (CAS_OD <= hodina < CAS_DO):
                    continue
                slot_datum = slot.get("date", datum)
                for kurt in slot.get("courts", []):
                    kid = kurt.get("id")
                    try:
                        cena = int(round(float(kurt.get("price", 0))))
                    except (ValueError, TypeError):
                        cena = 0
                    sloty.append({
                        "id":    f"{slot_datum}|{start}|{kid}|{delka}",
                        "cas":   start,
                        "kurt":  kurt.get("name", "?"),
                        "delka": delka,
                        "cena":  cena,
                    })
    return sloty


def poslat_zpravu(text):
    if len(text) > 4000:
        text = text[:4000] + "\n...(zkraceno)"
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=10,
    )
    print(f"Zprava odeslana: {r.status_code}")


def nacist_stav():
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    if r.status_code == 404:
        return {"videno": {}}, None
    data  = r.json()
    obsah = json.loads(base64.b64decode(data["content"]).decode())
    return obsah, data["sha"]


def _videno_z_obsahu(obsah):
    """Vrati slovnik {slot_id: ISO cas naposledy videno}.
    Zvlada i stary format (oznameno jako seznam) -> bere ho jako videno ted."""
    if isinstance(obsah.get("videno"), dict):
        return dict(obsah["videno"])
    nyni = datetime.now(timezone.utc).isoformat()
    stare = obsah.get("oznameno", [])
    if isinstance(stare, list):
        return {sid: nyni for sid in stare}
    return {}


def ulozit_stav(videno, sha, pokus=0):
    obsah_b64 = base64.b64encode(json.dumps({"videno": videno}).encode()).decode()
    payload   = {"message": "update state", "content": obsah_b64}
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json=payload,
        timeout=10,
    )
    if r.status_code in (200, 201):
        print("Stav ulozen")
        return
    # 409 = mezitim zapsal jiny beh; nacti cerstvy stav, sluc a zkus znovu
    if r.status_code == 409 and pokus < 3:
        print(f"  Konflikt pri ukladani, slucuji a zkousim znovu (pokus {pokus + 1})")
        cerstvy, novy_sha = nacist_stav()
        cerstve_videno = _videno_z_obsahu(cerstvy)
        # sjednoceni: u kazdeho slotu drz pozdejsi cas
        for sid, ts in cerstve_videno.items():
            if sid not in videno or ts > videno[sid]:
                videno[sid] = ts
        return ulozit_stav(videno, novy_sha, pokus + 1)
    print(f"Chyba ukladani stavu: {r.status_code} {r.text[:200]}")


def spustit():
    print(f"Kontrola: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    stav, sha = nacist_stav()
    videno    = _videno_z_obsahu(stav)   # {slot_id: ISO cas naposledy videno}

    nyni     = datetime.now(timezone.utc)
    nyni_iso = nyni.isoformat()

    for datum in dny_dopredu():
        try:
            sloty        = ziskat_sloty(datum)
            aktualni_ids = {s["id"] for s in sloty}

            # nove = slot, ktery aktualne NEznam (jeste neni ve videno)
            nove_ids = aktualni_ids - set(videno.keys())

            if nove_ids:
                nove_sloty = [s for s in sloty if s["id"] in nove_ids]
                nove_sloty.sort(key=lambda x: (x["cas"], x["kurt"], int(x["delka"] or 0)))
                radky = "\n".join(
                    f"{s['cas']}  {s['kurt']}  {s['delka']} min  {s['cena']} Kč"
                    for s in nove_sloty
                )
                datum_dt  = datetime.strptime(datum, "%Y-%m-%d")
                dny       = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]
                den_nazev = dny[datum_dt.weekday()]
                datum_cz  = datum_dt.strftime("%d.%m.%Y")
                zprava    = (f"\U0001F3BE <b>Uvolnil se kurt!</b>\n\n"
                             f"\U0001F4C5 {den_nazev} {datum_cz}\n\n{radky}\n\n\U0001F449 {BOOKING_URL}")
                poslat_zpravu(zprava)
                print(f"  {datum}: {len(nove_ids)} NOVYCH slotu, notifikace odeslana!")
            else:
                print(f"  {datum}: {len(sloty)} volnych, zadna zmena")

            # u vsech aktualne dostupnych slotu obnov cas "naposledy videno"
            for sid in aktualni_ids:
                videno[sid] = nyni_iso

        except Exception as e:
            print(f"  Chyba pro {datum}: {e}")

    # zapomen sloty, ktere uz GRACE_HODIN nebyly videt (uplne zmizely)
    mez = (nyni - timedelta(hours=GRACE_HODIN)).isoformat()
    videno = {sid: ts for sid, ts in videno.items() if ts >= mez}

    ulozit_stav(videno, sha)
    print("Hotovo.")


if __name__ == "__main__":
    spustit()
