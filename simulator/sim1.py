#!/usr/bin/env python3
"""
ocpp-charge-point-simulator — OCPP 1.6J + Nextion 3.5" Entegrasyonu
=====================================================================
Raspberry Pi 3B+ · GPIO 14/15 (UART) · /dev/ttyS0 · 9600 baud
Nextion sayfaları: home, user_info, status, rfid_scan
"""

import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
import base64

TZ_UTC = timezone.utc

try:
    import websockets
except ImportError:
    print("[ERROR] 'websockets' eksik. Kur:  pip install websockets")
    sys.exit(1)

try:
    import serial
except ImportError:
    print("[ERROR] 'pyserial' eksik. Kur:  pip install pyserial")
    sys.exit(1)

# ─── Konfigürasyon ───────────────────────────────────────────────────────────

sys.path.insert(0, ".")
from config import (
    CSMS_URL,
    CHARGE_BOX_ID,
    USER_ID_TAG,
    NFC_ALLOWED_ID,
    VENDOR, MODEL, SERIAL, FIRMWARE,
    HEARTBEAT_INTERVAL,
    METER_INCREMENT_WH,
    DEFAULT_VOLTAGE,
    DEFAULT_CURRENT,
    SUBPROTOCOL,
    PING_INTERVAL,
    BASIC_AUTH_USER,
    BASIC_AUTH_PASSWORD,
    NEXTION_PORT,
    NEXTION_BAUDRATE,
    PIC_CAR_CONNECTED,
    PIC_CAR_DISCONNECTED,
    MAX_POWER_KW,
    COST_PER_KWH,
)

# ─── Runtime State ────────────────────────────────────────────────────────────

msg_id         = 1
transaction_id = None
meter_wh       = 0
hb_interval    = HEARTBEAT_INTERVAL
hb_task        = None

# Şarj durumu
charge_start_time    = None     # şarj başladığında set edilir
transaction_end_time = None     # şarj durdurulduğunda set edilir
charging_active      = False    # şarj devam ediyor mu?

# Bağlantı durumu
is_connected = False
current_status = "NOT CONNECTED"

# ─── ANSI Renk Kodları ────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
DIM    = "\033[2m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(direction: str, msg: str):
    colors = {"SEND": CYAN, "RECV": GREEN, "INFO": DIM, "WARN": YELLOW, "ERR": RED}
    c = colors.get(direction, RESET)
    print(f"{DIM}{_ts()}{RESET}  {c}{BOLD}{direction:<4}{RESET}  {msg}")



# ─── NFC Kimlik Doğrulama ─────────────────────────────────────────────────────

def wait_for_nfc_auth():
    """
    BootNotification gönderilmeden önce NFC kart doğrulaması.

    PN532 (I2C) üzerinden kart okur. config/__init__.py içindeki
    NFC_ALLOWED_ID ile eşleşen kart okutulana kadar simülasyon başlamaz.
    Doğrulama başarılı olunca simülasyon devam eder; main() içinde
    WebSocket bağlantısı kurulunca DEFAULT_ID_TAG id.txt'e yazılır.
    Yanlış kart okutulursa uyarı verir ve tekrar bekler.
    Ctrl+C ile çıkılabilir.
    """
    from nfc_read import init_pn532, read_uid

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║     NFC Kimlik Doğrulama             ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════╝{RESET}")
    print(f"{DIM}İzin verilen kart UID : {NFC_ALLOWED_ID}{RESET}")
    print(f"{YELLOW}Kartı okuyucuya yaklaştırın...{RESET}\n")

    init_pn532()
    nxt("page rfid_scan")

    while True:
        try:
            uid = read_uid()
            if uid:
                uid_str = uid.hex().upper()
                if uid_str == NFC_ALLOWED_ID:
                    print(f"{GREEN}{BOLD}✓ Kart onaylandı  : {uid_str}{RESET}")
                    print(f"{GREEN}  Simülasyon başlıyor...{RESET}\n")
                    nxt("page home")
                    return  # Doğrulama başarılı → simülasyon devam eder
                else:
                    print(f"{RED}✗ Geçersiz kart   : {uid_str}  —  Tekrar deneyin{RESET}")
            time.sleep(0.1)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Çıkış yapılıyor...{RESET}")
            nxt("page rfid_scan")
            time.sleep(0.5)
            sys.exit(0)


# ─── Nextion Serial Bağlantısı ────────────────────────────────────────────────

_nxt_serial = None

