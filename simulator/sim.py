#!/usr/bin/env python3
"""
ocpp-charge-point-simulator — OCPP 1.6J + Nextion 3.5" Entegrasyonu
=====================================================================
Raspberry Pi 3B+ · GPIO 14/15 UART · /dev/ttyAMA0 · 9600 baud
Nextion nx4832t035 sayfaları:
  Sayfa 0 (home)      : con, percent, araba
  Sayfa 1 (user_info) : id
  Sayfa 2 (status)    : power, time, energy, cost
  Sayfa 3 (rfid_scan) : sadece görsel
"""

import asyncio
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
import base64

TZ_TR = timezone(timedelta(hours=3))

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

# ─── Konfigürasyon ────────────────────────────────────────────────────────────
# __init__.py dosyası bu klasörde bir paket config olarak kullanılıyor.
# Doğrudan import: python sim.py şeklinde çalıştırıldığında sys.path[0] = proje klasörü.

import importlib, os
_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
_spec = importlib.util.spec_from_file_location("_cfg", _cfg_path)
_cfg  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cfg)

CSMS_URL            = _cfg.CSMS_URL
CHARGE_BOX_ID       = _cfg.CHARGE_BOX_ID
DEFAULT_ID_TAG      = _cfg.DEFAULT_ID_TAG
VENDOR              = _cfg.VENDOR
MODEL               = _cfg.MODEL
SERIAL              = _cfg.SERIAL
FIRMWARE            = _cfg.FIRMWARE
HEARTBEAT_INTERVAL  = _cfg.HEARTBEAT_INTERVAL
METER_INCREMENT_WH  = _cfg.METER_INCREMENT_WH
DEFAULT_VOLTAGE     = _cfg.DEFAULT_VOLTAGE
DEFAULT_CURRENT     = _cfg.DEFAULT_CURRENT
SUBPROTOCOL         = _cfg.SUBPROTOCOL
PING_INTERVAL       = _cfg.PING_INTERVAL
BASIC_AUTH_USER     = _cfg.BASIC_AUTH_USER
BASIC_AUTH_PASSWORD = _cfg.BASIC_AUTH_PASSWORD
NEXTION_PORT        = _cfg.NEXTION_PORT
NEXTION_BAUDRATE    = _cfg.NEXTION_BAUDRATE
COLOR_NOT_CONNECTED = _cfg.COLOR_NOT_CONNECTED
COLOR_CONNECTED     = _cfg.COLOR_CONNECTED
COLOR_AVAILABLE     = _cfg.COLOR_AVAILABLE
COLOR_CHARGING      = _cfg.COLOR_CHARGING
PIC_CAR_CONNECTED   = _cfg.PIC_CAR_CONNECTED
PIC_CAR_DISCONNECTED= _cfg.PIC_CAR_DISCONNECTED
BATTERY_CAPACITY_WH = _cfg.BATTERY_CAPACITY_WH
INITIAL_CHARGE_WH   = _cfg.INITIAL_CHARGE_WH
WH_PER_STEP         = _cfg.WH_PER_STEP
TL_PER_500WH        = _cfg.TL_PER_500WH

# ─── Runtime State ────────────────────────────────────────────────────────────

msg_id         = 1
transaction_id = None
meter_wh       = 0          # OCPP meterStart / meterStop değeri (Wh)
hb_interval    = HEARTBEAT_INTERVAL
hb_task        = None

# Şarj durum izleyicileri
charging_active   = False      # şarj devam ediyor mu?
charge_start_time = None       # şarj başladığı Unix timestamp
charge_percent    = 0          # anlık % doluluk (0–100)
total_energy_wh   = 0          # bu oturumda çekilen toplam enerji (Wh)
total_cost        = 0.0        # bu oturumun toplam ücreti (TL)

# Bağlantı durumu
is_connected = False

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


# ─── Nextion Serial Bağlantısı ───────────────────────────────────────────────

_nxt_serial = None
_nxt_lock   = threading.Lock()      # thread-safe yazma


def nextion_open():
    """Serial portu aç. Hata olursa uyar ama çökme — OCPP devam eder."""
    global _nxt_serial
    try:
        _nxt_serial = serial.Serial(
            NEXTION_PORT,
            NEXTION_BAUDRATE,
            timeout=0.1
        )
        log("INFO", f"Nextion bağlandı → {NEXTION_PORT} @ {NEXTION_BAUDRATE} baud")
    except Exception as e:
        log("WARN", f"Nextion açılamadı ({NEXTION_PORT}): {e} — ekran komutları devre dışı")
        _nxt_serial = None


