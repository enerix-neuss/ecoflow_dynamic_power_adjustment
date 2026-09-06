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

def flatten_dict(obj, pre=""):
    result = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            result.update(flatten_dict(v, f"{pre}.{k}" if pre else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            result.update(flatten_dict(item, f"{pre}[{i}]"))
    else:
        result[pre] = obj
    return result

def generate_ecoflow_sign(params, headers, secret_key):
    # 1. Parameter flachdrücken und alphabetisch sortieren (Pflicht für POST)
    flat_params = flatten_dict(params) if params else {}
    sorted_params = sorted(flat_params.items())
    query_str = "&".join([f"{k}={v}" for k, v in sorted_params])
    
    # 2. Header-Daten extrahieren und alphabetisch sortieren
    sign_headers = {
        'accessKey': headers['accessKey'],
        'nonce': headers['nonce'],
        'timestamp': headers['timestamp']
    }
    sorted_headers = sorted(sign_headers.items())
    header_str = "&".join([f"{k}={v}" for k, v in sorted_headers])
    
    # 3. Den offiziellen Signatur-String nach Dokumentation zusammensetzen
    if query_str:
        final_str = f"{query_str}&{header_str}"
    else:
        final_str = header_str
        
    # 4. HMAC-SHA256 Verschlüsselung anwenden
    hashed = hmac.new(secret_key.encode('utf-8'), final_str.encode('utf-8'), hashlib.sha256).digest()
    return binascii.hexlify(hashed).decode('utf-8')

def call_ecoflow_api(url, method, params, access_key, secret_key):
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))
    
    headers = {
        'accessKey': access_key,
        'nonce': nonce,
        'timestamp': timestamp,
        'Content-Type': 'application/json'
    }
    
    # Signatur basierend auf dem sortierten JSON-Inhalt generieren
    headers['sign'] = generate_ecoflow_sign(params, headers, secret_key)
    
    try:
        if method == 'POST':
            res = requests.post(url, headers=headers, json=params)
        elif method == 'PUT':
            res = requests.put(url, headers=headers, json=params)
        else:
            res = requests.get(url, headers=headers, params=params)
            
        if res.status_code == 200:
            return res.json()
        else:
            print(f"API Fehler (Status {res.status_code}): {res.text[:200]}")
            return None
    except Exception as e:
        print(f"Netzwerkfehler bei API-Aufruf: {e}")
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
    print(" Offizielle EcoFlow v2.0 Nulleinspeisung Aktiv ")
    print("==================================================")
    
    # Der offizielle API v2.0 Quota-Endpunkt
    url_quota = 'https://ecoflow.com'

    letzte_berechnete_einspeisung = -1
    hochregel_zaehler = 0
    ERFORDERLICHE_ZYKLEN = 3

    while True:
        try:
            # 1. Hausverbrauch abfragen (Zwingend per POST laut API-Handbuch)
            sm_params = {"sn": sm_serial, "quotas": ["20_1.sumInWatts"]}
            sm_res = call_ecoflow_api(url_quota, 'POST', sm_params, access_key, secret_key)
            
            if sm_res and sm_res.get('code') == 0 and 'data' in sm_res:
                data_block = sm_res['data']
                val = data_block.get('20_1.sumInWatts')
                
                if val is not None:
                    haus_verbrauch = round(float(val))
                    print(f"Hausverbrauch aktuell: {haus_verbrauch} W")
                    
                    # 2. Aktuelle Einspeisung des PowerStreams abfragen
                    ps_params = {"sn": ps_serial, "quotas": ["20_1.permanentWatts"]}
                    ps_res = call_ecoflow_api(url_quota, 'POST', ps_params, access_key, secret_key)
                    
                    if ps_res and ps_res.get('code') == 0 and 'data' in ps_res:
                        ps_val = ps_res['data'].get('20_1.permanentWatts')
                        
                        if ps_val is not None:
                            # permanentWatts wird von der API in Zehntel-Watt geliefert (z.B. 1500 = 150W)
                            aktuelle_einspeisung = round(float(ps_val) / 10)
                            
                            if letzte_berechnete_einspeisung == -1:
                                letzte_berechnete_einspeisung = aktuelle_einspeisung

                            # 3. Nulleinspeisung berechnen
                            ziel_einspeisung = aktuelle_einspeisung + haus_verbrauch + offset
                            if ziel_einspeisung < 0: ziel_einspeisung = 0
                            if ziel_einspeisung > 800: ziel_einspeisung = 800
                            
                            # --- PEAK-FILTER ---
                            if ziel_einspeisung < letzte_berechnete_einspeisung:
                                hochregel_zaehler = 0
                                letzte_berechnete_einspeisung = ziel_einspeisung
                                print(f"-> Last sinkt. Sofortige Anpassung: {ziel_einspeisung} W")
                            elif ziel_einspeisung > letzte_berechnete_einspeisung:
                                hochregel_zaehler += 1
                                print(f"-> Last steigt! Peak-Filter aktiv. (Sekunde {hochregel_zaehler}/{ERFORDERLICHE_ZYKLEN})")
                                if hochregel_zaehler >= ERFORDERLICHE_ZYKLEN:
                                    letzte_berechnete_einspeisung = ziel_einspeisung
                                    print(f"   Last ist stabil! Erhöhe auf: {letzte_berechnete_einspeisung} W")
                                else:
                                    print(f"   Peak blockiert. Bleibe auf: {letzte_berechnete_einspeisung} W")
                            else:
                                hochregel_zaehler = 0

                            # 4. Wert setzen (Per PUT-Befehl an denselben Endpunkt)
                            if letzte_berechnete_einspeisung != aktuelle_einspeisung:
                                cmd_params = {
                                    "sn": ps_serial,
                                    "cmdCode": "WN511_SET_PERMANENT_WATTS_PACK",
                                    "params": {"permanentWatts": int(letzte_berechnete_einspeisung * 10)}
                                }
                                call_ecoflow_api(url_quota, 'PUT', cmd_params, access_key, secret_key)
                                print(f"====> BEFEHL GESENDET: PowerStream auf {letzte_berechnete_einspeisung} W.")
                            else:
                                print("-> Keine Anpassung nötig.")
                        else:
                            print("Konnte permanentWatts-Wert im Datensatz nicht finden.")
                    else:
                        print(f"Fehler bei PowerStream-Abfrage. API-Antwort: {ps_res}")
                else:
                    print("Konnte sumInWatts-Wert im Datensatz nicht finden.")
            else:
                print(f"API Fehler-Antwort: {sm_res}")
                
        except Exception as e:
            print(f"Fehler im Regelkreis: {e}")
            
        time.sleep(1)
