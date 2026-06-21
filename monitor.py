import requests
import json
import os
import base64
from datetime import datetime, timedelta

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ["GITHUB_REPOSITORY"]
STATE_FILE   = "state.json"

# Okno: vsedni dny, cas startu 17:00-20:59
CAS_OD = 17
CAS_DO = 21
POCET_DNI = 15

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
    dnes = datetime.now().date()
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
    # Deduplikace: jeden zaznam na kurt + cas startu (slouci delky 60/90/120),
    # u ceny si drzime nejnizsi (= nejkratsi dostupna delka).
    najdene = {}
    for lokace in data:
        for blok in lokace.get("availability", []):
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
                    kid  = kurt.get("id")
                    klic = f"{slot_datum}|{start}|{kid}"
                    try:
                        cena = float(kurt.get("price", 0))
                    except (ValueError, TypeError):
                        cena = 0.0
                    if klic not in najdene or cena < najdene[klic]["cena"]:
                        najdene[klic] = {
                            "id":   klic,
                            "cas":  start,
                            "kurt": kurt.get("name", "?"),
                            "cena": cena,
                        }
    return list(najdene.values())


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
        return {"oznameno": [], "aktualni": {}}, None
    data = r.json()
    obsah = json.loads(base64.b64decode(data["content"]).decode())
    if "oznameno" not in obsah:
        obsah = {"oznameno": [], "aktualni": obsah}
    return obsah, data["sha"]


def ulozit_stav(stav, sha, pokus=0):
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
        print("Stav ulozen")
        return
    # 409 = mezitim zapsal jiny beh; nacti cerstvy stav, sluc oznameno a zkus znovu
    if r.status_code == 409 and pokus < 3:
        print(f"  Konflikt pri ukladani, slucuji a zkousim znovu (pokus {pokus + 1})")
        cerstvy, novy_sha = nacist_stav()
        sloucene = set(stav.get("oznameno", [])) | set(cerstvy.get("oznameno", []))
        vsechny = set()
        for ids in stav.get("aktualni", {}).values():
            vsechny.update(ids)
        if vsechny:
            sloucene = sloucene & vsechny
        stav2 = {"oznameno": list(sloucene), "aktualni": stav.get("aktualni", {})}
        return ulozit_stav(stav2, novy_sha, pokus + 1)
    print(f"Chyba ukladani stavu: {r.status_code} {r.text[:200]}")


def spustit():
    print(f"Kontrola: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    stav, sha   = nacist_stav()
    uz_oznameno = set(stav.get("oznameno", []))
    aktualni    = {}

    for datum in dny_dopredu():
        try:
            sloty        = ziskat_sloty(datum)
            aktualni_ids = {s["id"] for s in sloty}
            aktualni[datum] = list(aktualni_ids)

            nove_ids = aktualni_ids - uz_oznameno

            if nove_ids:
                nove_sloty = [s for s in sloty if s["id"] in nove_ids]
                nove_sloty.sort(key=lambda x: (x["cas"], x["kurt"]))
                radky = "\n".join(
                    f"{s['cas']}  {s['kurt']}  od {int(s['cena'])} Kč"
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
                for s in nove_sloty:
                    uz_oznameno.add(s["id"])
            else:
                print(f"  {datum}: {len(sloty)} volnych, zadna zmena")

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
