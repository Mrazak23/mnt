import requests
import json
import os
from datetime import datetime, timedelta

EMAIL   = os.environ["SERVICE_EMAIL"]
HESLO   = os.environ["SERVICE_PASSWORD"]

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ["GITHUB_REPOSITORY"]
STATE_FILE   = "state.json"

CAS_OD = 17
CAS_DO = 21

LOCATION_ID  = os.environ["SERVICE_LOCATION_ID"]
RES_TYPE_ID  = int(os.environ["SERVICE_RES_TYPE_ID"])
FED_ID       = os.environ["SERVICE_FED_ID"]
ORG_ID       = os.environ["SERVICE_ORG_ID"]
CLUB_ID      = os.environ["SERVICE_CLUB_ID"]
API_BASE_URL = os.environ["SERVICE_API_URL"]
BOOKING_URL  = os.environ["SERVICE_BOOKING_URL"]
MISTO_NAZEV  = os.environ["SERVICE_LOCATION_NAME"]

token        = None
token_expiry = None

def pripojit():
    global token, token_expiry
    r = requests.post(
        f"{API_BASE_URL}/foys/api/v1/token",
        headers={
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "x-federationid": FED_ID,
            "x-organisationid": ORG_ID,
        },
        data={
            "grant_type": "password",
            "username": EMAIL,
            "password": HESLO,
            "clubId": CLUB_ID,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise Exception(f"Připojení selhalo: {r.status_code} {r.text[:200]}")
    data = r.json()
    token = data.get("access_token") or data.get("token") or data.get("accessToken")
    expires_in   = data.get("expires_in", 82800)
    token_expiry = datetime.now() + timedelta(seconds=expires_in - 3600)
    print("Připojení OK")

def hlavicky():
    if token is None or datetime.now() >= token_expiry:
        pripojit()
    return {
        "accept": "application/json",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "x-federationid": FED_ID,
        "x-organisationid": ORG_ID,
    }

def dny_dopredu():
    dnes    = datetime.now().date()
    vysledek = []
    for i in range(15):
        den = dnes + timedelta(days=i)
        if den.weekday() < 5:
            vysledek.append(den.strftime("%Y-%m-%d"))
    return vysledek

def ziskat_sloty(datum):
    r = requests.get(
        f"{API_BASE_URL}/court-booking/public/api/v1/locations/search",
        headers=hlavicky(),
        params=[
            ("reservationTypeId", RES_TYPE_ID),
            ("locationId", LOCATION_ID),
            ("playingTimes[]", 60),
            ("playingTimes[]", 90),
            ("playingTimes[]", 120),
            ("date", f"{datum}T00:00"),
        ],
        timeout=10,
    )
    if r.status_code == 401:
        pripojit()
        return ziskat_sloty(datum)
    if r.status_code != 200:
        print(f"Chyba API pro {datum}: {r.status_code}")
        return []

    data  = r.json()
    volne = []
    for lokace in data:
        for kurt in lokace.get("inventoryItemsTimeSlots", []):
            kurt_nazev = kurt.get("name", "?")
            for slot in kurt.get("timeSlots", []):
                if not slot.get("isAvailable", False):
                    continue
                cas_raw = slot.get("startTime", "")
                try:
                    hodina = int(cas_raw.split("T")[1][:2])
                except:
                    hodina = -1
                if CAS_OD <= hodina < CAS_DO:
                    volne.append({
                        "cas":   cas_raw,
                        "kurt":  kurt_nazev,
                        "delka": slot.get("duration", "?"),
                        "cena":  slot.get("price", "?"),
                        "id":    f"{kurt.get('id')}-{cas_raw}-{slot.get('duration')}",
                    })
    return volne

def poslat_zpravu(text):
    if len(text) > 4000:
        text = text[:4000] + "\n...(zkráceno)"
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    print(f"Zpráva odeslána: {r.status_code}")

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
        return {"oznameno": [], "aktualni": {}}, None
    data = r.json()
    import base64
    obsah = json.loads(base64.b64decode(data["content"]).decode())
    if "oznameno" not in obsah:
        obsah = {"oznameno": [], "aktualni": obsah}
    return obsah, data["sha"]

def ulozit_stav(stav, sha):
    import base64
    obsah_b64 = base64.b64encode(json.dumps(stav).encode()).decode()
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
        print("Stav uložen")
    else:
        print(f"Chyba ukládání stavu: {r.status_code} {r.text[:200]}")

def spustit():
    print(f"Kontrola: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    pripojit()

    stav, sha    = nacist_stav()
    uz_oznameno  = set(stav.get("oznameno", []))
    aktualni     = {}

    for datum in dny_dopredu():
        try:
            sloty       = ziskat_sloty(datum)
            aktualni_ids = {s["id"] for s in sloty}
            aktualni[datum] = list(aktualni_ids)

            nove_ids = aktualni_ids - uz_oznameno

            if nove_ids:
                nove_sloty = [s for s in sloty if s["id"] in nove_ids]
                radky = "\n".join(
                    f"{s['cas'][11:16]}  {s['kurt']}  ({s['delka']} min)  {s['cena']} Kč"
                    for s in nove_sloty
                )
                datum_dt  = datetime.strptime(datum, "%Y-%m-%d")
                dny       = ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"]
                den_nazev = dny[datum_dt.weekday()]
                datum_cz  = datum_dt.strftime("%d.%m.%Y")
                odkaz     = f"{BOOKING_URL}?location={MISTO_NAZEV}&date={datum}"
                zprava    = f"🎾 <b>Uvolnil se kurt!</b>\n\n📅 {den_nazev} {datum_cz}\n\n{radky}\n\n👉 {odkaz}"
                poslat_zpravu(zprava)
                print(f"  {datum}: {len(nove_ids)} NOVÝCH slotů, notifikace odeslána!")
                for s in nove_sloty:
                    uz_oznameno.add(s["id"])
            else:
                print(f"  {datum}: {len(sloty)} volných, žádná změna")

        except Exception as e:
            print(f"  Chyba pro {datum}: {e}")

    vsechny_aktualni = set()
    for ids in aktualni.values():
        vsechny_aktualni.update(ids)
    uz_oznameno = uz_oznameno & vsechny_aktualni

    ulozit_stav({"oznameno": list(uz_oznameno), "aktualni": aktualni}, sha)
    print("Hotovo.")

if __name__ == "__main__":
    spustit()