def nxt(cmd: str):
    """
    Nextion'a tek komut gönder.
    Format: component.attribute="value"\xff\xff\xff
    Sayısal attribute'lar için tırnak olmadan: component.attribute=value\xff\xff\xff
    """
    if _nxt_serial is None:
        return
    with _nxt_lock:
        try:
            packet = cmd.encode("latin-1") + b"\xff\xff\xff"
            _nxt_serial.write(packet)
        except Exception as e:
            log("WARN", f"Nextion yazma hatası: {e}")


# ─── Nextion UI Güncelleme Fonksiyonları ─────────────────────────────────────

def nxt_set_status(status: str):
    """
    Sayfa 0 (home): con txt objesi + araba picture objesi.
    status → 'NOT CONNECTED' | 'CONNECTED' | 'AVAILABLE' | 'CHARGING'

    Renk kodları (RGB565, config.py'den):
      NOT CONNECTED → 63488 kırmızı
      CONNECTED     → 16354 yeşil
      AVAILABLE     → 16354 yeşil
      CHARGING      → 2047  cyan
    """
    color_map = {
        "NOT CONNECTED": COLOR_NOT_CONNECTED,
        "CONNECTED":     COLOR_CONNECTED,
        "AVAILABLE":     COLOR_AVAILABLE,
        "CHARGING":      COLOR_CHARGING,
    }
    pco = color_map.get(status, COLOR_NOT_CONNECTED)
    pic = PIC_CAR_DISCONNECTED if status == "NOT CONNECTED" else PIC_CAR_CONNECTED

    # txt objesi: tırnaklı string
    nxt(f'con.txt="{status}"')
    # pco (ön plan rengi) sayısal attribute, tırnak yok
    nxt(f"con.pco={pco}")
    # picture objesi pic attribute: sayısal, tırnak yok
    nxt(f"araba.pic={pic}")


def nxt_set_charge_percent(pct: int):
    """Sayfa 0 (home): percent txt objesi → şarj yüzdesi göster."""
    nxt(f'percent.txt="%{pct}"')


def nxt_set_user_id(id_tag: str):
    """Sayfa 1 (user_info): id txt objesi → kullanıcı ID tag."""
    nxt(f'id.txt="{id_tag}"')


def nxt_update_status_page():
    """
    Sayfa 2 (status): power, time, energy, cost güncelle.
    Şarj aktif değilse donmuş son değerleri ekranda bırakır (hiçbir şey yazmaz).
    MeterValues her geldiğinde ve status_update_loop her saniyede çağırır.
    """
    power_kw = round(DEFAULT_VOLTAGE * DEFAULT_CURRENT / 1000, 2)
    nxt(f'power.txt="POWER : {power_kw} KW"')

    if charge_start_time is not None:
        elapsed = int(time.time() - charge_start_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        nxt(f'time.txt="TIME : {h:02d}:{m:02d}:{s:02d}"')

        # Gerçek zamanlı enerji: kW × geçen saat
        energy_kwh = round(power_kw * elapsed / 3600, 4)
        nxt(f'energy.txt="ENERGY: {energy_kwh} kWh"')

        # Ücret: enerji(Wh) / WH_PER_STEP × TL_PER_500WH
        cost = round((energy_kwh * 1000 / WH_PER_STEP) * TL_PER_500WH, 2)
        nxt(f'cost.txt="COST : {cost} TL"')
    else:
        # Başlamadan sıfırla
        nxt('time.txt="TIME : 00:00:00"')
        nxt('energy.txt="ENERGY: 0.0000 kWh"')
        nxt('cost.txt="COST : 0.00 TL"')


def nxt_set_clock():
    """Sayfa 0 (home): saat txt objesi → TR saati (UTC+3)."""
    tr = datetime.now(TZ_TR).strftime("%H:%M:%S")
    nxt(f'saat.txt="{tr}"')


# ─── Yardımcı Fonksiyonlar ────────────────────────────────────────────────────

def next_id() -> str:
    global msg_id
    i = msg_id
    msg_id += 1
    return str(i)


def iso_now() -> str:
    """OCPP zaman damgası: ISO 8601, UTC+3 → Z suffix ile."""
    return datetime.now(TZ_TR).isoformat().replace("+03:00", "Z")


def _calc_charge_percent(cumulative_wh: int) -> int:
    """
    Şarj yüzdesi hesabı:
      - Araç bataryası: BATTERY_CAPACITY_WH (3680 Wh = %100)
      - Başlangıç doluluk: INITIAL_CHARGE_WH (1840 Wh = %50)
      - Bu oturumda çekilen enerji cumulative_wh Wh ekleniyor.
      - Toplam doluluk = INITIAL_CHARGE_WH + cumulative_wh
      - % = toplam_dolu / BATTERY_CAPACITY_WH * 100
    Sonuç 0–100 aralığında sınırlanır.
    """
    total_filled = INITIAL_CHARGE_WH + cumulative_wh
    pct = int((total_filled / BATTERY_CAPACITY_WH) * 100)
    return max(0, min(100, pct))


# ─── OCPP Gönderme ────────────────────────────────────────────────────────────

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


# ─── OCPP 1.6 Mesajları ───────────────────────────────────────────────────────

async def boot_notification(ws):
    """BootNotification gönder. Nextion bağlantı durumu güncellenmez (zaten bağlı)."""
    await send(ws, "BootNotification", {
        "chargePointVendor":       VENDOR,
        "chargePointModel":        MODEL,
        "chargePointSerialNumber": SERIAL,
        "firmwareVersion":         FIRMWARE,
    })


async def heartbeat(ws):
    await send(ws, "Heartbeat", {})


async def status_notification(ws, connector_id: int, status: str, error_code: str = "NoError"):
    """
    OCPP StatusNotification gönder.
    Nextion con objesi OCPP status ile güncellenir:
      Available → AVAILABLE (yeşil)
      Charging  → CHARGING  (cyan)
      diğerleri → CONNECTED (yeşil, bağlı ama belirsiz)
    """
    await send(ws, "StatusNotification", {
        "connectorId": connector_id,
        "status":      status,
        "errorCode":   error_code,
        "timestamp":   iso_now(),
    })
    # OCPP status string'ini Nextion state'iyle eşleştir
    nextion_state_map = {
        "Available":    "AVAILABLE",
        "Charging":     "CHARGING",
        "Finishing":    "CONNECTED",
        "Preparing":    "CONNECTED",
        "SuspendedEV":  "CONNECTED",
        "SuspendedEVSE":"CONNECTED",
        "Unavailable":  "NOT CONNECTED",
        "Faulted":      "NOT CONNECTED",
    }
    nextion_status = nextion_state_map.get(status, "CONNECTED")
    nxt_set_status(nextion_status)


async def authorize(ws, id_tag: str = DEFAULT_ID_TAG):
    """
    Authorize isteği gönder.
    user_info sayfasındaki id objesine idTag yaz (yanıt beklemeden —
    isteği gönderdik, kullanıcı zaten bu tag'i kullanıyor).
    """
    log("INFO", f"Authorize isteği: idTag={id_tag}")
    nxt_set_user_id(id_tag)   # Sayfa 1 (user_info): id objesi ← idTag
    await send(ws, "Authorize", {"idTag": id_tag})


async def start_transaction(ws, id_tag: str = DEFAULT_ID_TAG):
    """
    StartTransaction gönder.
    - charge_percent = _calc_charge_percent(0) = %50 (INITIAL_CHARGE_WH / BATTERY_CAPACITY_WH)
    - percent objesi ekranda %50 ile başlar
    - total_energy_wh sıfırlanır (bu oturum için)
    """
    global meter_wh, charging_active, charge_start_time, charge_percent
    global total_energy_wh, total_cost

    charge_start_time = time.time()
    charging_active   = True
    total_energy_wh   = 0
    total_cost        = 0.0

    # Başlangıç % = INITIAL_CHARGE_WH / BATTERY_CAPACITY_WH * 100 = 50
    charge_percent = _calc_charge_percent(0)
    nxt_set_charge_percent(charge_percent)
    nxt_set_status("CHARGING")

    await send(ws, "StartTransaction", {
        "connectorId": 1,
        "idTag":       id_tag,
        "meterStart":  meter_wh,
        "timestamp":   iso_now(),
    })


async def stop_transaction(ws):
    """
    StopTransaction gönder.
    Ekran AVAILABLE durumuna döner, status sayfası son değerlerde kalır.
    """
    global transaction_id, charging_active

    if transaction_id is None:
        log("WARN", "Aktif işlem yok — önce StartTransaction gönderin!")
        return

    charging_active = False
    await send(ws, "StopTransaction", {
        "transactionId": transaction_id,
        "idTag":         DEFAULT_ID_TAG,
        "meterStop":     meter_wh,
        "timestamp":     iso_now(),
        "reason":        "Local",
    })
    nxt_set_status("AVAILABLE")
    log("INFO", f"Şarj durduruldu — Toplam: {total_energy_wh} Wh, {total_cost:.2f} TL")


async def meter_values(ws):
    """
    MeterValues gönder ve Nextion'ı güncelle.
    % şarj: INITIAL_CHARGE_WH + total_energy_wh üzerinden hesaplanır.
    """
    global meter_wh, transaction_id, charge_percent, total_energy_wh, total_cost

    if not charging_active:
        log("WARN", "Şarj aktif değil — MeterValues gönderilmedi")
        return

    # Enerji güncelle
    meter_wh        += METER_INCREMENT_WH
    total_energy_wh += WH_PER_STEP
    total_cost      += TL_PER_500WH

    # % hesabı (araç bataryası bazlı)
    charge_percent = _calc_charge_percent(total_energy_wh)

    payload = {
        "connectorId": 1,
        "meterValue": [{
            "timestamp": iso_now(),
            "sampledValue": [
                {"value": str(meter_wh),       "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                {"value": str(DEFAULT_VOLTAGE), "measurand": "Voltage",        "unit": "V"},
                {"value": str(DEFAULT_CURRENT), "measurand": "Current.Import", "unit": "A"},
            ]
        }]
    }
    if transaction_id:
        payload["transactionId"] = transaction_id

    await send(ws, "MeterValues", payload)

    # Nextion güncellemeleri
    nxt_set_charge_percent(charge_percent)   # home.percent
    nxt_update_status_page()                 # status sayfası

    log("INFO", f"Şarj: %{charge_percent} | Enerji bu oturum: {total_energy_wh} Wh | Ücret: {total_cost:.2f} TL")


# ─── Periyodik Görevler ───────────────────────────────────────────────────────

async def heartbeat_loop(ws, interval: int):
    log("INFO", f"Heartbeat döngüsü başladı — her {interval}s")
    while True:
        await asyncio.sleep(interval)
        try:
            await heartbeat(ws)
        except Exception:
            break


async def clock_loop():
    """Her saniye Nextion home sayfasındaki saati güncelle."""
    while True:
        nxt_set_clock()
        await asyncio.sleep(1)


async def status_update_loop():
    """Şarj aktifken her saniye status sayfasını güncelle (zaman çubuğu için)."""
    while True:
        if charging_active:
            nxt_update_status_page()
        await asyncio.sleep(1)


# ─── Gelen Mesaj İşleyici ─────────────────────────────────────────────────────

async def handle_message(ws, raw: str):
    global transaction_id, hb_interval, hb_task

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log("ERR", f"JSON parse hatası: {raw}")
        return

    msg_type = msg[0]
    mid      = msg[1]

    if msg_type == 3:
        # Yanıt (CALLRESULT)
        payload = msg[2]
        log("RECV", f"[Response/{mid}] {payload}")

        # BootNotification yanıtı → heartbeat aralığını güncelle
        if "interval" in payload and "status" in payload:
            resp_status = payload["status"]
            interval    = payload.get("interval", hb_interval)
            log("INFO", f"BootNotification yanıtı: status={resp_status}, interval={interval}s")
            if resp_status == "Accepted":
                hb_interval = interval
                if hb_task:
                    hb_task.cancel()
                hb_task = asyncio.create_task(heartbeat_loop(ws, hb_interval))
                # BootNotification kabul edildiyse connector Available bildir
                await status_notification(ws, 1, "Available")

        # StartTransaction yanıtı → transactionId kaydet
        if "transactionId" in payload:
            transaction_id = payload["transactionId"]
            log("INFO", f"Transaction başladı — ID={transaction_id}")

        # Authorize yanıtı → idTagInfo.status kontrolü
        if "idTagInfo" in payload:
            id_status = payload["idTagInfo"].get("status", "")
            log("INFO", f"Authorize yanıtı: {id_status}")
            if id_status != "Accepted":
                log("WARN", f"Authorize reddedildi: {id_status}")

    elif msg_type == 2:
        # Sunucudan gelen istek (CALL)
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
{BOLD}{CYAN}│  OCPP 1.6J Simülatör · Nextion 3.5" Entegre  │{RESET}
{BOLD}{CYAN}└──────────────────────────────────────────────┘{RESET}
  {BOLD}1{RESET}  BootNotification
  {BOLD}2{RESET}  Heartbeat (manual)
  {BOLD}3{RESET}  StatusNotification → Available  [{GREEN}yeşil{RESET}]
  {BOLD}4{RESET}  StatusNotification → Charging   [{CYAN}cyan{RESET}]
  {BOLD}5{RESET}  Authorize           ({DEFAULT_ID_TAG})
  {BOLD}6{RESET}  StartTransaction    [şarjı başlat, %{_calc_charge_percent(0)} → %100]
  {BOLD}7{RESET}  MeterValues         [+{WH_PER_STEP} Wh / +{TL_PER_500WH} TL]
  {BOLD}8{RESET}  StopTransaction     [şarjı durdur]
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
                # Available → Nextion: AVAILABLE (yeşil 16354)
                await status_notification(ws, 1, "Available")
            elif choice == "4":
                # Charging → Nextion: CHARGING (cyan 2047)
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
        log("WARN", f"Bağlantı kapandı: code={e.code} reason={getattr(e, 'reason', '')}")


# ─── Ana Fonksiyon ────────────────────────────────────────────────────────────

async def main():
    global hb_task, is_connected

    print(f"\n{BOLD}OCPP 1.6J Simülatör · Nextion nx4832t035 Entegrasyonu{RESET}")
    print(f"{DIM}Hedef CSMS : {CSMS_URL}{RESET}")
    print(f"{DIM}Charge Box : {CHARGE_BOX_ID}{RESET}")
    print(f"{DIM}Nextion    : {NEXTION_PORT} @ {NEXTION_BAUDRATE} baud{RESET}\n")

    # Nextion portu aç (başarısız olsa da devam et)
    nextion_open()

    # Başlangıç ekran durumu
    nxt_set_status("NOT CONNECTED")
    nxt_set_charge_percent(0)
    nxt_set_clock()

    # SIGINT (Ctrl+C) — ekranı sıfırla ve çık
    def handle_sigint(*_):
        log("INFO", "Ctrl+C — ekran sıfırlanıyor, çıkılıyor...")
        nxt_set_status("NOT CONNECTED")
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
            log("INFO", f"Bağlandı ✓  → {CSMS_URL}")

            # Nextion bağlantı kurulunca güncelle
            nxt_set_status("CONNECTED")

            # Otomatik BootNotification
            await boot_notification(ws)

            # Paralel görevler başlat
            recv_task   = asyncio.create_task(recv_loop(ws))
            input_task  = asyncio.create_task(console_input(ws))
            clock_task  = asyncio.create_task(clock_loop())
            status_task = asyncio.create_task(status_update_loop())

            done, pending = await asyncio.wait(
                [recv_task, input_task, clock_task, status_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

    except OSError as e:
        log("ERR", f"Bağlantı kurulamadı: {e}")
        log("INFO", "CSMS çalışıyor mu? URL ve portun doğru olduğundan emin olun.")
        nxt_set_status("NOT CONNECTED")
    except websockets.exceptions.InvalidStatusCode as e:
        log("ERR", f"WebSocket HTTP hatası: {e.status_code} — kimlik doğrulama sorunu olabilir")
        nxt_set_status("NOT CONNECTED")
    except websockets.exceptions.ConnectionClosed as e:
        log("WARN", f"Ana bağlantı kapandı: {e}")
        nxt_set_status("NOT CONNECTED")
    except Exception as e:
        log("ERR", f"Beklenmeyen hata: {type(e).__name__}: {e}")
        nxt_set_status("NOT CONNECTED")
    finally:
        is_connected = False
        log("INFO", "Simülatör sonlandı.")


# ─── Giriş Noktası ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(main())