def nextion_open():
    """Serial portu aç. Hata olursa uyar ama çökme."""
    global _nxt_serial
    try:
        _nxt_serial = serial.Serial(
            NEXTION_PORT,
            NEXTION_BAUDRATE,
            timeout=0.1
        )
        log("INFO", f"Nextion bağlandı → {NEXTION_PORT} @ {NEXTION_BAUDRATE}")
    except Exception as e:
        log("WARN", f"Nextion açılamadı: {e} — ekran komutları devre dışı")
        _nxt_serial = None


def nxt(cmd: str):
    """Nextion'a komut gönder. Her komut \xff\xff\xff ile biter."""
    if _nxt_serial is None:
        return
    try:
        _nxt_serial.write((cmd + "\xff\xff\xff").encode("latin-1"))
    except Exception as e:
        log("WARN", f"Nextion yazma hatası: {e}")


# ─── Nextion UI Güncelleme Fonksiyonları ─────────────────────────────────────

def nxt_set_time():
    """home sayfası saat → UTC saati."""
    utc = datetime.now(TZ_UTC).strftime("%H:%M:%S")
    nxt(f'saat.txt="{utc}"')


def nxt_set_status(status: str):
    """
    home sayfası con objesi + araba görseli.
    status: 'NOT CONNECTED' | 'CONNECTED' | 'AVAILABLE' | 'CHARGING'
    """
    global current_status
    current_status = status
    
    colors = {
        "NOT CONNECTED": 63488,   # kırmızı
        "CONNECTED":     2047,    # cyan
        "AVAILABLE":     2047,    # cyan
        "CHARGING":      11939,   # yeşil
    }
    
    # Ekranda göstereceğimiz yazıyı belirleme
    text_to_show = status
    if status == "CONNECTED":
        text_to_show = "AVAILABLE"  # WebSocket bağlı ama idle

    if status == "CHARGING":
        pic = 0
    elif status == "NOT CONNECTED":
        pic = PIC_CAR_DISCONNECTED
    else:
        pic = PIC_CAR_CONNECTED

    pco = colors.get(status, 63488)
    nxt(f'con.txt="{text_to_show}"')
    nxt(f"con.pco={pco}")
    nxt(f"araba.pic={pic}")


def nxt_set_user_id(id_tag: str):
    """Ana sayfa userinfo objesine idTag yazar."""
    nxt(f'userinfo.txt="{id_tag}"')


async def nextion_read_loop():
    """Nextion'dan gelen buton olaylarını dinle (Touch Event 0x65)."""
    loop = asyncio.get_event_loop()
    buf  = bytearray()
    while True:
        if _nxt_serial is None:
            await asyncio.sleep(0.1)
            continue
        try:
            chunk = await loop.run_in_executor(None, _nxt_serial.read, 32)
            if chunk:
                buf.extend(chunk)
            # 0x65 touch event paketi: [0x65, page, comp, event, 0xFF, 0xFF, 0xFF] = 7 byte
            while len(buf) >= 7:
                idx = buf.find(0x65)
                if idx == -1:
                    buf.clear()
                    break
                if idx > 0:
                    del buf[:idx]
                if len(buf) < 7:
                    break
                if buf[4] == 0xFF and buf[5] == 0xFF and buf[6] == 0xFF:
                    page_id = buf[1]
                    comp_id = buf[2]
                    event   = buf[3]   # 0x01 = press, 0x00 = release
                    del buf[:7]
                    if event == 0x01:
                        log("INFO", f"Nextion touch → page={page_id} comp={comp_id}")
                        # useridtag butonu: hangi sayfada / component ise buraya düş
                        # Nextion Editor'da butonun "id" değerine göre comp_id'yi eşle
                        if comp_id == 2:   # ← useridtag butonunun component ID'si
                            log("INFO", f"useridtag butonu → id yazılıyor: {USER_ID_TAG}")
                            nxt_set_user_id(USER_ID_TAG)
                else:
                    del buf[:1]   # bozuk paket, bir byte atla
        except Exception as e:
            log("WARN", f"Nextion okuma hatası: {e}")
            await asyncio.sleep(0.1)


