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

def ecoflow_sign(params, headers, secret_key):
    sorted_params = sorted(params.items()) if params else []
    query_str = "&".join([f"{k}={v}" for k, v in sorted_params])
    
    sign_headers = {
        'accessKey': headers['accessKey'],
        'nonce': headers['nonce'],
        'timestamp': headers['timestamp']
    }
    sorted_headers = sorted(sign_headers.items())
    header_str = "&".join([f"{k}={v}" for k, v in sorted_headers])
    
    if query_str:
        final_sign_str = f"{query_str}&{header_str}"
    else:
        final_sign_str = header_str
        
    hashed = hmac.new(secret_key.encode('utf-8'), final_sign_str.encode('utf-8'), hashlib.sha256).digest()
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
    
    headers['sign'] = ecoflow_sign(params, headers, secret_key)
    
    try:
        if method == 'GET':
            res = requests.get(url, headers=headers, params=params)
        elif method == 'PUT':
            res = requests.put(url, headers=headers, json=params)
        
        if res.status_code == 200:
            return res.json()
        else:
            print(f"API Fehler (Status {res.status_code}): {res.text[:200]}")
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
    print(" Offizielle EcoFlow API-Nulleinspeisung Aktiv ")
    print("==================================================")
    
    url_all_quota = 'https://ecoflow.com'
    url_set = 'https://ecoflow.com'

    letzte_berechnete_einspeisung = -1
    hochregel_zaehler = 0
    ERFORDERLICHE_ZYKLEN = 3

    while True:
        try:
            sm_res = call_ecoflow_api(url_all_quota, 'GET', {"sn": sm_serial}, access_key, secret_key)
            
            if sm_res and sm_res.get('code') == 0 and 'data' in sm_res:
                data = sm_res['data']
                val = data.get('20_1.sumInWatts') or data.get('20_1.wValue')
                
                if val is not None:
                    haus_verbrauch = round(float(val))
                    if haus_verbrauch > 2500:
                        haus_verbrauch = round(haus_verbrauch / 10)
                        
                    print(f"Hausverbrauch aktuell: {haus_verbrauch} W")
                    
                    ps_res = call_ecoflow_api(url_all_quota, 'GET', {"sn": ps_serial}, access_key, secret_key)
                    
                    if ps_res and ps_res.get('code') == 0 and 'data' in ps_res:
                        ps_data = ps_res['data']
                        ps_val = ps_data.get('20_1.permanentWatts')
                        
                        if ps_val is not None:
                            aktuelle_einspeisung = round(float(ps_val) / 10)
                            
                            if letzte_berechnete_einspeisung == -1:
                                letzte_berechnete_einspeisung = aktuelle_einspeisung

                            ziel_einspeisung = aktuelle_einspeisung + haus_verbrauch + offset
                            if ziel_einspeisung < 0: ziel_einspeisung = 0
                            if ziel_einspeisung > 800: ziel_einspeisung = 800
                            
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

                            if letzte_berechnete_einspeisung != aktuelle_einspeisung:
                                cmd_params = {
                                    "sn": ps_serial,
                                    "cmdCode": "WN511_SET_PERMANENT_WATTS_PACK",
                                    "params": {"permanentWatts": int(letzte_berechnete_einspeisung * 10)}
                                }
                                call_ecoflow_api(url_set, 'PUT', cmd_params, access_key, secret_key)
                                print(f"====> BEFEHL GESENDET: PowerStream auf {letzte_berechnete_einspeisung} W.")
                            else:
                                print("-> Keine Anpassung nötig.")
                        else:
                            print("Konnte permanentWatts im PowerStream nicht finden.")
                    else:
                        print(f"Fehler bei PowerStream-Abfrage: {ps_res}")
                else:
                    print("Konnte Verbrauchswert im Smart Meter nicht finden.")
            else:
                print(f"API verweigert Zugriff oder Zähler offline. Rückgabe: {sm_res}")
                
        except Exception as e:
            print(f"Fehler im Regelkreis: {e}")
            
        time.sleep(1)
