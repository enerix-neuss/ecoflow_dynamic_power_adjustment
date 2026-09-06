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
    
    headers = {
        'accessKey': key,
        'nonce': nonce,
        'timestamp': timestamp,
        'Content-Type': 'application/json'
    }
    
    sign_str = (get_qstr(get_map(params)) + '&' if params else '') + get_qstr(headers)
    headers['sign'] = hmac_sha256(sign_str, secret)
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, params=params)
        elif method == 'POST':
            # Einige EcoFlow-API-Endpunkte erwarten Parameter in der URL statt im Body
            response = requests.post(url, headers=headers, params=params)
        elif method == 'PUT':
            response = requests.put(url, headers=headers, json=params)
        
        if response.status_code == 200:
            try:
                return response.json()
            except Exception:
                print(f"API lieferte kein gültiges JSON. Text-Vorschau: {response.text[:150]}")
                return None
        else:
            print(f"API Fehler (Status {response.status_code}): {response.text[:150]}")
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
    print(" Smart-Delay EcoFlow-Nulleinspeisung Aktiviert ")
    print("==================================================")
    
    # ALTERNATIVE: Die dedizierte IoT-Open-API-Route von EcoFlow
    url_quota = 'https://ecoflow.com'

    letzte_berechnete_einspeisung = -1
    hochregel_zaehler = 0
    ERFORDERLICHE_ZYKLEN = 3

    while True:
        try:
            # 1. Hausverbrauch abfragen
            sm_params = {"sn": sm_serial, "quotas": ["20_1.sumInWatts"]}
            sm_payload = call_api('POST', url_quota, access_key, secret_key, sm_params)
            
            if sm_payload:
                if 'data' in sm_payload:
                    data_block = sm_payload['data']
                    val = data_block.get('20_1.sumInWatts') or data_block.get('20_1', {}).get('sumInWatts')
                    
                    if val is not None:
                        haus_verbrauch = round(float(val))
                        print(f"Hausverbrauch aktuell: {haus_verbrauch} W")
                        
                        # 2. Aktuelle Einspeisung abfragen
                        ps_params = {"sn": ps_serial, "quotas": ["20_1.permanentWatts"]}
                        ps_payload = call_api('POST', url_quota, access_key, secret_key, ps_params)
                        
                        if ps_payload and 'data' in ps_payload:
                            ps_data = ps_payload['data']
                            ps_val = ps_data.get('20_1.permanentWatts') or ps_data.get('20_1', {}).get('permanentWatts')
                            
                            if ps_val is not None:
                                aktuelle_einspeisung = float(ps_val)
                                if aktuelle_einspeisung > 2000:
                                    aktuelle_einspeisung = aktuelle_einspeisung / 10
                                aktuelle_einspeisung = round(aktuelle_einspeisung)
                                
                                if letzte_berechnete_einspeisung == -1:
                                    letzte_berechnete_einspeisung = aktuelle_einspeisung

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
                                        print(f"   Last stabil! Erhöhe auf: {ziel_einspeisung} W")
                                    else:
                                        print(f"   Peak blockiert. Bleibe auf: {letzte_berechnete_einspeisung} W")
                                else:
                                    hochregel_zaehler = 0

                                # 4. Wert senden
                                if letzte_berechnete_einspeisung != aktuelle_einspeisung:
                                    cmd_params = {
                                        "sn": ps_serial,
                                        "cmdCode": "WN511_SET_PERMANENT_WATTS_PACK",
                                        "params": {"permanentWatts": int(letzte_berechnete_einspeisung * 10)}
                                    }
                                    call_api('PUT', url_quota, access_key, secret_key, cmd_params)
                                    print(f"====> BEFEHL GESENDET: PowerStream auf {letzte_berechnete_einspeisung} W angepasst.")
                                else:
                                    print("-> Keine Anpassung nötig.")
                            else:
                                print("Konnte permanentWatts-Wert nicht finden.")
                        else:
                            print("Keine Antwort vom PowerStream.")
                    else:
                        print("Konnte sumInWatts-Wert nicht finden.")
                else:
                    print(f"API Fehler-Antwort: {sm_payload}")
            else:
                print("Keine Antwort von der API erhalten.")
                
        except Exception as e:
            print(f"Fehler im Regelkreis: {e}")
            
        time.sleep(1)
