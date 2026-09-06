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
    # Startet einen minimalistischen Webserver auf Port 10000, damit Render glücklich ist
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
            print(f"API Fehler ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        print(f"Netzwerkfehler bei API-Aufruf: {e}")
        return None

if __name__ == "__main__":
    # Fake-Server im Hintergrund starten, um Renders Port-Check zu bestehen
    threading.Thread(target=start_fake_server, daemon=True).start()

    # Ab hier folgt Ihr restlicher Code...
    access_key = os.getenv("ECOFLOW_ACCESS_KEY")

    # Render zieht sich die echten Zugangsdaten aus Ihren Umgebungsvariablen
    access_key = os.getenv("ECOFLOW_ACCESS_KEY")
    secret_key = os.getenv("ECOFLOW_SECRET_KEY")
    ps_serial = os.getenv("POWERSTREAM_SERIAL")
    sm_serial = os.getenv("SMARTMETER_SERIAL")
    offset = int(os.getenv("POWER_OFFSET", "-15"))

    if not all([access_key, secret_key, ps_serial, sm_serial]):
        print("FEHLER: Umgebungsvariablen auf Render nicht vollständig ausgefüllt!")
        sys.exit(1)

    print("==================================================")
    print(" Kostenlose EcoFlow-Nulleinspeisung Aktiviert ")
    print("==================================================")
    
    url_quota = 'https://api-e.ecoflow.com/iot-open/sign/device/quota'

    while True:
        try:
            # 1. Aktuellen Hausverbrauch vom EcoFlow Smart Meter abfragen
            sm_params = {"sn": sm_serial, "quotas": ["20_1.wValue"]}
            sm_payload = call_api('POST', url_quota, access_key, secret_key, sm_params)
            
            if sm_payload and 'data' in sm_payload and '20_1.wValue' in sm_payload['data']:
                # EcoFlow liefert den Wert oft in Zehntel-Watt (z.B. 1500 statt 150W), daher durch 10 teilen
                haus_verbrauch = round(sm_payload['data']['20_1.wValue'] / 10)
                print(f"Aktueller Hausverbrauch vom Zähler: {haus_verbrauch} W")
                
                # 2. Aktuelle Einspeisung des PowerStreams abfragen, um die Differenz zu berechnen
                ps_params = {"sn": ps_serial, "quotas": ["20_1.permanentWatts"]}
                ps_payload = call_api('POST', url_quota, access_key, secret_key, ps_params)
                
                if ps_payload and 'data' in ps_payload and '20_1.permanentWatts' in ps_payload['data']:
                    aktuelle_einspeisung = round(ps_payload['data']['20_1.permanentWatts'] / 10)
                    
                    # 3. Neue benötigte Leistung berechnen (inklusive Ihrem gewählten Offset-Schutz)
                    neue_einspeisung = aktuelle_einspeisung + haus_verbrauch + offset
                    
                    # Grenzen einhalten (PowerStream schafft min. 0W und max. 600W bzw. 800W)
                    if neue_einspeisung < 0:
                        neue_einspeisung = 0
                    elif neue_einspeisung > 800:
                        neue_einspeisung = 800
                        
                    print(f"Berechnete neue Einspeisung (mit Offset {offset}W): {neue_einspeisung} W")
                    
                    # 4. Den neuen Wert an den PowerStream senden, falls er sich geändert hat
                    if neue_einspeisung != aktuelle_einspeisung:
                        cmd_params = {
                            "sn": ps_serial,
                            "cmdCode": "WN511_SET_PERMANENT_WATTS_PACK",
                            "params": {"permanentWatts": neue_einspeisung * 10}
                        }
                        call_api('PUT', url_quota, access_key, secret_key, cmd_params)
                        print(f"--> Befehl gesendet: PowerStream auf {neue_einspeisung} W angepasst.")
                    else:
                        print("--> Keine Anpassung notwendig (Verbrauch stabil).")
            else:
                print("Konnte Daten vom Smart Meter nicht lesen.")
                
        except Exception as e:
            print(f"Fehler im Regelkreis: {e}")
            
        time.sleep(1)  # Sicherheits-Pause, um Sperrung durch EcoFlow zu vermeiden