def nxt_update_status():
    """
    status sayfası güncelle:
      power  : 22kW (21.85 kW yazılacak sadece)
      time   : HH.MM.SS (Start ile Stop arası)
      energy : Matematiksel hesap (kWh) 
      cost   : 0.19 $ / kWh
    """
    if charge_start_time is not None:
        if charging_active:
            elapsed = time.time() - charge_start_time
        else:
            if transaction_end_time:
                elapsed = transaction_end_time - charge_start_time
            else:
                elapsed = 0
                
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        nxt(f'time.txt="TIME : {h:02d}.{m:02d}.{s:02d}"')

        # 22kW limit -> saniyede 22/3600 kWh
        energy_kwh = (elapsed * MAX_POWER_KW) / 3600.0
        nxt(f'energy.txt="amount of use : {energy_kwh:.2f} kWh"')

        cost = energy_kwh * COST_PER_KWH
        nxt(f'cost.txt="{cost:.2f} $"')
        
        if charging_active:
            nxt('power.txt="POWER : 21.85 kW"')
        else:
            nxt('power.txt="POWER : 0.00 kW"')
    else:
        nxt('time.txt="TIME : 00.00.00"')
        nxt('energy.txt="amount of use : 0.00 kWh"')
        nxt('cost.txt="0.00 $"')
        nxt('power.txt="POWER : 0.00 kW"')


# ─── Yardımcı Fonksiyonlar ────────────────────────────────────────────────────

def next_id() -> str:
    global msg_id
    i = msg_id
    msg_id += 1
    return str(i)


def iso_now() -> str:
    return datetime.now(TZ_UTC).isoformat().replace("+00:00", "Z")


# ─── OCPP Gönderme ───────────────────────────────────────────────────────────

async def send(ws, action: str, payload: dict) -> str:
    mid = next_id()
    msg = json.dumps([2, mid, action, payload])
    await ws.send(msg)
    log("SEND", f"[{action}] {payload}")
    return mid


async def send_result(ws, mid: str, payload: dict):
    msg = json.dumps([3, mid, payload])
    await ws.send(msg)
    log("SEND", f"[Response/{mid}] {payload}")


# ─── OCPP 1.6 Mesajları ──────────────────────────────────────────────────────

async def boot_notification(ws):
    await send(ws, "BootNotification", {
        "chargePointVendor":       VENDOR,
        "chargePointModel":        MODEL,
        "chargePointSerialNumber": SERIAL,
        "firmwareVersion":         FIRMWARE,
    })


async def heartbeat(ws):
    await send(ws, "Heartbeat", {})


async def status_notification(ws, connector_id: int, status: str, error_code: str = "NoError"):
    await send(ws, "StatusNotification", {
        "connectorId": connector_id,
        "status":      status,
        "errorCode":   error_code,
        "timestamp":   iso_now(),
    })
    # Nextion con objesini OCPP status ile güncelle
    nxt_set_status(status.upper())


async def authorize(ws, id_tag: str = USER_ID_TAG):
    await send(ws, "Authorize", {"idTag": id_tag})


async def start_transaction(ws, id_tag: str = USER_ID_TAG):
    global meter_wh, charging_active, charge_start_time, transaction_end_time
    meter_wh = 0
    charge_start_time = time.time()
    transaction_end_time = None
    charging_active   = True
    nxt_set_status("CHARGING")
    nxt_update_status() # Reset UI quickly
    await send(ws, "StartTransaction", {
        "connectorId": 1,
        "idTag":       id_tag,
        "meterStart":  meter_wh,
        "timestamp":   iso_now(),
    })


async def stop_transaction(ws):
    global transaction_id, meter_wh, charging_active, transaction_end_time
    if transaction_id is None:
        log("WARN", "Aktif işlem yok!")
        return
        
    # Transaction sonu anındaki enerjiyi hesapla
    elapsed = time.time() - charge_start_time
    meter_wh = int((elapsed * MAX_POWER_KW * 1000) / 3600)
    
    charging_active = False
    transaction_end_time = time.time()
    nxt_set_status("AVAILABLE")
    
    await send(ws, "StopTransaction", {
        "transactionId": transaction_id,
        "idTag":         USER_ID_TAG,
        "meterStop":     meter_wh,
        "timestamp":     iso_now(),
        "reason":        "Local",
    })
    nxt_update_status()
    log("INFO", f"Şarj durduruldu — İşlem sonu, Gönderilen Meter: {meter_wh} Wh")


