import os
import sys
import json
import requests
import hashlib
import hmac
import random
import time
import binascii
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer

def start_fake_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHTTPRequestHandler)
    server.serve_forever()

def hmac_sha256(data, key):
    hashed = hmac.new(key.encode('utf-8'), data.encode('utf-8'), hashlib.sha256).digest()
    sign = binascii.hexlify(hashed).decode('utf-8')
    return sign

def get_map(json_obj, prefix=""):
    def flatten(obj, pre=""):
        result = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                result.update(flatten(v, f"{pre}.{k}" if pre else k))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                result.update(flatten(item, f"{pre}[{i}]"))
        else: 
            result[pre] = obj
        return result
    return flatten(json_obj, prefix)

def get_qstr(params): 
    return '&'.join([f"{key}={params[key]}" for key in sorted(params.keys())])

def call_api(method, url, key, secret, params=None):
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))
    headers = {'accessKey': key, 'nonce': nonce, 'timestamp': timestamp}
    
    sign_str = (get_qstr(get_map(params)) + '&' if params else '') + get_qstr(headers)
    headers['sign'] = hmac_sha256(sign_str, secret)
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, json=params)
        elif method == 'POST':
            response = requests.post(url, headers=headers, json=params)
        elif method == 'PUT':
            response = requests.put(url, headers=headers, json=params)
        
        if response.status_code == 200:
            return response.json()
        else:
            return None
    except Exception as e:
        print(f"Netzwerkfehler: {e}")
        return None

if __name__ == "__main__":
    threading.Thread(target=start_fake_server, daemon=True).start()

    access_key = os.getenv("ECOFLOW_ACCESS_KEY")
    secret_key = os.getenv("ECOFLOW_SECRET_KEY")
    ps_serial = os.getenv("POWERSTREAM_SERIAL")
    sm_serial = os.getenv("SMARTMETER_SERIAL")
    offset = int(os.getenv("POWER_OFFSET", "-15"))

    if not all([access_key, secret_key, ps_serial, sm_serial]):
        print("FEHLER: Umgebungsvariablen unvollständig!")
        sys.exit(1)

    print("==================================================")
    print(" Smart-Delay EcoFlow-Nulleinspeisung Aktiviert ")
    print("==================================================")
    
    url_quota = 'https://api-e.ecoflow.com/iot-open/sign/device/quota'

    # Variablen für die Stabilisierungs-Prüfung
    letzte_berechnete_einspeisung = -1
    hochregel_zaehler = 0
    ERFORDERLICHE_ZYKLEN = 3  # Wie viele Sekunden/Zyklen muss die hohe Last anliegen? (3 Sek bei sleep=1)

    while True:
        try:
            # 1. Hausverbrauch abfragen
            sm_params = {"sn": sm_serial, "quotas": ["20_1.sumInWatts"]}
            sm_payload = call_api('POST', url_quota, access_key, secret_key, sm_params)
            
            if sm_payload and 'data' in sm_payload and '20_1.wValue' in sm_payload['data']:
                haus_verbrauch = round(sm_payload['data']['20_1.wValue'] / 10)
                print(f"Hausverbrauch aktuell: {haus_verbrauch} W")
                
                # 2. Aktuelle Einspeisung abfragen
                ps_params = {"sn": ps_serial, "quotas": ["20_1.permanentWatts"]}
                ps_payload = call_api('POST', url_quota, access_key, secret_key, ps_params)
                
                if ps_payload and 'data' in ps_payload and '20_1.permanentWatts' in ps_payload['data']:
                    aktuelle_einspeisung = round(ps_payload['data']['20_1.permanentWatts'] / 10)
                    
                    # Falls beim ersten Start noch kein Wert vorliegt
                    if letzte_berechnete_einspeisung == -1:
                        letzte_berechnete_einspeisung = aktuelle_einspeisung

                    # 3. Zielwert ermitteln
                    ziel_einspeisung = aktuelle_einspeisung + haus_verbrauch + offset
                    if ziel_einspeisung < 0: ziel_einspeisung = 0
                    if ziel_einspeisung > 800: ziel_einspeisung = 800
                    
                    # --- DIE NEUE INTELLIGENTE PRÜFUNG ---
                    
                    # FALL A: Verbrauch fällt ab -> Sofort runterregeln (Keine Sekunde verschwenden!)
                    if ziel_einspeisung < letzte_berechnete_einspeisung:
                        hochregel_zaehler = 0  # Zähler zurücksetzen
                        letzte_berechnete_einspeisung = ziel_einspeisung
                        print(f"-> Verbrauch sinkt. Sofortige Anpassung geplant: {ziel_einspeisung} W")
                    
                    # FALL B: Verbrauch steigt -> Erst prüfen, ob es ein kurzer Peak ist
                    elif ziel_einspeisung > letzte_berechnete_einspeisung:
                        hochregel_zaehler += 1
                        print(f"-> Verbrauch steigt! Peak-Filter aktiv. (Zyklus {hochregel_zaehler}/{ERFORDERLICHE_ZYKLEN})")
                        
                        # Erst wenn die Last lang genug stabil war, übernehmen wir den hohen Wert
                        if hochregel_zaehler >= ERFORDERLICHE_ZYKLEN:
                            letzte_berechnete_einspeisung = ziel_einspeisung
                            print(f"   Last ist stabil! Erhöhe Einspeisung auf: {ziel_einspeisung} W")
                        else:
                            print(f"   Kurzzeitiger Peak ignoriert. Bleibe vorerst auf: {letzte_berechnete_einspeisung} W")
                    
                    else:
                        # Verbrauch ist exakt gleich geblieben
                        hochregel_zaehler = 0

                    # 4. Wert an den PowerStream senden (falls abweichend zum physisch eingestellten Wert)
                    if letzte_berechnete_einspeisung != aktuelle_einspeisung:
                        cmd_params = {
                            "sn": ps_serial,
                            "cmdCode": "WN511_SET_PERMANENT_WATTS_PACK",
                            "params": {"permanentWatts": letzte_berechnete_einspeisung * 10}
                        }
                        call_api('PUT', url_quota, access_key, secret_key, cmd_params)
                        print(f"====> BEFEHL GESENDET: PowerStream auf {letzte_berechnete_einspeisung} W angepasst.")
                    else:
                        print("-> Keine physische Änderung am PowerStream nötig.")
            else:
                print("Fehler beim Lesen des Smart Meters.")
                
        except Exception as e:
            print(f"Fehler im Regelkreis: {e}")
            
        time.sleep(1)  # 1 Sekunde Schleifenzeit
