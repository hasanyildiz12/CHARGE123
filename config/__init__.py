"""
ocpp-charge-point-simulator — Configuration
All tuneable parameters are defined here.
Edit this file to match your CSMS setup.
"""

# ─── CSMS Connection ─────────────────────────────────────────────────────────

CSMS_URL      = "wss://hasan-7fap.powerfill.app/ws/CP-1"
CHARGE_BOX_ID = "CP-1"

# ─── Charge Point Identity ───────────────────────────────────────────────────

VENDOR   = "TestVendor"
MODEL    = "TestModel"
SERIAL   = "SN-001"
FIRMWARE = "1.0.0"

# ─── Authentication ──────────────────────────────────────────────────────────

DEFAULT_ID_TAG      = "hasanyildizidtag"
BASIC_AUTH_USER     = "CP-1"
BASIC_AUTH_PASSWORD = "1234567890asdfgh"

# ─── Heartbeat ───────────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL = 30  # seconds (overridden by BootNotification response)

# ─── Meter Simulation ────────────────────────────────────────────────────────
# Her MeterValues çağrısında eklenen enerji miktarı (Wh)
METER_INCREMENT_WH = 500   # Wh added on every MeterValues call  →  %5 = 500 Wh
DEFAULT_VOLTAGE    = 230   # V
DEFAULT_CURRENT    = 16    # A

# ─── OCPP Protocol ───────────────────────────────────────────────────────────

SUBPROTOCOL   = "ocpp1.6"
PING_INTERVAL = None  # Managed by our own heartbeat loop

# ─── Nextion Display ─────────────────────────────────────────────────────────
# Raspberry Pi 3B+ primary UART: /dev/ttyAMA0 (GPIO 14 TX / GPIO 15 RX)
# NOT /dev/ttyS0 — mini UART hatalı, ttyAMA0 kullanılmalı.

NEXTION_PORT     = "/dev/ttyAMA0"
NEXTION_BAUDRATE = 9600

# ─── Nextion Renk Kodları (RGB565) ───────────────────────────────────────────
# PowerFill gösterimi: kırmızı=63488, yeşil=16354
# Nextion RGB565 formatı:  R(5 bit) G(6 bit) B(5 bit)
#   63488  = 0xF800  →  tam kırmızı
#   16354  = 0x3FE2  →  yeşil ton (PowerFill standart yeşil)
#   2016   = 0x07E0  →  tam yeşil
#   2047   = 0x07FF  →  cyan (şarj aktif)

COLOR_NOT_CONNECTED = 63488   # kırmızı   (0xF800)
COLOR_CONNECTED     = 16354   # yeşil     (PowerFill standart)
COLOR_AVAILABLE     = 16354   # yeşil     (bağlı + müsait)
COLOR_CHARGING      = 63519   #pembe      (şarj aktif)

# ─── Nextion Picture IDs ─────────────────────────────────────────────────────
# Görseller ID:
#   0  → yeşil araç (bağlı / müsait)
#   3  → kırmızı araç (bağlı değil)

PIC_CAR_CONNECTED    = 0
PIC_CAR_DISCONNECTED = 3

# ─── Şarj / Ücretlendirme ────────────────────────────────────────────────────
# Araç batarya kapasitesi (toplam enerji): 3680 Wh → tam dolu = %100
# %50 dolu = 1840 Wh başlangıç
# Her MeterValues adımı: 500 Wh (= 500/3680 * 100 ≈ %13.6 artış)
# Ücret: 500 Wh başına 5.0 TL

BATTERY_CAPACITY_WH = 3680    # 3.68 kWh = %100
INITIAL_CHARGE_WH   = 0    # Başlangıç doluluk: %50
WH_PER_STEP         = 500     # Her MeterValues adımındaki Wh (= METER_INCREMENT_WH)
TL_PER_500WH        = 5.0     # 500 Wh başına ücret (TL)