async def meter_values(ws):
    """MeterValues gönder ve Nextion'ı güncelle."""
    global meter_wh, transaction_id

    if not charging_active or not charge_start_time:
        log("WARN", "Şarj aktif değil — MeterValues gönderilmedi")
        return

    elapsed = time.time() - charge_start_time
    meter_wh = int((elapsed * MAX_POWER_KW * 1000) / 3600)  # Wh cinsinden enerji miktarı

    payload = {
        "connectorId": 1,
        "meterValue": [{
            "timestamp": iso_now(),
            "sampledValue": [
                {"value": str(meter_wh),        "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                {"value": str(DEFAULT_VOLTAGE),  "measurand": "Voltage",        "unit": "V"},
                {"value": str(DEFAULT_CURRENT),  "measurand": "Current.Import", "unit": "A"},
                {"value": str(int(MAX_POWER_KW * 1000)), "measurand": "Power.Active.Import", "unit": "W"}
            ]
        }]
    }
    if transaction_id:
        payload["transactionId"] = transaction_id

    await send(ws, "MeterValues", payload)

    # Nextion güncelle
    nxt_update_status()
    log("INFO", f"MeterValues Gönderildi: {meter_wh} Wh")


# ─── Periyodik Görevler ───────────────────────────────────────────────────────

async def heartbeat_loop(ws, interval: int):
    log("INFO", f"Heartbeat başladı — her {interval}s")
    while True:
        await asyncio.sleep(interval)
        try:
            await heartbeat(ws)
        except Exception:
            break


async def clock_loop():
    """Her saniye Nextion home sayfasındaki saati güncelle."""
    while True:
        nxt_set_time()
        # Her koşulda ekrandaki değerlerin korunmasını garantileyelim
        nxt_set_status(current_status)
        nxt_set_user_id(USER_ID_TAG)
        await asyncio.sleep(1)


async def status_update_loop():
    """Her saniye status sayfasını güncelle."""
    while True:
        nxt_update_status()
        await asyncio.sleep(1)


# ─── Gelen Mesaj İşleyici ─────────────────────────────────────────────────────

async def handle_message(ws, raw: str):
    global transaction_id, hb_interval, hb_task, is_connected

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log("ERR", f"JSON parse hatası: {raw}")
        return

    msg_type = msg[0]
    mid      = msg[1]

    if msg_type == 3:
        payload = msg[2]
        log("RECV", f"[Response/{mid}] {payload}")

        # BootNotification → heartbeat aralığı güncelle
        if "interval" in payload and "status" in payload:
            status   = payload["status"]
            interval = payload.get("interval", hb_interval)
            log("INFO", f"BootNotification: status={status}, interval={interval}s")
            if status == "Accepted":
                hb_interval = interval
                if hb_task:
                    hb_task.cancel()
                hb_task = asyncio.create_task(heartbeat_loop(ws, hb_interval))

        # StartTransaction → transactionId kaydet
        if "transactionId" in payload:
            transaction_id = payload["transactionId"]
            log("INFO", f"Transaction ID={transaction_id}")

    elif msg_type == 2:
        action  = msg[2]
        payload = msg[3] if len(msg) > 3 else {}
        log("RECV", f"[{action}] ← Sunucu: {payload}")

        responses = {
            "GetConfiguration":       {"configurationKey": [], "unknownKey": []},
            "ChangeConfiguration":    {"status": "Accepted"},
            "Reset":                  {"status": "Accepted"},
            "RemoteStartTransaction": {"status": "Accepted"},
            "RemoteStopTransaction":  {"status": "Accepted"},
            "TriggerMessage":         {"status": "Accepted"},
            "UnlockConnector":        {"status": "Unlocked"},
            "ClearCache":             {"status": "Accepted"},
        }
        await send_result(ws, mid, responses.get(action, {}))

    elif msg_type == 4:
        log("ERR", f"CALLERROR [{mid}]: {msg[2]} — {msg[3]}")


# ─── Konsol Menüsü ────────────────────────────────────────────────────────────

def print_menu():
    print(f"""
{BOLD}{CYAN}┌──────────────────────────────────────────────┐{RESET}
{BOLD}{CYAN}│  OCPP 1.6J Simülatör · Nextion Entegrasyonu  │{RESET}
{BOLD}{CYAN}└──────────────────────────────────────────────┘{RESET}
  {BOLD}1{RESET}  BootNotification
  {BOLD}2{RESET}  Heartbeat (manual)
  {BOLD}3{RESET}  StatusNotification → Available
  {BOLD}4{RESET}  StatusNotification → Charging
  {BOLD}5{RESET}  Authorize ({USER_ID_TAG})
  {BOLD}6{RESET}  StartTransaction   [şarjı başlat]
  {BOLD}7{RESET}  MeterValues        [+500 Wh, +%5]
  {BOLD}8{RESET}  StopTransaction    [şarjı durdur]
  {BOLD}m{RESET}  Menüyü göster
  {BOLD}q{RESET}  Çıkış
""")


async def console_input(ws):
    loop = asyncio.get_event_loop()
    print_menu()
    while True:
        try:
            choice = await loop.run_in_executor(None, input, f"\n{BOLD}>{RESET} ")
            choice = choice.strip().lower()

            if choice == "1":
                await boot_notification(ws)
            elif choice == "2":
                await heartbeat(ws)
            elif choice == "3":
                await status_notification(ws, 1, "Available")
            elif choice == "4":
                await status_notification(ws, 1, "Charging")
            elif choice == "5":
                await authorize(ws)
            elif choice == "6":
                await start_transaction(ws)
            elif choice == "7":
                await meter_values(ws)
            elif choice == "8":
                await stop_transaction(ws)
            elif choice in ("q", "quit", "exit"):
                log("INFO", "Çıkılıyor...")
                nxt_set_status("NOT CONNECTED")
                nxt("page rfid_scan")
                time.sleep(0.5)
                sys.exit(0)
            elif choice == "m":
                print_menu()
            else:
                log("WARN", f"Bilinmeyen komut: '{choice}' — 'm' ile menüye bak")

        except (EOFError, KeyboardInterrupt):
            break


# ─── Alma Döngüsü ─────────────────────────────────────────────────────────────

async def recv_loop(ws):
    try:
        async for message in ws:
            await handle_message(ws, message)
    except websockets.exceptions.ConnectionClosed as e:
        log("WARN", f"Bağlantı kapandı: code={e.code}")


# ─── Ana Fonksiyon ────────────────────────────────────────────────────────────

async def main():
    global hb_task, is_connected

    print(f"\n{BOLD}OCPP 1.6J Simülatör · Nextion 3.5\" Entegrasyonu{RESET}")
    print(f"{DIM}Bağlanılıyor: {CSMS_URL}{RESET}\n")

    # Başlangıç ekran durumu
    nxt_set_status("NOT CONNECTED")
    nxt_set_time()
    # NFC doğrulaması geçildi → config'deki idTag'i ana sayfa userinfo.txt'e yaz
    nxt_set_user_id(USER_ID_TAG)

    def handle_sigint(*_):
        log("INFO", "Ctrl+C — bağlantı kesiliyor...")
        nxt_set_status("NOT CONNECTED")
        nxt("page rfid_scan")
        time.sleep(0.5)
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_sigint)

    try:
        _credentials = base64.b64encode(
            f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}".encode()
        ).decode()

        async with websockets.connect(
            CSMS_URL,
            subprotocols=[SUBPROTOCOL],
            ping_interval=PING_INTERVAL,
            extra_headers={"Authorization": f"Basic {_credentials}"},
        ) as ws:
            is_connected = True
            log("INFO", f"Bağlandı ✓  ({CSMS_URL})")

            # Nextion'ı güncelle — bağlantı kurulunca otomatik
            nxt_set_status("CONNECTED")
            nxt_set_user_id(USER_ID_TAG)   # ana sayfa userinfo objesi kalıcı yazılır

            # Otomatik BootNotification
            await boot_notification(ws)

            # Paralel görevler
            recv_task    = asyncio.create_task(recv_loop(ws))
            input_task   = asyncio.create_task(console_input(ws))
            clock_task   = asyncio.create_task(clock_loop())
            status_task  = asyncio.create_task(status_update_loop())
            nextion_task = asyncio.create_task(nextion_read_loop())

            await asyncio.gather(recv_task, input_task, clock_task, status_task, nextion_task)

    except OSError as e:
        log("ERR", f"Bağlantı başarısız: {e}")
        log("INFO", "CSMS çalışıyor mu? URL doğru mu?")
        nxt_set_status("NOT CONNECTED")
    except Exception as e:
        log("ERR", f"Beklenmeyen hata: {e}")
        nxt_set_status("NOT CONNECTED")


# ─── Giriş Noktası ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 0. İlk olarak Nextion bağlantısını başlatıyoruz
    nextion_open()
    # 1. NFC kart doğrulaması — geçerli kart okutulana kadar bloke eder ve rfid_scan sayfasını açar
    wait_for_nfc_auth()
    # 2. Doğrulama başarılı → OCPP simülatörünü başlat
    asyncio.run(main())
