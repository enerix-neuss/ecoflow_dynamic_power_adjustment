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

def flatten_dict_ecoflow(obj, pre=""):
    """
    Formatiert Daten exakt nach dem EcoFlow-Standard für die Signatur.
    Wandelt Listen in 'name[0]' und Dictionaries in 'name.untername' um.
    """
    result = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            result.update(flatten_dict_ecoflow(v, f"{pre}.{k}" if pre else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            result.update(flatten_dict_ecoflow(item, f"{pre}[{i}]" if pre else f"[{i}]"))
    else:
        result[pre] = obj
    return result

def call_ecoflow_api_v2(url, method, payload, access_key, secret_key):
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))
    
    headers = {
        'accessKey': access_key,
        'nonce': nonce,
        'timestamp': timestamp,
        'Content-Type': 'application/json'
    }
    
    # 1. Alle Parameter flachdrücken
    flat_params = flatten_dict_ecoflow(payload) if payload else {}
    
    # 2. Header-Werte für die Signierung hinzufügen
    flat_params['accessKey'] = access_key
    flat_params['nonce'] = nonce
    flat_params['timestamp'] = timestamp
    
    # 3. Alle Keys alphabetisch sortieren und den Signatur-String bauen
    sorted_params = sorted(flat_params.items())
    sign_str = "&".join([f"{k}={v}" for k, v in sorted_params])
    
    # 4. HMAC-SHA256 Verschlüsselung anwenden
    hashed = hmac.new(secret_key.encode('utf-8'), sign_str.encode('utf-8'), hashlib.sha256).digest()
    headers['sign'] = binascii.hexlify(hashed).decode('utf-8')
    
    try:
        if method == 'POST':
            res = requests.post(url, headers=headers, json=payload)
        elif method == 'PUT':
            res = requests.put(url, headers=headers, json=payload)
        else:
            res = requests.get(url, headers=headers, json=payload)
            
        if res.status_code == 200:
            return res.json()
        else:
            print(f"API verweigert Zugriff (Status {res.status_code}): {res.text[:300]}")
            return None
    except Exception as e:
        print(f"Netzwerkfehler bei API-Aufruf: {e}")
        return None

if __name__ == "__main__":
    # Fake-Server im Hintergrund für Render starten
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
    
    # Offizieller v2.0 Endpunkt laut Entwicklerhandbuch
    url_quota = 'https://ecoflow.com'

    letzte_berechnete_einspeisung = -1
    hochregel_zaehler = 0
    ERFORDERLICHE_ZYKLEN = 3

    while True:
        try:
            # 1. Hausverbrauch vom Smart Meter abfragen
            sm_params = {"sn": sm_serial, "quotas": ["20_1.sumInWatts"]}
            sm_res = call_ecoflow_api_v2(url_quota, 'POST', sm_params, access_key, secret_key)
            
            if sm_res and sm_res.get('code') == 0 and 'data' in sm_res:
                data_block = sm_res['data']
                val = data_block.get('20_1.sumInWatts')
                
                if val is not None:
                    haus_verbrauch = round(float(val))
                    print(f"Hausverbrauch aktuell: {haus_verbrauch} W")
                    
                    # 2. Aktuelle Einspeisung des PowerStreams abfragen
                    ps_params = {"sn": ps_serial, "quotas": ["20_1.permanentWatts"]}
                    ps_res = call_ecoflow_api_v2(url_quota, 'POST', ps_params, access_key, secret_key)
                    
                    if ps_res and ps_res.get('code') == 0 and 'data' in ps_res:
                        ps_val = ps_res['data'].get('20_1.permanentWatts')
                        
                        if ps_val is not None:
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
                                print(f"-> Last sinkt. Anpassung auf: {ziel_einspeisung} W")
                            elif ziel_einspeisung > letzte_berechnete_einspeisung:
                                hochregel_zaehler += 1
                                print(f"-> Last steigt! Peak-Filter aktiv. (Sekunde {hochregel_zaehler}/{ERFORDERLICHE_ZYKLEN})")
                                if hochregel_zaehler >= ERFORDERLICHE_ZYKLEN:
                                    letzte_berechnete_einspeisung = ziel_einspeisung
                                    print(f"   Last stabil! Erhöhe auf: {letzte_berechnete_einspeisung} W")
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
                                call_ecoflow_api_v2(url_quota, 'PUT', cmd_params, access_key, secret_key)
                                print(f"====> BEFEHL GESENDET: PowerStream auf {letzte_berechnete_einspeisung} W.")
                            else:
                                print("-> Keine Anpassung nötig.")
                        else:
                            print("Konnte permanentWatts im Datensatz nicht finden.")
                    else:
                        print(f"Fehler bei PowerStream-Abfrage. API-Antwort: {ps_res}")
                else:
