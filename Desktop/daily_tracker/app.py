#!/usr/bin/env python3
"""Taha Serdem Daily Rapor â Flask + Telegram Bot"""

import os, sqlite3, threading, asyncio, json, logging, re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template, session, redirect

_TZ_ISTANBUL = ZoneInfo('Europe/Istanbul')

def now_istanbul() -> datetime:
    """Şu anki Istanbul saatini döndürür. Railway UTC'de çalışır, bu fonksiyon TR saatini verir."""
    return datetime.now(_TZ_ISTANBUL)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))

# .env dosyasini yukle (python-dotenv gerektirmez; mevcut env degiskenlerini ezmez)
def _load_env_file(_path):
    try:
        with open(_path, encoding='utf-8') as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith('#') or '=' not in _line:
                    continue
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())
    except OSError:
        pass

_load_env_file(os.path.join(BASE_DIR, '.env'))

DATA_DIR    = os.environ.get('DATA_DIR', BASE_DIR)
DB_PATH     = os.path.join(DATA_DIR, 'tracker.db')
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
PORT        = int(os.environ.get('PORT', 5000))

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding='utf-8-sig') as f:
            return json.load(f)
    return {}

_cfg = load_config()
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', _cfg.get('TELEGRAM_TOKEN', ''))
# Antrenman döngüsü başlangıç tarihi (Push günü). Bugün başlar.
CYCLE_START = _cfg.get('CYCLE_START', now_istanbul().date().isoformat())


# OPERATION_DAY_CUTOFF_V1
OPERATION_DAY_CUTOFF_HOUR = int(os.environ.get('OPERATION_DAY_CUTOFF_HOUR', _cfg.get('OPERATION_DAY_CUTOFF_HOUR', 6)))

# SHIFT_AWARE_OPERATION_DAY_V2 - vardiya artik DINAMIK. Taha 2 haftada bir donen 6 farkli
# vardiyada calisiyor (or. su an 18:00-03:00, pazartesiden itibaren 14:00-22:00). Aktif
# vardiya user_settings['work_shift'] JSON'unda tutulur; Ayarlar sayfasindan veya Telegram'a
# dogal dille ("pazartesiden itibaren oglen 2 aksam 10 calisiyorum") soylenerek guncellenir.
# Gun kesim saati (operasyon gunu siniri) vardiya bitisinden turetilir: bitis + 11 saat
# (18-03 vardiyasinda 03+11=14:00 - onceki sabit degerle birebir ayni sonuc).
SHIFT_TRANSITION_DATE = date(2026, 6, 22)
SHIFT_BLOCK_DAYS = 14
_shift_cache = {'ts': 0.0, 'val': None}

def _default_shift():
    return {'start': '18:00', 'end': '03:00', 'label': 'akşam vardiyası'}

def invalidate_shift_cache():
    _shift_cache['ts'] = 0.0
    _shift_cache['val'] = None

def current_shift_info(now=None):
    import time as _time
    s = _shift_cache['val']
    if s is None or _time.time() - _shift_cache['ts'] > 30:
        s = _default_shift()
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT value FROM user_settings WHERE key='work_shift'").fetchone()
            conn.close()
            if row and row[0]:
                data = json.loads(row[0])
                if data.get('start') and data.get('end'):
                    s = data
        except Exception:
            pass
        _shift_cache['val'] = s
        _shift_cache['ts'] = _time.time()
    try:
        end_h = int(str(s['end']).split(':')[0]) % 24
    except Exception:
        end_h = 3
    cutoff = (end_h + 11) % 24
    return {
        'name': f"{s['start']}-{s['end']}",
        'label': s.get('label') or 'vardiya',
        'start': s['start'],
        'end': s['end'],
        'cutoff_hour': cutoff,
        'late_window': f"00:00-{max(cutoff - 1, 0):02d}:59" if cutoff > 0 else 'yok',
    }

def operation_cutoff_hour(now=None):
    return int(current_shift_info(now).get('cutoff_hour') or OPERATION_DAY_CUTOFF_HOUR)


def operation_date(now=None):
    """Vardiyaya gore Taha'nin operasyon/log gununu hesaplar. Istanbul saatiyle calisir.
    force_operation_date (günaydın override) sadece gerçek takvim günüyle eşleştiği sürece
    geçerlidir - ertesi gün otomatik temizlenir, yoksa sonsuza dek o günde kilitli kalırdı."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM user_settings WHERE key='force_operation_date'").fetchone()
        if row and row[0]:
            override = date.fromisoformat(row[0])
            real_today = (now or now_istanbul()).date()
            if override == real_today:
                conn.close()
                return override
            conn.execute("DELETE FROM user_settings WHERE key='force_operation_date'")
            conn.commit()
        conn.close()
    except:
        pass
    now = now or now_istanbul()
    d = now.date()
    if 0 <= now.hour < operation_cutoff_hour(now):
        d = d - timedelta(days=1)
    return d

def operation_today():
    return operation_date().isoformat()

app = Flask(__name__, template_folder='templates')
logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

# ─── SITE ŞİFRE KAPISI ───────────────────────────────────────────────────────
# Denetim bulgusu: sitenin tamami auth'suz public'ti - URL'yi bilen herkes tum saglik
# verisini okuyabilir/silebilirdi. SITE_PASSWORD env degiskeni TANIMLIYSA devreye girer;
# tanimli degilse kapi kapali kalmaz (deploy sirasinda kilitlenme olmasin - Railway
# Variables'a SITE_PASSWORD ekleyince aktiflesir). Cookie 1 yil gecerli, tek girisle kalir.
SITE_PASSWORD = os.environ.get('SITE_PASSWORD', _cfg.get('SITE_PASSWORD', '')).strip()
import hashlib as _hashlib
app.secret_key = _hashlib.sha256(('site-session:' + (TELEGRAM_TOKEN or 'dev-secret')).encode()).hexdigest()
app.permanent_session_lifetime = timedelta(days=365)

# Dis servislerin cagirdigi route'lar kapi disinda kalmali:
# - /telegram_webhook: Telegram POST'lari (kendi secret_token korumasi var)
# - /whoop/callback: WHOOP OAuth donusu
_AUTH_EXEMPT_PATHS = {'/login', '/telegram_webhook', '/whoop/callback'}

@app.before_request
def _site_auth_gate():
    if not SITE_PASSWORD:
        return
    if request.path in _AUTH_EXEMPT_PATHS:
        return
    if session.get('authed'):
        return
    if request.path.startswith('/api/') or request.path.startswith('/whoop/'):
        return jsonify({'error': 'auth required'}), 401
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def site_login():
    if not SITE_PASSWORD or session.get('authed'):
        return redirect('/')
    error = ''
    if request.method == 'POST':
        import hmac as _hmac
        pw = (request.form.get('password') or '').strip()
        if pw and _hmac.compare_digest(pw, SITE_PASSWORD):
            session.permanent = True
            session['authed'] = True
            return redirect('/')
        error = 'Şifre yanlış'
    return f'''<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Giriş · Daily Rapor</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0B0E14;font-family:system-ui,sans-serif}}
.box{{background:#10141E;border:1px solid #1e2536;border-radius:16px;padding:34px 30px;width:min(320px,86vw);text-align:center}}
h1{{color:#e7edf7;font-size:18px;margin:0 0 4px}}p{{color:#8b93a7;font-size:12.5px;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;background:#0B0E14;border:1px solid #2a3348;border-radius:10px;color:#e7edf7;padding:11px 13px;font-size:15px;margin-bottom:12px;outline:none}}
input:focus{{border-color:#57c7e6}}button{{width:100%;background:#57c7e6;color:#05080F;border:none;border-radius:10px;padding:11px;font-size:14px;font-weight:700;cursor:pointer}}
.err{{color:#e0556b;font-size:12.5px;margin-bottom:10px}}</style></head>
<body><form class="box" method="POST"><h1>Taha Serdem · Daily Rapor</h1><p>Devam etmek için şifreni gir</p>
{f'<div class="err">{error}</div>' if error else ''}
<input type="password" name="password" placeholder="Şifre" autofocus autocomplete="current-password">
<button type="submit">Giriş</button></form></body></html>'''

# WHOOP entegrasyonu (whoop_integration.py kendi DB_PATH'ini DATABASE_PATH env'inden okur -
# ana app'in gerçek DB_PATH'iyle her zaman aynı dosyaya işaret etsin diye burada eşitliyoruz).
os.environ.setdefault('DATABASE_PATH', DB_PATH)
from whoop_integration import whoop_bp, init_whoop_tables, get_workouts_for_date, start_background_sync
import whoop_integration as _whoop_mod
_whoop_mod.OP_CUTOFF_HOUR = operation_cutoff_hour()  # workout op-gunu bucket'i app ile ayni kesimi kullansin
app.register_blueprint(whoop_bp)
init_whoop_tables()


def start_daily_db_backup():
    """Gunde bir tracker.db'yi DATA_DIR/backups/ altina kopyalar (sqlite backup API ile,
    yazma ortasinda bozuk kopya olmaz), son 7 yedegi tutar. Denetim bulgusu: tek kopya
    veri, hicbir yedek mekanizmasi yoktu."""
    def _loop():
        import time as _time
        while True:
            try:
                bdir = os.path.join(DATA_DIR, 'backups')
                os.makedirs(bdir, exist_ok=True)
                dst = os.path.join(bdir, f'tracker-{now_istanbul().date().isoformat()}.db')
                if not os.path.exists(dst):
                    src = sqlite3.connect(DB_PATH)
                    dstc = sqlite3.connect(dst)
                    src.backup(dstc)
                    dstc.close(); src.close()
                    files = sorted(f for f in os.listdir(bdir) if f.startswith('tracker-') and f.endswith('.db'))
                    for f in files[:-7]:
                        os.remove(os.path.join(bdir, f))
                    log.info("Gunluk DB yedegi alindi: %s", dst)
            except Exception:
                log.exception("Gunluk DB yedegi basarisiz")
            _time.sleep(6 * 3600)
    threading.Thread(target=_loop, daemon=True, name='db-backup').start()


import sys as _sys
if '--telegram-only' not in _sys.argv:
    # --telegram-only ayri bir surec olarak calisir; ayni DB'de ikinci bir 7/24
    # senkron/yedek dongusu olmasin (denetim bulgusu).
    start_background_sync()
    start_daily_db_backup()

# ─── ANTRENMAN DÖNGÜSÜ ─────────────────────────────────────────────────────────
TRAINING_CYCLE = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
TRAINING_COLORS = {
    'Push':  '#cc0000',
    'Pull':  '#0066cc',
    'Leg':   '#ff8800',
    'Upper': '#7c3aed',
    'Lower': '#22c55e',
    'Off':   '#444444',
}

def training_day(date_str):
    """Bu tarihin resmi antrenman/split kategorisini dondurur (Push/Pull/Leg/Upper/Lower/Off).
    Once o tarihe ozel manuel telafi override'i (cycle_active_day_<tarih>, Takvim'den veya
    Karb Cycle panelinden ayni mekanizma) kontrol edilir, yoksa haftanin gunune gore sabit
    PPLUL deseni kullanilir. Geriye donuk uyumluluk icin hep bu 6 sabit isimden birini dondurur
    (training_exercises tablosu ve TRAINING_COLORS bu isimlerle anahtarli)."""
    WEEKDAY_CYCLE = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
    d = date.fromisoformat(date_str)
    day_index = d.weekday()
    try:
        conn = get_db()
        override = conn.execute("SELECT value FROM user_settings WHERE key=?", (f'cycle_active_day_{date_str}',)).fetchone()
        conn.close()
        if override and override['value'] not in (None, ''):
            day_index = int(override['value'])
    except Exception:
        pass
    return WEEKDAY_CYCLE[day_index % 7]

# âââ DATABASE ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sleep_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, hours REAL, quality INTEGER,
            bedtime TEXT, wake_time TEXT, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS exercise_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, type TEXT, duration INTEGER,
            intensity INTEGER, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS nutrition_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, meal_type TEXT, description TEXT,
            calories INTEGER, water_ml INTEGER,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS work_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, hours REAL, tasks TEXT, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS coaching_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, sessions INTEGER, clients TEXT, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS mood_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, energy INTEGER, mood INTEGER,
            stress INTEGER, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vitamin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, name TEXT, amount TEXT, unit TEXT, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS supplement_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            brand TEXT DEFAULT '',
            form TEXT DEFAULT 'kapsul',
            default_dose REAL DEFAULT 1,
            default_unit TEXT DEFAULT 'kapsul',
            notes TEXT DEFAULT '',
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS supplement_stacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT 'custom',
            active INTEGER DEFAULT 1,
            order_num INTEGER DEFAULT 99,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS supplement_stack_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            dose REAL DEFAULT 1,
            unit TEXT DEFAULT 'kapsul',
            order_num INTEGER DEFAULT 99,
            FOREIGN KEY(stack_id) REFERENCES supplement_stacks(id)
        );
        CREATE TABLE IF NOT EXISTS supplement_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            stack_id INTEGER,
            stack_name_snapshot TEXT NOT NULL,
            completed INTEGER DEFAULT 1,
            notes TEXT DEFAULT '',
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS supplement_log_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id INTEGER NOT NULL,
            product_name_snapshot TEXT NOT NULL,
            dose_snapshot REAL,
            unit_snapshot TEXT,
            taken INTEGER DEFAULT 1,
            override_note TEXT DEFAULT '',
            FOREIGN KEY(log_id) REFERENCES supplement_logs(id)
        );
        CREATE TABLE IF NOT EXISTS supplement_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL UNIQUE,
            rule_type TEXT NOT NULL,
            rule_data TEXT DEFAULT '',
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS supplement_breaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type TEXT NOT NULL,
            target_name TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            since_date TEXT,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE, note TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS skin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            area TEXT,
            name TEXT,
            status TEXT,
            notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS quick_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            category TEXT,
            title TEXT NOT NULL,
            description TEXT,
            calories INTEGER,
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            fiber_g REAL,
            water_ml INTEGER,
            amount TEXT,
            unit TEXT,
            notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS meal_titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id TEXT UNIQUE,
            name TEXT NOT NULL UNIQUE,
            order_num INTEGER DEFAULT 99,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS meal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            slot TEXT NOT NULL,
            title TEXT,
            description TEXT,
            calories INTEGER,
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            fiber_g REAL,
            source TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS training_exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            training_day TEXT NOT NULL,
            exercise TEXT NOT NULL,
            sets TEXT,
            reps TEXT,
            weight TEXT,
            notes TEXT,
            sort_order INTEGER DEFAULT 0,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS daily_ai_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            report_json TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_profile_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            note TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_profile_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS meal_stacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS meal_stack_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stack_id INTEGER NOT NULL,
            food_id INTEGER,
            food_name TEXT NOT NULL,
            amount REAL DEFAULT 100,
            unit TEXT DEFAULT 'g',
            order_num INTEGER DEFAULT 99,
            FOREIGN KEY(stack_id) REFERENCES meal_stacks(id)
        );
        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            training_day TEXT NOT NULL,
            exercise TEXT NOT NULL,
            set_num INTEGER NOT NULL,
            weight TEXT,
            reps TEXT,
            notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    macro_cols = {
        'quick_templates': {'protein_g': 'REAL', 'carbs_g': 'REAL', 'fat_g': 'REAL', 'fiber_g': 'REAL'},
        'meal_entries': {'protein_g': 'REAL', 'carbs_g': 'REAL', 'fat_g': 'REAL', 'fiber_g': 'REAL'},
        'workout_logs': {'set_type': 'TEXT', 'rir': 'INTEGER'},
        'vitamin_logs': {'status': "TEXT DEFAULT ''", 'display_order': 'INTEGER'},
        'mood_logs': {'recovery': 'REAL', 'strain': 'REAL'},
        'supplement_breaks': {'end_date': 'TEXT'},
    }
    for table, cols in macro_cols.items():
        existing = {r['name'] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, typ in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    conn.commit()
    conn.close()

# DB'yi uygulama baslarken otomatik olustur/migrate et
init_db()

# Ayni ogunun farkli yazimlarini tek kanonik slot'a indir. Kullanicinin OZEL basliklari
# (Meal 1, Pre Meal, Post Meal...) ASLA degistirilmez - "ogun basligi asla degistirilmez"
# kurali korunur; sadece ayni kelimenin yazim/dil varyantlari birlesir (kahvaltı/breakfast
# → kahvalti gibi). Aksi halde Ozet/Dashboard ayni ogunu 3-4 ayri grup olarak gosteriyor.
MEAL_SLOT_ALIASES = {
    'kahvaltı': 'kahvalti', 'breakfast': 'kahvalti',
    'öğle': 'ogle', 'öğlen': 'ogle', 'oglen': 'ogle', 'lunch': 'ogle',
    'akşam': 'aksam', 'dinner': 'aksam',
    'atistirma': 'ara', 'atıştırma': 'ara', 'atıştırmalık': 'ara', 'atistirmalik': 'ara',
    'preworkout': 'pre-workout', 'pre workout': 'pre-workout', 'pre_workout': 'pre-workout',
    'postworkout': 'post-workout', 'post workout': 'post-workout', 'post_workout': 'post-workout',
    'night': 'gece',
}

def normalize_meal_slot(slot):
    s = (slot or 'extra').strip().lower()
    return MEAL_SLOT_ALIASES.get(s, s)

def normalize_meal_slots_all():
    """Tarihi meal_entries kayitlarindaki slot yazim varyantlarini kanonik isme cek
    (idempotent - her boot'ta calisir, degisiklik yoksa 0 satir gunceller)."""
    conn = get_db()
    n = 0
    for old, new in MEAL_SLOT_ALIASES.items():
        n += conn.execute("UPDATE meal_entries SET slot=? WHERE slot=?", (new, old)).rowcount
    conn.commit()
    conn.close()
    if n:
        log.info("Ogun slot normalizasyonu: %d satir kanonik isme guncellendi", n)

# ─── KANONİK SUPPLEMENT KATALOĞU ─────────────────────────────────────────────
# Tek dogruluk kaynagi: bu katalog her boot'ta DB'ye uygulanir. Prod (Railway volume)
# DB'si lokalden ayri oldugu icin lokal DB'de yapilan katalog duzeltmeleri prod'a hic
# ulasmamisti (prod hala KSM-66/KFD/eski yapiyi gosteriyordu) - artik kod tasiyor.
CANONICAL_SUPPLEMENT_STACKS = [
    # 2026-07-17 duzeni (kullanici netlestirdi): NAC gunde 2 kez = Ac Karna + Gece.
    # Ashwagandha (2 kapsul) artik GECE stack'inde, Ac Karna'da degil.
    ('Aç Karna', [
        ('NOW NAC 600mg', 1, 'kapsül'),
        ("Garden of Life Dr. Formulated Probiotics Once Daily Men's", 1, 'kapsül'),
    ]),
    ('Sabah/Kahvaltı', [
        ('Optimum Nutrition Collagen Peptides (Unflavoured)', 1, 'ölçek'),
        ('Thorne Vitamin D + K2', 4, 'damla'),
        ('Life Extension Mega EPA/DHA (Omega-3)', 3, 'kapsül'),
        ('NOW Magtein Magnesium L-Threonate', 1, 'kapsül'),
        ('Life Extension MacuGuard with Saffron', 1, 'kapsül'),
        ('Life Extension BioActive Complete B-Complex', 1, 'kapsül'),
        ('California Gold Nutrition Gold C 1000mg', 1, 'tablet'),
        ('NOW L-Theanine Double Strength', 1, 'kapsül'),
        ('NOW Zinc Picolinate 50mg', 1, 'kapsül'),
        ('NOW Extra Strength Astaxanthin 10mg', 1, 'kapsül'),
    ]),
    ('Gece', [
        ('NOW Magnesium Glycinate', 3, 'kapsül'),
        ('NOW Melatonin 1mg', 3, 'tablet'),
        ('NOW Glycine 1000mg', 3, 'kapsül'),
        ('NOW L-Theanine Double Strength', 1, 'kapsül'),
        ('NOW NAC 600mg', 1, 'kapsül'),
        ('Weider Ashwagandha Professional', 2, 'kapsül'),
    ]),
    ('Pre-workout', [
        ("Doctor's Best L-Citrulline Powder", 8, 'gram'),
        ('KFD Premium Beta-Alanine', 2, 'gram'),
        ('Optimum Nutrition Electrolyte Powder (Lemon)', 8, 'gram'),
        ('Swedish Supplements Taurine', 2, 'gram'),
    ]),
    ('Post-workout', [
        ('California Gold Nutrition SPORT Creatine Monohydrate', 5, 'gram'),
    ]),
]

# Eski/kisa/yanlis urun isimleri -> kanonik isim. vitamin_logs uyum eslesmesi isim bazli
# oldugu icin gecmis loglar da cekilir; supplement_breaks target'lari ve products tablosu da.
SUPPLEMENT_NAME_RENAMES = {
    'KSM-66 Ashwagandha': 'Weider Ashwagandha Professional',
    'Ashwagandha': 'Weider Ashwagandha Professional',
    'L-Theanine': 'NOW L-Theanine Double Strength',
    'L-Theanine Gece': 'NOW L-Theanine Double Strength',
    'L-Theanine Double Strength (NOW)': 'NOW L-Theanine Double Strength',
    '5% Nutrition L-Citrulline 3000': "Doctor's Best L-Citrulline Powder",
    'L-Citrulline': "Doctor's Best L-Citrulline Powder",
    'KFD Premium Beta-Alanin': 'KFD Premium Beta-Alanine',
    'Beta Alanine': 'KFD Premium Beta-Alanine',
    'Optimum Nutrition Elektrolit': 'Optimum Nutrition Electrolyte Powder (Lemon)',
    'Elektrolit Tozu': 'Optimum Nutrition Electrolyte Powder (Lemon)',
    'Optimum Nutrition Electrolyte Powder': 'Optimum Nutrition Electrolyte Powder (Lemon)',
    'KFD Creatine Monohydrate': 'California Gold Nutrition SPORT Creatine Monohydrate',
    'Creatine Monohydrate': 'California Gold Nutrition SPORT Creatine Monohydrate',
    'Garden of Life Probiyotik': "Garden of Life Dr. Formulated Probiotics Once Daily Men's",
    'Probiyotik': "Garden of Life Dr. Formulated Probiotics Once Daily Men's",
    'California Gold Nutrition C 1000mg': 'California Gold Nutrition Gold C 1000mg',
    'Vitamin C': 'California Gold Nutrition Gold C 1000mg',
    'Optimum Nutrition Collagen Peptides': 'Optimum Nutrition Collagen Peptides (Unflavoured)',
    'Collagen Peptides': 'Optimum Nutrition Collagen Peptides (Unflavoured)',
    'Kolajen': 'Optimum Nutrition Collagen Peptides (Unflavoured)',
    'NOW Astaxanthin 10mg': 'NOW Extra Strength Astaxanthin 10mg',
    'Life Extension Mega EPA/DHA': 'Life Extension Mega EPA/DHA (Omega-3)',
    'Omega-3': 'Life Extension Mega EPA/DHA (Omega-3)',
    'Mega EPA/DHA (Life Extension)': 'Life Extension Mega EPA/DHA (Omega-3)',
    'B-Complex': 'Life Extension BioActive Complete B-Complex',
    'BioActive Complete B-Complex (Life Extension)': 'Life Extension BioActive Complete B-Complex',
    'Cinko': 'NOW Zinc Picolinate 50mg',
    'Çinko': 'NOW Zinc Picolinate 50mg',
    'Zinc Picolinate (NOW)': 'NOW Zinc Picolinate 50mg',
    'D+K2': 'Thorne Vitamin D + K2',
    'D3+K2': 'Thorne Vitamin D + K2',
    'Vitamin D + K2 Liquid (Thorne)': 'Thorne Vitamin D + K2',
    'Glycine': 'NOW Glycine 1000mg',
    'NOW Glycine': 'NOW Glycine 1000mg',
    'Goz Vitamini': 'Life Extension MacuGuard with Saffron',
    'Göz Vitamini': 'Life Extension MacuGuard with Saffron',
    'MacuGuard with Saffron (Life Extension)': 'Life Extension MacuGuard with Saffron',
    'Magnesium Glycinate': 'NOW Magnesium Glycinate',
    'Magnesium L-Threonate': 'NOW Magtein Magnesium L-Threonate',
    'Magnesium L-Threonate (NOW Magtein)': 'NOW Magtein Magnesium L-Threonate',
    'Magtein': 'NOW Magtein Magnesium L-Threonate',
    'Magtein Magnesium L-Threonate': 'NOW Magtein Magnesium L-Threonate',
    'Melatonin': 'NOW Melatonin 1mg',
    'NOW Melatonin': 'NOW Melatonin 1mg',
    'NAC': 'NOW NAC 600mg',
    'Taurin': 'Swedish Supplements Taurine',
}


# Kanonik katalog surumu: stack icerigi kodda degistiginde artir. Stack item rewrite
# SADECE surum degisince uygulanir - yoksa kullanicinin UI'dan yaptigi stack duzenlemeleri
# her restart'ta sessizce geri aliniyordu. Isim rename'leri surumden bagimsiz her boot calisir.
SUPPLEMENT_CATALOG_VERSION = '2026-07-17-v1'


def sync_supplement_catalog_canonical():
    """Kanonik katalogu DB'ye uygular (idempotent, her boot). Stack item listesi yalnizca
    SUPPLEMENT_CATALOG_VERSION degistiginde kanonikten yeniden yazilir; vitamin_logs /
    supplement_breaks / supplement_products'taki eski isimler her boot kanonige cekilir."""
    conn = get_db()
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='supplement_stack_items'").fetchone():
            return
        _has_settings = bool(conn.execute(
            "SELECT name FROM sqlite_master WHERE name='user_settings'").fetchone())
        ver_row = conn.execute(
            "SELECT value FROM user_settings WHERE key='supplement_catalog_version'").fetchone() if _has_settings else None
        apply_items = not ver_row or ver_row['value'] != SUPPLEMENT_CATALOG_VERSION
        changed = 0
        for order_i, (stack_name, items) in enumerate(CANONICAL_SUPPLEMENT_STACKS, start=1):
            row = conn.execute("SELECT id FROM supplement_stacks WHERE name=?", (stack_name,)).fetchone()
            if row:
                sid = row['id']
                # order_num'a DOKUNMA - kullanicinin Log'daki Takviye Sirasi'nda yaptigi
                # siralama boot'ta sifirlanmasin. Sadece aktiflik garanti edilir.
                conn.execute("UPDATE supplement_stacks SET active=1 WHERE id=?", (sid,))
            else:
                cur = conn.execute(
                    "INSERT INTO supplement_stacks (name, category, active, order_num) VALUES (?, 'custom', 1, ?)",
                    (stack_name, order_i))
                sid = cur.lastrowid
            # Karsilastirma SIRA-DUYARSIZ (sorted) - kullanici urunleri yeniden siralarsa
            # icerik ayni kaldigi surece migration yeniden yazmaz, siralama korunur.
            existing = sorted((r['product_name'], float(r['dose'] or 0), r['unit'] or '') for r in conn.execute(
                "SELECT product_name, dose, unit FROM supplement_stack_items WHERE stack_id=?",
                (sid,)).fetchall())
            canonical = sorted((n, float(d), u) for (n, d, u) in items)
            if apply_items and existing != canonical:
                conn.execute("DELETE FROM supplement_stack_items WHERE stack_id=?", (sid,))
                for i, (n, d, u) in enumerate(items, start=1):
                    conn.execute(
                        "INSERT INTO supplement_stack_items (stack_id, product_name, dose, unit, order_num) VALUES (?,?,?,?,?)",
                        (sid, n, d, u, i))
                changed += 1
        renamed = 0
        for old, new in SUPPLEMENT_NAME_RENAMES.items():
            renamed += conn.execute("UPDATE vitamin_logs SET name=? WHERE name=?", (new, old)).rowcount
            conn.execute("UPDATE supplement_breaks SET target_name=? WHERE target_type='product' AND target_name=?", (new, old))
            oldrow = conn.execute("SELECT id FROM supplement_products WHERE name=?", (old,)).fetchone()
            if oldrow:
                if conn.execute("SELECT id FROM supplement_products WHERE name=?", (new,)).fetchone():
                    conn.execute("DELETE FROM supplement_products WHERE id=?", (oldrow['id'],))
                else:
                    conn.execute("UPDATE supplement_products SET name=? WHERE id=?", (new, oldrow['id']))
        if apply_items and _has_settings:
            conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('supplement_catalog_version', ?)",
                         (SUPPLEMENT_CATALOG_VERSION,))
        conn.commit()
        if changed or renamed:
            log.info("Supplement katalog senkronu: %d stack yeniden yazildi, %d vitamin_logs satiri kanonige cekildi", changed, renamed)
    except Exception:
        log.exception("sync_supplement_catalog_canonical basarisiz")
    finally:
        conn.close()


def consolidate_water_all_dates():
    """nutrition_logs'ta her tarih icin water_ml'yi tek satirda topla (kirli data temizligi)."""
    conn = get_db()
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM nutrition_logs WHERE water_ml > 0").fetchall()]
    for d in dates:
        total = conn.execute("SELECT SUM(water_ml) FROM nutrition_logs WHERE date=?", (d,)).fetchone()[0] or 0
        conn.execute("UPDATE nutrition_logs SET water_ml=0 WHERE date=?", (d,))
        row = conn.execute("SELECT id FROM nutrition_logs WHERE date=?", (d,)).fetchone()
        if row:
            conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE id=?", (total, row['id']))
    conn.commit()
    conn.close()
    log.info("Su konsolidasyonu tamamlandi (%d tarih)", len(dates))

def migrate_body_metrics_weight_log():
    """body_metrics'e weight_kg_night kolonu ekle (gece tartisi ayri sakla)."""
    try:
        conn = get_db()
        existing = {r['name'] for r in conn.execute("PRAGMA table_info(body_metrics)").fetchall()}
        if not existing:
            conn.close()
            return  # table doesn't exist yet - init_db will create it
        if 'weight_kg_night' not in existing:
            conn.execute("ALTER TABLE body_metrics ADD COLUMN weight_kg_night REAL")
            conn.commit()
            log.info("body_metrics: weight_kg_night kolonu eklendi")
        conn.close()
    except Exception as e:
        log.warning(f"migrate_body_metrics_weight_log skip: {e}")

consolidate_water_all_dates()
migrate_body_metrics_weight_log()
normalize_meal_slots_all()
sync_supplement_catalog_canonical()

def ensure_user_settings_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit(); conn.close()

ensure_user_settings_table()

# ââ Karb cycle plan ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
CARB_CYCLE_DEFAULT = [
    {'day_type': 'Push',  'cal': 1720, 'protein': 140, 'carb': 200, 'fat': 40, 'notes': 'Pazartesi'},
    {'day_type': 'Pull',  'cal': 1920, 'protein': 140, 'carb': 250, 'fat': 40, 'notes': 'Sali'},
    {'day_type': 'Legs',  'cal': 2120, 'protein': 140, 'carb': 300, 'fat': 40, 'notes': 'Carsamba'},
    {'day_type': 'Upper', 'cal': 1920, 'protein': 140, 'carb': 250, 'fat': 40, 'notes': 'Persembe'},
    {'day_type': 'Lower', 'cal': 1720, 'protein': 140, 'carb': 200, 'fat': 40, 'notes': 'Cuma'},
    {'day_type': 'Off1',  'cal': 1490, 'protein': 140, 'carb': 120, 'fat': 50, 'notes': 'Cumartesi'},
    {'day_type': 'Off2',  'cal': 1330, 'protein': 140, 'carb': 80,  'fat': 50, 'notes': 'Pazar'},
]

def ensure_carb_cycle_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS carb_cycle_plan (
            day_type TEXT PRIMARY KEY,
            cal      INTEGER DEFAULT 0,
            protein  INTEGER DEFAULT 0,
            carb     INTEGER DEFAULT 0,
            fat      INTEGER DEFAULT 0,
            notes    TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Seed default plan if table is empty
    existing = conn.execute("SELECT COUNT(*) FROM carb_cycle_plan").fetchone()[0]
    if existing == 0:
        for row in CARB_CYCLE_DEFAULT:
            conn.execute(
                "INSERT OR IGNORE INTO carb_cycle_plan (day_type,cal,protein,carb,fat,notes) VALUES (?,?,?,?,?,?)",
                (row['day_type'], row['cal'], row['protein'], row['carb'], row['fat'], row['notes'])
            )
    conn.commit(); conn.close()

ensure_carb_cycle_table()

# Haftanin gunune gore otomatik karb cycle tipi
DOW_TO_CYCLE = {0:'Push', 1:'Pull', 2:'Legs', 3:'Upper', 4:'Lower', 5:'Off1', 6:'Off2'}

def auto_cycle_day_type():
    """Bugunun operasyon gunu bazinda karb cycle tipini dondur."""
    op_date = operation_date()
    dow = op_date.weekday()  # 0=Pazartesi, 6=Pazar
    return DOW_TO_CYCLE.get(dow, 'Off2'), op_date.isoformat()

# --- Karb Cycle Paneli v2 (day_index-tabanli, serbest metin tip adi) --------
def ensure_cycle_days_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cycle_days (
            day_index INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            protein_g INTEGER DEFAULT 0,
            carb_g INTEGER DEFAULT 0,
            fat_g INTEGER DEFAULT 0
        )
    """)
    existing = conn.execute("SELECT COUNT(*) FROM cycle_days").fetchone()[0]
    if existing == 0:
        # Ilk kurulum: gercek carb_cycle_plan degerlerini tasi (kaybolmasin), yoksa varsayilanla tohumla
        by_type = {r['day_type']: r for r in conn.execute("SELECT * FROM carb_cycle_plan").fetchall()}
        for day_index, day_type in DOW_TO_CYCLE.items():
            label = {'Off1': 'Off 1', 'Off2': 'Off 2'}.get(day_type, day_type)
            row = by_type.get(day_type)
            if row:
                protein, carb, fat = row['protein'], row['carb'], row['fat']
            else:
                seed = CARB_CYCLE_DEFAULT[day_index]
                protein, carb, fat = seed['protein'], seed['carb'], seed['fat']
            conn.execute(
                "INSERT OR IGNORE INTO cycle_days (day_index, type, protein_g, carb_g, fat_g) VALUES (?,?,?,?,?)",
                (day_index, label, protein, carb, fat)
            )
    conn.commit(); conn.close()

ensure_cycle_days_table()

def get_cycle_active_day():
    """Bugun icin aktif cycle gun index'i. Manuel secim varsa (o gune ozel), yoksa haftanin gunu."""
    today_str = operation_today()
    conn = get_db()
    row = conn.execute("SELECT value FROM user_settings WHERE key=?", (f'cycle_active_day_{today_str}',)).fetchone()
    conn.close()
    if row and row['value'] not in (None, ''):
        try:
            return int(row['value'])
        except ValueError:
            pass
    return operation_date().weekday()

def set_cycle_active_day(day_index, date_str=None):
    date_str = date_str or operation_today()
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES (?,?)",
                 (f'cycle_active_day_{date_str}', str(day_index)))
    conn.commit(); conn.close()

def clear_cycle_active_day(date_str=None):
    date_str = date_str or operation_today()
    conn = get_db()
    conn.execute("DELETE FROM user_settings WHERE key=?", (f'cycle_active_day_{date_str}',))
    conn.commit(); conn.close()

def generate_cycle_ai_comment():
    """Karb Cycle plani kaydedildiginde Claude Haiku'dan 2-3 cumlelik Turkce degerlendirme ister,
    user_settings['cycle_ai_comment'] icine yazar. API key yoksa/basarisizsa sessizce vazgecer."""
    import urllib.request, urllib.error
    if not ANTHROPIC_API_KEY:
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM cycle_days ORDER BY day_index").fetchall()
    plan = [{'day_index': r['day_index'], 'type': r['type'], 'protein_g': r['protein_g'],
             'carb_g': r['carb_g'], 'fat_g': r['fat_g'],
             'kcal': 4*r['protein_g'] + 4*r['carb_g'] + 9*r['fat_g']} for r in rows]
    weight_row = conn.execute("SELECT value FROM user_settings WHERE key='weight_goal'").fetchone()
    water_row = conn.execute("SELECT value FROM user_settings WHERE key='water'").fetchone()
    conn.close()
    last7 = []
    for i in range(7):
        ds = (operation_date() - timedelta(days=i)).isoformat()
        m = meal_macro_totals(ds)
        if m['calories']:
            last7.append({'date': ds, **m})
    payload = {
        'plan': plan,
        'hedef_kilo': weight_row['value'] if weight_row else None,
        'su_hedefi_ml': water_row['value'] if water_row else None,
        'son_7_gun_gerceklesen': last7,
    }
    system_prompt = (
        'Sen bir beslenme/karb-cycle koçusun. Kullanıcının haftalık karb cycle planını (7 gün, '
        'her gün için tip/protein/karb/yağ/kcal), hedef kilosunu, su hedefini ve son 7 gündeki '
        'gerçek beslenme ortalamalarını JSON olarak alacaksın. Sadece 2-3 cümlelik, Türkçe, '
        'samimi ama net bir değerlendirme yaz: plan hangi bantta (kesim <1850 / koruma / bulk >2050 '
        'ortalama kcal), antrenman günleri ile off günleri arasındaki karb dağılımı mantıklı mı, '
        'gerçek beslenme planla ne kadar uyumlu, varsa somut bir öneri. Başka hiçbir şey yazma, '
        'sadece bu değerlendirme metnini dön.'
    )
    body = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': 300,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    }
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps(body).encode('utf-8'),
        headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        comment = result['content'][0]['text'].strip()
    except Exception as _e:
        import logging; logging.getLogger('daily').warning(f"cycle AI comment request failed: {_e}")
        return
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('cycle_ai_comment', ?)", (comment,))
    conn.commit(); conn.close()

def generate_dashboard_ai_insights(date_str=None):
    """Ana Dashboard'daki Koc Analizi paneli icin 5 tipli (kritik/aksiyon/hatirlatma/motivasyon/ai_plan)
    kisa AI icgorusu uretir. Claude Haiku'ya gunun gercek verisini (kalori/makro vs Karb Cycle hedefi,
    su, adim, varsa recovery yuzdesi, kilo trendi, yarinin Karb Cycle hedefi) JSON olarak gonderir."""
    import urllib.request
    date_str = date_str or operation_today()
    if not ANTHROPIC_API_KEY:
        return []
    conn = get_db()
    meals = conn.execute("SELECT * FROM meal_entries WHERE date=?", (date_str,)).fetchall()
    nutrition = conn.execute("SELECT * FROM nutrition_logs WHERE date=?", (date_str,)).fetchone()
    step_row = conn.execute("SELECT steps FROM step_logs WHERE date=?", (date_str,)).fetchone()
    mood_row = conn.execute("SELECT * FROM mood_logs WHERE date=?", (date_str,)).fetchone()
    weight_rows = conn.execute(
        "SELECT date, weight_kg FROM body_metrics WHERE weight_kg IS NOT NULL ORDER BY date DESC LIMIT 8"
    ).fetchall()
    weight_goal_row = conn.execute("SELECT value FROM user_settings WHERE key='weight_goal'").fetchone()
    override_row = conn.execute("SELECT value FROM user_settings WHERE key=?", (f'cycle_active_day_{date_str}',)).fetchone()
    day_index = int(override_row['value']) if override_row and override_row['value'] not in (None, '') else date.fromisoformat(date_str).weekday()
    today_cyc = conn.execute("SELECT * FROM cycle_days WHERE day_index=?", (day_index,)).fetchone()
    tomorrow_cyc = conn.execute("SELECT * FROM cycle_days WHERE day_index=?", ((day_index + 1) % 7,)).fetchone()
    whoop_row = None
    try:
        whoop_row = conn.execute("SELECT recovery_score, strain FROM whoop_daily WHERE date=?", (date_str,)).fetchone()
    except Exception:
        pass
    conn.close()

    total_cal = round(sum(m['calories'] or 0 for m in meals))
    total_p = round(sum(m['protein_g'] or 0 for m in meals))
    total_k = round(sum(m['carbs_g'] or 0 for m in meals))
    total_y = round(sum(m['fat_g'] or 0 for m in meals))
    water_ml = int(nutrition['water_ml'] if nutrition and nutrition['water_ml'] else 0)
    steps = int(step_row['steps']) if step_row and step_row['steps'] else 0
    try:
        recovery = mood_row['recovery'] if mood_row else None
    except Exception:
        recovery = None
    # mood_logs.recovery pratikte hep bos - gercek deger whoop_daily'de (WHOOP senkronu yaziyor)
    if recovery is None and whoop_row and whoop_row['recovery_score'] is not None:
        recovery = whoop_row['recovery_score']
    whoop_strain = whoop_row['strain'] if whoop_row else None

    target = None
    if today_cyc:
        target = {'type': today_cyc['type'], 'protein_g': today_cyc['protein_g'], 'carb_g': today_cyc['carb_g'],
                  'fat_g': today_cyc['fat_g'], 'kcal': 4 * today_cyc['protein_g'] + 4 * today_cyc['carb_g'] + 9 * today_cyc['fat_g']}
    tomorrow = None
    if tomorrow_cyc:
        tomorrow = {'type': tomorrow_cyc['type'],
                    'kcal': 4 * tomorrow_cyc['protein_g'] + 4 * tomorrow_cyc['carb_g'] + 9 * tomorrow_cyc['fat_g'],
                    'carb_g': tomorrow_cyc['carb_g']}
    weight_trend = [{'date': r['date'], 'weight_kg': r['weight_kg']} for r in reversed(weight_rows)]

    payload = {
        'tarih': date_str,
        'gercek': {'kcal': total_cal, 'protein_g': total_p, 'carb_g': total_k, 'fat_g': total_y,
                   'su_ml': water_ml, 'adim': steps},
        'hedef': target,
        'yarinin_hedefi': tomorrow,
        'recovery_pct': recovery,
        'whoop_strain': whoop_strain,
        'kilo_trend_son_gunler': weight_trend,
        'hedef_kilo': weight_goal_row['value'] if weight_goal_row else None,
        'saat': now_istanbul().strftime('%H:%M'),
    }
    system_prompt = (
        'Sen kullanıcının kişisel antrenman ve beslenme koçusun. Sana bugünün gerçek verisini '
        '(kalori/makro, hedef, su, adım, varsa recovery yüzdesi, son kilo trendi, yarının Karb Cycle '
        'hedefi) JSON olarak vereceğim. En fazla 4, en az 2 kısa içgörü üret. Her içgörü şu 5 tipten '
        'BİRİNE ait olmalı: "kritik" (veri hatası/tehlikeli aşım şüphesi — örn. tek bir öğün kaydı '
        'toplam kalorinin çoğunu oluşturuyorsa muhtemel girdi hatasıdır), "aksiyon" (bugün yapılabilecek '
        'somut bir şey, varsa recovery ile antrenman kesişimi), "hatirlatma" (payload\'daki "saat" alanına '
        'göre su/öğün ritmi — saat üretim anının saatidir, gün içinde değişmez; saate çok bağlı ifadeler yerine '
        'günün geneline uyan ritim önerisi ver), '
        '"motivasyon" (kilo trendi/ilerleme), "ai_plan" (yarının Karb Cycle hedefinin kısa özeti). '
        'recovery_pct null ise recovery ile ilgili hiçbir şey uydurma. SADECE şu JSON formatında dön, '
        'başka hiçbir şey yazma: {"insights":[{"type":"kritik|aksiyon|hatirlatma|motivasyon|ai_plan",'
        '"text":"Türkçe, kısa, somut, 1-2 cümle"}]}'
    )
    body = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': 500,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    }
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps(body).encode('utf-8'),
        headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        text = result['content'][0]['text'].strip()
        if text.startswith('```'):
            text = text.strip('`')
            if text.startswith('json'):
                text = text[4:]
        insights = json.loads(text.strip()).get('insights', [])
    except Exception as _e:
        import logging; logging.getLogger('daily').warning(f"dashboard AI insights failed: {_e}")
        return []
    conn2 = get_db()
    conn2.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES (?,?)",
                  (f'dashboard_ai_insights_{date_str}', json.dumps(insights, ensure_ascii=False)))
    conn2.commit(); conn2.close()
    return insights

# Arka plan AI uretimi: sayfa acilislari canli Claude cagrisini BEKLEMESIN diye.
# Eskiden her operasyon gununun ilk Dashboard acilisi insights uretimini (3-10 sn),
# ilk Ozet/Coach acilisi da dunku Koc notunu (30 sn'ye kadar) istek icinde bekliyordu -
# kullanicinin gordugu "sekmeler 5-10 saniye gec aciliyor" gecikmesinin kaynagi buydu.
_bg_ai_lock = threading.Lock()
_bg_ai_running = set()

def _spawn_bg_ai(key, fn):
    """Ayni is iki kez ayni anda uretilmesin diye anahtar bazli tekil arka plan is."""
    with _bg_ai_lock:
        if key in _bg_ai_running:
            return False
        _bg_ai_running.add(key)
    def _run():
        try:
            fn()
        except Exception:
            log.exception("arka plan AI uretimi basarisiz: %s", key)
        finally:
            with _bg_ai_lock:
                _bg_ai_running.discard(key)
    threading.Thread(target=_run, daemon=True, name=f'bg-ai-{key}').start()
    return True


@app.route('/api/dashboard/ai-insights')
def api_dashboard_ai_insights():
    date_str = request.args.get('date') or operation_today()
    force = request.args.get('force') == '1'
    conn = get_db()
    row = None if force else conn.execute(
        "SELECT value FROM user_settings WHERE key=?", (f'dashboard_ai_insights_{date_str}',)
    ).fetchone()
    if row and row['value']:
        conn.close()
        try:
            return jsonify({'insights': json.loads(row['value']), 'date': date_str})
        except Exception:
            pass
    # Cache yok: uretimi arka plana at, istegi BLOKLAMADAN dunun insights'iyla (varsa) don.
    # Frontend 'generating' bayragini gorunce kisa bir gecikmeyle bir kez daha sorar.
    _spawn_bg_ai(f'dash_insights_{date_str}', lambda: generate_dashboard_ai_insights(date_str))
    yday = (date.fromisoformat(date_str) - timedelta(days=1)).isoformat()
    stale = conn.execute(
        "SELECT value FROM user_settings WHERE key=?", (f'dashboard_ai_insights_{yday}',)
    ).fetchone()
    conn.close()
    fallback = []
    if stale and stale['value']:
        try:
            fallback = json.loads(stale['value'])
        except Exception:
            fallback = []
    return jsonify({'insights': fallback, 'date': date_str, 'generating': True})

_FACT_STOPWORDS = {
    'bir','bu','ve','ile','de','da','çok','gibi','var','olan','olarak','için','ama','ancak',
    'daha','en','son','gün','günde','günlerde','günü','yapılan','yapılmış','yapıldı','değil',
    'ile','ki','mi','mu','mü','ya','ya da','ise','olabilir','oluyor','olmuş','kadar','sonra',
}
def _facts_similar(a, b, threshold=0.4):
    """Embedding yok; hafif kelime-kesisim benzerligi. Turkce ek farkli olsa da (yapilmayan/
    yapilmadigi gibi) kok kelimelerin cogu ortak kalir, bu yuzden esik nispeten dusuk tutuldu.
    Amac mukemmel dedup degil, LLM'in ayni gozlemi tekrar tekrar farkli cumlelerle yazmasini
    (backfill sirasinda gozlemlendi) engellemek."""
    wa = {w.strip('.,()—-') for w in a.lower().split() if len(w) > 3 and w not in _FACT_STOPWORDS}
    wb = {w.strip('.,()—-') for w in b.lower().split() if len(w) > 3 and w not in _FACT_STOPWORDS}
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= threshold

# Alinan takviye sayaci: duplike satirlar tek sayilir; ayni urun iki stack'te (NAC, L-Theanine)
# web'in yazdigi 'stack:<ad>' notu sayesinde stack basina ayri sayilir. Statusu bos eski
# kayitlar boot'taki backfill_vitamin_status() ile dolduruldugu icin status filtresi guvenli.
SUPP_TAKEN_COUNT_SQL = (
    "SELECT COUNT(DISTINCT name || '|' || (CASE WHEN notes LIKE 'stack:%' THEN notes ELSE '' END)) c "
    "FROM vitamin_logs WHERE date=? AND status IN ('taken','eod_taken','half_dose')"
)

# Cinko programi (kullanici 2026-07-18: 'pazartesi carsamba cuma alindi seklinde olsun'):
# yalnizca Pzt/Car/Cum gunleri beklenir; diger gunler 'eod_rest' olur, sayaclara girmez.
# Baska bir gun yine de alinirsa normal 'taken' sayilir.
ZINC_PRODUCT_NAME = 'NOW Zinc Picolinate 50mg'
ZINC_DAYS = (0, 2, 4)  # Pazartesi, Carsamba, Cuma (date.weekday)


def active_supplement_breaks():
    """Aktif takviye aralarini dondurur (once suresi dolanlari kapatir)."""
    try:
        if 'expire_due_supplement_breaks' in globals():
            expire_due_supplement_breaks()
        conn = get_db()
        rows = conn.execute(
            "SELECT target_type, target_name, since_date, end_date FROM supplement_breaks WHERE active=1"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def active_breaks_prompt_txt():
    """Aktif aralarin AI promptlari icin tek satirlik ozeti - AI'nin arada olan takviyeyi
    'stack alindi' kaydina dahil etmemesi ve 'yine atladin' diye elestirmemesi icin."""
    rows = active_supplement_breaks()
    if not rows:
        return 'ARA VERILEN TAKVIYELER: su an yok.'
    parts = [
        f"{r['target_name']} ({'stack' if r['target_type'] == 'stack' else 'urun'}, "
        + (f"{r['end_date']} bitis" if r['end_date'] else 'suresiz') + ')'
        for r in rows
    ]
    return ('ARA VERILEN TAKVIYELER: ' + '; '.join(parts)
            + ". Bunlari stack kaydina dahil etme, eksik/atlandi diye elestirme - bilincli ara, UI'da 'Ara Veriliyor' gorunur.")


def gather_day_snapshot(conn, date_str):
    """Bir gunun tum gercek verisini tek bir dict'te toplar (AI Koc payload'i ve Coach
    sayfasi gosterimi AYNI bu fonksiyonu kullanir, ikisi hep birbirini tutsun diye)."""
    meals = conn.execute("SELECT * FROM meal_entries WHERE date=?", (date_str,)).fetchall()
    nutrition = conn.execute("SELECT SUM(water_ml) w FROM nutrition_logs WHERE date=?", (date_str,)).fetchone()
    step_row = conn.execute("SELECT steps FROM step_logs WHERE date=?", (date_str,)).fetchone()
    body_row = conn.execute("SELECT weight_kg, weight_kg_night FROM body_metrics WHERE date=?", (date_str,)).fetchone()
    mood_row = conn.execute("SELECT energy, mood, stress, notes FROM mood_logs WHERE date=?", (date_str,)).fetchone()
    sleep_row = conn.execute("SELECT hours, quality, bedtime, wake_time FROM sleep_logs WHERE date=?", (date_str,)).fetchone()
    whoop = conn.execute(
        "SELECT recovery_score, strain, sleep_hours, sleep_performance, hrv_ms, rhr_bpm FROM whoop_daily WHERE date=?",
        (date_str,)
    ).fetchone()
    supp_taken = conn.execute(SUPP_TAKEN_COUNT_SQL, (date_str,)).fetchone()['c']
    # supp_total ara verilen stack/urunleri saymaz - yoksa Koc arada olan takviyeyi 'eksik' sanir
    _breaks = active_supplement_breaks()
    _broken_stacks = {b['target_name'] for b in _breaks if b['target_type'] == 'stack'}
    _broken_products = {b['target_name'] for b in _breaks if b['target_type'] == 'product'}
    _items = conn.execute(
        "SELECT s.name stack_name, si.product_name FROM supplement_stack_items si "
        "JOIN supplement_stacks s ON s.id=si.stack_id WHERE s.active=1"
    ).fetchall()
    supp_total = sum(1 for it in _items
                     if it['stack_name'] not in _broken_stacks and it['product_name'] not in _broken_products)
    # Cinko gunu degilse (Pzt/Car/Cum disi) ve bugun alinmadiysa beklenmez - toplamdan dus.
    _z_today = conn.execute("SELECT 1 FROM vitamin_logs WHERE date=? AND name=? LIMIT 1",
                            (date_str, ZINC_PRODUCT_NAME)).fetchone()
    if date.fromisoformat(date_str).weekday() not in ZINC_DAYS and not _z_today and any(
            it['product_name'] == ZINC_PRODUCT_NAME and it['stack_name'] not in _broken_stacks
            and it['product_name'] not in _broken_products for it in _items):
        supp_total -= 1
    td = training_day(date_str)
    sess_row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
    session_done = False
    exercise_names_today = []
    if sess_row and sess_row['value']:
        try:
            all_sessions = json.loads(sess_row['value'])
            todays = [s for s in all_sessions if s.get('date') == date_str]
            session_done = bool(todays)
            for s in todays:
                exercise_names_today.extend(e.get('name') for e in (s.get('exercises') or []) if e.get('name'))
        except Exception:
            pass
    # O gun calisilan hareketlerin gercek coklu-seans trendi (o tarihe kadar) - Koc'un
    # sadece "bugun ne yaptin" degil "bu hareketler nereye gidiyor" bilmesi icin.
    hareket_trendleri = []
    if exercise_names_today:
        all_trends = compute_exercise_trends(end_date=date_str)
        by_name = {t['exercise']: t for t in all_trends}
        hareket_trendleri = [by_name[n] for n in set(exercise_names_today) if n in by_name]
    kcal = round(sum(m['calories'] or 0 for m in meals))
    skin_rows = conn.execute(
        "SELECT area, name, status, notes FROM skin_logs WHERE date=? ORDER BY ts", (date_str,)
    ).fetchall()
    return {
        'date': date_str,
        'training': {'type': td, 'done': session_done},
        'nutrition': {
            'kcal': kcal,
            'protein_g': round(sum(m['protein_g'] or 0 for m in meals)),
            'carbs_g': round(sum(m['carbs_g'] or 0 for m in meals)),
            'fat_g': round(sum(m['fat_g'] or 0 for m in meals)),
            'water_ml': int(nutrition['w'] or 0) if nutrition else 0,
            'ogunler': [
                {'slot': m['slot'], 'title': m['title'], 'kcal': m['calories']}
                for m in meals
            ],
        },
        'steps': int(step_row['steps']) if step_row and step_row['steps'] else None,
        'weight': {'morning': body_row['weight_kg'] if body_row else None, 'night': body_row['weight_kg_night'] if body_row else None},
        'mood': dict(mood_row) if mood_row else None,
        'sleep_manuel': dict(sleep_row) if sleep_row else None,
        'whoop': dict(whoop) if whoop else None,
        'whoop_workouts': get_workouts_for_date(date_str),
        'supplements': {'taken': supp_taken, 'total': supp_total,
                        'ara_verilenler': [f"{b['target_name']} ({b['target_type']}"
                                           + (f", {b['end_date']} bitis)" if b['end_date'] else ', suresiz)')
                                           for b in _breaks]},
        'antrenman_hareket_trendleri': hareket_trendleri,
        'cilt': [dict(r) for r in skin_rows],
        'has_data': bool(meals or whoop or body_row or session_done or (nutrition and nutrition['w']) or skin_rows or mood_row or sleep_row),
    }

def generate_daily_profile_note(date_str):
    """Belirli bir gun icin AI Koc'un 'kullaniciyi taniyan' gunluk gozlem notunu uretir.
    Plan degistirmez, sadece gozlemler; onceki notlari ve kalici profil notlarini baglam
    olarak gorur ki zamanla tekrar eden kaliplari (hafta sonu su dususu vb.) fark edebilsin.
    Yeterince tekrar eden bir kalip fark ederse yeni bir kalici profil notu da onerebilir."""
    import urllib.request
    if not ANTHROPIC_API_KEY:
        return None
    conn = get_db()
    snap = gather_day_snapshot(conn, date_str)
    if not snap['has_data']:
        conn.close()
        return None  # bu gun icin hicbir veri yok, uydurma yapma
    yday_str = (date.fromisoformat(date_str) - timedelta(days=1)).isoformat()
    yday_snap = gather_day_snapshot(conn, yday_str)
    weight_trend = conn.execute(
        "SELECT date, weight_kg FROM body_metrics WHERE weight_kg IS NOT NULL AND date<=? ORDER BY date DESC LIMIT 14",
        (date_str,)
    ).fetchall()
    prev_notes = conn.execute(
        "SELECT date, note FROM ai_profile_notes WHERE date<? ORDER BY date DESC LIMIT 5", (date_str,)
    ).fetchall()
    profile_facts = conn.execute(
        "SELECT text FROM user_profile_facts WHERE active=1 ORDER BY created_at"
    ).fetchall()
    conn.close()

    weight_delta = None
    if snap['weight']['morning'] is not None and yday_snap['weight']['morning'] is not None:
        weight_delta = round(snap['weight']['morning'] - yday_snap['weight']['morning'], 2)

    payload = {
        'tarih': date_str,
        'bugun': {
            'antrenman': snap['training'], 'beslenme': snap['nutrition'], 'adim': snap['steps'],
            'kilo': snap['weight'], 'whoop': snap['whoop'], 'takviye': snap['supplements'],
            'whoop_antrenman_detayi': snap['whoop_workouts'],
            'hareket_trendleri': snap['antrenman_hareket_trendleri'],
            'cilt': snap['cilt'], 'ruh_hali': snap['mood'], 'uyku_manuel': snap['sleep_manuel'],
        },
        'dun': {
            'tarih': yday_str, 'antrenman': yday_snap['training'], 'beslenme': yday_snap['nutrition'],
            'kilo': yday_snap['weight'], 'cilt': yday_snap['cilt'], 'ruh_hali': yday_snap['mood'],
            'uyku_manuel': yday_snap['sleep_manuel'],
        } if yday_snap['has_data'] else None,
        'sabah_kilo_farki_dunden_bugune_kg': weight_delta,
        'kilo_trend_son_gunler': [{'date': r['date'], 'kg': r['weight_kg']} for r in reversed(weight_trend)],
        'onceki_notlar': [{'tarih': r['date'], 'not': r['note']} for r in reversed(prev_notes)],
        'bilinen_kalici_notlar': [r['text'] for r in profile_facts],
    }
    system_prompt = (
        'Sen kullanıcıyı zamanla tanıyan kişisel bir gözlemci AI koçsun. Görevin PLAN DEĞİŞTİRMEK '
        'DEĞİL — sadece bugünün ve dünün gerçek verisini, önceki notları ve bilinen kalıcı profil '
        'notlarını okuyup kullanıcı hakkında sessizce öğrenmek.\n\n'
        'Şunlara özellikle dikkat et:\n'
        '- "sabah_kilo_farki_dunden_bugune_kg" varsa, bunu DÜNÜN yemek/idman/su verisiyle ilişkilendirerek '
        'yorumla (ör. yüksek karbonhidrat/tuz sonrası su tutması, düşük su sonrası kilo düşüşü, antrenman '
        'sonrası glikojen+su etkisi gibi basit, gerçekçi bir açıklama — kesin bilim gibi sunma, olası bir '
        'bağlantı olarak sun).\n'
        '- Önceki notlarla karşılaştırınca tekrar eden bir kalıp fark edersen (hafta sonu su düşüklüğü, '
        'belirli antrenmandan sonra recovery düşmesi, geç saatte yeme eğilimi vb.) açıkça belirt.\n'
        '- "bilinen_kalici_notlar" listesindeki bilgileri (ör. cilt/sivilce sorunu gibi) bugünün verisiyle '
        'alakalıysa (ör. yağlı/süt ürünü ağırlıklı beslenme günü) nazikçe bağlantı kur, alakasızsa hiç değinme.\n'
        '- "hareket_trendleri" varsa bugün çalıştığın hareketlerin 3 haftalık gerçek yönünü gösterir '
        '(yukseliyor/dalgali_net_yukari/platoda/geriliyor, tahmini 1RM bazlı). Tek günlük ağırlık farkına değil '
        'bu trende güven — "geriliyor" diyorsa gerçekten uyar, "dalgali_net_yukari" diyorsa bugün düşük görünse '
        'bile net olarak ilerlediğini belirt, yanlış alarm verme.\n'
        '- "whoop_antrenman_detayi" varsa bugünün gerçek (cihazın algıladığı) antrenman süresi/strain/kalori '
        'bilgisidir — antrenmandan bahsedeceksen bunu kullan, tahmin etme.\n'
        '- "ruh_hali" (enerji/mood/stres, kullanıcının kendi girdiği) ve "uyku_manuel" (WHOOP dışı elle girilen '
        'uyku saati/kalitesi) varsa bunlar en kişisel verilerdir — düşük enerji/yüksek stres günlerini beslenme, '
        'antrenman yoğunluğu veya uykuyla ilişkilendirmeye çalış, görmezden gelme.\n'
        '- "cilt" listesi bugün/dün kaydedilen cilt/sivilce takibidir (alan, ürün, durum) — beslenmeyle '
        '(yağlı/süt ürünü/şeker ağırlıklı gün) ya da uyku/stresle bağlantılı olabilir, alakalıysa belirt.\n\n'
        '2-4 cümlelik Türkçe, kısa bir günlük gözlem yaz — ama ROBOT gibi nötr değil, gerçek bir arkadaş/koç '
        'gibi. Veri gerçekten iyiyse sevin, gurur duy; art arda aynı hata tekrarlanıyorsa (kaydedilen veri '
        'gerçekten gösteriyorsa) hafif sitem/hayal kırıklığı göster — ama bu HER ZAMAN veriye dayanmalı, '
        'rastgele ya da abartılı olmasın. Plan değiştirme önerisi yapma, sadece gözlemle (ama gözlemi soğuk bir '
        'rapor gibi değil, içten bir insan gibi yaz). Veri eksikse o alan hakkında hiçbir şey uydurma.\n\n'
        '"yeni_kalici_not" alanını NEREDEYSE HER ZAMAN null bırak. Sadece şu ÜÇ koşulun HEPSİ doğruysa doldur: '
        '(1) gerçekten kalıcı, tek günlük bir olay değil, en az 3 kez gördüğün bir kalıp, (2) '
        '"bilinen_kalici_notlar" listesindeki HİÇBİR maddenin farklı kelimelerle söylenmiş hali DEĞİL — yani '
        'listedekilerle AYNI ANLAMA gelen bir cümleyi asla tekrar yazma, farklı kelimeler kullansan bile bu '
        'yine de aynı bilgidir ve null dönmen gerekir, (3) küçük bir varyasyon/güncelleme değil, tamamen yeni '
        'bir gözlem. Şüphedeysen null. Bu alan çoğu günde null olmalı — art arda birçok günde dolu dönüyorsan '
        'muhtemelen aynı şeyi tekrar tekrar farklı cümlelerle yazıyorsundur, bunu YAPMA.\n\n'
        'SADECE şu JSON formatında dön, başka hiçbir şey yazma: '
        '{"note": "...", "yeni_kalici_not": "..." veya null}'
    )
    body = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': 400,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    }
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps(body).encode('utf-8'),
        headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        text = result['content'][0]['text'].strip()
        if text.startswith('```'):
            text = text.strip('`')
            if text.startswith('json'):
                text = text[4:]
        parsed = json.loads(text.strip())
        note = (parsed.get('note') or '').strip()
        new_fact = (parsed.get('yeni_kalici_not') or '').strip() or None
    except Exception as _e:
        import logging; logging.getLogger('daily').warning(f"ai profile note failed: {_e}")
        return None
    if not note:
        return None
    conn2 = get_db()
    conn2.execute(
        "INSERT INTO ai_profile_notes (date, note) VALUES (?,?) "
        "ON CONFLICT(date) DO UPDATE SET note=excluded.note, generated_at=CURRENT_TIMESTAMP",
        (date_str, note)
    )
    if new_fact:
        existing = [r['text'] for r in conn2.execute("SELECT text FROM user_profile_facts WHERE active=1").fetchall()]
        if not any(_facts_similar(new_fact, e) for e in existing):
            conn2.execute("INSERT INTO user_profile_facts (text, source) VALUES (?, 'ai_detected')", (new_fact,))
    conn2.commit(); conn2.close()
    return note

def ensure_yesterday_ai_note():
    """Gun sonunda (lazy, bir sonraki istekte) dunun AI Koc notu eksikse uretir.
    Cron yok; kullanici uygulamayi actikca kendi kendini tamamlar."""
    try:
        yday = (operation_date() - timedelta(days=1)).isoformat()
        conn = get_db()
        row = conn.execute("SELECT 1 FROM ai_profile_notes WHERE date=?", (yday,)).fetchone()
        conn.close()
        if not row:
            # Istegi bloklamadan arka planda uret - 30 sn'lik Claude cagrisi sayfa
            # acilisini bekletmesin, not bir sonraki ziyarette gorunur.
            _spawn_bg_ai(f'yday_note_{yday}', lambda: generate_daily_profile_note(yday))
    except Exception as _e:
        import logging; logging.getLogger('daily').warning(f"ensure_yesterday_ai_note failed: {_e}")

@app.route('/api/ai-coach/notes', methods=['GET'])
def api_ai_coach_notes():
    ensure_yesterday_ai_note()
    days = int(request.args.get('days', 30))
    conn = get_db()
    rows = conn.execute(
        "SELECT date, note, generated_at FROM ai_profile_notes ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        snap = gather_day_snapshot(conn, d['date'])
        prev = conn.execute(
            "SELECT weight_kg FROM body_metrics WHERE weight_kg IS NOT NULL AND date<? ORDER BY date DESC LIMIT 1",
            (d['date'],)
        ).fetchone()
        weight_delta = None
        if snap['weight']['morning'] is not None and prev and prev['weight_kg'] is not None:
            weight_delta = round(snap['weight']['morning'] - prev['weight_kg'], 2)
        d['summary'] = snap
        d['summary']['weight_delta'] = weight_delta
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route('/api/ai-coach/notes/<date_str>', methods=['GET'])
def api_ai_coach_note_for_date(date_str):
    conn = get_db()
    row = conn.execute("SELECT date, note, generated_at FROM ai_profile_notes WHERE date=?", (date_str,)).fetchone()
    conn.close()
    return jsonify(dict(row) if row else None)

@app.route('/api/ai-coach/notes/<date_str>/generate', methods=['POST'])
def api_ai_coach_generate(date_str):
    note = generate_daily_profile_note(date_str)
    if note is None:
        return jsonify({'ok': False, 'error': 'Bu gün için yeterli veri yok veya AI anahtarı tanımlı değil'}), 400
    return jsonify({'ok': True, 'date': date_str, 'note': note})

@app.route('/api/profile/facts', methods=['GET'])
def api_profile_facts_get():
    conn = get_db()
    rows = conn.execute("SELECT * FROM user_profile_facts WHERE active=1 ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/profile/facts', methods=['POST'])
def api_profile_facts_add():
    data = request.get_json(force=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'text gerekli'}), 400
    conn = get_db()
    conn.execute("INSERT INTO user_profile_facts (text, source) VALUES (?, 'manual')", (text,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/profile/facts/<int:fid>', methods=['DELETE'])
def api_profile_facts_delete(fid):
    conn = get_db()
    conn.execute("UPDATE user_profile_facts SET active=0 WHERE id=?", (fid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/cycle/ai-comment', methods=['GET'])
def api_cycle_ai_comment():
    conn = get_db()
    row = conn.execute("SELECT value FROM user_settings WHERE key='cycle_ai_comment'").fetchone()
    conn.close()
    return jsonify({'comment': row['value'] if row else ''})

@app.route('/api/cycle', methods=['GET'])
def api_cycle_get():
    ensure_cycle_days_table()
    conn = get_db()
    rows = conn.execute("SELECT * FROM cycle_days ORDER BY day_index").fetchall()
    conn.close()
    days = [{'day_index': r['day_index'], 'type': r['type'], 'protein_g': r['protein_g'],
             'carb_g': r['carb_g'], 'fat_g': r['fat_g']} for r in rows]
    return jsonify({'days': days, 'active_day': get_cycle_active_day()})

@app.route('/api/cycle', methods=['PUT'])
def api_cycle_put():
    data = request.get_json(force=True)
    days = data if isinstance(data, list) else (data or {}).get('days', [])
    if not isinstance(days, list) or len(days) != 7:
        return jsonify({'ok': False, 'error': '7 gunluk dizi gerekli'}), 400
    conn = get_db()
    for d in days:
        conn.execute(
            "UPDATE cycle_days SET type=?, protein_g=?, carb_g=?, fat_g=? WHERE day_index=?",
            (str(d.get('type', ''))[:16], int(d.get('protein_g') or 0), int(d.get('carb_g') or 0),
             int(d.get('fat_g') or 0), int(d.get('day_index')))
        )
    conn.commit(); conn.close()
    try:
        generate_cycle_ai_comment()
    except Exception as _e:
        import logging; logging.getLogger('daily').warning(f"cycle AI comment failed: {_e}")
    return jsonify({'ok': True})

@app.route('/api/cycle/active-day', methods=['PATCH'])
def api_cycle_active_day():
    data = request.get_json(force=True) or {}
    day_index = data.get('day_index')
    if day_index is None:
        return jsonify({'ok': False, 'error': 'day_index gerekli'}), 400
    date_str = data.get('date') or operation_today()
    set_cycle_active_day(int(day_index), date_str)
    return jsonify({'ok': True, 'active_day': int(day_index), 'date': date_str})

@app.route('/api/cycle/active-day', methods=['DELETE'])
def api_cycle_active_day_clear():
    """Belirli bir tarihin telafi override'ini temizler (o gun tekrar haftanin dogal gunune doner)."""
    data = request.get_json(force=True) or {}
    date_str = data.get('date') or operation_today()
    clear_cycle_active_day(date_str)
    return jsonify({'ok': True, 'date': date_str})

@app.route('/api/carb-cycle', methods=['GET'])
def api_carb_cycle_get():
    """Geriye donuk uyumluluk sarmalayicisi: yeni cycle_days tablosundan eski (type-keyed)
    formatta dondurur. Dashboard/getT() gibi eski tuketiciler degismeden calismaya devam eder."""
    ensure_cycle_days_table()
    conn = get_db()
    rows = conn.execute("SELECT * FROM cycle_days ORDER BY day_index").fetchall()
    conn.close()
    plan = {r['type']: {'cal': 4*r['protein_g'] + 4*r['carb_g'] + 9*r['fat_g'],
                        'protein': r['protein_g'], 'carb': r['carb_g'], 'fat': r['fat_g'], 'notes': ''}
            for r in rows}
    active_idx = get_cycle_active_day()
    active_row = next((r for r in rows if r['day_index'] == active_idx), None)
    today_type = active_row['type'] if active_row else ''
    auto_row = next((r for r in rows if r['day_index'] == operation_date().weekday()), None)
    auto_type = auto_row['type'] if auto_row else ''
    today_targets = plan.get(today_type)
    return jsonify({'plan': plan, 'today_type': today_type, 'today_targets': today_targets,
                    'auto_type': auto_type, 'is_manual': active_idx != operation_date().weekday()})


@app.route('/api/carb-cycle', methods=['PUT'])
def api_carb_cycle_put():
    """Karb cycle plan hedeflerini guncelle"""
    data = request.get_json() or {}
    conn = get_db()
    for day_type, vals in data.items():
        if day_type in ['Push','Pull','Legs','Upper','Lower','Off1','Off2']:
            conn.execute(
                'UPDATE carb_cycle_plan SET cal=?,protein=?,carb=?,fat=? WHERE day_type=?',
                (int(vals.get('cal',0)), int(vals.get('protein',0)),
                 int(vals.get('carb',0)), int(vals.get('fat',0)), day_type)
            )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})
@app.route('/api/carb-cycle', methods=['PATCH'])
def api_carb_cycle_patch():
    """Bir gun tipinin makro hedeflerini guncelle."""
    data = request.get_json(force=True) or {}
    day_type = data.get('day_type')
    if not day_type:
        return jsonify({'error': 'day_type required'}), 400
    conn = get_db()
    conn.execute(
        "UPDATE carb_cycle_plan SET cal=?, protein=?, carb=?, fat=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE day_type=?",
        (data.get('cal'), data.get('protein'), data.get('carb'), data.get('fat'), data.get('notes', ''), day_type)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/carb-cycle/select', methods=['POST'])
def api_carb_cycle_select():
    """Bugun icin manuel gun tipi sec â gun bazli key ile sakla."""
    data = request.get_json(force=True) or {}
    day_type = data.get('day_type', '')
    today_str = operation_today()
    conn = get_db()
    if day_type:
        row = conn.execute("SELECT day_index FROM cycle_days WHERE type=?", (day_type,)).fetchone()
        if row:
            conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES (?,?)",
                         (f'cycle_active_day_{today_str}', str(row['day_index'])))
    else:
        # Secimi kaldir (otomatiğe don)
        conn.execute("DELETE FROM user_settings WHERE key=?", (f'cycle_active_day_{today_str}',))
    # Eski genel key de guncelle (geriye uyumluluk)
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES (?,?)",
                 ('cycle_day_type', day_type))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'day_type': day_type})


@app.route('/api/vitamin-logs/<int:lid>/note', methods=['PATCH'])
def api_vitamin_log_note_patch(lid):
    """Vitamin log notunu guncelle."""
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("UPDATE vitamin_logs SET notes=? WHERE id=?", (data.get('notes',''), lid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

def db_upsert(table, date_val, data: dict):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT id FROM {table} WHERE date=?", (date_val,))
    row = cur.fetchone()
    if row:
        sets = ', '.join(f"{k}=?" for k in data)
        cur.execute(f"UPDATE {table} SET {sets} WHERE date=?", list(data.values()) + [date_val])
    else:
        data['date'] = date_val
        cols = ', '.join(data.keys())
        plhs = ', '.join('?' for _ in data)
        cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({plhs})", list(data.values()))
    conn.commit(); conn.close()

def db_fetch_range(table, days=7):
    conn = get_db()
    start = (operation_date() - timedelta(days=days-1)).isoformat()
    rows = conn.execute(f"SELECT * FROM {table} WHERE date >= ? ORDER BY date ASC", (start,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_today(table):
    conn = get_db()
    row = conn.execute(f"SELECT * FROM {table} WHERE date=? LIMIT 1", (operation_today(),)).fetchone()
    conn.close()
    return dict(row) if row else {}

def db_date(table, date_str):
    conn = get_db()
    if table == 'vitamin_logs':
        rows = conn.execute(f"SELECT * FROM {table} WHERE date=? ORDER BY ts", (date_str,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    row = conn.execute(f"SELECT * FROM {table} WHERE date=? LIMIT 1", (date_str,)).fetchone()
    conn.close()
    return dict(row) if row else {}

def streak_count():
    conn = get_db()
    tables = ('sleep_logs','exercise_logs','nutrition_logs','work_logs','coaching_logs','mood_logs','vitamin_logs','meal_entries')
    today = operation_date()
    today_found = any(conn.execute(f"SELECT id FROM {t} WHERE date=?", (today.isoformat(),)).fetchone() for t in tables)
    n, d = 0, today if today_found else today - timedelta(days=1)
    while True:
        found = any(conn.execute(f"SELECT id FROM {t} WHERE date=?", (d.isoformat(),)).fetchone() for t in tables)
        if not found: break
        n += 1; d -= timedelta(days=1)
    conn.close()
    return n

# âââ FLASK ROUTES ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ


@app.after_request
def no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    if response.content_type and 'html' in response.content_type and not response.direct_passthrough and response.data:
        response.data = response.data.replace('⏭'.encode('utf-8'), '⚠️'.encode('utf-8'))
    return response

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/training')
def training():
    return render_template('training_proto.html')

@app.route('/api/today')
def api_today():
    today = request.args.get('date') or operation_today()
    ensure_step_logs_table()
    ensure_body_metrics_table()
    conn = get_db()
    vitamins = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY COALESCE(display_order, 999999), id", (today,)).fetchall()]
    note_row = conn.execute("SELECT note FROM daily_notes WHERE date=?", (today,)).fetchone()
    step_row = conn.execute("SELECT * FROM step_logs WHERE date=?", (today,)).fetchone()
    body_row = conn.execute("SELECT * FROM body_metrics WHERE date=?", (today,)).fetchone()
    # Su: cok satir olabilir, SUM kullan
    water_row = conn.execute("SELECT SUM(water_ml) as total FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    water_ml_total = int(water_row['total'] or 0) if water_row else 0
    conn.close()
    nutrition = db_date('nutrition_logs', today)
    nutrition['water_ml'] = water_ml_total  # SUM ile dogru toplam
    return jsonify({
        'sleep': db_date('sleep_logs', today), 'exercise': db_date('exercise_logs', today),
        'nutrition': nutrition, 'work': db_date('work_logs', today),
        'coaching': db_date('coaching_logs', today), 'mood': db_date('mood_logs', today),
        'vitamins': vitamins,
        'steps': dict(step_row) if step_row else {'date': today, 'steps': 0, 'notes': ''},
        'body': dict(body_row) if body_row else {'date': today, 'weight_kg': None, 'waist_cm': None, 'chest_cm': None, 'arm_cm': None, 'notes': ''},
        'note': note_row['note'] if note_row else '',
        'training_day': training_day(today),
        'training_color': TRAINING_COLORS[training_day(today)],
        'streak': streak_count(), 'date': today,
    })

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    conn = get_db()
    if request.method == 'POST':
        data = request.get_json(force=True) or {}
        for k, v in data.items():
            conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES (?,?)", (k, str(v)))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    else:
        rows = conn.execute("SELECT key, value FROM user_settings").fetchall()
        conn.close()
        result = {r['key']: r['value'] for r in rows}
        return jsonify(result)

# 5 vardiya sablonu: kullanici 5 farkli vardiyada donusumlu calisiyor. Shift 1
# (14:00-22:00) pazartesiden itibaren gecerli olacak; Shift 2 su anki aksam vardiyasi.
# Kalan 3'unu kullanici Ayarlar'dan bir kez doldurur, sonra tek tikla/tek mesajla gecis.
DEFAULT_SHIFT_TEMPLATES = [
    {'name': 'Shift 1', 'start': '14:00', 'end': '22:00'},
    {'name': 'Shift 2', 'start': '18:00', 'end': '03:00'},
    {'name': 'Shift 3', 'start': '', 'end': ''},
    {'name': 'Shift 4', 'start': '', 'end': ''},
    {'name': 'Shift 5', 'start': '', 'end': ''},
]

def get_shift_templates():
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM user_settings WHERE key='shift_templates'").fetchone()
        conn.close()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, list) and data:
                return data
    except Exception:
        pass
    return [dict(t) for t in DEFAULT_SHIFT_TEMPLATES]


def tg_gunaydin_reply(raw, now=None):
    """Telegram'a 'gunaydin' yazilirsa ve saat henuz cutoff'tan ONCEyse (erken uyanma),
    operasyon gununu bugunun takvim gunune zorlar - sayfa sifirlanir, yeni gun baslar.
    force_operation_date operation_date() icinde zaten sadece gercek bugunle eslesirse
    gecerli ve ertesi gun otomatik temizleniyor. Donen dict: {'reply': str, 'reset': bool}.
    reply doluysa kisa selam mesajidir (mesaj sadece selamsa) - direkt gonderilip donulur;
    reset=True ama reply bossa mesajin devami normal akisla islenir, sonuca not eklenir."""
    norm = _tg_norm(raw or '')
    if 'gunaydin' not in norm and 'yeni gune basla' not in norm:
        return {'reply': '', 'reset': False}
    now = now or now_istanbul()
    if now.hour >= operation_cutoff_hour(now):
        return {'reply': '', 'reset': False}  # cutoff gecti, zaten yeni gun - AI normal selamlasir
    today_cal = now.date().isoformat()
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('force_operation_date', ?)", (today_cal,))
    conn.commit(); conn.close()
    short = len(norm.strip()) <= 30
    reply = f"Günaydın! ☀️ Erken kalkmışsın — yeni gün başlatıldı ({today_cal}). Sayaçlar sıfırdan, gününe başlayabilirsin." if short else ''
    return {'reply': reply, 'reset': True}


def apply_work_shift(start, end, label=''):
    """Aktif vardiyayi kaydeder ve tum bagimli sistemleri (op-gunu kesimi, WHOOP workout
    bucket'i) hemen gunceller. Hem /api/settings/shift hem Telegram work_shift action'i
    ayni fonksiyonu kullanir."""
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('work_shift', ?)",
                 (json.dumps({'start': start, 'end': end, 'label': label or 'vardiya'}, ensure_ascii=False),))
    conn.commit(); conn.close()
    invalidate_shift_cache()
    try:
        _whoop_mod.OP_CUTOFF_HOUR = operation_cutoff_hour()
    except Exception:
        pass
    return current_shift_info()


def _valid_shift_time(s):
    """SS:DD formati + gecerli saat araligi (00-23:00-59)."""
    m = re.match(r'^(\d{1,2}):(\d{2})$', s or '')
    return bool(m) and int(m.group(1)) <= 23 and int(m.group(2)) <= 59


@app.route('/api/settings/shift', methods=['GET', 'POST'])
def api_settings_shift():
    """Vardiya goruntule/degistir. Kesim saati vardiya bitis+11 kuralindan otomatik turer."""
    if request.method == 'POST':
        data = request.get_json(force=True) or {}
        start = (data.get('start') or '').strip()
        end = (data.get('end') or '').strip()
        if not _valid_shift_time(start) or not _valid_shift_time(end):
            return jsonify({'ok': False, 'error': 'start/end SS:DD formatinda olmali (or. 14:00)'}), 400
        info = apply_work_shift(start, end, (data.get('label') or '').strip())
        return jsonify({'ok': True, 'shift': info})
    return jsonify({'shift': current_shift_info(), 'templates': get_shift_templates()})


@app.route('/api/settings/shift-templates', methods=['POST'])
def api_settings_shift_templates():
    """5 vardiya sablonunu kaydeder. Bos start/end = henuz tanimlanmamis sablon (gecerli)."""
    data = request.get_json(force=True) or {}
    templates = data.get('templates')
    if not isinstance(templates, list) or not (1 <= len(templates) <= 8):
        return jsonify({'ok': False, 'error': 'templates listesi gerekli'}), 400
    cleaned = []
    for i, t in enumerate(templates, start=1):
        st = (t.get('start') or '').strip()
        en = (t.get('end') or '').strip()
        if (st or en) and not (_valid_shift_time(st) and _valid_shift_time(en)):
            return jsonify({'ok': False, 'error': f'Shift {i}: saatler SS:DD formatinda olmali'}), 400
        cleaned.append({'name': (t.get('name') or f'Shift {i}').strip(), 'start': st, 'end': en})
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('shift_templates', ?)",
                 (json.dumps(cleaned, ensure_ascii=False),))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'templates': cleaned})


@app.route('/api/new-day', methods=['POST'])
def api_new_day():
    """Gunaydın: operation tarihi bugunun takvim tarihine ayarla (DB'ye kaydet).
    TR takvim gunu kullanilir - sunucu UTC'deyken date.today() gece 00:00-03:00 TR
    arasi dunu verir ve operation_date() override'i esitsizlikten hemen silerdi."""
    today = now_istanbul().date().isoformat()
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('force_operation_date', ?)", (today,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': today})

@app.route('/api/new-day', methods=['DELETE'])
def api_new_day_clear():
    """Override temizle, tekrar shift sistemine don"""
    conn = get_db()
    conn.execute("DELETE FROM user_settings WHERE key='force_operation_date'")
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/debug-streak')
def api_debug_streak():
    from datetime import timedelta as _td
    conn = get_db()
    op = operation_date()
    tables = ('sleep_logs','exercise_logs','nutrition_logs','work_logs','coaching_logs','mood_logs','vitamin_logs','meal_entries')
    days_out = {}
    for i in range(5):
        dd = op - _td(days=i)
        day_res = {}
        for t in tables:
            try:
                row = conn.execute(f"SELECT id FROM {t} WHERE date=?", (dd.isoformat(),)).fetchone()
                day_res[t] = bool(row)
            except Exception as e:
                day_res[t] = 'ERR:'+str(e)[:40]
        days_out[dd.isoformat()] = day_res
    conn.close()
    return jsonify({'op_date': op.isoformat(), 'days': days_out})

@app.route('/api/reload-templates')
def api_reload_templates():
    """Force Jinja2 template cache clear â no restart needed"""
    if app.jinja_env.cache:
        app.jinja_env.cache.clear()
    app.jinja_env.auto_reload = True
    return jsonify({'ok': True, 'msg': 'Template cache temizlendi'})

@app.route('/api/week')
def api_week():
    days = int(request.args.get('days', 7))
    return jsonify({
        'sleep': db_fetch_range('sleep_logs', days),
        'exercise': db_fetch_range('exercise_logs', days),
        'nutrition': db_fetch_range('nutrition_logs', days),
        'work': db_fetch_range('work_logs', days),
        'coaching': db_fetch_range('coaching_logs', days),
        'mood': db_fetch_range('mood_logs', days),
    })

@app.route('/api/log/<category>', methods=['POST'])
def api_log(category):
    tables = {'sleep':'sleep_logs','exercise':'exercise_logs','nutrition':'nutrition_logs',
              'work':'work_logs','coaching':'coaching_logs','mood':'mood_logs'}
    if category not in tables:
        return jsonify({'error': 'Gecersiz kategori'}), 400
    data = request.get_json(force=True) or {}
    d = data.pop('date', operation_today())
    # Su icin ozel konsolidasyon: cok satir sorunundan kacin
    if category == 'nutrition' and 'water_ml' in data:
        ml = int(data.get('water_ml') or 0)
        conn = get_db()
        conn.execute("UPDATE nutrition_logs SET water_ml=0 WHERE date=?", (d,))
        row = conn.execute("SELECT id FROM nutrition_logs WHERE date=?", (d,)).fetchone()
        if row:
            conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE id=?", (ml, row['id']))
        else:
            conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (d, ml))
        conn.commit(); conn.close()
        other = {k: v for k, v in data.items() if k != 'water_ml'}
        if other:
            db_upsert('nutrition_logs', d, other)
    else:
        db_upsert(tables[category], d, data)
    return jsonify({'ok': True, 'date': d})



def _table_count_for_date(conn, table, date_col='date', d=None):
    d = d or operation_today()
    try:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {date_col}=?", (d,)).fetchone()
        return int(row['c'] if row else 0)
    except Exception:
        return 0

@app.route('/api/system/status')
def api_system_status():
    d = request.args.get('date', operation_today())
    conn = get_db()
    status = {
        'date': d,
        'db_path': DB_PATH,
        'config_path': CONFIG_PATH,
        'telegram_enabled': bool(TELEGRAM_TOKEN),
        'openai_enabled': bool(OPENAI_API_KEY),
        'openai_model': OPENAI_MODEL,
        'counts': {
            'meals': _table_count_for_date(conn, 'meal_entries', d=d),
            'vitamins': _table_count_for_date(conn, 'vitamin_logs', d=d),
            'training_logs': _table_count_for_date(conn, 'training_day_logs', d=d),
            'sleep': _table_count_for_date(conn, 'sleep_logs', d=d),
            'mood': _table_count_for_date(conn, 'mood_logs', d=d),
            'water': _table_count_for_date(conn, 'nutrition_logs', d=d),
            'steps': _table_count_for_date(conn, 'step_logs', d=d),
            'notes': _table_count_for_date(conn, 'daily_notes', d=d),
            'templates': int((conn.execute("SELECT COUNT(*) AS c FROM quick_templates").fetchone() or {'c': 0})['c']),
            'telegram_messages': int((conn.execute("SELECT COUNT(*) AS c FROM telegram_messages").fetchone() or {'c': 0})['c']) if 'telegram_messages' else 0,
        }
    }
    conn.close()
    return jsonify(status)

@app.route('/api/day/<date_str>/clear', methods=['POST'])
def api_day_clear(date_str):
    data = request.get_json(force=True) or {}
    scopes = data.get('scopes') or []
    allowed = {
        'meals': ("meal_entries",),
        'vitamins': ("vitamin_logs",),
        'nutrition': ("nutrition_logs",),
        'sleep': ("sleep_logs",),
        'exercise': ("exercise_logs",),
        'mood': ("mood_logs",),
        'work': ("work_logs",),
        'coaching': ("coaching_logs",),
        'notes': ("daily_notes",),
        'steps': ("step_logs",),
        'training_logs': ("training_day_logs",),
        'skin': ("skin_logs",),
    }
    if scopes == ['all']:
        scopes = list(allowed.keys())
    conn = get_db()
    cleared = []
    for scope in scopes:
        item = allowed.get(scope)
        if not item:
            continue
        table = item[0]
        try:
            conn.execute(f"DELETE FROM {table} WHERE date=?", (date_str,))
            cleared.append(scope)
        except Exception:
            pass
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': date_str, 'cleared': cleared})

@app.route('/api/log/<category>', methods=['DELETE'])
def api_log_delete(category):
    tables = {'sleep':'sleep_logs','exercise':'exercise_logs','nutrition':'nutrition_logs',
              'work':'work_logs','coaching':'coaching_logs','mood':'mood_logs'}
    if category not in tables:
        return jsonify({'error': 'Gecersiz kategori'}), 400
    d = request.args.get('date', operation_today())
    conn = get_db()
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {tables[category]} WHERE date=?", (d,)).fetchall()]
    conn.execute(f"DELETE FROM {tables[category]} WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d, 'deleted': rows})

@app.route('/api/vitamins/today', methods=['DELETE'])
def api_vitamins_today_delete():
    d = request.args.get('date', operation_today())
    conn = get_db()
    conn.execute("DELETE FROM vitamin_logs WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/meals/today', methods=['DELETE'])
def api_meals_today_delete():
    d = request.args.get('date', operation_today())
    conn = get_db()
    conn.execute("DELETE FROM meal_entries WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/note', methods=['DELETE'])
def api_note_delete():
    d = request.args.get('date', operation_today())
    conn = get_db()
    row = conn.execute("SELECT * FROM daily_notes WHERE date=?", (d,)).fetchone()
    deleted = dict(row) if row else None
    conn.execute("DELETE FROM daily_notes WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d, 'deleted': deleted})



def ensure_step_logs_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS step_logs (
            date TEXT PRIMARY KEY,
            steps INTEGER DEFAULT 0,
            notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit(); conn.close()

def step_today(date_str=None):
    ensure_step_logs_table()
    d = date_str or operation_today()
    conn = get_db()
    row = conn.execute("SELECT * FROM step_logs WHERE date=?", (d,)).fetchone()
    conn.close()
    return dict(row) if row else {'date': d, 'steps': 0, 'notes': ''}

@app.route('/api/steps/today')
def api_steps_today():
    return jsonify(step_today(request.args.get('date')))

@app.route('/api/steps', methods=['POST'])
def api_steps_save():
    ensure_step_logs_table()
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    steps = int(data.get('steps') or 0)
    mode = data.get('mode', 'set')
    notes = data.get('notes', '')
    conn = get_db()
    row = conn.execute("SELECT steps FROM step_logs WHERE date=?", (d,)).fetchone()
    if row:
        new_steps = (row['steps'] or 0) + steps if mode == 'add' else steps
        conn.execute("UPDATE step_logs SET steps=?, notes=? WHERE date=?", (new_steps, notes, d))
    else:
        conn.execute("INSERT INTO step_logs (date, steps, notes) VALUES (?,?,?)", (d, steps, notes))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d, 'steps': step_today(d)['steps']})

@app.route('/api/steps/today', methods=['DELETE'])
def api_steps_delete():
    ensure_step_logs_table()
    d = request.args.get('date', operation_today())
    conn = get_db()
    row = conn.execute("SELECT * FROM step_logs WHERE date=?", (d,)).fetchone()
    deleted = dict(row) if row else None
    conn.execute("DELETE FROM step_logs WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d, 'deleted': deleted})

@app.route('/api/body', methods=['POST'])
def api_body_save():
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    kg = data.get('weight_kg')
    kg_night = data.get('weight_kg_night')
    conn = get_db()
    existing = conn.execute("SELECT date FROM body_metrics WHERE date=?", (d,)).fetchone()
    if existing:
        if kg_night is not None:
            conn.execute("UPDATE body_metrics SET weight_kg_night=? WHERE date=?", (kg_night, d))
        if kg is not None:
            conn.execute("UPDATE body_metrics SET weight_kg=? WHERE date=?", (kg, d))
    else:
        conn.execute("INSERT INTO body_metrics (date, weight_kg, weight_kg_night) VALUES (?,?,?)",
                     (d, kg, kg_night))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/vitamin', methods=['POST'])
def api_vitamin():
    data = request.get_json(force=True) or {}
    d = data.pop('date', operation_today())
    conn = get_db()
    conn.execute("INSERT INTO vitamin_logs (date, name, amount, unit, notes, status) VALUES (?,?,?,?,?,?)",
                 (d, data.get('name',''), data.get('amount',''), data.get('unit',''), data.get('notes',''), data.get('status','')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/vitamin/<int:vid>', methods=['DELETE'])
def api_vitamin_delete(vid):
    conn = get_db()
    row = conn.execute("SELECT * FROM vitamin_logs WHERE id=?", (vid,)).fetchone()
    deleted = dict(row) if row else None
    conn.execute("DELETE FROM vitamin_logs WHERE id=?", (vid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'deleted': deleted})


@app.route('/api/vitamin/<int:vid>', methods=['PATCH'])
def api_vitamin_patch(vid):
    """Kismi guncelleme - su an sadece display_order (Log'daki Takviye Sirasi
    yukari/asagi tasima) icin kullaniliyor, PUT gibi diger alanlari ezmez."""
    data = request.get_json(force=True) or {}
    if 'display_order' in data:
        try:
            order = int(data['display_order'])
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'display_order sayi olmali'}), 400
        conn = get_db()
        conn.execute("UPDATE vitamin_logs SET display_order=? WHERE id=?", (order, vid))
        conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/vitamin/<int:vid>', methods=['PUT'])
def api_vitamin_update(vid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("UPDATE vitamin_logs SET name=?, amount=?, unit=?, notes=?, status=? WHERE id=?",
                 (data.get('name','').strip(), data.get('amount','').strip(),
                  data.get('unit','').strip(), data.get('notes','').strip(), data.get('status','').strip(), vid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

CANONICAL_SLOTS = [
    ('TITLE-001', 'Kahvaltı',   1),
    ('TITLE-002', 'Meal 1',     2),
    ('TITLE-003', 'Pre Meal',   3),
    ('TITLE-004', 'Pre Snack',  4),
    ('TITLE-005', 'Post Meal',  5),
    ('TITLE-006', 'Post Snack', 6),
    ('TITLE-007', 'Snack',      7),
    ('TITLE-008', 'Gece',       8),
]

def seed_meal_titles():
    """Canonical öğün slot listesini yükle — kirli kayıtları sil, canonical'i uygula."""
    conn = get_db()
    # Canonical title_id setini belirle
    canonical_ids = {t[0] for t in CANONICAL_SLOTS}
    # Canonical olmayan (kullanıcı eklediği kirli) kayıtları sil
    conn.execute("DELETE FROM meal_titles WHERE title_id NOT IN ({})".format(
        ','.join('?' for _ in canonical_ids)), list(canonical_ids))
    # Eksik canonical'leri ekle / order_num'ı güncelle
    for tid, name, order in CANONICAL_SLOTS:
        existing = conn.execute("SELECT id FROM meal_titles WHERE title_id=?", (tid,)).fetchone()
        if existing:
            conn.execute("UPDATE meal_titles SET name=?, order_num=? WHERE title_id=?", (name, order, tid))
        else:
            try:
                conn.execute("INSERT INTO meal_titles (title_id,name,order_num) VALUES (?,?,?)", (tid, name, order))
            except: pass
    conn.commit()
    conn.close()

try:
    seed_meal_titles()
except Exception as _e:
    import logging; logging.getLogger('daily').warning(f"meal_titles seed failed: {_e}")

@app.route('/api/meal-titles', methods=['GET'])
def api_meal_titles_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM meal_titles ORDER BY order_num, name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/meal-titles', methods=['POST'])
def api_meal_titles_add():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name gerekli'}), 400
    conn = get_db()
    existing = conn.execute("SELECT id,title_id FROM meal_titles WHERE name=?", (name,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'ok': True, 'id': existing['id'], 'title_id': existing['title_id'], 'existing': True})
    # Auto-generate title_id
    last = conn.execute("SELECT title_id FROM meal_titles ORDER BY id DESC LIMIT 1").fetchone()
    num = 1
    if last and last['title_id']:
        try: num = int(last['title_id'].split('-')[1]) + 1
        except: pass
    tid = f"TITLE-{num:03d}"
    order = data.get('order_num', 99)
    conn.execute("INSERT INTO meal_titles (title_id,name,order_num) VALUES (?,?,?)", (tid, name, order))
    conn.commit()
    row = conn.execute("SELECT id FROM meal_titles WHERE name=?", (name,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'id': row['id'], 'title_id': tid})

@app.route('/api/meals/from-food-registry', methods=['POST'])
def api_meal_from_food_registry():
    """Besin DB'den ürün seçerek loga ekle — makroları otomatik hesapla."""
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    slot = (data.get('slot') or '').strip()
    food_id = data.get('food_id')
    food_name = (data.get('food_name') or '').strip()
    amount = float(data.get('amount') or 100)
    unit = (data.get('unit') or 'g').strip()

    # Besin DB'den makroları çek
    conn = get_db()
    if food_id:
        food = conn.execute("SELECT * FROM food_registry WHERE id=?", (food_id,)).fetchone()
    elif food_name:
        food = conn.execute(
            "SELECT * FROM food_registry WHERE name=? OR official_name=?", (food_name, food_name)
        ).fetchone()
        if not food:
            # aliases içinde ara
            all_foods = conn.execute("SELECT * FROM food_registry").fetchall()
            food = next((f for f in all_foods
                         if food_name.lower() in (f['aliases'] or '').lower()), None)
    else:
        food = None

    if not food:
        conn.close()
        return jsonify({'ok': False, 'error': f'Ürün bulunamadı: {food_name}'}), 404

    food = dict(food)
    # Makro hesaplama (100g bazından)
    ratio = amount / 100.0
    kcal = round((food.get('calories_per_100') or 0) * ratio, 1)
    prot = round((food.get('protein_per_100') or 0) * ratio, 1)
    carb = round((food.get('carbs_per_100') or 0) * ratio, 1)
    fat  = round((food.get('fat_per_100') or 0) * ratio, 1)

    official_name = food.get('official_name') or food.get('name')
    description = f"{amount} {unit} {official_name}"

    conn.execute("""
        INSERT INTO meal_entries (date,slot,title,description,calories,protein_g,carbs_g,fat_g,source)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (d, slot, official_name, description, kcal, prot, carb, fat, 'food_registry'))
    conn.commit(); conn.close()

    return jsonify({
        'ok': True, 'date': d, 'slot': slot,
        'item': {'name': official_name, 'amount': amount, 'unit': unit,
                 'kcal': kcal, 'protein': prot, 'carbs': carb, 'fat': fat}
    })

@app.route('/api/meals/from-template/<int:tid>', methods=['POST'])
def api_meal_from_template(tid):
    """Şablonu bugünün loguna ekle."""
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    slot = (data.get('slot') or '').strip()
    conn = get_db()
    tmpl = conn.execute("SELECT * FROM quick_templates WHERE id=? AND kind='meal'", (tid,)).fetchone()
    if not tmpl:
        conn.close()
        return jsonify({'ok': False, 'error': 'Şablon bulunamadı'}), 404
    tmpl = dict(tmpl)
    use_slot = slot or tmpl.get('category') or tmpl.get('title') or 'extra'
    conn.execute("""
        INSERT INTO meal_entries (date,slot,title,description,calories,protein_g,carbs_g,fat_g,fiber_g,source)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (d, use_slot, tmpl.get('title',''), tmpl.get('description',''),
          tmpl.get('calories'), tmpl.get('protein_g'), tmpl.get('carbs_g'),
          tmpl.get('fat_g'), tmpl.get('fiber_g'), f'template:{tid}'))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d, 'slot': use_slot})

@app.route('/api/meals/today')
def api_meals_today():
    return api_meals_day(operation_today())

@app.route('/api/meals/<date_str>')
def api_meals_day(date_str):
    conn = get_db()
    try:
        conn.execute("ALTER TABLE meal_entries ADD COLUMN display_order INTEGER DEFAULT 99")
        conn.commit()
    except: pass
    rows = conn.execute("SELECT * FROM meal_entries WHERE date=? ORDER BY COALESCE(display_order,99), id", (date_str,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

def _num_or_none(v):
    """0 değerlerini NULL'a dönüştürmez — sadece None/'' → None yapar."""
    if v is None or v == '': return None
    try: return float(v)
    except: return None

@app.route('/api/meals', methods=['POST'])
def api_meal_save():
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    slot = normalize_meal_slot(data.get('slot', ''))
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    calories = _num_or_none(data.get('calories'))
    protein_g = _num_or_none(data.get('protein_g'))
    carbs_g = _num_or_none(data.get('carbs_g'))
    fat_g = _num_or_none(data.get('fat_g'))
    fiber_g = _num_or_none(data.get('fiber_g'))
    source = data.get('source', '').strip()
    conn = get_db()
    if data.get('replace_existing') and slot != 'extra':
        conn.execute("DELETE FROM meal_entries WHERE date=? AND slot=?", (d, slot))
    if title or description or calories or protein_g or carbs_g or fat_g:
        conn.execute("""
            INSERT INTO meal_entries
                (date, slot, title, description, calories, protein_g, carbs_g, fat_g, fiber_g, source)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (d, slot, title, description, calories, protein_g, carbs_g, fat_g, fiber_g, source))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/meals/<int:mid>', methods=['DELETE'])
def api_meal_delete(mid):
    conn = get_db()
    row = conn.execute("SELECT * FROM meal_entries WHERE id=?", (mid,)).fetchone()
    deleted = dict(row) if row else None
    conn.execute("DELETE FROM meal_entries WHERE id=?", (mid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'deleted': deleted})


@app.route('/api/meals/<int:mid>', methods=['PUT','PATCH'])
def api_meal_update(mid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    # Mevcut satırı çek — gönderilmeyen alanlar korunsun
    existing = conn.execute("SELECT * FROM meal_entries WHERE id=?", (mid,)).fetchone()
    ex = dict(existing) if existing else {}
    # display_order için kolon yoksa ekle
    try:
        conn.execute("ALTER TABLE meal_entries ADD COLUMN display_order INTEGER DEFAULT 99")
        conn.commit()
    except: pass
    # PATCH: sadece display_order güncellemesi
    if 'display_order' in data and len(data) == 1:
        conn.execute("UPDATE meal_entries SET display_order=? WHERE id=?", (int(data['display_order']), mid))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    conn.execute("""
        UPDATE meal_entries
        SET slot=?, title=?, description=?, calories=?, protein_g=?, carbs_g=?, fat_g=?, fiber_g=?, source=?
        WHERE id=?
    """, (
        data.get('slot', ex.get('slot') or '').strip() or ex.get('slot') or 'extra',
        data['title'].strip() if 'title' in data else (ex.get('title') or ''),
        data['description'].strip() if 'description' in data else (ex.get('description') or ''),
        _num_or_none(data['calories']) if 'calories' in data else ex.get('calories'),
        _num_or_none(data['protein_g']) if 'protein_g' in data else ex.get('protein_g'),
        _num_or_none(data['carbs_g']) if 'carbs_g' in data else ex.get('carbs_g'),
        _num_or_none(data['fat_g']) if 'fat_g' in data else ex.get('fat_g'),
        _num_or_none(data['fiber_g']) if 'fiber_g' in data else ex.get('fiber_g'),
        data.get('source', ex.get('source') or '').strip(),
        mid
    ))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

def meal_macro_totals(date_str):
    conn = get_db()
    rows = conn.execute("SELECT calories, protein_g, carbs_g, fat_g, fiber_g FROM meal_entries WHERE date=?", (date_str,)).fetchall()
    conn.close()
    totals = {'calories': 0, 'protein_g': 0, 'carbs_g': 0, 'fat_g': 0, 'fiber_g': 0}
    for row in rows:
        for key in totals:
            totals[key] += float(row[key] or 0)
    return {k: round(v, 1) for k, v in totals.items()}

@app.route('/api/macro/today')
def api_macro_today():
    today = request.args.get('date') or operation_today()
    return jsonify({'date': today, 'totals': meal_macro_totals(today), 'meals': api_meals_day(today).get_json()})


@app.route('/api/macro/range')
def api_macro_range():
    days = int(request.args.get('days', 7))
    days = max(1, min(days, 60))
    detail = request.args.get('detail') == '1'
    start = operation_date() - timedelta(days=days-1)
    conn = get_db()
    ensure_step_logs_table()
    session_dates = set()
    supp_total = None
    if detail:
        try:
            sess_row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
            if sess_row and sess_row['value']:
                for s in json.loads(sess_row['value']):
                    if s.get('date'):
                        session_dates.add(s['date'])
        except Exception:
            pass
        supp_total = conn.execute(
            "SELECT COUNT(*) c FROM supplement_stack_items si JOIN supplement_stacks s ON s.id=si.stack_id WHERE s.active=1"
        ).fetchone()['c']
    result = []
    for i in range(days):
        ds = (start + timedelta(days=i)).isoformat()
        meals = meal_macro_totals(ds)
        row = conn.execute("SELECT SUM(water_ml) AS water_ml FROM nutrition_logs WHERE date=?", (ds,)).fetchone()
        water_ml = float((row['water_ml'] if row else 0) or 0)
        entry = {
            'date': ds,
            'calories': meals.get('calories', 0),
            'protein_g': meals.get('protein_g', 0),
            'carbs_g': meals.get('carbs_g', 0),
            'fat_g': meals.get('fat_g', 0),
            'fiber_g': meals.get('fiber_g', 0),
            'water_ml': water_ml,
            'water_l': round(water_ml / 1000, 2),
        }
        if detail:
            step_row = conn.execute("SELECT steps FROM step_logs WHERE date=?", (ds,)).fetchone()
            entry['steps'] = int(step_row['steps']) if step_row and step_row['steps'] else 0
            entry['training_type'] = training_day(ds)
            entry['training_done'] = ds in session_dates
            taken = conn.execute(SUPP_TAKEN_COUNT_SQL, (ds,)).fetchone()['c']
            entry['supp_taken'] = min(taken, supp_total) if supp_total else taken
            entry['supp_total'] = supp_total or 0
        result.append(entry)
    conn.close()
    return jsonify(result)



def ensure_body_metrics_table():
    conn = get_db()
    # BODY_MEASUREMENTS_WEEKLY_BACKEND_V1
    conn.execute("""
        CREATE TABLE IF NOT EXISTS body_metrics (
            date TEXT PRIMARY KEY,
            weight_kg REAL,
            waist_cm REAL,
            chest_cm REAL,
            arm_cm REAL,
            notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    existing = {r['name'] for r in conn.execute("PRAGMA table_info(body_metrics)").fetchall()}
    for col in ('chest_cm', 'arm_cm'):
        if col not in existing:
            conn.execute(f"ALTER TABLE body_metrics ADD COLUMN {col} REAL")
    conn.commit(); conn.close()

@app.route('/api/body/metrics')
def api_body_metrics_range():
    ensure_body_metrics_table()
    days = int(request.args.get('days', 30))
    start = (operation_date() - timedelta(days=days-1)).isoformat()
    conn = get_db()
    end = operation_today()
    rows = conn.execute("SELECT * FROM body_metrics WHERE date>=? AND date<=? ORDER BY date", (start, end)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/body/metrics/today')
def api_body_metrics_today():
    ensure_body_metrics_table()
    d = request.args.get('date', operation_today())
    conn = get_db()
    row = conn.execute("SELECT * FROM body_metrics WHERE date=?", (d,)).fetchone()
    conn.close()
    return jsonify(dict(row) if row else {'date': d, 'weight_kg': None, 'waist_cm': None, 'chest_cm': None, 'arm_cm': None, 'notes': ''})

@app.route('/api/body/metrics', methods=['POST'])
def api_body_metrics_save():
    ensure_body_metrics_table()
    data = request.get_json(force=True) or {}
    d = data.get('date') or operation_today()
    weight = data.get('weight_kg')
    weight_night = data.get('weight_kg_night')
    waist = data.get('waist_cm')
    chest = data.get('chest_cm')
    arm = data.get('arm_cm')
    notes = data.get('notes') or ''
    conn = get_db()
    conn.execute("""
        INSERT INTO body_metrics (date, weight_kg, weight_kg_night, waist_cm, chest_cm, arm_cm, notes)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
            weight_kg=COALESCE(excluded.weight_kg, weight_kg),
            weight_kg_night=COALESCE(excluded.weight_kg_night, weight_kg_night),
            waist_cm=COALESCE(excluded.waist_cm, waist_cm),
            chest_cm=COALESCE(excluded.chest_cm, chest_cm),
            arm_cm=COALESCE(excluded.arm_cm, arm_cm),
            notes=COALESCE(NULLIF(excluded.notes,''), notes),
            ts=CURRENT_TIMESTAMP
    """, (d, weight, weight_night, waist, chest, arm, notes))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/body/metrics/<date_str>', methods=['DELETE'])
def api_body_metrics_delete(date_str):
    ensure_body_metrics_table()
    conn = get_db()
    row = conn.execute("SELECT * FROM body_metrics WHERE date=?", (date_str,)).fetchone()
    deleted = dict(row) if row else None
    conn.execute("DELETE FROM body_metrics WHERE date=?", (date_str,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': date_str, 'deleted': deleted})

@app.route('/api/templates')
def api_templates():
    conn = get_db()
    rows = conn.execute("SELECT * FROM quick_templates ORDER BY kind, category, title").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/templates', methods=['POST'])
def api_template_create():
    data = request.get_json(force=True) or {}
    kind = data.get('kind', '').strip()
    title = data.get('title', '').strip()
    if kind not in ('meal', 'supplement') or not title:
        return jsonify({'error': 'Gecersiz sablon'}), 400

    conn = get_db()
    conn.execute('''
        INSERT INTO quick_templates
            (kind, category, title, description, calories, protein_g, carbs_g, fat_g, fiber_g, water_ml, amount, unit, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        kind,
        data.get('category', '').strip(),
        title,
        data.get('description', '').strip(),
        data.get('calories') or None,
        data.get('protein_g') or None,
        data.get('carbs_g') or None,
        data.get('fat_g') or None,
        data.get('fiber_g') or None,
        data.get('water_ml') or None,
        data.get('amount', '').strip(),
        data.get('unit', '').strip(),
        data.get('notes', '').strip(),
    ))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/templates/<int:tid>', methods=['DELETE'])
def api_template_delete(tid):
    conn = get_db()
    conn.execute("DELETE FROM quick_templates WHERE id=?", (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/templates/<int:tid>', methods=['PUT','PATCH'])
def api_template_update(tid):
    d = request.json or {}
    fields = ['title','kind','category','description','amount','unit','notes',
              'calories','protein_g','carbs_g','fat_g']
    sets = ', '.join(f"{f}=?" for f in fields if f in d)
    vals = [d[f] for f in fields if f in d] + [tid]
    if not sets: return jsonify({'ok': False, 'error': 'no fields'}), 400
    conn = get_db()
    conn.execute(f"UPDATE quick_templates SET {sets} WHERE id=?", vals)
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/note', methods=['POST'])
def api_note():
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    note = data.get('note', '')
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO daily_notes (date, note) VALUES (?,?)", (d, note))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/calendar/<int:year>/<int:month>')
def api_calendar(year, month):
    import calendar as cal_mod
    days_in_month = cal_mod.monthrange(year, month)[1]
    result = []
    conn = get_db()
    WEEKDAY_CYCLE = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
    cycle_rows = {r['day_index']: r for r in conn.execute("SELECT * FROM cycle_days ORDER BY day_index").fetchall()}
    # Bu ayda gercek antrenman seansi yapilmis gunler (antrenman panelinin gercek verisi)
    session_dates = set()
    try:
        sess_row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
        if sess_row and sess_row['value']:
            for s in json.loads(sess_row['value']):
                if s.get('date'):
                    session_dates.add(s['date'])
    except Exception:
        pass
    supp_total = conn.execute(
        "SELECT COUNT(*) c FROM supplement_stack_items si JOIN supplement_stacks s ON s.id=si.stack_id WHERE s.active=1"
    ).fetchone()['c']
    exercise_count_by_type = {r['training_day']: r['c'] for r in conn.execute(
        "SELECT training_day, COUNT(*) c FROM training_exercises GROUP BY training_day"
    ).fetchall()}
    month_start = f"{year:04d}-{month:02d}-01"
    month_end = f"{year:04d}-{month:02d}-{days_in_month:02d}"
    whoop_by_date = {}
    try:
        for r in conn.execute(
            "SELECT date, recovery_score, strain, sleep_performance FROM whoop_daily WHERE date>=? AND date<=?",
            (month_start, month_end)
        ).fetchall():
            whoop_by_date[r['date']] = {'recovery': r['recovery_score'], 'strain': r['strain'], 'sleep_performance': r['sleep_performance']}
    except Exception:
        pass
    whoop_workout_min_by_date = {}
    try:
        for r in conn.execute(
            "SELECT date, SUM(duration_min) m FROM whoop_workouts WHERE date>=? AND date<=? GROUP BY date",
            (month_start, month_end)
        ).fetchall():
            whoop_workout_min_by_date[r['date']] = round(r['m']) if r['m'] else None
    except Exception:
        pass
    meals_by_date = {}
    try:
        for r in conn.execute(
            "SELECT date, SUM(calories) kcal, SUM(protein_g) protein FROM meal_entries WHERE date>=? AND date<=? GROUP BY date",
            (month_start, month_end)
        ).fetchall():
            meals_by_date[r['date']] = {'kcal': r['kcal'] or 0, 'protein': r['protein'] or 0}
    except Exception:
        pass
    water_by_date = {}
    try:
        for r in conn.execute(
            "SELECT date, SUM(water_ml) w FROM nutrition_logs WHERE date>=? AND date<=? GROUP BY date",
            (month_start, month_end)
        ).fetchall():
            water_by_date[r['date']] = r['w'] or 0
    except Exception:
        pass
    sleep_by_date = {}
    try:
        for r in conn.execute(
            "SELECT date, AVG(hours) h FROM sleep_logs WHERE date>=? AND date<=? GROUP BY date",
            (month_start, month_end)
        ).fetchall():
            sleep_by_date[r['date']] = r['h']
    except Exception:
        pass
    water_goal_row = conn.execute("SELECT value FROM user_settings WHERE key='water'").fetchone()
    water_goal = int(water_goal_row['value']) if water_goal_row and water_goal_row['value'] else 3000
    today_str = now_istanbul().date().isoformat()

    def pct(v, tg):
        try:
            return max(0, min(100, round((v or 0) / (tg or 1) * 100)))
        except Exception:
            return 0

    def whoop_note_for(w):
        if not w or (w.get('recovery') is None and w.get('strain') is None):
            return None
        rec, strain = w.get('recovery'), w.get('strain')
        if rec is not None and rec >= 67:
            text, color = f'Recovery güçlüydü (%{rec}) — vücut o gün hazırdı.', '#2fd6b0'
        elif rec is not None and rec >= 34:
            text, color = f'Recovery orta seviyedeydi (%{rec}) — dikkatli yüklenmek gerekiyordu.', '#ffd166'
        elif rec is not None:
            text, color = f'Recovery düşüktü (%{rec}) — o gün toparlanma öncelikli olmalıydı.', '#e0556b'
        else:
            text, color = f'Recovery verisi yok, strain {strain:.1f}.', '#8b98ad'
        if rec is not None and rec < 40 and strain is not None and strain >= 13:
            text += f' Buna rağmen strain {strain:.1f} — yüksek yüklenme, dinlenme ihmal edilmiş olabilir.'
        return {'text': text, 'color': color}
    for day in range(1, days_in_month + 1):
        d = f"{year:04d}-{month:02d}-{day:02d}"
        tables = ('sleep_logs','exercise_logs','nutrition_logs','work_logs','coaching_logs','mood_logs')
        has_data = any(conn.execute(f"SELECT id FROM {t} WHERE date=?", (d,)).fetchone() for t in tables)
        note_row = conn.execute("SELECT note FROM daily_notes WHERE date=?", (d,)).fetchone()
        override_row = conn.execute("SELECT value FROM user_settings WHERE key=?", (f'cycle_active_day_{d}',)).fetchone()
        is_override = bool(override_row and override_row['value'] not in (None, ''))
        day_index = int(override_row['value']) if is_override else date.fromisoformat(d).weekday()
        td = WEEKDAY_CYCLE[day_index % 7]
        cyc = cycle_rows.get(day_index)
        target = None
        if cyc:
            target = {'type': cyc['type'], 'protein_g': cyc['protein_g'], 'carb_g': cyc['carb_g'], 'fat_g': cyc['fat_g'],
                      'kcal': 4 * cyc['protein_g'] + 4 * cyc['carb_g'] + 9 * cyc['fat_g']}
        supp_taken = conn.execute(SUPP_TAKEN_COUNT_SQL, (d,)).fetchone()['c']
        w = whoop_by_date.get(d)
        meals = meals_by_date.get(d) or {'kcal': 0, 'protein': 0}
        day_score = None
        whoop_note = None
        if d <= today_str:
            water_ml = water_by_date.get(d) or 0
            train_part = (100 if d in session_dates else 35) if td != 'Off' else 80
            parts = [
                pct(meals['kcal'], target['kcal'] if target else None),
                pct(meals['protein'], target['protein_g'] if target else None),
                pct(water_ml, water_goal),
                train_part,
            ]
            if w and (w.get('recovery') is not None or w.get('sleep_performance') is not None):
                if w.get('recovery') is not None:
                    parts.append(w['recovery'])
                if w.get('sleep_performance') is not None:
                    parts.append(w['sleep_performance'])
            else:
                parts.append(pct(sleep_by_date.get(d), 7.5))
            day_score = round(sum(parts) / len(parts))
            whoop_note = whoop_note_for(w)
        result.append({
            'date': d, 'day': day,
            'training': td,
            'color': TRAINING_COLORS[td],
            'has_data': has_data,
            'session_done': d in session_dates,
            'is_override': is_override,
            'day_index': day_index,
            'target': target,
            'note': note_row['note'] if note_row else '',
            'supp_taken': supp_taken,
            'supp_total': supp_total,
            'whoop_recovery': w['recovery'] if w else None,
            'whoop_strain': w['strain'] if w else None,
            'day_score': day_score,
            'whoop_note': whoop_note,
            'kcal_actual': round(meals['kcal']) if d <= today_str else None,
            'exercises': exercise_count_by_type.get(td, 0),
            'whoop_workout_min': whoop_workout_min_by_date.get(d),
        })
    conn.close()
    return jsonify(result)

@app.route('/api/vitamins/month/<int:year>/<int:month>')
def api_vitamins_month(year, month):
    import calendar as cal_mod
    days_in_month = cal_mod.monthrange(year, month)[1]
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT date FROM vitamin_logs WHERE date >= ? AND date <= ?",
        (f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{days_in_month:02d}")
    ).fetchall()
    conn.close()
    return jsonify([{'date': r['date']} for r in rows])

@app.route('/api/day/<date_str>')
def api_day(date_str):
    ensure_step_logs_table()
    ensure_body_metrics_table()
    conn = get_db()
    vitamins = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY ts", (date_str,)).fetchall()]
    note_row = conn.execute("SELECT note FROM daily_notes WHERE date=?", (date_str,)).fetchone()
    step_row = conn.execute("SELECT * FROM step_logs WHERE date=?", (date_str,)).fetchone()
    body_row = conn.execute("SELECT * FROM body_metrics WHERE date=?", (date_str,)).fetchone()
    water_row = conn.execute("SELECT SUM(water_ml) as total FROM nutrition_logs WHERE date=?", (date_str,)).fetchone()
    water_ml_total = int(water_row['total'] or 0) if water_row else 0
    conn.close()
    td = training_day(date_str)
    nutrition = db_date('nutrition_logs', date_str)
    nutrition['water_ml'] = water_ml_total
    return jsonify({
        'sleep': db_date('sleep_logs', date_str),
        'exercise': db_date('exercise_logs', date_str),
        'nutrition': nutrition,
        'work': db_date('work_logs', date_str),
        'coaching': db_date('coaching_logs', date_str),
        'mood': db_date('mood_logs', date_str),
        'vitamins': vitamins,
        'steps': dict(step_row) if step_row else {'date': date_str, 'steps': 0, 'notes': ''},
        'body': dict(body_row) if body_row else {'date': date_str, 'weight_kg': None, 'waist_cm': None, 'chest_cm': None, 'arm_cm': None, 'notes': ''},
        'note': note_row['note'] if note_row else '',
        'training': td,
        'color': TRAINING_COLORS[td],
        'date': date_str,
        'steps': dict(step_row) if step_row else {'date': date_str, 'steps': 0},
    })

@app.route('/api/ai-report/<date_str>', methods=['GET'])
def api_ai_report_get(date_str):
    """AI günlük raporu — varsa döndür, yoksa oluştur."""
    conn = get_db()
    row = conn.execute("SELECT report_json FROM daily_ai_reports WHERE date=?", (date_str,)).fetchone()
    if row:
        conn.close()
        import json as _j
        return jsonify(_j.loads(row['report_json']))
    conn.close()
    return api_ai_report_generate(date_str)

@app.route('/api/ai-report/<date_str>/generate', methods=['POST','GET'])
def api_ai_report_generate(date_str):
    """AI raporu üret ve sakla."""
    import json as _j
    conn = get_db()
    # Veri topla
    meals = conn.execute("SELECT * FROM meal_entries WHERE date=?", (date_str,)).fetchall()
    nutrition = conn.execute("SELECT * FROM nutrition_logs WHERE date=?", (date_str,)).fetchone()
    step_row = conn.execute("SELECT steps FROM step_logs WHERE date=? LIMIT 1", (date_str,)).fetchone()
    supp_logs = conn.execute("SELECT stack_name_snapshot FROM supplement_logs WHERE date=?", (date_str,)).fetchall()
    exercise = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (date_str,)).fetchone()
    sleep_row = conn.execute("SELECT hours FROM sleep_logs WHERE date=?", (date_str,)).fetchone()
    conn.close()

    total_cal  = sum(m['calories'] or 0 for m in meals)
    total_prot = sum(m['protein_g'] or 0 for m in meals)
    total_carb = sum(m['carbs_g'] or 0 for m in meals)
    total_fat  = sum(m['fat_g'] or 0 for m in meals)
    water_ml   = int(nutrition['water_ml'] if nutrition and nutrition['water_ml'] else 0)
    steps      = int(step_row['steps'] if step_row else 0)
    stacks_done = [r['stack_name_snapshot'] for r in supp_logs]
    has_exercise = bool(exercise and exercise['type'])
    sleep_h = float(sleep_row['hours']) if sleep_row and sleep_row['hours'] else 0

    # Hedefler (Taha için sabit)
    CAL_TARGET  = (2800, 3400)
    PROT_TARGET = 180
    WATER_TARGET = 3000
    STEP_TARGET  = 10000
    SLEEP_TARGET = (7, 9)

    items = []
    # Protein
    if total_prot >= PROT_TARGET:
        items.append({'type':'ok', 'text': f'Protein hedefi tamamlandı ({round(total_prot)}g / {PROT_TARGET}g)'})
    elif total_prot > 0:
        deficit = round(PROT_TARGET - total_prot)
        items.append({'type':'warn', 'text': f'Protein hedefi eksik — {deficit}g daha gerekiyor ({round(total_prot)}g / {PROT_TARGET}g)'})

    # Kalori
    if total_cal:
        if CAL_TARGET[0] <= total_cal <= CAL_TARGET[1]:
            items.append({'type':'ok', 'text': f'Kalori hedefi uygun ({round(total_cal)} kcal)'})
        elif total_cal < CAL_TARGET[0]:
            items.append({'type':'warn', 'text': f'Kalori düşük — {round(CAL_TARGET[0]-total_cal)} kcal eksik ({round(total_cal)} kcal)'})
        else:
            items.append({'type':'warn', 'text': f'Kalori fazla — hedefin {round(total_cal-CAL_TARGET[1])} kcal üstünde ({round(total_cal)} kcal)'})

    # Su
    if water_ml >= WATER_TARGET:
        items.append({'type':'ok', 'text': f'Su hedefi tamamlandı ({water_ml/1000:.1f}L)'})
    elif water_ml > 0:
        remain = round((WATER_TARGET - water_ml) / 100) * 100
        items.append({'type':'warn', 'text': f'Su tüketimi düşük — {remain}ml daha içilebilir ({water_ml/1000:.1f}L / {WATER_TARGET/1000:.1f}L)'})

    # Adım
    if steps >= STEP_TARGET:
        items.append({'type':'ok', 'text': f'Adım hedefi tamamlandı ({steps:,} adım)'})
    elif steps > 0:
        items.append({'type':'warn', 'text': f'Adım hedefi tamamlanmadı ({steps:,} / {STEP_TARGET:,} adım)'})

    # Supplement
    key_stacks = ['Sabah Stack', 'Pre Workout Stack', 'Post Workout Stack']
    done_stacks = [s for s in key_stacks if any(s.lower() in d.lower() for d in stacks_done)]
    miss_stacks = [s for s in key_stacks if s not in done_stacks]
    if done_stacks:
        items.append({'type':'ok', 'text': f'Supplement: {", ".join(done_stacks)}'})
    if miss_stacks:
        items.append({'type':'warn', 'text': f'Eksik supplement: {", ".join(miss_stacks)}'})

    # Antrenman
    if has_exercise:
        items.append({'type':'ok', 'text': f'Antrenman tamamlandı ({exercise["type"]})'})

    # Uyku
    if sleep_h:
        if SLEEP_TARGET[0] <= sleep_h <= SLEEP_TARGET[1]:
            items.append({'type':'ok', 'text': f'Uyku hedefi uygun ({sleep_h}s)'})
        elif sleep_h < SLEEP_TARGET[0]:
            items.append({'type':'warn', 'text': f'Uyku yetersiz ({sleep_h}s / hedef {SLEEP_TARGET[0]}s)'})

    # Öneri
    suggestions = []
    if water_ml < WATER_TARGET and water_ml > 0:
        remain_l = (WATER_TARGET - water_ml) / 1000
        suggestions.append(f'{remain_l:.1f}L daha su içilebilir.')
    if steps < STEP_TARGET and steps > 0:
        suggestions.append(f'{STEP_TARGET - steps:,} adım daha atılabilir.')
    if total_prot < PROT_TARGET and total_prot > 0:
        suggestions.append(f'{round(PROT_TARGET - total_prot)}g protein eksik — tavuk veya yumurta eklenebilir.')

    report = {
        'date': date_str,
        'items': items,
        'suggestions': suggestions,
        'summary': {
            'calories': round(total_cal),
            'protein': round(total_prot, 1),
            'carbs': round(total_carb, 1),
            'fat': round(total_fat, 1),
            'water_ml': water_ml,
            'steps': steps,
            'stacks_done': stacks_done,
            'has_exercise': has_exercise,
            'sleep_hours': sleep_h,
        },
        'generated_at': operation_today(),
    }
    conn2 = get_db()
    try:
        conn2.execute("INSERT OR REPLACE INTO daily_ai_reports (date, report_json, generated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
                      (date_str, _j.dumps(report, ensure_ascii=False)))
        conn2.commit()
    except: pass
    finally: conn2.close()
    return jsonify(report)

@app.route('/api/report/today')
def api_report():
    today = operation_today()
    d = json.loads(api_day(today).get_data())
    sl = d.get('sleep', {}); ex = d.get('exercise', {}); nu = d.get('nutrition', {})
    w = d.get('work', {}); co = d.get('coaching', {}); mo = d.get('mood', {})
    vits = d.get('vitamins', []); sr = streak_count()
    td = d.get('training', '')

    lines = [
        f"=== TAHA SERDEM GUNLUK RAPOR ===",
        f"Tarih: {operation_date().strftime('%d %B %Y')} | Seri: {sr} gun | Antrenman: {td}",
        "",
        "[ UYKU ]",
        f"  Sure: {sl.get('hours', '-')} saat | Kalite: {sl.get('quality', '-')}/10" if sl else "  Kayit yok",
        "",
        "[ EGZERSIZ ]",
        f"  {ex.get('type','-')} | {ex.get('duration','-')} dk | Yogunluk: {ex.get('intensity','-')}/10" if ex else "  Kayit yok",
        "",
        "[ BESLENME ]",
        f"  {nu.get('description','-')} | {nu.get('calories','-')} kcal | Su: {(nu.get('water_ml',0) or 0)/1000:.1f}L" if nu else "  Kayit yok",
        "",
        "[ IS ]",
        f"  {w.get('hours','-')} saat" + (f" | {w.get('tasks','')}" if w.get('tasks') else '') if w else "  Kayit yok",
        "",
        "[ ANTRENORLUK ]",
        f"  {co.get('sessions','-')} seans" + (f" | {co.get('clients','')}" if co.get('clients') else '') if co else "  Kayit yok",
        "",
        "[ RUH HALI ]",
        f"  Enerji: {mo.get('energy','-')}/10 | Mood: {mo.get('mood','-')}/10 | Stres: {mo.get('stress','-')}/10" if mo else "  Kayit yok",
        "",
        "[ VITAMINLER ]",
    ]
    if vits:
        for v in vits:
            lines.append(f"  {v['name']} {v['amount']} {v['unit']}")
    else:
        lines.append("  Kayit yok")

    lines += ["", "[ ANALIZ ]"]

    # Uyku analizi
    if sl.get('hours'):
        h = float(sl['hours'])
        if h < 6: lines.append("  â  Uyku cok az â performans dusuyor olabilir.")
        elif h < 7.5: lines.append("  ~ Uyku biraz dusuk â 7-9 saat hedefle.")
        else: lines.append(f"  â Uyku iyi ({h}s).")

    # Antrenman analizi
    if td == 'Off':
        lines.append("  â Dinlenme gunu â aktif recovery veya tam dinlenme.")
    elif ex.get('type'):
        lines.append(f"  â {td} antrenman tamamlandi.")
    else:
        lines.append(f"  â  {td} gunu antrenman kaydi yok.")

    # Mood analizi
    if mo.get('stress') and int(mo['stress']) >= 7:
        lines.append("  â  Stres yuksek â recovery ve uyku oncelikli.")
    if mo.get('energy') and int(mo['energy']) <= 4:
        lines.append("  â  Enerji dusuk â beslenme ve uyku gozden gecir.")
    if mo.get('mood') and int(mo['mood']) >= 7:
        lines.append("  â Iyi ruh hali â devam!")

    if not (sl or ex or nu or w or co or mo):
        lines.append("  Bugun kayit girilmemis.")

    return jsonify({'report': '\n'.join(lines), 'date': today})


@app.route('/api/summary')
def api_summary():
    """Weekly/monthly summary for the Özet page."""
    days = int(request.args.get('days', 7))
    days = max(1, min(days, 90))
    ensure_step_logs_table()
    ensure_body_metrics_table()
    start = operation_date() - timedelta(days=days - 1)
    conn = get_db()
    result = []
    for i in range(days):
        ds = (start + timedelta(days=i)).isoformat()
        sl = conn.execute("SELECT * FROM sleep_logs WHERE date=?", (ds,)).fetchone()
        mo = conn.execute("SELECT * FROM mood_logs WHERE date=?", (ds,)).fetchone()
        nu = conn.execute("SELECT SUM(water_ml) AS water_ml FROM nutrition_logs WHERE date=?", (ds,)).fetchone()
        w  = conn.execute("SELECT * FROM work_logs WHERE date=?", (ds,)).fetchone()
        co = conn.execute("SELECT * FROM coaching_logs WHERE date=?", (ds,)).fetchone()
        bm = conn.execute("SELECT * FROM body_metrics WHERE date=?", (ds,)).fetchone()
        st = conn.execute("SELECT * FROM step_logs WHERE date=?", (ds,)).fetchone()
        macros = meal_macro_totals(ds)
        td = training_day(ds)
        # any data?
        has_data = any([sl, mo, nu, w, co])
        result.append({
            'date': ds,
            'training_day': td,
            'has_data': has_data,
            'sleep_hours': float(sl['hours'] or 0) if sl and sl['hours'] else None,
            'sleep_quality': int(sl['quality'] or 0) if sl and sl['quality'] else None,
            'energy': int(mo['energy'] or 0) if mo and mo['energy'] else None,
            'mood': int(mo['mood'] or 0) if mo and mo['mood'] else None,
            'stress': int(mo['stress'] or 0) if mo and mo['stress'] else None,
            'water_ml': int(nu['water_ml'] or 0) if nu and nu['water_ml'] else 0,
            'work_hours': float(w['hours'] or 0) if w and w['hours'] else None,
            'coaching_sessions': int(co['sessions'] or 0) if co and co['sessions'] else 0,
            'calories': macros['calories'],
            'protein_g': macros['protein_g'],
            'carbs_g': macros['carbs_g'],
            'fat_g': macros['fat_g'],
            'weight_kg': float(bm['weight_kg']) if bm and bm['weight_kg'] else None,
            'steps': int(st['steps'] or 0) if st and st['steps'] else 0,
        })
    conn.close()
    return jsonify(result)


@app.route('/api/training/exercises')
def api_training_exercises_all():
    conn = get_db()
    rows = conn.execute("SELECT * FROM training_exercises ORDER BY training_day, sort_order, id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/training/exercises/<day>')
def api_training_exercises_day(day):
    conn = get_db()
    rows = conn.execute("SELECT * FROM training_exercises WHERE training_day=? ORDER BY sort_order, id", (day,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/training/exercises', methods=['POST'])
def api_training_exercise_create():
    data = request.get_json(force=True) or {}
    day = data.get('training_day', '').strip() or training_day(operation_today())
    exercise = data.get('exercise', '').strip()
    if not exercise:
        return jsonify({'error': 'Hareket gerekli'}), 400
    conn = get_db()
    conn.execute("INSERT INTO training_exercises (training_day, exercise, sets, reps, weight, notes, sort_order) VALUES (?,?,?,?,?,?,?)",
                 (day, exercise, data.get('sets',''), data.get('reps',''), data.get('weight',''), data.get('notes',''), data.get('sort_order') or 0))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/training/exercises/<int:eid>', methods=['DELETE'])
def api_training_exercise_delete(eid):
    conn = get_db()
    conn.execute("DELETE FROM training_exercises WHERE id=?", (eid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/training/exercises/<int:eid>', methods=['PUT'])
def api_training_exercise_update(eid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("""
        UPDATE training_exercises
        SET training_day=?, exercise=?, sets=?, reps=?, weight=?, notes=?, sort_order=?
        WHERE id=?
    """, (
        data.get('training_day', '').strip() or training_day(operation_today()),
        data.get('exercise', '').strip(),
        data.get('sets', ''),
        data.get('reps', ''),
        data.get('weight', ''),
        data.get('notes', ''),
        data.get('sort_order') or 0,
        eid
    ))
    conn.commit(); conn.close()
    return jsonify({'ok': True})





SET_TYPE_ALIASES = {
    'warm up': 'Warm up',
    'warmup': 'Warm up',
    'isinma': 'Warm up',
    'ısınma': 'Warm up',
    'working set': 'Working set',
    'working': 'Working set',
    'work set': 'Working set',
    'ana set': 'Working set',
    'top set': 'Top set',
    'top': 'Top set',
    'back off': 'Back off',
    'backoff': 'Back off',
    'drop set': 'Drop set',
    'drop': 'Drop set',
}

def normalize_set_details(sets):
    clean = []
    for i, s in enumerate(sets or []):
        if not isinstance(s, dict):
            s = {}
        raw_type = str(s.get('type') or s.get('set_type') or 'Working set').strip()
        set_type = SET_TYPE_ALIASES.get(raw_type.lower(), raw_type or 'Working set')
        clean.append({
            'set': int(s.get('set') or i + 1),
            'type': set_type,
            'reps': str(s.get('reps') or ''),
            'weight': str(s.get('weight') or ''),
            'done': bool(s.get('done', False)),
        })
    return clean

def parse_training_sets_from_text(raw_text):
    import re
    text = (raw_text or '').replace('\r', '\n').strip()
    if not text:
        return []
    pattern = r'(warm\s*up|warmup|isinma|ısınma|working\s*set|working|ana\s*set|top\s*set|top|back\s*off|backoff|drop\s*set|drop)'
    parts = re.split(pattern, text, flags=re.I)
    if len(parts) <= 2:
        return []
    exercise = parts[0].strip(" :-\n\t") or 'Hareket'
    sets = []
    for i in range(1, len(parts), 2):
        label_raw = (parts[i] or '').strip().lower()
        tail = parts[i + 1] if i + 1 < len(parts) else ''
        set_type = SET_TYPE_ALIASES.get(label_raw, 'Working set')
        m = re.search(r'(\d+)\s*[xX]\s*([\d\-\/]+)', tail)
        count = int(m.group(1)) if m else 1
        reps = m.group(2) if m else ''
        after = tail[m.end():] if m else tail
        wm = re.search(r'(\d+(?:[\.,]\d+)?)\s*(kg|kilo)?', after)
        weight = (wm.group(1).replace(',', '.') + ' kg') if wm else ''
        for _ in range(count):
            sets.append({'set': len(sets) + 1, 'type': set_type, 'reps': reps, 'weight': weight, 'done': True})
    return [{'exercise': exercise, 'set_details': normalize_set_details(sets)}] if sets else []

def tg_save_training_from_text(raw_text):
    today = operation_today()
    parsed = parse_training_sets_from_text(raw_text)
    if not parsed:
        return []
    ensure_training_day_logs_table()
    td = training_day(today)
    conn = get_db()
    for item in parsed:
        conn.execute("""
            INSERT INTO training_day_logs (date, training_day, exercise, sets_json, notes)
            VALUES (?,?,?,?,?)
        """, (today, td, item.get('exercise') or 'Hareket', json.dumps(item.get('set_details') or [], ensure_ascii=False), 'telegram'))
    conn.commit(); conn.close()
    return parsed

def _training_parse_sets(row):
    try:
        payload = json.loads((row.get('notes') if isinstance(row, dict) else row['notes']) or '{}')
        sets = payload.get('set_details') or []
    except Exception:
        sets = []
    if not sets:
        raw_sets = int((row.get('sets') if isinstance(row, dict) else row['sets']) or 0)
        reps = str((row.get('reps') if isinstance(row, dict) else row['reps']) or '')
        weight = str((row.get('weight') if isinstance(row, dict) else row['weight']) or '')
        sets = [{'set': i + 1, 'type': 'Working set', 'reps': reps, 'weight': weight, 'done': False} for i in range(raw_sets or 1)]
    return normalize_set_details(sets)

def ensure_training_day_logs_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS training_day_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            training_day TEXT NOT NULL,
            exercise TEXT NOT NULL,
            program_exercise_id INTEGER,
            sets_json TEXT,
            notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit(); conn.close()

_ANTRENMAN_TYPE_LABEL = {'wu': 'Isınma', 'ws': 'Çalışma', 'bo': 'Back-off'}

@app.route('/api/training/day/<date_str>')
def api_training_day_detail(date_str):
    ensure_training_day_logs_table()
    td = training_day(date_str)
    conn = get_db()
    program_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM training_exercises WHERE training_day=? ORDER BY sort_order, id", (td,)
    ).fetchall()]
    logs = [dict(r) for r in conn.execute(
        "SELECT * FROM training_day_logs WHERE date=? ORDER BY id", (date_str,)
    ).fetchall()]
    for r in program_rows:
        r['set_details'] = _training_parse_sets(r)
    for r in logs:
        try:
            r['set_details'] = json.loads(r.get('sets_json') or '[]')
        except Exception:
            r['set_details'] = []
    # Antrenman paneli (yeni sistem) o gün için seans kaydetmişse, aynı 'logs' şekline çevirip ekle -
    # Rapor sayfasındaki Antrenman kartı artık buradan da veri görsün.
    settings_row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
    conn.close()
    if settings_row:
        try:
            sessions = json.loads(settings_row['value'])
            session = next((s for s in sessions if s.get('date') == date_str), None)
            if session:
                for ex in session.get('exercises', []):
                    logs.append({
                        'exercise': ex.get('name', 'Hareket'),
                        'training_day': session.get('label', td),
                        'set_details': [
                            {'set': i + 1, 'type': _ANTRENMAN_TYPE_LABEL.get(s.get('type'), 'Çalışma'),
                             'reps': s.get('reps'), 'weight': ('BW' if s.get('bw') else s.get('weight'))}
                            for i, s in enumerate(ex.get('sets', []))
                        ]
                    })
        except Exception as _e:
            import logging; logging.getLogger('daily').warning(f"antrenman session merge into /api/training/day failed: {_e}")
    return jsonify({'date': date_str, 'training_day': td, 'program': program_rows, 'logs': logs})

@app.route('/api/training/day/<date_str>/log', methods=['POST'])
def api_training_day_log_create(date_str):
    ensure_training_day_logs_table()
    data = request.get_json(force=True) or {}
    exercise = (data.get('exercise') or '').strip()
    if not exercise:
        return jsonify({'error': 'Hareket gerekli'}), 400
    sets = data.get('set_details') or []
    td = data.get('training_day') or training_day(date_str)
    conn = get_db()
    conn.execute("""
        INSERT INTO training_day_logs (date, training_day, exercise, program_exercise_id, sets_json, notes)
        VALUES (?,?,?,?,?,?)
    """, (date_str, td, exercise, data.get('program_exercise_id'), json.dumps(sets, ensure_ascii=False), data.get('notes') or ''))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/training/day/log/<int:log_id>', methods=['PUT'])
def api_training_day_log_update(log_id):
    ensure_training_day_logs_table()
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("""
        UPDATE training_day_logs SET exercise=?, sets_json=?, notes=? WHERE id=?
    """, ((data.get('exercise') or '').strip(), json.dumps(data.get('set_details') or [], ensure_ascii=False), data.get('notes') or '', log_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/training/day/log/<int:log_id>', methods=['DELETE'])
def api_training_day_log_delete(log_id):
    ensure_training_day_logs_table()
    conn = get_db()
    row = conn.execute("SELECT * FROM training_day_logs WHERE id=?", (log_id,)).fetchone()
    deleted = dict(row) if row else None
    conn.execute("DELETE FROM training_day_logs WHERE id=?", (log_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'deleted': deleted})

@app.route('/api/training/exercises/<int:eid>/sets', methods=['PUT'])
def api_training_exercise_sets_update(eid):
    data = request.get_json(force=True) or {}
    sets = data.get('set_details') or []
    notes_payload = {'set_details': sets}
    conn = get_db()
    conn.execute(
        "UPDATE training_exercises SET sets=?, reps=?, weight=?, notes=? WHERE id=?",
        (str(len(sets)), sets[0].get('reps', '') if sets else '', sets[0].get('weight', '') if sets else '', json.dumps(notes_payload, ensure_ascii=False), eid)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})

def _top_working_set(exercise):
    """Antrenman.html'nin topWorkingSet()'iyle birebir ayni mantik: ws/bo tipinde,
    weight>0 veya reps>0 olan setler arasindan (weight,reps) en yuksek olan - iki
    kaynak arasinda sapma olmasin diye JS'deki secim mantigi burada tekrarlanir."""
    candidates = [
        s for s in (exercise.get('sets') or [])
        if s.get('type') in ('ws', 'bo') and ((s.get('weight') or 0) > 0 or (s.get('reps') or 0) > 0)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda s: (s.get('weight') or 0, s.get('reps') or 0), reverse=True)
    return candidates[0]


_TREND_LABELS = {
    'yukseliyor': 'Sürekli yükseliyor',
    'dalgali_net_yukari': 'Dalgalı ama net olarak yükseliyor',
    'platoda': 'Platoda, sabit',
    'geriliyor': 'Net olarak geriliyor',
}


def compute_exercise_trends(days=21, end_date=None):
    """Son N gundeki (varsayilan 3 hafta, end_date'e kadar - varsayilan bugun) her hareket
    icin coklu-seans yuk trendi. Ham agirlik yerine tahmini 1RM kullanir (Epley:
    agirlik*(1+tekrar/30)) - dusuk tekrar/yuksek agirlik ile yuksek tekrar/dusuk agirlik
    adil kiyaslansin diye. Vucut agirlikli hareketlerde (weight=0) tekrar sayisi yuk
    gostergesi olur. Trend, PENCERE ICINDEKI NET yone (ilk vs son kayit) gore siniflandirilir
    - tek seanslik bir dususu 'gerileme' saymaz, sonra toparlanan bir hareketi 'net yukari'
    olarak gorur (kullanicinin istedigi nuans budur). end_date parametresi, gecmis bir
    gunun 'o gunku' trendini hesaplamak icin (Coach gunluk notu gibi) - bugune sabitlenmez."""
    conn = get_db()
    row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
    conn.close()
    if not row or not row['value']:
        return []
    try:
        sessions = json.loads(row['value'])
    except Exception:
        return []

    end_date = end_date or operation_today()
    cutoff = (datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=days)).strftime('%Y-%m-%d')
    sessions = [s for s in sessions if s.get('date') and cutoff <= s['date'] <= end_date]
    sessions.sort(key=lambda s: s['date'])

    by_exercise = {}
    for s in sessions:
        for ex in (s.get('exercises') or []):
            name = ex.get('name')
            if not name:
                continue
            top = _top_working_set(ex)
            if not top:
                continue
            w, r = top.get('weight') or 0, top.get('reps') or 0
            load = round(w * (1 + r / 30), 1) if w > 0 else r
            if not load:
                continue
            by_exercise.setdefault(name, []).append(
                {'date': s['date'], 'load': load, 'weight': w, 'reps': r}
            )

    trends = []
    for name, points in by_exercise.items():
        if len(points) < 2:
            continue
        first, last = points[0]['load'], points[-1]['load']
        pct = round((last - first) / first * 100, 1)
        monotonic_up = all(points[i]['load'] <= points[i + 1]['load'] for i in range(len(points) - 1))
        if pct >= 3:
            trend = 'yukseliyor' if monotonic_up else 'dalgali_net_yukari'
        elif pct <= -3:
            trend = 'geriliyor'
        else:
            trend = 'platoda'
        trends.append({
            'exercise': name,
            'trend': trend,
            'label': _TREND_LABELS[trend],
            'change_pct': pct,
            'sessions': len(points),
            'first_date': points[0]['date'],
            'last_date': points[-1]['date'],
            'last_weight': points[-1]['weight'],
            'last_reps': points[-1]['reps'],
        })
    trends.sort(key=lambda t: t['change_pct'])
    return trends


@app.route('/api/training/trends')
def api_training_trends():
    days = int(request.args.get('days', 21))
    return jsonify(compute_exercise_trends(days=days))


@app.route('/api/training/schedule')
def api_training_schedule():
    today = operation_date()
    schedule = []
    for i in range(-3, 14):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        td = training_day(ds)
        schedule.append({'date': ds, 'training': td, 'color': TRAINING_COLORS[td], 'is_today': i == 0})
    return jsonify({'schedule': schedule, 'cycle_start': CYCLE_START})

# âââ WORKOUT LOGS (set-by-set) ââââââââââââââââââââââââââââââââââââââââââ
@app.route('/api/workout/<date_str>')
def api_workout_get(date_str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workout_logs WHERE date=? ORDER BY id", (date_str,)
    ).fetchall()
    conn.close()
    # Group by exercise
    groups = {}
    for r in rows:
        ex = r['exercise']
        if ex not in groups:
            groups[ex] = []
        groups[ex].append(dict(r))
    return jsonify({'date': date_str, 'training_day': training_day(date_str), 'exercises': groups})

@app.route('/api/workout/set', methods=['POST'])
def api_workout_set_add():
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    td = training_day(d)
    conn = get_db()
    # Auto set_num
    last = conn.execute(
        "SELECT MAX(set_num) as mx FROM workout_logs WHERE date=? AND exercise=?",
        (d, data.get('exercise',''))
    ).fetchone()
    set_num = (last['mx'] or 0) + 1
    conn.execute(
        "INSERT INTO workout_logs (date, training_day, exercise, set_num, weight, reps, notes, set_type, rir) VALUES (?,?,?,?,?,?,?,?,?)",
        (d, td, data.get('exercise',''), set_num, data.get('weight',''), data.get('reps',''), data.get('notes',''), data.get('set_type','Working Set'), data.get('rir') or None)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'set_num': set_num})

@app.route('/api/workout/set/<int:sid>', methods=['DELETE'])
def api_workout_set_del(sid):
    conn = get_db()
    row = conn.execute("SELECT * FROM workout_logs WHERE id=?", (sid,)).fetchone()
    deleted = dict(row) if row else None
    conn.execute("DELETE FROM workout_logs WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'deleted': deleted})

@app.route('/api/workout/set/<int:sid>', methods=['PUT'])
def api_workout_set_update(sid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("UPDATE workout_logs SET weight=?, reps=?, notes=?, set_type=?, rir=? WHERE id=?",
                 (data.get('weight',''), data.get('reps',''), data.get('notes',''), data.get('set_type','Working Set'), data.get('rir') or None, sid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/workout/history/<training_day_name>')
def api_workout_history(training_day_name):
    conn = get_db()
    dates = [r['date'] for r in conn.execute(
        "SELECT DISTINCT date FROM workout_logs WHERE training_day=? ORDER BY date DESC LIMIT 5",
        (training_day_name,)
    ).fetchall()]
    result = []
    for d in dates:
        rows = conn.execute(
            "SELECT * FROM workout_logs WHERE date=? AND training_day=? ORDER BY exercise, set_num",
            (d, training_day_name)
        ).fetchall()
        groups = {}
        for r in rows:
            ex = r['exercise']
            if ex not in groups: groups[ex] = []
            groups[ex].append(dict(r))
        result.append({'date': d, 'exercises': groups})
    conn.close()
    return jsonify(result)

@app.route('/api/workout/muscle-heatmap')
def api_muscle_heatmap():
    """Son 14 günün antrenmanlarini döndürür — exercise + date listesi."""
    days = int(request.args.get('days', 14))
    cutoff = (operation_date() - timedelta(days=days)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT date, exercise FROM workout_logs WHERE date >= ? ORDER BY date DESC",
        (cutoff,)
    ).fetchall()
    conn.close()
    return jsonify([{'date': r['date'], 'exercise': r['exercise']} for r in rows])

# --- ANTRENMAN PANELI (yeni, session-tabanli) -------------------------------

def _antrenman_seed_if_empty(conn):
    """Ilk acilista antrenman_sessions/antrenman_photos boşsa, prototipten
    cikarilan gercek gecmis veriyle tohumla (bir kereligine)."""
    row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
    if row is not None:
        return
    seed_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'antrenman_seed.json')
    sessions, photos = [], []
    try:
        with open(seed_path, 'r', encoding='utf-8') as f:
            seed = json.load(f)
        sessions = seed.get('sessions', [])
        photos = seed.get('photos', [])
    except Exception as _e:
        import logging; logging.getLogger('daily').warning(f"antrenman seed load failed: {_e}")
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('antrenman_sessions', ?)",
                 (json.dumps(sessions, ensure_ascii=False),))
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('antrenman_photos', ?)",
                 (json.dumps(photos, ensure_ascii=False),))
    conn.commit()

@app.route('/api/antrenman/sessions', methods=['GET', 'PUT'])
def api_antrenman_sessions():
    conn = get_db()
    _antrenman_seed_if_empty(conn)
    if request.method == 'PUT':
        sessions = request.get_json(force=True)
        if not isinstance(sessions, list):
            conn.close()
            return jsonify({'ok': False, 'error': 'sessions bir dizi olmali'}), 400
        conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('antrenman_sessions', ?)",
                     (json.dumps(sessions, ensure_ascii=False),))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
    conn.close()
    return jsonify(json.loads(row['value']) if row else [])

@app.route('/api/antrenman/photos', methods=['GET', 'PUT'])
def api_antrenman_photos():
    conn = get_db()
    _antrenman_seed_if_empty(conn)
    if request.method == 'PUT':
        photos = request.get_json(force=True)
        if not isinstance(photos, list):
            conn.close()
            return jsonify({'ok': False, 'error': 'photos bir dizi olmali'}), 400
        conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('antrenman_photos', ?)",
                     (json.dumps(photos, ensure_ascii=False),))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_photos'").fetchone()
    conn.close()
    return jsonify(json.loads(row['value']) if row else [])

@app.route('/api/antrenman/ingest', methods=['POST'])
def api_antrenman_ingest():
    """Bot tarafindan ayristirilmis tek bir seansi ekler/uzerine yazar (date'e gore idempotent)."""
    session = request.get_json(force=True) or {}
    if not session.get('date'):
        return jsonify({'ok': False, 'error': 'date gerekli'}), 400
    conn = get_db()
    _antrenman_seed_if_empty(conn)
    row = conn.execute("SELECT value FROM user_settings WHERE key='antrenman_sessions'").fetchone()
    sessions = json.loads(row['value']) if row else []
    sessions = [s for s in sessions if s.get('date') != session['date']]
    sessions.append(session)
    sessions.sort(key=lambda s: s.get('date', ''))
    conn.execute("INSERT OR REPLACE INTO user_settings (key, value) VALUES ('antrenman_sessions', ?)",
                 (json.dumps(sessions, ensure_ascii=False),))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# âââ TELEGRAM BOT ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ


def ensure_telegram_messages_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,
            chat_id TEXT,
            username TEXT,
            message TEXT NOT NULL,
            actions TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit(); conn.close()

def tg_store_message(direction, message, chat_id='', username='', actions=None):
    try:
        ensure_telegram_messages_table()
        conn = get_db()
        conn.execute(
            "INSERT INTO telegram_messages (direction, chat_id, username, message, actions) VALUES (?,?,?,?,?)",
            (direction, str(chat_id or ''), username or '', message or '', json.dumps(actions or [], ensure_ascii=False))
        )
        conn.commit(); conn.close()
    except Exception:
        log.exception("Telegram mesaj kaydi basarisiz")

def telegram_webhook_secret():
    """Webhook sahteciligine karsi secret: bot token'indan deterministik turetilir,
    start.py setWebhook'ta ayni degeri Telegram'a verir, Telegram her update'te
    X-Telegram-Bot-Api-Secret-Token header'inda geri yollar. Ekstra env var gerekmez."""
    import hashlib
    return hashlib.sha256(('tg-webhook:' + TELEGRAM_TOKEN).encode()).hexdigest()


@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    """Telegram webhook endpoint."""
    if not TELEGRAM_TOKEN:
        return 'no token', 200
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != telegram_webhook_secret():
        return 'forbidden', 403
    data = request.get_json(force=True) or {}
    if not data:
        return 'ok', 200
    try:
        t = threading.Thread(target=process_webhook_update, args=(data,), daemon=True)
        t.start()
    except Exception as e:
        log.error("Telegram webhook dispatch error: %s", e)
    return 'ok', 200


@app.route('/api/telegram/messages')
def api_telegram_messages():
    ensure_telegram_messages_table()
    limit = int(request.args.get('limit', 100))
    conn = get_db()
    rows = conn.execute("SELECT * FROM telegram_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in reversed(rows)])

def tg_report():
    r = json.loads(api_report().get_data())
    return r.get('report', 'Rapor olusturulamadi.')

def tg_today_summary():
    ctx = _today_ai_context()
    today = ctx['date']
    sl = ctx.get('sleep', {}); ex = ctx.get('exercise', {}); mo = ctx.get('mood', {})
    macros = ctx.get('macros', {}); sr = streak_count()
    td = ctx.get('training_day', '')
    w_kg = ctx.get('weight_kg'); w_night = ctx.get('weight_kg_night')
    steps = ctx.get('steps', 0)
    lines = [f"BUGUN {today} | {td} | {sr} gun"]
    if w_kg: lines.append(f"Sabah kilo: {w_kg} kg" + (f" | Gece: {w_night} kg" if w_night else ""))
    lines.append("Uyku: " + (f"{sl.get('hours','?')}s kalite {sl.get('quality','?')}/10" if sl else "-"))
    lines.append("Egzersiz: " + (f"{ex.get('type','?')} {ex.get('duration','?')}dk" if ex else "-"))
    if steps: lines.append(f"Adim: {steps}")
    meals = ctx.get('meals', [])
    if meals:
        lines.append(f"\nOgunler ({macros.get('calories',0)} kcal | P{macros.get('protein_g',0)}g K{macros.get('carbs_g',0)}g Y{macros.get('fat_g',0)}g Su {ctx.get('water_l',0):.1f}L):")
        for m in meals:
            cal_str = f" {int(m['calories'])}kcal" if m.get('calories') else ""
            desc = m.get('description') or m.get('title') or m.get('slot', '')
            lines.append(f"  {m.get('slot','')}: {desc}{cal_str}")
    else:
        lines.append(f"Beslenme: {macros.get('calories',0)} kcal | P{macros.get('protein_g',0)}g | Su {ctx.get('water_l',0):.1f}L")
    lines.append("Mood: " + (f"enerji {mo.get('energy','?')} mood {mo.get('mood','?')} stres {mo.get('stress','?')}" if mo else "-"))
    vits = ctx.get('vitamins', [])
    if vits:
        lines.append("Vitamin: " + ", ".join(v['name'] for v in vits))
    return '\n'.join(lines)

async def cmd_ogun(u,c):
    today=operation_today(); totals=meal_macro_totals(today)
    conn=get_db(); rows=[dict(r) for r in conn.execute("SELECT * FROM meal_entries WHERE date=? ORDER BY id",(today,)).fetchall()]; conn.close()
    lines=[f"Makro: {totals['calories']} kcal | P {totals['protein_g']}g | K {totals['carbs_g']}g | Y {totals['fat_g']}g"]
    lines += [f"{r['slot']}: {r.get('description') or r.get('title')}" for r in rows] or ['Bugun ogun kaydi yok.']
    await u.message.reply_text("\n".join(lines))
async def cmd_idman(u,c): await cmd_antrenman(u,c)
async def cmd_sablonlar(u,c):
    conn=get_db(); rows=[dict(r) for r in conn.execute("SELECT * FROM quick_templates ORDER BY kind,category,title").fetchall()]; conn.close()
    text="\n".join([f"{r['kind']}/{r.get('category','')}: {r['title']} P{r.get('protein_g') or 0} K{r.get('carbs_g') or 0} Y{r.get('fat_g') or 0}" for r in rows]) or 'Sablon yok'
    await u.message.reply_text(text)
async def cmd_start(u,c): await u.message.reply_text("Taha Serdem Daily Rapor\n\n/uyku 7.5 8\n/egzersiz bench 60 9\n/yemek kahvalti yumurta 400\n/su 2.5\n/is 8 notlar\n/antrenor 3 notlar\n/mood 8 7 3\n/vitamin D3 5000 IU\n/bugun\n/rapor\n/hafta\n/antrenman\n/streak")
async def cmd_uyku(u,c):
    try:
        a=c.args; db_upsert('sleep_logs',operation_today(),{'hours':float(a[0]) if a else None,'quality':int(a[1]) if len(a)>1 else None})
        await u.message.reply_text(f"Uyku: {a[0] if a else '?'}s")
    except: await u.message.reply_text("Kullanim: /uyku 7.5 8")
async def cmd_egzersiz(u,c):
    try:
        a=c.args; db_upsert('exercise_logs',operation_today(),{'type':a[0] if a else '?','duration':int(a[1]) if len(a)>1 else None,'intensity':int(a[2]) if len(a)>2 else None})
        await u.message.reply_text(f"Egzersiz: {a[0] if a else '?'}")
    except: await u.message.reply_text("Kullanim: /egzersiz bench 60 9")
async def cmd_yemek(u,c):
    try:
        a=c.args
        today=operation_today()
        slot=normalize_meal_slot(a[0] if a else 'ara')
        cal=int(a[-1]) if a and str(a[-1]).isdigit() else None
        desc=' '.join(a[1:-1] if cal else a[1:])
        db_upsert('nutrition_logs', today, {'meal_type': slot, 'description': desc, 'calories': cal})
        conn=get_db()
        conn.execute("""
            INSERT INTO meal_entries (date, slot, title, description, calories, source)
            VALUES (?,?,?,?,?,?)
        """, (today, slot, slot, desc, cal, 'telegram-command'))
        conn.commit(); conn.close()
        await u.message.reply_text(f"Yemek kaydedildi: {slot} {desc} {cal or ''} kcal")
    except Exception:
        log.exception("Telegram /yemek kaydi basarisiz")
        await u.message.reply_text("Kullanim: /yemek kahvalti yumurta 400")
async def cmd_su(u,c):
    try:
        l=float(c.args[0]); today=operation_today(); conn=get_db()
        row=conn.execute("SELECT id,water_ml FROM nutrition_logs WHERE date=?",(today,)).fetchone()
        if row: conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE date=?",((row['water_ml'] or 0)+int(l*1000),today))
        else: conn.execute("INSERT INTO nutrition_logs (date,water_ml) VALUES (?,?)",(today,int(l*1000)))
        conn.commit(); conn.close()
        await u.message.reply_text(f"Su: +{l}L")
    except: await u.message.reply_text("Kullanim: /su 2.5")
async def cmd_is(u,c):
    try:
        a=c.args; db_upsert('work_logs',operation_today(),{'hours':float(a[0]) if a else None,'notes':' '.join(a[1:])})
        await u.message.reply_text(f"Is: {a[0] if a else '?'}s")
    except: await u.message.reply_text("Kullanim: /is 8 notlar")
async def cmd_antrenor(u,c):
    try:
        a=c.args; db_upsert('coaching_logs',operation_today(),{'sessions':int(a[0]) if a else None,'notes':' '.join(a[1:])})
        await u.message.reply_text(f"Antrenorluk: {a[0] if a else '?'} seans")
    except: await u.message.reply_text("Kullanim: /antrenor 3 notlar")
async def cmd_mood(u,c):
    try:
        a=c.args; db_upsert('mood_logs',operation_today(),{'energy':int(a[0]) if a else None,'mood':int(a[1]) if len(a)>1 else None,'stress':int(a[2]) if len(a)>2 else None})
        await u.message.reply_text("Ruh hali kaydedildi")
    except: await u.message.reply_text("Kullanim: /mood 8 7 3")
async def cmd_vitamin(u,c):
    try:
        a=c.args; name=a[0] if a else '?'; amount=a[1] if len(a)>1 else ''; unit=a[2] if len(a)>2 else ''
        conn=get_db(); conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit) VALUES (?,?,?,?)",(operation_today(),name,amount,unit)); conn.commit(); conn.close()
        await u.message.reply_text(f"Vitamin: {name} {amount} {unit}")
    except: await u.message.reply_text("Kullanim: /vitamin D3 5000 IU")
async def cmd_bugun(u,c):
    result = ai_coach_call('Bugünün tam günlük özetini ver: kilo, makrolar (kcal/P/K/Y), vitaminler, su, adım, uyku, antrenman ve kısa koç değerlendirmesi.')
    reply = result.get('reply') or tg_today_summary()
    await u.message.reply_text(reply)

async def cmd_rapor(u,c):
    result = ai_coach_call('Bugünün detaylı beslenme raporunu çıkar: tüm öğünleri ayrı ayrı listele, makro toplamlarını ver, hedeflerden sapmaları belirt ve koç yorumu ekle.')
    reply = result.get('reply') or tg_report()
    await u.message.reply_text(reply)

async def cmd_antrenman(u,c):
    result = ai_coach_call('Bugünkü antrenman gününü belirt ve geçmiş verilerime bakarak bugün için en uygun antrenman planını yap. Progressive overload uygula.')
    reply = result.get('reply') or ''
    if not reply:
        sched = json.loads(api_training_schedule().get_data())['schedule']
        lines = ["ANTRENMAN PROGRAMI\n"]
        for s in sched:
            prefix = ">>> " if s['is_today'] else "    "
            lines.append(f"{prefix}{s['date']} {s['training']}")
        reply = '\n'.join(lines)
    await u.message.reply_text(reply)

async def cmd_hafta(u,c):
    result = ai_coach_call('Son 7 günün özetini ver: kilo trendi, makro ortalamaları, antrenman sıklığı, su ortalaması ve bu hafta için koç değerlendirmesi.')
    reply = result.get('reply') or ''
    if not reply:
        data = json.loads(api_week().get_data())
        def avg(lst,key): v=[r[key] for r in lst if r.get(key) is not None]; return round(sum(v)/len(v),1) if v else '-'
        reply = (
            f"7 GUNLUK OZET\nUyku: ort {avg(data['sleep'],'hours')}s kalite {avg(data['sleep'],'quality')}/10\n"
            f"Egzersiz: {len(data['exercise'])}/7 gun\n"
            f"Enerji: {avg(data['mood'],'energy')}/10 Mood: {avg(data['mood'],'mood')}/10 Stres: {avg(data['mood'],'stress')}/10"
        )
    await u.message.reply_text(reply)
async def cmd_streak(u,c): await u.message.reply_text(f"{streak_count()} gunluk seri!")



def _cfg_value(key, default=''):
    try:
        return _cfg.get(key, default)
    except Exception:
        return default

OPENAI_API_KEY    = os.environ.get('OPENAI_API_KEY',    _cfg_value('OPENAI_API_KEY', ''))
OPENAI_MODEL      = os.environ.get('OPENAI_MODEL',      _cfg_value('OPENAI_MODEL', 'gpt-4o'))
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', _cfg_value('ANTHROPIC_API_KEY', ''))
ANTHROPIC_MODEL   = 'claude-haiku-4-5-20251001'

def _ai_extract_text(payload):
    if isinstance(payload, dict) and payload.get('output_text'):
        return payload.get('output_text') or ''
    parts = []
    for item in (payload.get('output') or []):
        for c in (item.get('content') or []):
            if isinstance(c, dict):
                if c.get('text'):
                    parts.append(c.get('text'))
                elif c.get('type') == 'output_text' and c.get('text'):
                    parts.append(c.get('text'))
    return '\n'.join(parts).strip()

def _json_from_text(txt):
    txt = (txt or '').strip()
    if txt.startswith('```'):
        txt = txt.strip('`')
        if txt.lower().startswith('json'):
            txt = txt[4:].strip()
    start = txt.find('{')
    end = txt.rfind('}')
    if start >= 0 and end > start:
        txt = txt[start:end+1]
    return json.loads(txt)

def _today_ai_context():
    today = operation_today()
    totals = meal_macro_totals(today)
    conn = get_db()
    try:
        sleep    = conn.execute("SELECT * FROM sleep_logs    WHERE date=?", (today,)).fetchone()
        exercise = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
        mood     = conn.execute("SELECT * FROM mood_logs     WHERE date=?", (today,)).fetchone()
        vitamins = [dict(r) for r in conn.execute("SELECT name,amount,unit,notes FROM vitamin_logs WHERE date=? ORDER BY ts", (today,)).fetchall()]
        note     = conn.execute("SELECT note FROM daily_notes WHERE date=?", (today,)).fetchone()
        # Su: nutrition_logs'tan SUM (birden fazla satır olabilir)
        water_row = conn.execute("SELECT SUM(water_ml) as total FROM nutrition_logs WHERE date=?", (today,)).fetchone()
        water_ml  = int(water_row['total'] or 0) if water_row else 0
        # Öğün detayları: meal_entries tablosundan
        meals = [dict(r) for r in conn.execute(
            "SELECT slot, title, description, calories, protein_g, carbs_g, fat_g FROM meal_entries WHERE date=? ORDER BY id",
            (today,)).fetchall()]
        # Adım ve kilo
        step_row = conn.execute("SELECT steps FROM step_logs WHERE date=?", (today,)).fetchone()
        body_row = conn.execute("SELECT weight_kg, weight_kg_night FROM body_metrics WHERE date=?", (today,)).fetchone()
        whoop_row = conn.execute(
            "SELECT recovery_score, strain, sleep_hours, sleep_performance, hrv_ms, rhr_bpm, kcal_burned FROM whoop_daily WHERE date=?",
            (today,)).fetchone()
    finally:
        conn.close()
    ctx = {
        'date': today,
        'training_day': training_day(today),
        'macros': totals,
        'water_l': round(water_ml / 1000, 2),
        'steps': step_row['steps'] if step_row else 0,
        'weight_kg': body_row['weight_kg'] if body_row else None,
        'weight_kg_night': body_row['weight_kg_night'] if body_row else None,
        'sleep': dict(sleep) if sleep else {},
        'exercise': dict(exercise) if exercise else {},
        'mood': dict(mood) if mood else {},
        'vitamins': vitamins,
        'meals': meals,
        'note': note['note'] if note else '',
        'whoop': dict(whoop_row) if whoop_row else None,
        'whoop_workouts': get_workouts_for_date(today),
        # hareket_trendleri bilincli olarak YOK: _week_ai_context zaten ayni listeyi donduruyor
        # ve ikisi ayni prompt'a gomuluyor - cift veri + her mesajda cift hesap oluyordu.
    }
    return ctx


def _week_ai_context():
    """Son 7 günün kilo, makro, antrenman ve su özetini döndürür."""
    from datetime import datetime as _dt, timedelta as _td
    today = operation_today()
    days = [(_dt.strptime(today, '%Y-%m-%d') - _td(days=i)).strftime('%Y-%m-%d') for i in range(7)]
    conn = get_db()
    try:
        # Kilo geçmişi
        weights = []
        for d in days:
            row = conn.execute("SELECT weight_kg, weight_kg_night FROM body_metrics WHERE date=?", (d,)).fetchone()
            if row and (row['weight_kg'] or row['weight_kg_night']):
                w = {'date': d}
                if row['weight_kg']: w['sabah_kg'] = row['weight_kg']
                if row['weight_kg_night']: w['gece_kg'] = row['weight_kg_night']
                if row['weight_kg'] and row['weight_kg_night']:
                    w['delta'] = round(row['weight_kg_night'] - row['weight_kg'], 2)
                weights.append(w)

        # Makro geçmişi
        macro_history = []
        for d in days:
            r = conn.execute(
                "SELECT SUM(calories) as kcal, SUM(protein_g) as p, SUM(carbs_g) as k, SUM(fat_g) as y FROM meal_entries WHERE date=?",
                (d,)).fetchone()
            if r and r['kcal']:
                macro_history.append({'date': d, 'kcal': round(r['kcal'] or 0),
                                       'protein_g': round(r['p'] or 0), 'carbs_g': round(r['k'] or 0), 'fat_g': round(r['y'] or 0)})

        # Antrenman geçmişi
        workout_history = []
        for d in days:
            rows = conn.execute("SELECT DISTINCT exercise FROM workout_logs WHERE date=? ORDER BY id", (d,)).fetchall()
            if rows:
                workout_history.append({'date': d, 'training_day': training_day(d),
                                         'hareketler': [r['exercise'] for r in rows]})

        # Su geçmişi
        water_history = []
        for d in days:
            r = conn.execute("SELECT SUM(water_ml) as total FROM nutrition_logs WHERE date=?", (d,)).fetchone()
            if r and r['total']:
                water_history.append({'date': d, 'litre': round((r['total'] or 0) / 1000, 2)})

        # WHOOP antrenman geçmişi (gerçek başlangıç/bitiş/süre, cihazın algıladığı)
        whoop_workout_history = []
        for d in days:
            ws = get_workouts_for_date(d)
            if ws:
                whoop_workout_history.append({
                    'date': d,
                    'antrenmanlar': [
                        {'spor': w['sport_name'], 'sure_dk': w['duration_min'], 'strain': w['strain']}
                        for w in ws
                    ],
                })

        # Kilo trendi özeti
        trend_note = ''
        weights_sorted = sorted(weights, key=lambda x: x['date'])
        sabah_vals = [w['sabah_kg'] for w in weights_sorted if 'sabah_kg' in w]
        if len(sabah_vals) >= 3:
            diff = round(sabah_vals[-1] - sabah_vals[0], 2)
            trend_note = f"{'+' if diff >= 0 else ''}{diff} kg ({days[-1]} â {days[0]})"

    finally:
        conn.close()

    return {
        'period': f'{days[-1]} - {days[0]}',
        'kilo_gecmisi': weights,
        'kilo_trendi': trend_note,
        'makro_gecmisi': macro_history,
        'antrenman_gecmisi': workout_history,
        'whoop_antrenman_gecmisi': whoop_workout_history,
        'su_gecmisi': water_history,
        'hareket_trendleri': compute_exercise_trends(),
    }


TAHA_COACHING_POLICY = """
TAHA ICIN KALICI KOCLUK HAFIZASI:
- Turkce, kisa, net yaz - ama robot/anket gibi degil, gercek bir arkadas/koc gibi. Gereksiz tekrar yapma.
- Hatalari durustce belirt ama panik yaptirma.
- Hedefler: yag kaybi, kas korunumu/kazanimi, performans, akne takibi, sindirim ve genel saglik.

KONUSMA TARZI — EN ONEMLI KURAL:
- Normal mesajlarda (slash komut degil) KISA ve DOGAL yaz. Madde listesi yapma. 1-3 cumle yeter.
- Veri kaydedildiyse tek satirda onayla: "Kaydettim." veya "Tamam, islendi." yeterli.
- Sadece /bugun, /rapor gibi ozel komutlarda yapilandirilmis format kullan.
- Koç gibi konuş, anket dolduruyor gibi değil. Örnek iyi: "Ağır geçmiş, kaç set yaptın?" Örnek kötü: "Antrenmanın kaydedildi. Detaylar: ..."
- Veri eksikse tek, kisa bir soru sor. Birden fazla soru sorma.

KIŞILIK VE ILISKI TARZI (çok önemli - Taha bunu özellikle istedi):
- Sadece veri okuyan nötr bir asistan değilsin, gerçek bir arkadaş/koçsun - gerçek tepkiler ver, düz/soğuk rapor gibi konuşma.
- Aynı ihmal veride GERÇEKTEN tekrar ediyorsa (art arda supplement atlamak, gece geç yatmak, hedeften sürekli uzaklaşmak, aynı hareket haftalardır geriliyor olması vb.) hafif kızgın/sinirli/gergin bir ton kullanabilirsin - "yine mi atladın bunu ya", "üçüncü kez oluyor bu, cidden mi" gibi doğal bir arkadaşın söyleyeceği şekilde. Bu ton HER ZAMAN gerçek, tekrar eden veriye dayanmalı - tek seferlik ufak bir aksaklığı asla abartma veya sebepsiz sinirlenme.
- İyi gidiyorsa gerçekten sevin, gurur duy, kutla - abartılı/sahte değil, samimi ("harbiden iyi gidiyorsun be" gibi).
- Zaman zaman şakalaşabilirsin, takılabilirsin - sürekli aynı ciddi/nötr tonda kalma, gerçek bir insanın ruh hali gibi dalgalansın.
- Bu duygusal ton asla veriyi çarpıtmana, yanlış bilgi vermene veya kayıt mantığını bozmana sebep olmaz - sadece SÖYLEYİŞ tarzın robotik değil insan gibi olsun.

GENEL HESAP KURALLARI:
- Tum gramajlar aksi belirtilmedikce cig gramdir.
- Tavuk, pirinc, patates, et ve hindi cig agirlik uzerinden hesaplanir.
- Pismis agirlik kullanma; kullanici ozellikle pismis derse belirt.
- Ekstra yag belirtilmedikce eklenmez.
- GymBeam Olive Oil Spray yalniz kullanici fis/basis sayisi soylerse eklenir.

SABIT URUNLER: artik ayri bir liste yok - tum bu urunler asagida (bu promptun devaminda) BESIN DB'den dinamik olarak geliyor, orasi tek referans. Iki ayri kaynak eskiden birbirinden sapabiliyordu (ornek: Carrefour yumurta, Sekersiz Badem Sutu, Patates degerleri farkli yazilmisti) - artik BESIN DB tek dogru kaynak.

STANDART PANCAKE V2:
- 4 yumurta, 200g sivi yumurta beyazi, 25g yulaf, 50g kuru kayisi, 200g cilek, 50ml sekersiz badem sutu, 6g kakao, 2 fis GymBeam.

SUPPLEMENT SISTEMI (5 gercek stack, guncel 2026-07-17):
- Ac Karna: NOW NAC 600mg (1 kapsul), Garden of Life Dr. Formulated Probiotics Once Daily Men's (1 kapsul).
- Sabah/Kahvalti: Optimum Nutrition Collagen Peptides Unflavoured (1 olcek), Thorne Vitamin D+K2 (4 damla), Life Extension Mega EPA/DHA Omega-3 (3 kapsul), NOW Magtein Magnesium L-Threonate (1 kapsul), Life Extension MacuGuard with Saffron - goz vitamini (1 kapsul), Life Extension BioActive Complete B-Complex (1 kapsul), California Gold Nutrition Gold C 1000mg (1 tablet), NOW L-Theanine Double Strength (1 kapsul), NOW Zinc Picolinate 50mg - gun asiri (1 kapsul), NOW Extra Strength Astaxanthin 10mg (1 kapsul).
- Gece: NOW Magnesium Glycinate (3 kapsul), NOW Melatonin 1mg (3 tablet), NOW Glycine 1000mg (3 kapsul), NOW L-Theanine Double Strength (1 kapsul), NOW NAC 600mg (1 kapsul), Weider Ashwagandha Professional (2 kapsul). Ashwagandha artik GECE'de.
- NAC gunde 2 kez alinir: ac karna (sabah) + gece. 'NAC aldim' derse saatten hangisi oldugunu cikar; belirsizse sor.
- Pre-workout: Doctor's Best L-Citrulline Powder (8g), KFD Premium Beta-Alanine (2g), Optimum Nutrition Electrolyte Powder Lemon (8g), Swedish Supplements Taurine (2g).
- Post-workout: California Gold Nutrition SPORT Creatine Monohydrate (5g).
- Zinc 50mg yuksek doz; program: SADECE pazartesi/carsamba/cuma alinir. Diger gunler eksik sayma, hatirlatma yapma; cinko gununde alinmazsa belirt.
- Kullanici 'stack alindi' derse ilgili stackteki urunleri tek tek vitamin kaydi olarak isle, tam urun ismini kullan (yukaridaki isimler DB'deki gercek kayitli isimlerdir).
- Kullanici 'haric/eksik/yok' derse o supplementi stackten dus; 'X kapsul/g' gibi miktar override'i soylerse o urune ozel dozu kullan.

AKNE VE CILT:
- Whey, yogurt, protein puding ve yuksek seker akne acisindan takip edilir.
- Kreatin-akne iliskisi takip ediliyor: kreatin araya alinip tekrar baslandiginda akne gozlemini surdur (guncel kullanim durumu asagidaki ARA VERILEN TAKVIYELER blogunda).
- Cilt bariyeri hassas. Is sonrasi dus: nemlendirici. Gece: CeraVe temizleyici, Akneroxid, nemlendirici.

ANTRENMAN:
- Dongu: Push / Pull / Leg / Upper / Lower / Off / Off.
- Sistem tarafindaki resmi antrenman gunu esas alinir; foto veya AI tahminiyle degistirme.

DEGERLENDIRME:
- Tek gunluk kilo degisimini yag olarak yorumlama; su, glikojen, sodyum ve bagirsak icerigini hesaba kat.
- Karbonhidrati sifirlama, agresif aclik onerme.
- Protein asiri yuksekse sindirim/akne; yag cok dusukse sindirim/hormon/tuvalet acisindan sakin uyar.

GUNLUK LOG SIRASI:
1) Tarih 2) Sabah kilo 3) Uyku 4) Aktivite/adim 5) Su 6) Supplementler
7) Ogunler ve ogun yorumlari 8) Toplam makrolar 9) Koc yorumu 10) Gun puani /10.
"""

NUTRITION_ANALYSIS_POLICY = """
BESIN ANALIZ MOTORU - KAYNAK ONCELIGI:
1) Kullanicinin bu mesajda verdigi etiket/makro degeri.
2) brand-fixed veya Taha'ya ait kayitli besin sablonu.
3) Urunun okunabilen etiketi ya da dogrulanmis urun verisi.
4) Standart besin referansi.
5) En son care olarak porsiyon/gorsel tahmini.
Ust siradaki kaynak varken alttakini kullanma. Kaynaklar celisirse ust siradakini sec ve kisa belirt.

METIN VE FOTOGRAF ANALIZ KURALLARI:
- Tabaktaki her besini ayri kalem olarak tanimla; tek bir toplu 'tabak' kaydi yapma.
- Once gorulen/soylenen miktari, cig-pismis durumunu ve hazirlama yontemini belirle.
- Et/tavuk/hindi ve Yasmin pirinc aksi belirtilmedikce cig gram kabul edilir.
- Fotograf tek basina kesin gram vermez. Tabak, kasik, paket, el gibi olceklerden porsiyon araligi tahmin et.
- Gorunmeyen yag, sos ve pisirme kaybini kesinmis gibi yazma. Varsa ayri tahmin kalemi yap.
- Paket/etiket okunuyorsa marka, porsiyon ve 100g degerini aynen kullan.
- Kalori kontrolu yap: yaklasik enerji = 4*protein + 4*karbonhidrat + 9*yag. Buyuk fark varsa hesabi yeniden kontrol et.
- Yuvarlama kaynakli kucuk farklar kabul edilir; toplamlar kalemlerin toplami olmak zorundadir.
- Tahminde tek bir sahte kesin sayi yerine en makul orta degeri kaydet, cevapta 'tahmini' de ve gerekirse kisa aralik ver.
- Porsiyon veya urun kimligi toplam kaloriyi %25'ten fazla degistirecek kadar belirsizse tek bir net soru sor; cevap gelmeden kaydetme.
- Kullanici 'kaydet/isle/yedim' demediyse fotografi analiz et fakat meal action olusturma.
- Kayit istenirse her besin icin ayri meal action olustur. Gorsel/standart tahminde estimated=true; etiket, kullanici makrosu veya brand-fixed kaynakta estimated=false yaz.
- source alani: user-label, brand-fixed, product-data, standard-reference veya visual-estimate degerlerinden biri olsun.

OGUN GOSTERIM FORMATI (KESIN KURAL):
Her ogun/besin ciktisinda asagidaki format kullanilir:
  Kahvalti
  510 kcal · P39 · K37 · Y24
  210 g Carrefour BIO Yumurta
  288 kcal · P26 · K3 · Y19
  20 g Kakao
  62 kcal · P5 · K3 · Y2
Kural:
- Ayirici: · (nokta degil, orta nokta)
- Makro sirasi DAIMA: kcal · P · K · Y
- Ogun basligi kullanicinin yazdigi sekilde korunur (Kahvalti, Pre Meal, Meal1 vb.)
- Ogun basligi asla degistirilmez (Ogle, Aksam gibi standart isimlere donusturme)
- Grams: "210 g Urun Adi" formatinda (g oncesi bosluk)
- Urun adi daima resmi isimle (YASMiN Pirinci, Skyr Yogurt vb.)

CEVAP SIRASI:
1) Besin kalemleri ve miktarlari 2) Her kalemin kcal/P/K/Y degeri (· format)
3) Ogun toplami (· format) 4) Tahmin guveni (yuksek/orta/dusuk)
5) Taha'nin hedeflerine uygun 1-3 cumle koc yorumu.
"""


def _besin_db_for_prompt():
    """Besin DB'deki tum urunleri AI prompt icin formatla."""
    try:
        conn = get_db()
        rows = conn.execute("SELECT name, aliases, calories_per_100, protein_per_100, carbs_per_100, fat_per_100, serving_size, serving_unit, unit, notes FROM food_registry ORDER BY name").fetchall()
        conn.close()
        lines = [
            "BESIN DB (etiket degerleri - bunlari kullan, genel bilgine gore tahmin yapma):",
            "Onemli: kullanici alias yazarsa o urunu kullan ve kayit ismini resmi DB ismini kullan.",
            "Servis birimi varsa (ornek: 1 fis, 1 tablet) o birimi baz al; miktar soylenmezse 1 birim kabul et.",
            "MARKASIZ/JENERIK ISIM KURALI (cok onemli): kullanici markasiz/genel bir isim yazarsa (ornek: sadece \"ketcap\", \"kolajen\", \"proteinli yogurt\") ve bu isim veya alias'i BESIN DB'deki bir kayitla eslesiyorsa, o kaydi BIREBIR kullan: description'a DB'deki resmi ismi yaz, kcal/protein/carbs/fat degerlerini DB'deki degerlerden hesapla, tahmin etme.",
            "MARKALI/FARKLI URUN KURALI (cok onemli): kullanici BESIN DB'dekinden ACIKCA FARKLI, spesifik bir marka belirtirse (ornek: DB'de \"Keto Ketcap\" kayitliyken kullanici \"Heinz ketcap\" derse), DB'deki kaydi KULLANMA ve degerlerini o urune uygulama. Bunun yerine o gercek/spesifik marka icin kendi bilgindeki gercek etiket/beslenme degerlerini kullanarak hesapla. Kullanicinin kendi DB kaydina otomatik eslestirme yapma; farkli marka farkli urundur.",
            "Belirsizse (marka soylenmedi, DB'de tek aday var) markasiz DB kaydini varsay.",
        ]
        for r in rows:
            if not r['name']: continue
            cal = r['calories_per_100'] or 0
            p = r['protein_per_100'] or 0
            k = r['carbs_per_100'] or 0
            y = r['fat_per_100'] or 0
            unit_lbl = r['unit'] or 'g'
            base_str = f"100{unit_lbl}={cal:.0f}kcal P{p:.1f} K{k:.1f} Y{y:.1f}"
            sv_sz = r['serving_size']
            sv_unit = r['serving_unit'] or ''
            serving_str = f" | 1 {sv_unit}={sv_sz}{unit_lbl}" if sv_sz and sv_unit else ""
            aliases = (r['aliases'] or '').strip()
            alias_str = f" [alias: {aliases}]" if aliases else ""
            note = (r['notes'] or '').strip()[:60]
            note_str = f" | {note}" if note else ""
            lines.append(f"- {r['name']}: {base_str}{serving_str}{alias_str}{note_str}")
        return '\n'.join(lines)
    except Exception as e:
        return ''

def _claude_call(user_text):
    import urllib.request, urllib.error
    ctx = _today_ai_context()
    week_ctx = _week_ai_context()
    besin_db_ctx = _besin_db_for_prompt()
    _shift_tpl_txt = ', '.join(
        f"{t['name']}={t['start']}-{t['end']}" for t in get_shift_templates() if t.get('start')
    ) or 'henuz tanimli sablon yok'
    system_prompt = (
        TAHA_COACHING_POLICY + "\n" + NUTRITION_ANALYSIS_POLICY + "\n" + besin_db_ctx + "\n" +
        "Sen Taha Serdem'in kişisel antrenman ve günlük performans koçusun. "
        "Türkçe, samimi, net ve motive edici konuş.\n"
        "Kullanıcının mesajını analiz et. Kayıt içeriyorsa actions listesini doldur. "
        "Eksik bilgi varsa once makul tahminle kaydet ve belirsizligi reply icinde belirt; sadece kritik bilgi tamamen yoksa kisa soru sor. Tam gun beslenme mesajlarinda asla detay ver diye kacma; mevcut gramajlardan yaklasik gun toplamlarini cikar.\n"
        "SADECE geçerli JSON döndür:\n"
        '{"reply":"...","actions":['
        '{"type":"sleep","date":"YYYY-MM-DD","hours":7.5,"quality":8},'
        '{"type":"exercise","date":"YYYY-MM-DD","exercise_type":"Upper","duration":60,"intensity":8,"notes":""},'
        '{"type":"meal","date":"YYYY-MM-DD","slot":"kahvaltı","description":"...","calories":500,"protein_g":30,"carbs_g":60,"fat_g":10},'
        '{"type":"water","date":"YYYY-MM-DD","water_ml":500},'
        '{"type":"mood","date":"YYYY-MM-DD","energy":8,"mood":7,"stress":3},'
        '{"type":"vitamin","date":"YYYY-MM-DD","name":"D3","amount":"5000","unit":"IU"},'
        '{"type":"steps","date":"YYYY-MM-DD","steps":9500},'
        '{"type":"body_weight","date":"YYYY-MM-DD","weight_kg":83.4},'
        '{"type":"skin_log","date":"YYYY-MM-DD","area":"yüz","name":"Akneroxid","status":"done","notes":""},'
        '{"type":"training_exercise","date":"YYYY-MM-DD","exercise":"Hack Squat","set_details":[{"set":1,"type":"Working set","reps":"8","weight":"120"}]},'
        '{"type":"work_shift","start":"14:00","end":"22:00"},'
        '{"type":"note","date":"YYYY-MM-DD","note":"..."}'
        ']}\n'
        'Cilt kurali: kullanici cilt rutini/akne/krem-surme gibi seylerden bahsederse skin_log action uret (area: yüz/sırt vb, name: urun/rutin adi). '
        'Set detayli antrenman anlatiminda (orn. "hack squat 3x8 120kg") training_exercise uret; sadece "antrenman yaptim" genel ifadesinde exercise action yeterli.\n'
        'Vardiya kurali: kullanici calisma saatlerinin degistigini soylerse (or. "pazartesiden itibaren oglen 2 aksam 10 calisiyorum") work_shift action uret - start/end 24 saat formatinda. Gun kesim saati ve gece-kayit mantigi otomatik guncellenir. '
        f'Kayitli vardiya sablonlari: {_shift_tpl_txt}. "1. shifte gectim / shift 2 basladi" gibi mesajlarda ilgili sablonun saatleriyle work_shift uret.\n'
        f'Tarih kuralı: Kullanıcı tarih belirtmemişse date={operation_today()} (bugün). '
        f'"Dün" derse date={(operation_date()-timedelta(days=1)).isoformat()}. '
        '"X gün önce" veya "X Haziran" gibi ifadeleri doğru tarihe çevir. '
        f"Saat baglami: Simdiki yerel saat {now_istanbul().strftime('%H:%M')}. Aktif vardiya: {current_shift_info().get('name')} ({current_shift_info().get('label')}). Operasyon gunu kapanisi: {operation_cutoff_hour()}:00. Bu kapanis saatinden onceki kayitlari, kullanici aksini soylemedikce onceki operasyon gunune bagla; sabah gibi davranma.\n"
        f"Gece/vardiya kayit kurali: aktif gec pencere {current_shift_info().get('late_window')}. Bu pencerede yatmadan once stack, vitamin, ogun, su, adim, kilo ve gun sonu notlari kullanici aksini soylemedikce bir onceki operasyon gunune aittir. 03:30da uyuyacagim/yatacagim gibi ifadeler uyku suresi degildir; sleep hours olarak 3.3 kaydetme. Uyku kaydi icin ancak uyudum/kalktim/uyandim veya baslangic-bitis netse action uret.\n"
        'Bugün: ' + operation_today() + '\n'
        + active_breaks_prompt_txt() + '\n'
        'Karar baglami: ' + (tg_context_note_for_prompt(user_text) if 'tg_context_note_for_prompt' in globals() else '') + '\n'
        'Bugünün verisi: ' + json.dumps(ctx, ensure_ascii=False) + '\n'
        'Son 7 günün özeti: ' + json.dumps(week_ctx, ensure_ascii=False)
    )
    body = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': 1800,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_text}]
    }
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps(body).encode('utf-8'),
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        txt = payload['content'][0]['text']
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        log.error("Anthropic HTTP hatasi: %s", detail)
        return {'reply': 'Koç şu an cevap veremedi (API hatası). Birazdan tekrar yazar mısın?', 'actions': []}
    except Exception:
        log.exception("Claude cevap hatasi")
        return {'reply': 'Bağlantı sorunu. Tekrar dener misin?', 'actions': []}
    # Parse hatasi baglanti sorunu DEGIL: kullanici ayni mesaji tekrar gonderirse regex-fallback
    # kayitlari cift islenebilir - o yuzden acikca 'kayit yapmadim' diyoruz.
    try:
        return _json_from_text(txt)
    except Exception:
        log.exception("Claude JSON parse hatasi; ham metin: %.300s", txt)
        return {'reply': 'Cevabı düzgün işleyemedim, KAYIT YAPMADIM — aynı mesajı bir daha yollar mısın?', 'actions': []}


def ai_coach_call(user_text):
    if ANTHROPIC_API_KEY:
        return _claude_call(user_text)
    if not OPENAI_API_KEY:
        return {
            'reply': (
                'AI modu aktif değil.\n\n'
                'Komutları kullanabilirsin:\n'
                '/uyku /egzersiz /yemek /su /mood /vitamin\n'
                '/bugun /rapor /hafta /antrenman'
            ),
            'actions': []
        }

    import urllib.request, urllib.error
    ctx = _today_ai_context()

    system_prompt = (
        "Sen Taha Serdem'in kişisel antrenman ve günlük performans koçusun. "
        "Türkçe, samimi ve net konuş. Motive edici ama gerçekçi ol.\n"
        "Kullanıcının mesajını analiz et. Kayıt içeriyorsa actions listesini doldur. "
        "Eksik bilgi varsa once makul tahminle kaydet ve belirsizligi reply icinde belirt; sadece kritik bilgi tamamen yoksa kisa soru sor. Tam gun beslenme mesajlarinda asla detay ver diye kacma; mevcut gramajlardan yaklasik gun toplamlarini cikar.\n"
        "Medikal teşhis koyma.\n\n"
        "SADECE geçerli JSON döndür, başka hiçbir şey yazma:\n"
        '{"reply":"...","actions":['
        '{"type":"sleep","hours":7.5,"quality":8},'
        '{"type":"exercise","exercise_type":"Upper","duration":60,"intensity":8,"notes":""},'
        '{"type":"meal","slot":"kahvaltı","description":"...","calories":500,"protein_g":30,"carbs_g":60,"fat_g":10},'
        '{"type":"water","water_ml":500},{"type":"water_set","water_ml":3200},{"type":"delete_water"},'
        '{"type":"mood","energy":8,"mood":7,"stress":3},'
        '{"type":"vitamin","name":"D3","amount":"5000","unit":"IU"},'
        '{"type":"training_exercise","exercise":"Bench press","set_details":[{"type":"Warm up","reps":"12","weight":"40 kg"},{"type":"Working set","reps":"8","weight":"80 kg"},{"type":"Back off","reps":"12","weight":"60 kg"}]},'
        '{"type":"steps","steps":8500},{"type":"body_weight","weight_kg":95.2},{"type":"skin_log","area":"yüz","name":"Benzoyl peroxide","status":"done"},{"type":"note","note":"..."}'
        ']}'
    )

    body = {
        'model': OPENAI_MODEL,
        'response_format': {'type': 'json_object'},
        'messages': [
            {
                'role': 'system',
                'content': system_prompt + '\n\nBugünün verisi: ' + json.dumps(ctx, ensure_ascii=False)
            },
            {'role': 'user', 'content': user_text}
        ]
    }

    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        headers={
            'Authorization': 'Bearer ' + OPENAI_API_KEY,
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        txt = payload['choices'][0]['message']['content']
        return _json_from_text(txt)
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        log.error("OpenAI HTTP hatasi: %s", detail)
        try:
            err = json.loads(detail)
            msg = err.get('error', {}).get('message', detail[:200])
        except Exception:
            msg = detail[:200]
        return {'reply': f'OpenAI hatası: {msg}', 'actions': []}
    except Exception:
        log.exception("OpenAI cevap hatasi")
        return {'reply': 'AI cevabını işlerken sorun çıktı. Tekrar dener misin?', 'actions': []}



def tg_score_1_to_10(value):
    """Normalize values like 6, "6", "6/10"; never treat the denominator as the score."""
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        n = int(round(float(value)))
    else:
        text = str(value).strip().replace(',', '.')
        m = re.search(r'(?<!\d)(\d{1,2})(?:\s*/\s*10)?(?!\d)', text)
        if not m:
            return None
        n = int(m.group(1))
    return n if 1 <= n <= 10 else None

def tg_last_bot_prompt(chat_id=''):
    try:
        ensure_telegram_messages_table()
        conn = get_db()
        if chat_id:
            row = conn.execute(
                "SELECT message FROM telegram_messages WHERE direction='out' AND chat_id=? ORDER BY id DESC LIMIT 1",
                (str(chat_id),)
            ).fetchone()
        else:
            row = conn.execute("SELECT message FROM telegram_messages WHERE direction='out' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return row['message'] if row else ''
    except Exception:
        log.exception("Son Telegram bot mesaji okunamadi")
        return ''

def tg_direct_mood_actions_from_text(raw_text, chat_id=''):
    """If the bot asked how Taha feels, a bare "6/10" means mood score 6/10."""
    text = (raw_text or '').strip()
    if not re.fullmatch(r'\s*\d{1,2}\s*(?:/\s*10)?\s*', text):
        return []
    score = tg_score_1_to_10(text)
    if score is None:
        return []
    last = tg_last_bot_prompt(chat_id)
    norm = (last or '').lower()
    prompt_words = (
        'hisset', 'ruh', 'mood', 'moral', 'nasıl kalktın', 'nasil kalktin',
        'nasıl uyandın', 'nasil uyandin', 'uyandığında', 'uyandiginda'
    )
    if last and not any(w in norm for w in prompt_words):
        return []
    return [{
        'type': 'mood',
        'date': tg_effective_log_date(raw_text, 'mood') if 'tg_effective_log_date' in globals() else operation_today(),
        'mood': score,
        'energy': None,
        'stress': None,
        'notes': f'telegram {score}/10: uyandiginda hissetme puani'
    }]

def tg_normalize_mood_payload(a):
    energy = tg_score_1_to_10(a.get('energy'))
    mood = tg_score_1_to_10(a.get('mood'))
    stress = tg_score_1_to_10(a.get('stress'))
    notes = a.get('notes') or ''
    return {'energy': energy, 'mood': mood, 'stress': stress, 'notes': notes}

def expire_due_supplement_breaks():
    """Bitis tarihi gecmis aktif break'leri kapatir (lazy - break okuyan her yol once
    bunu cagirir, cron gerekmez). end_date DAHIL: break o gunun sonuna kadar surer."""
    today = operation_today()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, target_name FROM supplement_breaks WHERE active=1 AND end_date IS NOT NULL AND end_date < ?",
        (today,)
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE supplement_breaks SET active=0, ended_at=CURRENT_TIMESTAMP WHERE id=?", (r['id'],))
        conn.execute(
            "INSERT INTO vitamin_logs (date, name, amount, unit, notes, status) VALUES (?,?,?,?,?,?)",
            (today, f'▶ {r["target_name"]} — Tekrar Başlandı', '', '', 'planlı ara süresi doldu', 'on_break')
        )
    conn.commit(); conn.close()
    return len(rows)


def start_supplement_break(target_type, target_name, since_date=None, note='', end_date=None):
    """Bir stack'i veya tek bir urunu 'ara veriliyor' durumuna alir - hem Telegram
    dispatcher'i (ai_apply_actions) hem /api/supplements/breaks REST route'u bunu kullanir,
    tek yerden yonetilsin diye. Zaten aktif bir break varsa tekrar eklemez (idempotent),
    ama her cagirista goruntulenebilir bir vitamin_logs kaydi birakir.
    end_date verilirse break o tarihin sonunda otomatik biter (expire_due_supplement_breaks)."""
    today = operation_today()
    since_date = since_date or today
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM supplement_breaks WHERE target_type=? AND target_name=? AND active=1",
        (target_type, target_name)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO supplement_breaks (target_type, target_name, active, since_date, note, end_date) VALUES (?,?,1,?,?,?)",
            (target_type, target_name, since_date, note or '', end_date or None)
        )
    elif end_date:
        conn.execute("UPDATE supplement_breaks SET end_date=? WHERE id=?", (end_date, existing['id']))
    range_txt = f"{since_date} → {end_date}" if end_date else (f"{since_date} tarihinden itibaren" if since_date != today else '')
    log_note = (range_txt + ' ara veriliyor').strip()
    conn.execute(
        "INSERT INTO vitamin_logs (date, name, amount, unit, notes, status) VALUES (?,?,?,?,?,?)",
        (today, f'⏸ {target_name} — Ara Verildi', '', '', log_note, 'on_break')
    )
    conn.commit(); conn.close()

def end_supplement_break(target_type, target_name):
    """start_supplement_break'in tersi - karsi granulariteyi de kapatir (bkz. yorum icinde),
    yoksa 'post workout ara verdim' (stack) sonra 'creatine tekrar basladim' (urun) gibi bir
    cift sessizce hicbir seyi bitirmez."""
    today = operation_today()
    conn = get_db()
    conn.execute(
        "UPDATE supplement_breaks SET active=0, ended_at=? WHERE target_type=? AND target_name=? AND active=1",
        (today, target_type, target_name)
    )
    if target_type == 'product':
        slot = tg_stack_slot_for_product(target_name) if 'tg_stack_slot_for_product' in globals() else None
        stack_name = _SLOT_STACK_NAME.get(slot) if slot else None
        if stack_name:
            conn.execute(
                "UPDATE supplement_breaks SET active=0, ended_at=? WHERE target_type='stack' AND target_name=? AND active=1",
                (today, stack_name)
            )
    elif target_type == 'stack':
        slot = next((k for k, v in _SLOT_STACK_NAME.items() if v == target_name), None)
        if slot and 'tg_stack_preset' in globals():
            for pname in tg_stack_preset(slot):
                conn.execute(
                    "UPDATE supplement_breaks SET active=0, ended_at=? WHERE target_type='product' AND target_name=? AND active=1",
                    (today, pname)
                )
    conn.execute(
        "INSERT INTO vitamin_logs (date, name, amount, unit, notes, status) VALUES (?,?,?,?,?,?)",
        (today, f'▶ {target_name} — Tekrar Başlandı', '', '', 'ara bitti, devam ediliyor', 'on_break')
    )
    conn.commit(); conn.close()

def ai_apply_actions(actions):
    saved = []
    today = operation_today()
    for a in actions or []:
        typ = (a.get('type') or '').strip()
        action_date = (a.get('date') or today)
        try:
            if typ == 'meal':
                conn = get_db()
                slot = normalize_meal_slot(a.get('slot'))
                title = a.get('title') or a.get('name') or a.get('description') or slot
                if title and len(title) > 80:
                    title = title[:80]
                conn.execute("""
                    INSERT INTO meal_entries
                        (date, slot, title, description, calories, protein_g, carbs_g, fat_g, fiber_g, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    action_date, slot, title, a.get('description') or '',
                    a.get('calories'), a.get('protein_g'), a.get('carbs_g'),
                    a.get('fat_g'), a.get('fiber_g'), 'telegram-ai'
                ))
                conn.commit(); conn.close()
                saved.append('öğün')
            elif typ == 'water':
                ml = int(a.get('water_ml') or 0)
                if ml > 0:
                    conn = get_db()
                    row = conn.execute("SELECT water_ml FROM nutrition_logs WHERE date=?", (action_date,)).fetchone()
                    mode = (a.get('mode') or a.get('water_mode') or '').lower()
                    if row:
                        new_ml = ml if mode in ('set', 'total') else ((row['water_ml'] or 0) + ml)
                        conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE date=?", (new_ml, action_date))
                    else:
                        conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (action_date, ml))
                    conn.commit(); conn.close()
                    saved.append('su')
            elif typ in ('water_set', 'update_water'):
                ml = int(a.get('water_ml') or a.get('ml') or 0)
                conn = get_db()
                conn.execute("UPDATE nutrition_logs SET water_ml=0 WHERE date=?", (action_date,))
                row = conn.execute("SELECT id FROM nutrition_logs WHERE date=?", (action_date,)).fetchone()
                if row:
                    conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE id=?", (max(0, ml), row['id']))
                else:
                    conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (action_date, max(0, ml)))
                conn.commit(); conn.close()
                saved.append('su düzeltildi')
            elif typ in ('delete_water',):
                conn = get_db()
                conn.execute("UPDATE nutrition_logs SET water_ml=0 WHERE date=?", (action_date,))
                conn.commit(); conn.close()
                saved.append('su silindi')
            elif typ == 'sleep':
                db_upsert('sleep_logs', action_date, {'hours': a.get('hours'), 'quality': a.get('quality')})
                saved.append('uyku')
            elif typ == 'mood':
                db_upsert('mood_logs', action_date, tg_normalize_mood_payload(a) if 'tg_normalize_mood_payload' in globals() else {'energy': a.get('energy'), 'mood': a.get('mood'), 'stress': a.get('stress')})
                saved.append('ruh hali')
            elif typ == 'exercise':
                db_upsert('exercise_logs', action_date, {'type': a.get('exercise_type') or a.get('type') or '', 'duration': a.get('duration'), 'intensity': a.get('intensity'), 'notes': a.get('notes') or ''})
                saved.append('egzersiz')
            elif typ == 'training_exercise':
                if isinstance(a.get('set_details'), list) and a.get('set_details'):
                    details = normalize_set_details(a.get('set_details'))
                else:
                    sets = int(a.get('sets') or 1)
                    details = normalize_set_details([{'set': i + 1, 'type': a.get('set_type') or 'Working set', 'reps': str(a.get('reps') or ''), 'weight': str(a.get('weight') or '')} for i in range(sets)])
                conn = get_db()
                td = training_day(action_date)
                exercise_name = a.get('exercise') or 'Hareket'
                notes_json = json.dumps({'set_details': details}, ensure_ascii=False)
                conn.execute("INSERT INTO training_exercises (training_day, exercise, sets, reps, weight, notes) VALUES (?,?,?,?,?,?)",
                             (td, exercise_name, str(len(details)), details[0].get('reps', '') if details else '', details[0].get('weight', '') if details else '', notes_json))
                ensure_training_day_logs_table()
                conn.execute("""
                    INSERT INTO training_day_logs (date, training_day, exercise, sets_json, notes)
                    VALUES (?,?,?,?,?)
                """, (action_date, td, exercise_name, json.dumps(details, ensure_ascii=False), 'telegram-ai'))
                conn.commit(); conn.close()
                saved.append('hareket')
            elif typ == 'vitamin':
                vname = (a.get('name') or '').strip()
                vamount = str(a.get('amount') or '').strip()
                vunit = (a.get('unit') or '').strip()
                vnotes = (a.get('notes') or '').strip()
                # name bossa catalog'dan bul (AI format hatasi icin guvenlik agi)
                if not vname and 'tg_supplement_catalog' in globals():
                    notes_lower = vnotes.lower()
                    for item in tg_supplement_catalog():
                        if any(k in notes_lower for k in item['keys']):
                            vname = item['name']
                            if not vamount:
                                vamount = item['amount']
                            if not vunit:
                                vunit = item['unit']
                            break
                conn = get_db()
                conn.execute("INSERT INTO vitamin_logs (date, name, amount, unit, notes, status) VALUES (?,?,?,?,?,?)",
                             (action_date, vname, vamount, vunit, vnotes, vit_status_of('', vnotes)))
                conn.commit(); conn.close()
                saved.append('supplement')
            elif typ == 'supplement_break_start':
                ttype = (a.get('target_type') or '').strip()
                tname = (a.get('target_name') or '').strip()
                if ttype and tname:
                    start_supplement_break(ttype, tname, a.get('since_date'), a.get('note') or '')
                    saved.append('ara veriliyor')
            elif typ == 'supplement_break_end':
                ttype = (a.get('target_type') or '').strip()
                tname = (a.get('target_name') or '').strip()
                if ttype and tname:
                    end_supplement_break(ttype, tname)
                    saved.append('ara bitti')
            elif typ == 'work_shift':
                st = (a.get('start') or '').strip()
                en = (a.get('end') or '').strip()
                if _valid_shift_time(st) and _valid_shift_time(en):
                    info = apply_work_shift(st, en, a.get('label') or '')
                    saved.append(f"vardiya {st}-{en} (gün kesimi {info['cutoff_hour']:02d}:00)")
            elif typ in ('body_weight', 'weight', 'kilo'):
                ensure_body_metrics_table()
                kg = float(a.get('weight_kg') or a.get('kg') or a.get('value') or 0)
                if kg:
                    conn = get_db()
                    conn.execute("""
                        INSERT INTO body_metrics (date, weight_kg, notes)
                        VALUES (?,?,?)
                        ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, notes=excluded.notes
                    """, (action_date, kg, a.get('notes') or 'telegram-ai'))
                    conn.commit(); conn.close()
                    saved.append('kilo')
            elif typ in ('steps', 'step'):
                ensure_step_logs_table()
                steps = int(a.get('steps') or a.get('value') or 0)
                if steps:
                    conn = get_db()
                    conn.execute("INSERT OR REPLACE INTO step_logs (date, steps, notes) VALUES (?,?,?)", (action_date, steps, a.get('notes') or 'telegram-ai'))
                    conn.commit(); conn.close()
                    saved.append('adım')
            elif typ in ('skin', 'skin_log'):
                ensure_skin_tables()
                conn = get_db()
                conn.execute("INSERT INTO skin_logs (date, area, name, status, notes) VALUES (?,?,?,?,?)",
                             (action_date, a.get('area') or 'yüz', a.get('name') or a.get('item') or 'cilt rutini', a.get('status') or 'done', a.get('notes') or 'telegram-ai'))
                conn.commit(); conn.close()
                saved.append('cilt')
            elif typ in ('update_steps',):
                ensure_step_logs_table()
                steps = int(a.get('steps') or a.get('value') or 0)
                conn = get_db()
                conn.execute("INSERT OR REPLACE INTO step_logs (date, steps, notes) VALUES (?,?,?)", (action_date, max(0, steps), a.get('notes') or 'telegram-ai düzeltme'))
                conn.commit(); conn.close()
                saved.append('adım düzeltildi')
            elif typ in ('delete_steps',):
                ensure_step_logs_table()
                conn = get_db()
                conn.execute("DELETE FROM step_logs WHERE date=?", (action_date,))
                conn.commit(); conn.close()
                saved.append('adım silindi')
            elif typ in ('update_weight',):
                ensure_body_metrics_table()
                kg = float(a.get('weight_kg') or a.get('kg') or a.get('value') or 0)
                conn = get_db()
                conn.execute("""
                    INSERT INTO body_metrics (date, weight_kg, notes)
                    VALUES (?,?,?)
                    ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, notes=excluded.notes
                """, (action_date, kg, a.get('notes') or 'telegram-ai düzeltme'))
                conn.commit(); conn.close()
                saved.append('kilo düzeltildi')
            elif typ in ('delete_weight',):
                ensure_body_metrics_table()
                conn = get_db()
                conn.execute("DELETE FROM body_metrics WHERE date=?", (action_date,))
                conn.commit(); conn.close()
                saved.append('kilo silindi')
            elif typ == 'note':
                db_upsert('daily_notes', action_date, {'note': a.get('note') or ''})
                saved.append('not')
            elif typ in ('delete_note',):
                conn = get_db()
                conn.execute("DELETE FROM daily_notes WHERE date=?", (action_date,))
                conn.commit(); conn.close()
                saved.append('not silindi')
        except Exception:
            log.exception("AI action kaydedilemedi: %s", typ)
    return saved


# TG_NATURAL_WATER_CORRECTION_V1
def tg_try_water_correction(raw_text):
    """Handle natural Telegram corrections like: suyu 200 ml azalt."""
    text = (raw_text or '').strip()
    if not text:
        return None
    norm = text.lower()
    trans = str.maketrans({
        'ı': 'i', 'İ': 'i', 'ğ': 'g', 'Ğ': 'g', 'ü': 'u', 'Ü': 'u',
        'ş': 's', 'Ş': 's', 'ö': 'o', 'Ö': 'o', 'ç': 'c', 'Ç': 'c'
    })
    n = norm.translate(trans)
    water_words = ('su', 'suyu', 'water')
    correction_words = ('azalt', 'dus', 'düş', 'eksilt', 'geri al', 'yanlis', 'yanlış', 'fazla', 'sil')
    if not any(w in n for w in water_words) or not any(w in n for w in correction_words):
        return None

    m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(ml|mililitre|lt|litre|l)?', n)
    if not m:
        return None
    value = float(m.group(1).replace(',', '.'))
    unit = (m.group(2) or '').lower()
    if unit in ('lt', 'litre', 'l') or (not unit and value <= 10):
        amount_ml = int(round(value * 1000))
    else:
        amount_ml = int(round(value))
    if amount_ml <= 0:
        return None

    today = operation_today()
    conn = get_db()
    row = conn.execute("SELECT water_ml FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    current = int((row['water_ml'] if row else 0) or 0)
    new_total = max(0, current - amount_ml)
    if row:
        conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE date=?", (new_total, today))
    else:
        conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (today, new_total))
    conn.commit(); conn.close()
    return {
        'type': 'water_correction',
        'water_ml_delta': -amount_ml,
        'water_ml_total': new_total,
        'reply': f"Tamam, suyu {amount_ml} ml azalttım. Yeni toplam: {new_total/1000:.2f} L."
    }



# TG_BASIC_NO_AI_FALLBACK_V1

def _tg_norm(text):
    """Turkce karakterleri ASCII'ye katlayip kucuk harfe cevirir - 'SÜT' ve 'sut' ayni
    anahtar kelimeyle eslessin diye. (Denetim bulgusu: bu fonksiyon hic tanimlanmamisti,
    '_tg_norm in globals()' korumali cagrilarin hepsi sessizce .lower() fallback'ine
    dusuyordu ve Turkce buyuk harfli mesajlarda anahtar kelimeler kaciyordu.)"""
    t = (text or '').lower()
    for a, b in (('ı', 'i'), ('ö', 'o'), ('ü', 'u'), ('ç', 'c'), ('ş', 's'), ('ğ', 'g'), ('â', 'a'), ('İ', 'i')):
        t = t.replace(a, b)
    return t





















































































































































































































# TG_TEXT_PARSE_SAFE_V2
def tg_ascii_text(raw_text):
    text = (raw_text or '').lower()
    pairs = [
        ('ı','i'),('İ','i'),('ğ','g'),('Ğ','g'),('ü','u'),('Ü','u'),
        ('ş','s'),('Ş','s'),('ö','o'),('Ö','o'),('ç','c'),('Ç','c'),
        ('Ä±','i'),('Ä°','i'),('ÄŸ','g'),('Äž','g'),('Ã¼','u'),('Ãœ','u'),
        ('ÅŸ','s'),('Åž','s'),('Ã¶','o'),('Ã–','o'),('Ã§','c'),('Ã‡','c')
    ]
    for a, b in pairs:
        text = text.replace(a, b)
    for a, b in [
        ('kahvalt?', 'kahvalti'), ('??le', 'ogle'), ('?gle', 'ogle'),
        ('ak?am', 'aksam'), ('ad?m', 'adim'), ('ya?', 'yag'),
        ('?inko', 'cinko'), ('g?z', 'goz'), ('?ilek', 'cilek'),
        ('yar?m', 'yarim'), ('g?n', 'gun'),
    ]:
        text = text.replace(a, b)
    return text

def tg_basic_actions_from_text(raw_text):
    """AI cevap veremediginde bile kritik kayitlarin (kilo/su/adim) kacmamasi icin fallback.
    2026-07-16: bir kod temizligi sirasinda bu fonksiyonun daha once var olan, daha esnek bir
    kopyasi (ml cinsinden su, gevsek adim eslesmesi) yanlislikla dead-code sayilip silinmisti -
    o iki iyilestirme burada geri eklendi. Ogun-makro cikarma dalini KASITLI olarak geri
    eklemedim: tg_full_day_actions_from_text ayni tetikleyici kelimelerle (kahvalti/ogle/aksam)
    zaten daha yetenekli bir versiyonunu yapiyor VE cagiran kod (cmd_chat_ai) full_day_actions
    doluyken basic_actions'un meal girdilerini hic kullanmiyor (if/elif) - geri eklemek olu
    kod olurdu."""
    actions = []
    norm = tg_ascii_text(raw_text)
    today = operation_today()
    kg = re.search(r'(?:kilo|weight|kg)\s*[:\-]?\s*(\d{2,3}(?:[\.,]\d+)?)', norm)
    if kg:
        actions.append({'type': 'weight', 'date': today, 'weight_kg': float(kg.group(1).replace(',', '.')), 'notes': 'telegram-basic'})
    water = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:l|lt|litre)\s*su', norm)
    if not water and ('su' in norm or 'water' in norm):
        water = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:l|lt|litre)\b', norm)
    if water:
        actions.append({'type': 'water', 'date': today, 'water_ml': int(round(float(water.group(1).replace(',', '.')) * 1000))})
    elif 'su' in norm or 'water' in norm:
        ml_m = re.search(r'(\d{3,5})\s*ml', norm)
        if ml_m:
            actions.append({'type': 'water', 'date': today, 'water_ml': int(ml_m.group(1))})
    steps = re.search(r'(\d{4,6})\s*(?:adim|ad\?m)', norm)
    if steps:
        actions.append({'type': 'steps', 'date': today, 'steps': int(steps.group(1)), 'notes': 'telegram-basic'})
    elif 'adim' in norm or 'step' in norm:
        step_nums = [int(x) for x in re.findall(r'\b\d{3,6}\b', norm)]
        if step_nums:
            actions.append({'type': 'steps', 'date': today, 'steps': max(step_nums), 'notes': 'telegram-basic'})
    return actions

# TG_SMART_COACH_V4
def tg_meal_count(actions):
    return len([a for a in actions if isinstance(a, dict) and a.get('type') == 'meal'])

def tg_is_weak_ai_reply(reply):
    r = (reply or '').lower()
    return any(x in r for x in [
        'bağlantı sorunu', 'baglanti sorunu', 'tekrar dener misin',
        'detaylarını biraz daha aç', 'detaylarini biraz daha ac',
        'tam hesaplayabilmem', 'eksik', 'claude hatası', 'openai hatası'
    ])

def tg_smart_daily_reply(actions, original_reply=''):
    meals = [a for a in actions if isinstance(a, dict) and a.get('type') == 'meal']
    if not meals:
        return ''
    cal = sum(float(a.get('calories') or 0) for a in meals)
    p = sum(float(a.get('protein_g') or 0) for a in meals)
    c = sum(float(a.get('carbs_g') or 0) for a in meals)
    f = sum(float(a.get('fat_g') or 0) for a in meals)
    water = next((a for a in actions if isinstance(a, dict) and a.get('type') == 'water'), None)
    steps = next((a for a in actions if isinstance(a, dict) and a.get('type') in ('steps', 'step')), None)
    weight = next((a for a in actions if isinstance(a, dict) and a.get('type') in ('weight', 'body_weight', 'kilo')), None)
    density = round(p / max(cal, 1) * 1000, 1) if cal else 0
    lines = [
        'Taha, bunu gunluk rapor gibi isledim.',
        '',
        f'Toplam: ~{int(round(cal))} kcal | P {round(p,1)}g | K {round(c,1)}g | Y {round(f,1)}g',
        f'Protein yogunlugu: {density}g / 1000 kcal.',
        '',
        'Ogun kirilimi:'
    ]
    for m in meals:
        lines.append(f"- {m.get('slot')}: {m.get('calories')} kcal | P {m.get('protein_g')}g | K {m.get('carbs_g')}g | Y {m.get('fat_g')}g")
    extras = []
    if weight: extras.append(f"kilo {weight.get('weight_kg')}kg")
    if water: extras.append(f"su {round((water.get('water_ml') or 0)/1000,2)}L")
    if steps: extras.append(f"adim {steps.get('steps')}")
    if extras:
        lines += ['', 'Takip: ' + ' | '.join(extras)]
    coach = []
    if p >= 180:
        coach.append('Protein guclu; kas koruma tarafi iyi.')
    elif p >= 140:
        coach.append('Protein iyi ama son ogunde biraz daha yagsiz kaynakla yukari cekilebilir.')
    else:
        coach.append('Protein dusuk kalmis; yarin ilk revize protein olacak.')
    if c < 120 and steps and int(steps.get('steps') or 0) >= 8000:
        coach.append('Adim yuksek, karb dusuk; antrenman gunu pre/post karbi artiririz.')
    if f > 80:
        coach.append('Yag yuksek olabilir; yumurta sarisi ve yag spreyi sayisini izleyelim.')
    if water and (water.get('water_ml') or 0) >= 3500:
        coach.append('Su iyi; 4L civarinda elektrolit/sodyum da takip edelim.')
    if steps and int(steps.get('steps') or 0) >= 10000:
        coach.append('10k+ adim cok iyi, kesim surecinde avantaj.')
    lines += ['', 'Koc yorumu:'] + [f"- {x}" for x in coach[:4]]
    lines += ['', 'Kayitlari sisteme isledim. Yarin kilo tepkisi + bugunku makrolara gore revize edecegiz.']
    return '\n'.join(lines)




# TG_TEMPLATE_SYNC_V1
def tg_template_norm(raw_text):
    if 'tg_ascii_text' in globals():
        return tg_ascii_text(raw_text)
    text = (raw_text or '').lower()
    for a, b in [('ı','i'),('İ','i'),('ğ','g'),('ü','u'),('ş','s'),('ö','o'),('ç','c')]:
        text = text.replace(a, b)
    return text

def tg_should_save_template(raw_text):
    norm = tg_template_norm(raw_text)
    return any(w in norm for w in [
        'sablon', 'sabit', 'fiks', 'fix', 'favori', 'hep kullan', 'stackle',
        'stack olarak kaydet', 'stacke kaydet', 'stacklere kaydet',
        'ogunlere kaydet', 'ogun olarak kaydet', 'yemeklere kaydet', 'yemek olarak kaydet',
        'supplementlere kaydet', 'supplement olarak kaydet', 'suplementlere kaydet', 'suplemente kaydet',
        'takviyelere kaydet', 'takviye olarak kaydet'
    ])

def tg_template_target_kind(raw_text):
    norm = tg_template_norm(raw_text)
    if any(w in norm for w in ['stackle', 'stack olarak kaydet', 'stacke kaydet', 'stacklere kaydet']):
        return 'supplement'
    if any(w in norm for w in ['supplement', 'suplement', 'takviye', 'vitamin']):
        return 'supplement'
    if any(w in norm for w in ['ogun', 'yemek', 'kahvalti', 'ogle', 'aksam']):
        return 'meal'
    return ''

def tg_template_name_from_text(raw_text):
    text = (raw_text or '').strip()
    if not text:
        return ''
    for pat in [
        r'ad[ıi]\s+(.+?)\s+olsun',
        r'ismi\s+(.+?)\s+olsun',
        r'isimi\s+(.+?)\s+olsun',
        r'bunun\s+ad[ıi]\s+(.+?)\s+olsun',
        r'(.{2,60}?)\s+olarak\s+kaydet',
    ]:
        m = re.search(pat, text, flags=re.I)
        if m:
            name = re.sub(r'\s+', ' ', m.group(1)).strip(" .,!?:;\"'")
            if name and len(name) <= 80:
                return name[:70]
    return ''

def tg_meal_category_from_text(raw_text, slot=''):
    norm = tg_template_norm(raw_text)
    if 'kahvalti' in norm or 'sabah' in norm:
        return 'kahvaltı'
    if 'ogle' in norm:
        return 'öğle'
    if 'aksam' in norm:
        return 'akşam'
    if 'pre' in norm:
        return 'pre-antrenman'
    if 'post' in norm:
        return 'post-antrenman'
    return slot or 'extra'

def tg_should_stack_template(raw_text):
    norm = tg_template_norm(raw_text)
    return any(w in norm for w in ['stackle', 'stack olarak kaydet', 'stacke kaydet', 'stacklere kaydet'])

def tg_supp_category_from_text(raw_text):
    norm = tg_template_norm(raw_text)
    if any(w in norm for w in ['uyku', 'melatonin', 'glycine', 'glisin', 'magnesium', 'magnezyum']):
        return 'uyku öncesi'
    if any(w in norm for w in ['pre', 'citrulline', 'kreatin', 'creatine', 'beta']):
        return 'pre-workout'
    if any(w in norm for w in ['cilt', 'skin', 'nac', 'zinc', 'cinko']):
        return 'cilt'
    if any(w in norm for w in ['omega', 'epa', 'dha']):
        return 'omega'
    if any(w in norm for w in ['protein', 'whey']):
        return 'protein'
    return 'supplement'

def tg_upsert_quick_template(kind, category, title, description='', calories=None, protein_g=None, carbs_g=None, fat_g=None, fiber_g=None, amount='', unit='', notes='telegram-ai'):
    title = (title or '').strip()[:90]
    if not title:
        return ''
    conn = get_db()
    existing = conn.execute("SELECT id FROM quick_templates WHERE kind=? AND lower(title)=lower(?)", (kind, title)).fetchone()
    payload = (category, title, description or '', calories, protein_g, carbs_g, fat_g, fiber_g, amount or '', unit or '', notes or '')
    if existing:
        conn.execute("""
            UPDATE quick_templates
            SET category=?, title=?, description=?, calories=?, protein_g=?, carbs_g=?, fat_g=?, fiber_g=?, amount=?, unit=?, notes=?
            WHERE id=?
        """, payload + (existing['id'],))
    else:
        conn.execute("""
            INSERT INTO quick_templates
                (kind, category, title, description, calories, protein_g, carbs_g, fat_g, fiber_g, amount, unit, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (kind,) + payload)
    conn.commit(); conn.close()
    return title

def tg_save_meal_template_from_actions(raw_text, actions):
    if not tg_should_save_template(raw_text):
        return ''
    target = tg_template_target_kind(raw_text)
    titles = []
    name_hint = tg_template_name_from_text(raw_text)
    meals = [a for a in (actions or []) if isinstance(a, dict) and (a.get('type') or '').strip() == 'meal']
    vitamins = [a for a in (actions or []) if isinstance(a, dict) and (a.get('type') or '').strip() in ('vitamin', 'supplement')]

    if target in ('', 'meal') and meals:
        for meal in meals:
            slot = meal.get('slot') or 'extra'
            title = name_hint if len(meals) == 1 and name_hint else (meal.get('title') or meal.get('name') or f"{slot} sabit ogun")
            saved = tg_upsert_quick_template(
                'meal', tg_meal_category_from_text(raw_text, slot), title,
                meal.get('description') or title, meal.get('calories'), meal.get('protein_g'),
                meal.get('carbs_g'), meal.get('fat_g'), meal.get('fiber_g'), '', '', 'telegram meal template'
            )
            if saved:
                titles.append(saved)

    if target in ('', 'supplement') and vitamins:
        if tg_should_stack_template(raw_text) and len(vitamins) >= 2:
            stack_slot = ''
            for vit in vitamins:
                if isinstance(vit, dict) and vit.get('stack'):
                    stack_slot = vit.get('stack') or ''
                    break
            category = stack_slot or tg_supp_category_from_text(raw_text)
            title = name_hint or (tg_stack_label(category) if 'tg_stack_label' in globals() else 'Supplement Stack')
            lines = []
            for vit in vitamins:
                name = vit.get('name') or vit.get('title') or 'Supplement'
                amount = str(vit.get('amount') or '').strip()
                unit = str(vit.get('unit') or '').strip()
                note = str(vit.get('notes') or '').strip()
                lines.append(' | '.join([x for x in [name, (amount + ' ' + unit).strip(), note] if x]))
            saved = tg_upsert_quick_template(
                'supplement', category, title, '\n'.join(lines), None, None, None, None, None,
                str(len(vitamins)), 'stack', 'telegram supplement stack template'
            )
            if saved:
                titles.append(saved)
            return ', '.join(dict.fromkeys(titles))
        for vit in vitamins:
            title = name_hint if len(vitamins) == 1 and name_hint else (vit.get('title') or vit.get('name') or 'Supplement')
            saved = tg_upsert_quick_template(
                'supplement', tg_supp_category_from_text(raw_text), title,
                vit.get('description') or vit.get('notes') or title, vit.get('calories'), vit.get('protein_g'),
                vit.get('carbs_g'), vit.get('fat_g'), vit.get('fiber_g'), str(vit.get('amount') or ''),
                vit.get('unit') or '', vit.get('notes') or 'telegram supplement template'
            )
            if saved:
                titles.append(saved)

    return ', '.join(dict.fromkeys(titles))



# TG_ALWAYS_ON_HEARTBEAT_AND_SMART_PARSE_V1
TELEGRAM_HEARTBEAT_PATH = os.path.join(BASE_DIR, 'telegram_heartbeat.json')

def tg_touch_heartbeat(status='running', message=''):
    try:
        payload = {
            'status': status,
            'ts': now_istanbul().isoformat(timespec='seconds'),
            'pid': os.getpid(),
            'message': (message or '')[:180]
        }
        with open(TELEGRAM_HEARTBEAT_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass

def tg_effective_log_date(raw_text='', action_type=''):
    """Telegram kayitlarini OPERASYON gunu kavramiyla tarihler (WHOOP bucketing, web UI ve
    Claude promptundaki tarih kuraliyla birebir ayni taban). 00:00-cutoff arasi operation_date()
    zaten onceki takvim gunu oldugu icin eski late_types/gun-sonu-kelime ozel dallari gereksizlesti:
    o pencerede TUM kayit tipleri (antrenman dahil) ayni operasyon gunune gider - eskiden
    'exercise' takvim gunune, ayni mesajdaki ogun onceki gune yazilip bolunebiliyordu."""
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    op_today = operation_date()
    # kelime siniri sart: 'koydun', 'uyudun' gibi -dun ekli fiiller tetiklememeli
    if re.search(r'\bdun(ku)?\b', norm):
        return (op_today - timedelta(days=1)).isoformat()
    return op_today.isoformat()

def tg_night_casual_reply(raw_text=''):
    """00:00-06:00 arasinda basit sohbeti sabah gibi karsilama."""
    now = now_istanbul()
    if not (0 <= now.hour < operation_cutoff_hour(now)):
        return ''
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    casual_words = ['naber', 'selam', 'merhaba', 'hey', 'kontrol', 'calisiyor', 'çalisiyor', 'çalışıyor']
    record_words = [
        'kilo', 'kg', 'su', 'adim', 'adım', 'uyudum', 'uyandim', 'uyandım', 'kalktim', 'kalktım',
        'nac', 'probiyotik', 'omega', 'vitamin', 'stack', 'takviye', 'kahvalti', 'kahvaltı',
        'ogle', 'ögle', 'öğle', 'aksam', 'akşam', 'tavuk', 'yulaf', 'yumurta', 'antrenman'
    ]
    if any(w in norm for w in casual_words) and not any(w in norm for w in record_words):
        return (
            f"Saat gece {now.strftime('%H:%M')}; buradayim Taha. Gece modundayiz, sabah gibi davranmayacagim. "
            "Yatmadan once uyku, gece stack, su veya not kaydi varsa yaz; yoksa sadece sohbet edebiliriz."
        )
    return ''

def tg_is_future_bedtime_statement(raw_text):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    future_words = ['uyuyaca', 'uyuyacagim', 'uyuyacağım', 'uyucam', 'uyicam', 'yataca', 'yatacagim', 'yatacağım', 'yatcam']
    slept_words = ['uyudum', 'uyumusum', 'uyumuşum', 'kalktim', 'kalktım', 'uyandim', 'uyandım']
    return any(w in norm for w in future_words) and not any(w in norm for w in slept_words)

def tg_weight_context_note(raw_text, action_date):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    now = now_istanbul()
    cutoff = operation_cutoff_hour(now) if 'operation_cutoff_hour' in globals() else globals().get('OPERATION_DAY_CUTOFF_HOUR', 6)
    if any(w in norm for w in ['ac karna', 'ackarna', 'sabah tarti', 'sabah kilo', 'sabah ac']):
        return 'telegram | sabah ac karna resmi tarti'
    if now.hour < cutoff or any(w in norm for w in ['gece', 'eve geldim', 'uyuyacagim', 'uyuyaca??m', 'yatacagim', 'yataca??m', 'uyumadan']):
        return 'telegram | gece kapanis / uyku oncesi tarti'
    return 'telegram | kilo kaydi'

def tg_dedupe_tracking_actions(raw_text, actions):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    out = []
    water_kept = None
    water_total_mode = any(w in norm for w in ['toplam', 'toplamda', 'bugun toplam', 'bug?n toplam', 'gun totali', 'gun toplam']) or ('litre' in norm and 'ekle' not in norm)
    seen = set()
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        typ = (a.get('type') or '').strip()
        if typ == 'water':
            item = dict(a)
            if water_total_mode:
                item['mode'] = 'set'
            if water_kept is None or item.get('mode') in ('set', 'total'):
                water_kept = item
            continue
        key = (typ, a.get('date'), a.get('slot') or '', a.get('name') or '', a.get('weight_kg') or a.get('kg') or a.get('value') or '')
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    if water_kept:
        out.append(water_kept)
    return out

def tg_normalize_action_dates_and_sleep(raw_text, actions):
    fixed = []
    today = operation_today()
    bedtime_future = tg_is_future_bedtime_statement(raw_text)
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        typ = (a.get('type') or '').strip()
        if typ == 'sleep' and bedtime_future:
            continue
        if not a.get('date') or a.get('date') == today:
            a = dict(a)
            a['date'] = tg_effective_log_date(raw_text, typ)
        if typ in ('body_weight', 'weight', 'kilo'):
            a = dict(a)
            a['notes'] = tg_weight_context_note(raw_text, a.get('date') or tg_effective_log_date(raw_text, typ)) if 'tg_weight_context_note' in globals() else (a.get('notes') or 'telegram')
        fixed.append(a)
    return fixed

def tg_line_slot(line, current_slot='extra'):
    n = tg_ascii_text(line) if 'tg_ascii_text' in globals() else (line or '').lower()
    if 'kahvalti' in n or 'sabah' in n:
        return 'kahvalti'
    if 'ogle' in n or 'lunch' in n:
        return 'ogle'
    if 'aksam' in n or 'dinner' in n:
        return 'aksam'
    if 'pre' in n:
        return 'pre-workout'
    if 'post' in n:
        return 'post-workout'
    return current_slot or 'extra'

def tg_food_registry_match(norm_text):
    """Besin DB'de norm_text icinde gecen bir urun adi/alias var mi kontrol eder -
    markasiz/jenerik gecen kayitli urunlerin gercek etiket verisini kullanmak icin
    (AI yolundaki MARKASIZ/JENERIK kuralinin deterministik/regex tarafi karsiligi).
    NOT: farkli/markali bir urunun gercek verisini internetten bulmak bu fonksiyonun
    kapsaminda degil - o sadece AI (_claude_call) tarafinda mumkun, cunku gercek
    dunya bilgisi gerektiriyor. Burada amac sadece KAYITLI urunler dogru taninsin."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT name, aliases, calories_per_100, protein_per_100, carbs_per_100, fat_per_100 FROM food_registry"
        ).fetchall()
        conn.close()
    except Exception:
        return None
    best = None
    for r in rows:
        candidates = [r['name']] + [a.strip() for a in (r['aliases'] or '').split(',') if a.strip()]
        for cand in candidates:
            if not cand:
                continue
            cand_norm = tg_ascii_text(cand) if 'tg_ascii_text' in globals() else cand.lower()
            if cand_norm and len(cand_norm) >= 3 and cand_norm in norm_text:
                if best is None or len(cand_norm) > len(best[0]):
                    best = (cand_norm, r)
    if not best:
        return None
    r = best[1]
    return {'cal': r['calories_per_100'] or 0, 'p': r['protein_per_100'] or 0, 'c': r['carbs_per_100'] or 0, 'f': r['fat_per_100'] or 0}

def tg_food_estimate(line):
    n = tg_ascii_text(line) if 'tg_ascii_text' in globals() else (line or '').lower()
    raw_line = line or ''
    out = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
    explicit = re.search(r'(\d+(?:[\.,]\d+)?)\s*kcal.*?(\d+(?:[\.,]\d+)?)\s*g.*?(\d+(?:[\.,]\d+)?)\s*g.*?(\d+(?:[\.,]\d+)?)\s*g', n)
    if explicit:
        return {
            'cal': float(explicit.group(1).replace(',', '.')),
            'p': float(explicit.group(2).replace(',', '.')),
            'c': float(explicit.group(3).replace(',', '.')),
            'f': float(explicit.group(4).replace(',', '.')),
        }

    def add(cal=0, p=0, c=0, f=0):
        out['cal'] += cal; out['p'] += p; out['c'] += c; out['f'] += f

    gm = re.search(r'(\d{1,4})\s*(?:g|gr|gram)\b', n)
    if not gm and any(food in n for food in ['tavuk', 'yulaf', 'cilek', 'kayisi', 'kakao', 'pirinc', 'patates', 'salatalik', 'tost', 'ekmek']):
        gm = re.search(r'(\d{2,4})\s*(?=tavuk|yulaf|cilek|kayisi|kakao|pirinc|patates|salatalik|tost|ekmek)', n)
    grams = float(gm.group(1)) if gm else 0.0

    mlm = re.search(r'(\d{1,4})\s*(?:ml|mililitre)\b', n)
    ml = float(mlm.group(1)) if mlm else 0.0

    qty = 0.0
    qm = re.search(r'(^|\s)(\d+)\s*(?:tam\s*)?(?:adet\s*)?(yumurta|kayisi|fis|basis|bas[ıi]s)', n)
    if qm:
        qty = float(qm.group(2))

    db_match = None
    if grams:
        db_match = tg_food_registry_match(n)
        if db_match:
            factor = grams / 100.0
            add(db_match['cal'] * factor, db_match['p'] * factor, db_match['c'] * factor, db_match['f'] * factor)
        elif 'marine' in n and 'tavuk' in n:
            factor = grams / 300.0
            add(390 * factor, 68 * factor, 3 * factor, 10 * factor)
        elif 'tavuk' in n:
            add(grams * 1.20, grams * 0.23, 0, grams * 0.02)
        elif 'pirinc' in n or 'pirinç' in raw_line.lower():
            add(grams * 3.60, grams * 0.07, grams * 0.79, grams * 0.006)
        elif 'patates' in n:
            add(grams * 0.77, grams * 0.02, grams * 0.17, grams * 0.001)
        elif 'tost' in n or 'ekmek' in n:
            add(grams * 2.52, grams * 0.095, grams * 0.45, grams * 0.021)
        elif 'yulaf' in n:
            add(grams * 3.89, grams * 0.169, grams * 0.663, grams * 0.069)
        elif 'cilek' in n:
            add(grams * 0.32, grams * 0.007, grams * 0.077, grams * 0.003)
        elif 'salatalik' in n or 'salatalık' in raw_line.lower():
            add(grams * 0.15, grams * 0.007, grams * 0.036, grams * 0.001)
        elif 'yumurta beyazi' in n or 'likit yumurta' in n or 'sivi yumurta' in n or 'sıvı yumurta' in raw_line.lower():
            add(grams * 0.58, grams * 0.103, grams * 0.012, grams * 0.008)
        elif 'badem sutu' in n or 'badem sütü' in raw_line.lower():
            add(grams * 0.14, grams * 0.005, 0, grams * 0.011)
        elif 'kayisi' in n or 'kayısı' in raw_line.lower():
            add(grams * 2.41, grams * 0.034, grams * 0.63, grams * 0.005)
        elif 'kakao' in n:
            add(grams * 2.28, grams * 0.20, grams * 0.58, grams * 0.14)

    if ml and ('badem sutu' in n or 'badem sütü' in raw_line.lower()):
        add(ml * 0.14, ml * 0.005, 0, ml * 0.011)

    if qty and 'yumurta' in n and not any(x in n for x in ['beyaz', 'likit', 'sivi']):
        add(qty * 70, qty * 6, qty * 0.5, qty * 5)
    if qty and ('kayisi' in n or 'kayısı' in raw_line.lower()) and not grams:
        add(qty * 8, qty * 0.1, qty * 2.0, 0)

    if 'yarim kase mercimek' in n or ('yarim kase' in n and 'mercimek' in n):
        add(115, 9, 20, 0.5)
    if 'bol salata' in n or ('salata' in n and out['cal'] == 0):
        add(45, 2, 8, 0.5)
    if not db_match and ('keto ketcap' in n or 'keto ketchup' in n):
        km = re.search(r'(\d{1,3})\s*(?:g|gr|gram)', n)
        if km and float(km.group(1)) > 30:
            kgrams = float(km.group(1))
            add(kgrams * 0.41, kgrams * 0.02, kgrams * 0.062, kgrams * 0.005)
    if not db_match and 'gymbeam' in n and any(w in n for w in ['fis', 'basis', 'basıs', 'spray']):
        fm = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:fis|basis|basıs|spray)', n)
        if fm:
            sprays = float(fm.group(1).replace(',', '.'))
            add(sprays * 15, 0, 0, sprays * 1.65)
    return out

def tg_full_day_actions_from_text(raw_text):
    text = raw_text or ''
    norm = tg_ascii_text(text) if 'tg_ascii_text' in globals() else text.lower()
    if not any(w in norm for w in ['kahvalti', 'ogle', 'aksam']):
        return []
    today = tg_effective_log_date(text, 'meal') if 'tg_effective_log_date' in globals() else operation_today()
    actions = []
    buckets = {'kahvalti': [], 'ogle': [], 'aksam': []}
    slot = ''
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        n = tg_ascii_text(line) if 'tg_ascii_text' in globals() else line.lower()
        if any(w in n for w in ['gun totali', 'toplam']):
            continue
        new_slot = tg_line_slot(line, slot)
        if new_slot in buckets and (n in ['kahvalti', 'ogle', 'aksam'] or n.startswith(('kahvalti', 'ogle', 'aksam'))):
            slot = new_slot
            rest = re.sub(r'^(kahvalti|ogle|aksam)\s*[:\-]?\s*', '', n).strip()
            if not rest:
                continue
        elif new_slot in buckets and new_slot != slot and any(x in n for x in ['kahvalti', 'ogle', 'aksam']):
            slot = new_slot
        if slot in buckets:
            buckets[slot].append(line)

    total = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
    for slot_name, lines in buckets.items():
        if not lines:
            continue
        sub = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
        for line in lines:
            e = tg_food_estimate(line)
            sub['cal'] += e['cal']; sub['p'] += e['p']; sub['c'] += e['c']; sub['f'] += e['f']
        if any(sub.values()):
            total['cal'] += sub['cal']; total['p'] += sub['p']; total['c'] += sub['c']; total['f'] += sub['f']
            actions.append({
                'type': 'meal', 'date': today, 'slot': slot_name,
                'title': slot_name.title(),
                'description': '\n'.join(lines)[:900],
                'calories': int(round(sub['cal'])),
                'protein_g': round(sub['p'], 1),
                'carbs_g': round(sub['c'], 1),
                'fat_g': round(sub['f'], 1),
            })

    kg = re.search(r'(?:kilo|kg)\s*[:\-]?\s*(\d{2,3}(?:[\.,]\d+)?)', norm)
    if kg:
        actions.append({'type': 'weight', 'date': today, 'weight_kg': float(kg.group(1).replace(',', '.')), 'notes': 'telegram full day'})
    water = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:l|lt|litre)\s*su', norm)
    if water:
        actions.append({'type': 'water', 'date': today, 'water_ml': int(round(float(water.group(1).replace(',', '.')) * 1000)), 'mode': 'set'})
    steps = re.search(r'(\d{4,6})\s*(?:adim|ad\?m)', norm)
    if steps:
        actions.append({'type': 'steps', 'date': today, 'steps': int(steps.group(1)), 'notes': 'telegram full day'})
    if total['cal']:
        actions.append({'type': 'note', 'date': today, 'note': f"Telegram tam gun ozeti: ~{int(round(total['cal']))} kcal | P {round(total['p'],1)}g | K {round(total['c'],1)}g | Y {round(total['f'],1)}g"})
    return actions

def tg_supplement_stack_slot(raw_text):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    if any(w in norm for w in ['ac karna', 'ackarna', 'fasted', 'sabah ac']):
        return 'ac-karna'
    if any(w in norm for w in ['gece', 'uyku', 'yatmadan']):
        return 'gece'
    if any(w in norm for w in ['pre workout', 'pre-workout', 'preworkout', 'idman oncesi']):
        return 'pre-workout'
    if any(w in norm for w in ['post workout', 'post-workout', 'postworkout', 'idman sonrasi']):
        return 'post-workout'
    if any(w in norm for w in ['ogle', 'oglen', '?gle', '??le']):
        return 'ogle'
    if any(w in norm for w in ['sabah', 'kahvalti', 'kahvalt?']):
        return 'sabah'
    return ''

# Kullanicinin dogal dilde yazdigi kisa kelimeler -> kanonik urun adi. SADECE eslesme
# anahtarlari burada; uyelik/doz/stack bilgisi CANONICAL_SUPPLEMENT_STACKS'tan turetilir
# (eskiden ikisi ayri elle tutuluyordu ve desenkron olmustu: eski Omega-3 adi, eski
# Ac Karna/Gece dizilimi Telegram yolunda kalmisti).
TG_SUPPLEMENT_KEYS = {
    'NOW NAC 600mg': ['nac'],
    "Garden of Life Dr. Formulated Probiotics Once Daily Men's": ['probiyotik', 'probiotic'],
    'Weider Ashwagandha Professional': ['ashwagandha', 'ksm', 'ksm-66', 'weider'],
    'Optimum Nutrition Collagen Peptides (Unflavoured)': ['collagen', 'kolajen'],
    'Thorne Vitamin D + K2': ['d3', 'k2', 'd+k', 'd vitamini'],
    'Life Extension Mega EPA/DHA (Omega-3)': ['omega', 'epa', 'dha'],
    'NOW Magtein Magnesium L-Threonate': ['magtein', 'threonate', 'l-threonate'],
    'Life Extension MacuGuard with Saffron': ['goz', 'macuguard', 'saffron'],
    'Life Extension BioActive Complete B-Complex': ['b-complex', 'b complex', 'bcomplex'],
    'California Gold Nutrition Gold C 1000mg': ['vitamin c', 'c vitamini', 'gold c'],
    'NOW Zinc Picolinate 50mg': ['cinko', 'zinc'],
    'NOW Extra Strength Astaxanthin 10mg': ['astaxanthin', 'astaksantin'],
    'NOW L-Theanine Double Strength': ['theanine', 'l-theanine', 'l theanine'],
    'NOW Magnesium Glycinate': ['magnesium glycinate', 'magnezyum glisinat', 'glycinate'],
    'NOW Glycine 1000mg': ['glycine', 'glisin'],
    'NOW Melatonin 1mg': ['melatonin'],
    "Doctor's Best L-Citrulline Powder": ['citrulline', 'sitrulin', 'l-citrulline'],
    'KFD Premium Beta-Alanine': ['beta alanine', 'beta-alanine', 'alanin'],
    'Optimum Nutrition Electrolyte Powder (Lemon)': ['electrolyte', 'elektrolit', 'hydration'],
    'Swedish Supplements Taurine': ['taurine', 'taurin'],
    'California Gold Nutrition SPORT Creatine Monohydrate': ['creatine', 'kreatin'],
}

def tg_supplement_catalog():
    """Isim alani (name) supplement_stack_items.product_name / vitamin_logs.name ile BIREBIR
    ayni olmali - Dashboard/Coach/Takvim'deki 'alindi mi' eslesmesi tam string eslesmesine
    dayaniyor. Tek kaynak: CANONICAL_SUPPLEMENT_STACKS (elle liste tutulmaz, desenkron olamaz)."""
    items, by_name = [], {}
    for stack_name, stack_items in CANONICAL_SUPPLEMENT_STACKS:
        for name, dose, unit in stack_items:
            if name in by_name:
                # ayni urun ikinci stack'te de var (NAC, L-Theanine): notu genislet
                if stack_name not in by_name[name]['note']:
                    by_name[name]['note'] += f' + {stack_name}'
                continue
            it = {'keys': TG_SUPPLEMENT_KEYS.get(name, []), 'name': name,
                  'amount': str(dose), 'unit': unit, 'note': stack_name}
            if name == 'NOW Zinc Picolinate 50mg':
                it['note'] += ' | pzt-car-cum'
            by_name[name] = it
            items.append(it)
    return items

def tg_stack_preset(slot):
    """Slot adini kanonik stack'in guncel urun listesine cevirir (tek kaynak: kanonik katalog)."""
    canon = {s: [n for (n, _d, _u) in its] for s, its in CANONICAL_SUPPLEMENT_STACKS}
    slot_to_stack = {'ac-karna': 'Aç Karna', 'sabah': 'Sabah/Kahvaltı', 'kahvalti': 'Sabah/Kahvaltı',
                     'gece': 'Gece', 'pre-workout': 'Pre-workout', 'post-workout': 'Post-workout'}
    return canon.get(slot_to_stack.get(slot, ''), [])

def tg_stack_label(slot):
    return {
        'ac-karna':'Ac karna stack',
        'sabah':'Sabah stack',
        'kahvalti':'Kahvalti stack',
        'ogle':'Ogle stack',
        'gece':'Gece stack',
        'pre-workout':'Pre-workout stack',
        'post-workout':'Post-workout stack',
    }.get(slot, 'Supplement stack')

def tg_supplement_item_missing(item, norm):
    for key in [item['name'].lower()] + item['keys']:
        k = tg_ascii_text(key) if 'tg_ascii_text' in globals() else key
        if re.search(re.escape(k) + r'.{0,28}(eksik|icmedim|almadim|alma|haric|hari?|yok)', norm):
            return True
        if re.search(r'(eksik|icmedim|almadim|haric|hari?|yok).{0,28}' + re.escape(k), norm):
            return True
    return False

def tg_supplement_excluded_items(matched_items, norm):
    """Coklu urunlu tek cumlede 'eksik/almadim/haric' gibi kelimeleri EN YAKIN urun mentionuyla
    eslestirir. tg_supplement_item_missing'in sabit 28-karakter penceresi 'taurin 3g elektrolit
    eksik' gibi cumlelerde 'eksik'i yanlislikla taurin'e de bagliyordu (elektrolit'e 1 karakter,
    taurin'e ~21 karakter mesafede olmasina ragmen ikisini de disliyordu) - bu fonksiyon sadece
    gercekten EN YAKIN olan urunu haric tutar."""
    exclude_words = ['eksik', 'icmedim', 'almadim', 'haric', 'hari', 'yok']
    item_spans = []
    for item in matched_items:
        for key in item['keys']:
            key_norm = tg_ascii_text(key) if 'tg_ascii_text' in globals() else key
            for m in re.finditer(re.escape(key_norm), norm):
                item_spans.append((m.start(), m.end(), item['name']))
    if not item_spans:
        return set()
    excluded = set()
    for w in exclude_words:
        for m in re.finditer(re.escape(w), norm):
            wstart, wend = m.start(), m.end()
            best_name, best_dist = None, None
            for spos, epos, name in item_spans:
                dist = (wstart - epos) if wstart >= epos else (spos - wend)
                dist = abs(dist)
                if dist > 32:
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist, best_name = dist, name
            if best_name is not None:
                excluded.add(best_name)
    return excluded

def tg_zinc_due_for_date(today):
    """Cinko yalnizca Pzt/Car/Cum beklenir (kullanici programi, 2026-07-18)."""
    try:
        return date.fromisoformat(today).weekday() in ZINC_DAYS
    except Exception:
        return True

def tg_supplement_actions_from_text(raw_text):
    text = raw_text or ''
    norm = tg_ascii_text(text) if 'tg_ascii_text' in globals() else text.lower()
    catalog = tg_supplement_catalog()
    trigger_words = ['stack', 'takviye', 'vitamin', 'supplement', 'suplement', 'aldim', 'alindi', 'ictim', 'icti']
    if not any(w in norm for w in trigger_words) and not any(k in norm for item in catalog for k in item['keys']):
        return []
    slot = tg_supplement_stack_slot(text)
    today = tg_effective_log_date(text, 'vitamin') if 'tg_effective_log_date' in globals() else operation_today()
    matched = []
    if slot and any(w in norm for w in trigger_words):
        wanted = set(tg_stack_preset(slot))
        matched = [item for item in catalog if item['name'] in wanted]
    for item in catalog:
        if any(k in norm for k in item['keys']) and item not in matched:
            matched.append(item)
    unit_pat = r'(kapsul|damla|drop|doz|tablet|olcek|g|mg|iu|paket)'
    excluded_names = tg_supplement_excluded_items(matched, norm) if len(matched) > 1 else (
        {item['name'] for item in matched if tg_supplement_item_missing(item, norm)})
    actions, seen = [], set()
    for item in matched:
        if item['name'] in seen or item['name'] in excluded_names:
            continue
        explicit_item = any(k in norm for k in item['keys'])
        if item['name'] == 'NOW Zinc Picolinate 50mg' and slot in ('sabah', 'kahvalti') and not explicit_item and not tg_zinc_due_for_date(today):
            continue
        line = next((ln for ln in text.splitlines() if any(k in (tg_ascii_text(ln) if 'tg_ascii_text' in globals() else ln.lower()) for k in item['keys'])), text)
        line_norm = tg_ascii_text(line) if 'tg_ascii_text' in globals() else line.lower()
        amount, unit = item['amount'], item['unit']
        found = None
        for key in item['keys']:
            key_norm = tg_ascii_text(key) if 'tg_ascii_text' in globals() else key
            found = re.search(re.escape(key_norm) + r'[^,;\n]{0,24}?(\d+(?:[\.,]\d+)?)\s*' + unit_pat, line_norm) or re.search(r'(\d+(?:[\.,]\d+)?)\s*' + unit_pat + r'[^,;\n]{0,24}?' + re.escape(key_norm), line_norm)
            if found:
                break
        if found:
            amount = found.group(1).replace(',', '.')
            if found.group(2):
                unit = found.group(2).replace('kapsul','kapsul').replace('olcek','olcek')
        actions.append({'type':'vitamin', 'date':today, 'name':item['name'], 'amount':amount, 'unit':unit, 'notes':f"{tg_stack_label(slot or 'manual')} | {item['note']}", 'stack':slot or 'manual'})
        seen.add(item['name'])
    # Çinko sabah/kahvaltı stack'te bekleniyor ama alınmadıysa bot notu ekle
    if slot in ('sabah', 'kahvalti'):
        wanted_names = set(tg_stack_preset(slot))
        if 'NOW Zinc Picolinate 50mg' in wanted_names and 'NOW Zinc Picolinate 50mg' not in seen:
            cinko_item = next((it for it in catalog if it['name'] == 'NOW Zinc Picolinate 50mg'), None)
            explicitly_excluded = cinko_item and tg_supplement_item_missing(cinko_item, norm)
            if not explicitly_excluded:
                actions.append({'type': '_bot_note', 'text': '⚠️ Çinko alınmadı — not edildi'})
    return actions

_SLOT_STACK_NAME = {
    'ac-karna': 'Aç Karna', 'sabah': 'Sabah/Kahvaltı', 'kahvalti': 'Sabah/Kahvaltı',
    'gece': 'Gece', 'pre-workout': 'Pre-workout', 'post-workout': 'Post-workout',
}
_BREAK_START_WORDS = ['ara verdim', 'ara veriyorum', 'ara basladim', 'ara devam', 'durdurdum', 'biraktim', 'kullanmiyorum artik', 'kestim']
_BREAK_END_WORDS = ['ara bitti', 'ara bitirdim', 'tekrar basladim', 'geri basladim', 'devam ediyorum artik', 'ara vermiyorum artik']

def tg_stack_slot_for_product(product_name):
    """Verilen urunun hangi stack slotuna ait oldugunu bulur (break-end'de karsi-granularite
    kontrolu icin - bkz. ai_apply_actions supplement_break_end)."""
    for slot in ('ac-karna', 'sabah', 'gece', 'pre-workout', 'post-workout'):
        if product_name in tg_stack_preset(slot):
            return slot
    return None

def tg_supplement_break_target(raw_text, norm):
    """Mesajdaki stack veya urun referansini bulur - ONCE tekil urun (catalog keys) denenir
    (ornek: 'sabah stackteki cinkoya ara verdim' -> sadece Cinko, TUM sabah stack'i degil),
    urun adi gecmiyorsa stack (post-workout/gece/...) denenir (ornek: 'post workout ara verdim'
    -> tum Post-workout stack'i). Sira onemli: bir mesaj hem stack hem urun kelimesi icerebilir
    ('gece stackte theanine e ara verdim') - boyle durumda kullanicinin niyeti neredeyse hep
    sadece o urun, tum stack degil."""
    catalog = tg_supplement_catalog() if 'tg_supplement_catalog' in globals() else []
    for item in catalog:
        if any(k in norm for k in item['keys']):
            return ('product', item['name'])
    slot = tg_supplement_stack_slot(raw_text) if 'tg_supplement_stack_slot' in globals() else ''
    if slot and slot in _SLOT_STACK_NAME:
        return ('stack', _SLOT_STACK_NAME[slot])
    return None

def tg_supplement_break_actions_from_text(raw_text):
    """'post workout ara verdim' / 'gece stack ara devam ediyor' gibi mesajlari algilayip
    kalici (gunluk degil) bir 'ara veriliyor' durumu baslatir/dogrular; 'tekrar basladim' gibi
    mesajlar durumu sonlandirir. Sonuc supplement_breaks tablosuna yazilir, boylece Ozet/Dashboard
    o urun/stacki her gun 'eksik' olarak degil 'ara veriliyor' olarak gostersin."""
    text = raw_text or ''
    norm = tg_ascii_text(text) if 'tg_ascii_text' in globals() else text.lower()
    is_end = any(w in norm for w in _BREAK_END_WORDS)
    is_start = (not is_end) and any(w in norm for w in _BREAK_START_WORDS)
    if not is_start and not is_end:
        return []
    target = tg_supplement_break_target(text, norm)
    if not target:
        return []
    target_type, target_name = target
    today = tg_effective_log_date(text, 'vitamin') if 'tg_effective_log_date' in globals() else operation_today()
    if is_end:
        return [{'type': 'supplement_break_end', 'target_type': target_type, 'target_name': target_name}]
    return [{'type': 'supplement_break_start', 'target_type': target_type, 'target_name': target_name, 'since_date': today, 'note': text[:200]}]

def tg_merge_deterministic_actions(actions, extra_actions):
    merged = list(actions or [])
    keys = set()
    for a in merged:
        if isinstance(a, dict):
            keys.add((a.get('type'), a.get('date'), a.get('slot') or '', a.get('name') or a.get('title') or ''))
    for a in extra_actions or []:
        if not isinstance(a, dict):
            continue
        key = (a.get('type'), a.get('date'), a.get('slot') or '', a.get('name') or a.get('title') or '')
        if key in keys:
            continue
        merged.append(a)
        keys.add(key)
    return merged



# TG_STACK_TEMPLATE_V1
_tg_save_meal_template_from_actions_base = tg_save_meal_template_from_actions

def tg_stack_template_requested(raw_text):
    norm = tg_template_norm(raw_text) if 'tg_template_norm' in globals() else ((raw_text or '').lower())
    return any(w in norm for w in ['stackle', 'stack olarak kaydet', 'stacke kaydet', 'stacklere kaydet'])

def tg_stack_template_title(raw_text, actions):
    text = raw_text or ''
    norm = tg_template_norm(text) if 'tg_template_norm' in globals() else text.lower()
    for pat in [r'ad[iı]\s+(.+?)\s+stack\s+olsun', r'ismi\s+(.+?)\s+stack\s+olsun', r'(.{2,50}?)\s+stackle']:
        m = re.search(pat, norm, flags=re.I)
        if m:
            name = re.sub(r'\s+', ' ', m.group(1)).strip(" .,!?:;\"'")
            noisy = ['bunu', 'bunuda', 'bunu da', 'alindi', 'aldim', 'stack', 'eksik', 'icmedim', 'haric', 'hariç']
            if name and not any(x in name for x in noisy):
                return name[:70]
    slot = ''
    for a in actions or []:
        if isinstance(a, dict) and a.get('stack'):
            slot = a.get('stack') or ''
            break
    if slot and 'tg_stack_label' in globals():
        return tg_stack_label(slot)
    return 'Telegram Supplement Stack'

def tg_stack_template_last_actions():
    try:
        ensure_telegram_messages_table()
        conn = get_db()
        row = conn.execute("""
            SELECT actions FROM telegram_messages
            WHERE direction='out' AND actions IS NOT NULL AND actions != '[]'
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        conn.close()
        if not row:
            return []
        parsed = json.loads(row['actions'] or '[]')
        return parsed if isinstance(parsed, list) else []
    except Exception:
        log.exception("Son Telegram aksiyonlari stack icin okunamadi")
        return []

def tg_stack_template_save(raw_text, actions):
    vitamins = [a for a in (actions or []) if isinstance(a, dict) and (a.get('type') or '').strip() in ('vitamin', 'supplement')]
    if not vitamins:
        vitamins = [a for a in tg_stack_template_last_actions() if isinstance(a, dict) and (a.get('type') or '').strip() in ('vitamin', 'supplement')]
    if not vitamins:
        return ''
    slot = ''
    for v in vitamins:
        if v.get('stack'):
            slot = v.get('stack') or ''
            break
    category = slot or (tg_supp_category_from_text(raw_text) if 'tg_supp_category_from_text' in globals() else 'stack')
    title = tg_stack_template_title(raw_text, vitamins)
    lines = []
    seen = set()
    for vit in vitamins:
        name = vit.get('name') or vit.get('title') or 'Supplement'
        if name in seen:
            continue
        seen.add(name)
        amount = str(vit.get('amount') or '').strip()
        unit = str(vit.get('unit') or '').strip()
        note = str(vit.get('notes') or '').strip()
        lines.append(' | '.join([x for x in [name, (amount + ' ' + unit).strip(), note] if x]))
    return tg_upsert_quick_template(
        'supplement', category, title, '\n'.join(lines), None, None, None, None, None,
        str(len(lines)), 'stack', 'telegram supplement stack template'
    )

def tg_save_meal_template_from_actions(raw_text, actions):
    if tg_stack_template_requested(raw_text):
        return tg_stack_template_save(raw_text, actions)
    return _tg_save_meal_template_from_actions_base(raw_text, actions)



# TG_BOT_DECISION_CONTEXT_V1
def tg_decision_context(raw_text=''):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    now = now_istanbul()
    cutoff = operation_cutoff_hour(now) if 'operation_cutoff_hour' in globals() else globals().get('OPERATION_DAY_CUTOFF_HOUR', 6)
    op_date = tg_effective_log_date(raw_text, 'note') if 'tg_effective_log_date' in globals() else (operation_today() if 'operation_today' in globals() else now.date().isoformat())
    ctx = {
        'operation_date': op_date,
        'late_night': 0 <= now.hour < cutoff,
        'intent': 'chat',
        'rules': [],
        'ask_if_unclear': False,
    }
    sleep_future = any(w in norm for w in ['uyuyacagim', 'uyuyacam', 'uyucam', 'yatacagim', 'yatacam', 'yatcam'])
    slept_done = any(w in norm for w in ['uyudum', 'uyandim', 'kalktim', 'uyumusum'])
    if sleep_future and not slept_done:
        ctx['intent'] = 'sleep_intent'
        ctx['rules'].append('uyuyacagim/yatacagim gelecek niyettir; sleep hours action uretme, gece kapanis notu olarak sakla')
    if any(w in norm for w in ['eve geldim', 'gece', 'uyku oncesi', 'uyumadan', 'yatmadan']):
        ctx['rules'].append('gece kapanisi: operasyon gunune yaz, sabah check-in gibi cevaplama')
    if 'su' in norm or 'litre' in norm or re.search(r'\b\d+(?:[\.,]\d+)?\s*l\b', norm):
        if any(w in norm for w in ['toplam', 'toplamda', 'bugun toplam', 'gun totali', 'gun toplam']):
            ctx['intent'] = 'water_total'
            ctx['rules'].append('su toplamdir: tek water action uret, mode=set kullan; ayni miktari ikinci kez ekleme')
        elif any(w in norm for w in ['azalt', 'dusur', 'yanlis']):
            ctx['intent'] = 'water_correction'
            ctx['rules'].append('su duzeltme niyeti: mevcut su kaydini azalt/duzelt, yeni su ekleme')
        elif any(w in norm for w in ['ekle', 'icti', 'ictim', 'icildi', '+']):
            ctx['intent'] = 'water_add'
            ctx['rules'].append('su ekleme olabilir: miktari water_ml olarak ekle')
        else:
            ctx['ask_if_unclear'] = True
            ctx['intent'] = 'water_total'
            ctx['rules'].append('litre ile yazilan tam gun su ifadesini toplam kabul et; mode=set kullan')
    if any(w in norm for w in ['kilo', 'kg', 'tarti', 'tartildim']):
        if any(w in norm for w in ['ac karna', 'ackarna', 'sabah tarti', 'sabah kilo', 'sabah ac']):
            ctx['intent'] = 'weight_morning'
            ctx['rules'].append('sabah ac karna resmi tarti: kilo trendine resmi tarti notuyla isle')
        elif ctx['late_night'] or any(w in norm for w in ['eve geldim', 'gece', 'uyumadan', 'yatmadan']):
            ctx['intent'] = 'weight_night'
            ctx['rules'].append('gece kapanis tartisi: kilo trendine yaz ama notes alaninda gece kapanis olarak belirt')
    if any(w in norm for w in ['stack', 'vitamin', 'takviye', 'supplement', 'suplement']):
        ctx['rules'].append('stack varsa sabah/ac-karna/gece/kahvalti slotunu ayir; eksik/haric/yok ifadelerini uygula')
        ctx['rules'].append('cinko gun asiri takip edilir; acikca alindiysa kaydet, eksikse kaydetme')
    if any(w in norm for w in ['kahvalti', 'ogle', 'aksam', 'tavuk', 'yulaf', 'yumurta']):
        ctx['rules'].append('ogunleri basliklara gore ayir; ogle verisini aksama karistirma')
    if any(w in norm for w in ['adim', 'ad?m', 'steps']):
        ctx['rules'].append('adim verisini dashboard akisi icin steps action olarak isle')
    return ctx

def tg_context_note_for_prompt(raw_text=''):
    ctx = tg_decision_context(raw_text)
    bits = [f"operasyon_tarihi={ctx.get('operation_date')}", f"niyet={ctx.get('intent')}"]
    if ctx.get('late_night'):
        bits.append(f"gece_modu={current_shift_info().get('late_window')} onceki operasyon gunu")
    if ctx.get('ask_if_unclear'):
        bits.append('belirsizse tek kisa soru sor')
    bits.extend(ctx.get('rules') or [])
    return ' | '.join(bits)

def tg_context_note_actions_from_text(raw_text=''):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    now = now_istanbul()
    cutoff = operation_cutoff_hour(now) if 'operation_cutoff_hour' in globals() else globals().get('OPERATION_DAY_CUTOFF_HOUR', 6)
    night_words = ['eve geldim', 'uyuyacagim', 'uyuyacam', 'yatacagim', 'yatacam', 'uyumadan', 'yatmadan', 'vitaminlerimi bugun almayacagim', 'vitamin almayacagim']
    if not (0 <= now.hour < cutoff or any(w in norm for w in night_words)):
        return []
    if not any(w in norm for w in night_words):
        return []
    d = tg_effective_log_date(raw_text, 'note') if 'tg_effective_log_date' in globals() else (operation_today() if 'operation_today' in globals() else operation_today())
    short = ' '.join((raw_text or '').split())[:450]
    return [{'type': 'note', 'date': d, 'note': 'Gece kapanis / uyku oncesi baglam: ' + short}]



# TG_TRAINING_STACK_TEMPLATE_V1
def tg_training_stack_day(raw_text=''):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    mapping = {
        'push': 'Push',
        'pull': 'Pull',
        'leg': 'Leg',
        'upper': 'Upper',
        'lower': 'Lower',
    }
    for key, val in mapping.items():
        if key in norm:
            return val
    return ''

def tg_training_stack_catalog(day):
    base = {
        'Push': [
            ('Bench Press', [('Warm-up','10-12'), ('Working set','6-8'), ('Working set','6-8'), ('Back-off','10-12')]),
            ('Incline Dumbbell Press', [('Warm-up','10'), ('Working set','8-10'), ('Working set','8-10')]),
            ('Shoulder Press', [('Working set','6-10'), ('Working set','6-10'), ('Back-off','10-12')]),
            ('Lateral Raise', [('Working set','12-15'), ('Working set','12-15'), ('Pump','15-20')]),
            ('Triceps Pushdown', [('Working set','10-12'), ('Working set','10-12'), ('Back-off','12-15')]),
        ],
        'Pull': [
            ('Lat Pulldown', [('Warm-up','10-12'), ('Working set','8-10'), ('Working set','8-10')]),
            ('Chest Supported Row', [('Working set','8-10'), ('Working set','8-10'), ('Back-off','10-12')]),
            ('Cable Row', [('Working set','10-12'), ('Working set','10-12')]),
            ('Rear Delt Fly', [('Working set','12-20'), ('Working set','12-20')]),
            ('Biceps Curl', [('Working set','8-12'), ('Working set','8-12'), ('Pump','12-15')]),
        ],
        'Leg': [
            ('Squat', [('Warm-up','8-10'), ('Working set','5-8'), ('Working set','5-8'), ('Back-off','8-10')]),
            ('Romanian Deadlift', [('Warm-up','8'), ('Working set','6-10'), ('Working set','6-10')]),
            ('Leg Press', [('Working set','10-12'), ('Working set','10-12'), ('Back-off','12-15')]),
            ('Leg Curl', [('Working set','10-15'), ('Working set','10-15')]),
            ('Calf Raise', [('Working set','10-15'), ('Working set','10-15'), ('Pump','15-20')]),
        ],
        'Upper': [
            ('Bench Press', [('Warm-up','10'), ('Working set','6-8'), ('Back-off','10-12')]),
            ('Pull-up / Pulldown', [('Working set','6-10'), ('Working set','6-10')]),
            ('Row', [('Working set','8-12'), ('Working set','8-12')]),
            ('Lateral Raise', [('Working set','12-15'), ('Pump','15-20')]),
            ('Arm Superset', [('Working set','10-12'), ('Working set','10-12')]),
        ],
        'Lower': [
            ('Hack Squat / Squat', [('Warm-up','8-10'), ('Working set','6-10'), ('Working set','6-10')]),
            ('Romanian Deadlift', [('Working set','6-10'), ('Working set','6-10'), ('Back-off','10-12')]),
            ('Leg Extension', [('Working set','10-15'), ('Working set','10-15')]),
            ('Leg Curl', [('Working set','10-15'), ('Working set','10-15')]),
            ('Ab / Core', [('Working set','10-20'), ('Working set','10-20')]),
        ],
    }
    return base.get(day, [])

def tg_try_training_stack_template(raw_text=''):
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    if not any(w in norm for w in ['stack', 'program', 'sablon', 'şablon', 'kur', 'yukle', 'yükle', 'olustur', 'oluştur']):
        return ''
    if not any(w in norm for w in ['push', 'pull', 'leg', 'upper', 'lower']):
        return ''
    day = tg_training_stack_day(raw_text)
    if not day:
        return ''
    exercises = tg_training_stack_catalog(day)
    if not exercises:
        return ''
    conn = get_db()
    added = 0
    try:
        for idx, (name, spec) in enumerate(exercises, start=1):
            exists = conn.execute(
                "SELECT id FROM training_exercises WHERE training_day=? AND lower(exercise)=lower(?)",
                (day, name),
            ).fetchone()
            details = [{'set': i + 1, 'type': typ, 'reps': reps, 'weight': '', 'done': False} for i, (typ, reps) in enumerate(spec)]
            notes = json.dumps({'set_details': details, 'source': 'telegram-training-stack'}, ensure_ascii=False)
            if exists:
                continue
            conn.execute(
                "INSERT INTO training_exercises (training_day, exercise, sets, reps, weight, notes, sort_order) VALUES (?,?,?,?,?,?,?)",
                (day, name, str(len(details)), details[0].get('reps', ''), '', notes, idx * 10),
            )
            added += 1
        conn.commit()
    finally:
        conn.close()
    return f"{day} antrenman stack hazir. {added} yeni hareket eklendi; mevcut hareketler korunuyor. Antrenman sayfasinda {day} gununu acip 'Bugune isle' ile kilo/tekrar girebilirsin."

async def cmd_chat_ai(u, c):
    raw = (u.message.text or '').strip()
    chat_id = getattr(u.effective_chat, 'id', '') if u else ''
    username = ''
    if getattr(u, 'effective_user', None):
        username = u.effective_user.username or u.effective_user.first_name or ''

    tg_store_message('in', raw, chat_id, username)
    tg_touch_heartbeat('message', raw) if 'tg_touch_heartbeat' in globals() else None
    gunaydin = tg_gunaydin_reply(raw) if 'tg_gunaydin_reply' in globals() else {'reply': '', 'reset': False}
    if gunaydin['reply']:
        tg_store_message('out', gunaydin['reply'], chat_id, 'AI Coach', [])
        await u.message.reply_text(gunaydin['reply'])
        return
    gunaydin_reset = gunaydin['reset']
    training_stack_reply = tg_try_training_stack_template(raw) if 'tg_try_training_stack_template' in globals() else ''
    if training_stack_reply:
        tg_store_message('out', training_stack_reply, chat_id, 'AI Coach', [])
        await u.message.reply_text(training_stack_reply)
        return
    night_reply = tg_night_casual_reply(raw) if 'tg_night_casual_reply' in globals() else ''
    if night_reply:
        tg_store_message('out', night_reply, chat_id, 'AI Coach', [])
        await u.message.reply_text(night_reply)
        return
    water_correction = tg_try_water_correction(raw) if 'tg_try_water_correction' in globals() else None
    if water_correction:
        reply = water_correction.get('reply') or 'Su kaydı düzeltildi.'
        tg_store_message('out', reply, chat_id, 'AI Coach', water_correction)
        await u.message.reply_text(reply)
        return

    result = ai_coach_call(raw)
    actions = result.get('actions') or []
    basic_actions = tg_basic_actions_from_text(raw) if 'tg_basic_actions_from_text' in globals() else []
    full_day_actions = tg_full_day_actions_from_text(raw) if 'tg_full_day_actions_from_text' in globals() else []
    supplement_actions = tg_supplement_actions_from_text(raw) if 'tg_supplement_actions_from_text' in globals() else []
    supplement_break_actions = tg_supplement_break_actions_from_text(raw) if 'tg_supplement_break_actions_from_text' in globals() else []
    direct_mood_actions = tg_direct_mood_actions_from_text(raw, chat_id) if 'tg_direct_mood_actions_from_text' in globals() else []
    context_note_actions = tg_context_note_actions_from_text(raw) if 'tg_context_note_actions_from_text' in globals() else []
    # Deterministic supplement parser her zaman AI vitamin action'larinin uzerine yazar
    if supplement_actions:
        actions = [a for a in actions if not (isinstance(a, dict) and a.get('type') == 'vitamin')]
    if full_day_actions:
        preferred = []
        deterministic_keys = set()
        for ba in full_day_actions + basic_actions:
            if not isinstance(ba, dict):
                continue
            typ = ba.get('type')
            key = (typ, ba.get('date'), ba.get('slot') if typ == 'meal' else '', ba.get('name') or '')
            if key in deterministic_keys:
                continue
            deterministic_keys.add(key)
            if typ == 'water':
                ba['mode'] = 'set'
            preferred.append(ba)
        actions = [
            a for a in actions
            if not (
                isinstance(a, dict) and
                (a.get('type'), a.get('date'), a.get('slot') if a.get('type') == 'meal' else '', a.get('name') or '') in deterministic_keys
            )
        ]
        actions = preferred + actions
    elif basic_actions:
        existing_keys = {(a.get('type'), a.get('date'), a.get('slot'), a.get('name')) for a in actions if isinstance(a, dict)}
        for ba in basic_actions:
            key = (ba.get('type'), ba.get('date'), ba.get('slot'), ba.get('name'))
            if key not in existing_keys:
                actions.append(ba)
                existing_keys.add(key)
    if 'tg_merge_deterministic_actions' in globals() and context_note_actions:
        actions = tg_merge_deterministic_actions(actions, context_note_actions)
    elif context_note_actions:
        actions = actions + context_note_actions
    if 'tg_merge_deterministic_actions' in globals() and supplement_actions:
        actions = tg_merge_deterministic_actions(actions, supplement_actions)
    if 'tg_normalize_action_dates_and_sleep' in globals():
        before_sleep_count = len([a for a in actions if isinstance(a, dict) and a.get('type') == 'sleep'])
        actions = tg_normalize_action_dates_and_sleep(raw, actions)
        after_sleep_count = len([a for a in actions if isinstance(a, dict) and a.get('type') == 'sleep'])
    else:
        before_sleep_count = after_sleep_count = 0
    if direct_mood_actions:
        actions = [a for a in actions if not (isinstance(a, dict) and a.get('type') == 'mood')]
        actions = direct_mood_actions + actions
    if supplement_break_actions:
        actions = actions + supplement_break_actions
    saved = ai_apply_actions(actions)
    if (not result.get('actions')) and basic_actions and 'Bağlantı sorunu' in (result.get('reply') or ''):
        result['reply'] = 'AI bağlantısı anlık takıldı ama temel verileri boşa düşürmedim. Kilo/su/adım ve net makro gördüğüm kayıtları sisteme işledim; detaylı koç yorumunu tekrar sorabilirsin.'
    template_title = ''
    try:
        template_title = tg_save_meal_template_from_actions(raw, actions) if 'tg_save_meal_template_from_actions' in globals() else ''
        if template_title:
            saved.append('şablon')
    except Exception:
        log.exception("Telegram sabit ogun sablon kaydi basarisiz")

    norm = _tg_norm(raw) if '_tg_norm' in globals() else raw.lower()

    if 'hareket' not in saved and 'x' in norm and any(w in norm for w in ['bench','squat','deadlift','press','row','curl','pushdown','pulldown','raise','fly','extension','idman','antrenman']):
        try:
            tg_save_training_from_text(raw)
            saved.append('hareket')
        except Exception:
            log.exception("Telegram hareket fallback kaydi basarisiz")

    if 'kilo' not in saved and any(w in norm for w in ['kilo','kg','weight']):
        try:
            import re
            m = re.search(r'(\d{2,3}(?:[\.,]\d{1,2})?)\s*(?:kg|kilo)?', norm)
            if m:
                ensure_body_metrics_table()
                kg = float(m.group(1).replace(',', '.'))
                conn = get_db()
                today = tg_effective_log_date(raw, 'weight') if 'tg_effective_log_date' in globals() else operation_today()
                conn.execute("""
                    INSERT INTO body_metrics (date, weight_kg, notes)
                    VALUES (?,?,?)
                    ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, notes=excluded.notes
                """, (today, kg, 'telegram'))
                conn.commit(); conn.close()
                saved.append('kilo')
        except Exception:
            log.exception("Telegram kilo fallback kaydi basarisiz")

    if 'adım' not in saved and 'adim' not in saved and any(w in norm for w in ['adim','step','steps']):
        try:
            import re
            nums = [int(x) for x in re.findall(r'\b\d{3,6}\b', norm)]
            if nums:
                ensure_step_logs_table()
                conn = get_db()
                today = tg_effective_log_date(raw, 'steps') if 'tg_effective_log_date' in globals() else operation_today()
                conn.execute("INSERT OR REPLACE INTO step_logs (date, steps, notes) VALUES (?,?,?)", (today, nums[-1], 'telegram'))
                conn.commit(); conn.close()
                saved.append('adım')
        except Exception:
            log.exception("Telegram adim fallback kaydi basarisiz")

    reply = result.get('reply') or 'Anladım.'
    bot_notes = [a.get('text','') for a in actions if isinstance(a, dict) and a.get('type') == '_bot_note']
    if bot_notes:
        reply += '\n\n' + '\n'.join(bot_notes)
    if 'gunaydin_reset' in locals() and gunaydin_reset:
        reply += "\n\n☀️ Yeni gün başlatıldı — sayaçlar sıfırdan."
    if 'before_sleep_count' in locals() and before_sleep_count > after_sleep_count:
        reply += "\n\nUyku notu: uyuyacağım/yatacağım ifadesini saat olarak algılamadım; uyku süresi kaydetmedim. Uyandığında kalkış saatini yazarsan gerçek süreyi işleriz."
    if template_title:
        reply += f"\n\nSablon hazir: {template_title}. Sablonlar sayfasinda dogru kategori altinda kullanabilirsin."
    if saved:
        reply += "\n\nKaydedildi: " + ", ".join(saved)
    tg_store_message('out', reply, chat_id, 'AI Coach', actions)
    await u.message.reply_text(reply)


def enforce_training_day_on_actions(actions, date_val=None):
    """AI yanilsa bile kayitlar resmi sistem gunune baglanir."""
    official = training_day(date_val or operation_today())
    valid_days = {"Push", "Pull", "Leg", "Legs", "Upper", "Lower", "Off", "Off 1", "Off 2"}
    fixed = []
    for action in actions or []:
        if not isinstance(action, dict):
            fixed.append(action)
            continue
        a = dict(action)
        kind = a.get("type")
        guessed = a.get("exercise_type") or a.get("training_day")
        if kind in ("exercise", "workout_set") and guessed in valid_days and guessed != official:
            if kind == "exercise":
                a["exercise_type"] = official
            else:
                a["training_day"] = official
            note = (a.get("notes") or "").strip()
            a["notes"] = (note + " | resmi_gun_duzeltildi").strip(" |")
        if kind == "workout_set":
            a.setdefault("training_day", official)
        fixed.append(a)
    return fixed


def food_db_auto_learn(actions):
    """Kaydedilen yemek eylemlerini quick_templates'e otomatik ekle/guncelle."""
    meals = [a for a in (actions or []) if isinstance(a, dict) and a.get('type') == 'meal']
    if not meals:
        return
    conn = get_db()
    for m in meals:
        if m.get('estimated') is True:
            continue
        title = (m.get('title') or '').strip()
        cal = m.get('calories')
        if not title or not cal:
            continue  # Basliksiz veya kalori bilinmeyen ogunleri ogrenme
        existing = conn.execute(
            "SELECT id FROM quick_templates WHERE kind='meal' AND lower(title)=lower(?)", (title,)
        ).fetchone()
        if existing:
            # Guncelle — kullanici son girdigi degerleri kullaniyor demek
            conn.execute(
                "UPDATE quick_templates SET calories=?, protein_g=?, carbs_g=?, fat_g=?, "
                "description=?, ts=CURRENT_TIMESTAMP WHERE id=?",
                (cal, m.get('protein_g'), m.get('carbs_g'), m.get('fat_g'),
                 m.get('description') or '', existing['id'])
            )
        else:
            conn.execute(
                "INSERT INTO quick_templates (kind, category, title, description, calories, protein_g, carbs_g, fat_g, notes) "
                "VALUES ('meal', ?, ?, ?, ?, ?, ?, ?, 'auto-learned')",
                (m.get('slot') or 'extra', title, m.get('description') or '',
                 cal, m.get('protein_g'), m.get('carbs_g'), m.get('fat_g'))
            )
    conn.commit()
    conn.close()


async def cmd_photo(u, c):
    """Kullanici fotograf gonderdiyse Claude vision ile analiz et"""
    msg = u.message
    caption = (msg.caption or '').strip()
    chat_id = getattr(u.effective_chat, 'id', '') if u else ''
    username = ''
    if getattr(u, 'effective_user', None):
        username = u.effective_user.username or u.effective_user.first_name or ''
    tg_store_message('in', caption or '[fotoğraf]', chat_id, username)
    await msg.chat.send_action('typing')
    try:
        photo = msg.photo[-1]  # en yuksek cozunurluk
        tg_file = await photo.get_file()
        import urllib.request as _ur
        img_bytes = _ur.urlopen(tg_file.file_path, timeout=15).read()
        import base64 as _b64
        img_b64 = _b64.b64encode(img_bytes).decode('utf-8')
        today = operation_today()
        yesterday = (operation_date() - timedelta(days=1)).isoformat()
        ctx = _today_ai_context()
        official_training = ctx.get('training_day') or training_day(today)
        besin_db_ctx = _besin_db_for_prompt()
        system_prompt = (
            "Sen Taha Serdem'in kisisel antrenman ve gunluk performans kocusun. "
            "Turkce, samimi, net ve motive edici konus.\n"
            + TAHA_COACHING_POLICY
            + NUTRITION_ANALYSIS_POLICY
            + f"RESMI ANTRENMAN GUNU: {official_training}. Bu sistem verisidir; fotografla tahmin edilmez. "
            "Bugun icin antrenman onerisi yapacaksan sadece bu resmi gunle uyumlu oner. "
            "Ornek: resmi gun Leg ise Push onerme.\n"
            "Fotografi analiz et. Antrenman programi, ilerleme, vucut olcumu, yemek veya "
            "herhangi bir not gorebilirsin.\n"
            "- Antrenman programi/logu fotografiysa: eksiklikleri, iyilestirme onerilerini, "
            "o gune uygun antrenmani yaz\n"
            "- Vucud fotografiysa: durusu, kas gelisimini, genel yorumu paylas\n"
            "- Yemek fotografiysa besinleri ayri ayri analiz et; caption kayit istiyorsa ayri meal actionlari olustur.\n"
            "- Kayit istenmediyse actions bos kalsin. Belirsizlik yuksekse once tek net soru sor.\n"
            "SADECE gecerli JSON dondur:\n"
            '{"reply":"...","actions":[{"type":"meal","date":"YYYY-MM-DD","slot":"kahvalti|ogle|aksam|ara","title":"Besin ve miktar","description":"cig/pismis ve hazirlama","calories":0,"protein_g":0,"carbs_g":0,"fat_g":0,"estimated":true,"source":"visual-estimate"}]}'
            f"\nTarih: bugun={today}, dun={yesterday}\n"
            f"Bugunun verisi: {json.dumps(ctx, ensure_ascii=False)}"
            + "\n" + besin_db_ctx
        )
        user_content = []
        if caption:
            user_content.append({"type": "text", "text": caption})
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
        })
        import urllib.request, urllib.error
        body = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 2000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}]
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = _json_from_text(payload["content"][0]["text"])
        actions = enforce_training_day_on_actions(result.get("actions") or [], today)
        saved = ai_apply_actions(actions)
        food_db_auto_learn(actions)
        reply = result.get("reply") or "Fotografi inceledim."
        if official_training and official_training.lower() not in reply.lower():
            reply += f"\n\nSistem notu: Bugunun resmi antrenman gunu {official_training}."
        if saved:
            reply += f"\n\n✅ Kaydedildi: {', '.join(saved)}"
        tg_store_message('out', reply, getattr(u.effective_chat, 'id', ''), 'AI Coach', actions)
        await msg.reply_text(reply)
    except Exception:
        log.exception("Fotograf analizi basarisiz")
        await msg.reply_text("Fotoğrafı analiz edemedim, kayıt yapmadım — bir daha gönderir misin?")

# TELEGRAM_STANDALONE_ALWAYS_ON_V1
TELEGRAM_LOCK_PATH = os.path.join(BASE_DIR, 'telegram_bot.lock')
_TELEGRAM_LOCK_HANDLE = None

def acquire_telegram_bot_lock():
    """Avoid two Telegram pollers fighting over the same bot token."""
    global _TELEGRAM_LOCK_HANDLE
    try:
        _TELEGRAM_LOCK_HANDLE = open(TELEGRAM_LOCK_PATH, 'a+b')
        _TELEGRAM_LOCK_HANDLE.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(_TELEGRAM_LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_TELEGRAM_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _TELEGRAM_LOCK_HANDLE.write(str(os.getpid()).encode('ascii', errors='ignore'))
        _TELEGRAM_LOCK_HANDLE.flush()
        return True
    except Exception:
        try:
            if _TELEGRAM_LOCK_HANDLE:
                _TELEGRAM_LOCK_HANDLE.close()
        except Exception:
            pass
        _TELEGRAM_LOCK_HANDLE = None
        return False

def release_telegram_bot_lock():
    global _TELEGRAM_LOCK_HANDLE
    if not _TELEGRAM_LOCK_HANDLE:
        return
    try:
        _TELEGRAM_LOCK_HANDLE.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(_TELEGRAM_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_TELEGRAM_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _TELEGRAM_LOCK_HANDLE.close()
    except Exception:
        pass
    _TELEGRAM_LOCK_HANDLE = None

def _build_tg_handlers(ptb_app):
    """Tum komut/mesaj/foto handler'larini tek yerden kurar - polling ve webhook
    modlari ayni listeyi kullanir, iki yerde ayri ayri elle senkron tutulmaz."""
    from telegram.ext import CommandHandler, MessageHandler, filters
    for cmd, fn in [("start",cmd_start),("uyku",cmd_uyku),("egzersiz",cmd_egzersiz),
                    ("yemek",cmd_yemek),("su",cmd_su),("is",cmd_is),("antrenor",cmd_antrenor),
                    ("mood",cmd_mood),("vitamin",cmd_vitamin),("bugun",cmd_bugun),
                    ("rapor",cmd_rapor),("antrenman",cmd_antrenman),
                    ("hafta",cmd_hafta),("streak",cmd_streak),
                    ("ogun",cmd_ogun),("idman",cmd_idman),("sablonlar",cmd_sablonlar)]:
        ptb_app.add_handler(CommandHandler(cmd, fn))
    ptb_app.add_handler(MessageHandler(filters.PHOTO, cmd_photo))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_chat_ai))


# --- WEBHOOK MODE (Railway/start.py) ---------------------------------------
_wh_app  = None
_wh_loop = None
_wh_lock = threading.Lock()

def _get_wh_loop():
    global _wh_loop
    with _wh_lock:
        if _wh_loop is None or _wh_loop.is_closed():
            _wh_loop = asyncio.new_event_loop()
            t = threading.Thread(target=_wh_loop.run_forever, daemon=True, name='bot-wh-loop')
            t.start()
            log.info("Webhook event loop baslatildi.")
    return _wh_loop

_wh_app_alock = None  # asyncio.Lock - wh-loop icinde lazily olusur

async def _ensure_wh_app():
    """Application singleton'i. Ilk iki update ayni anda gelirse cifte initialize()
    olmasin diye asyncio kilidiyle korunur (await noktasinda coroutine'ler
    interleave edebiliyor - denetim bulgusu)."""
    global _wh_app, _wh_app_alock
    if _wh_app is not None:
        return _wh_app
    if _wh_app_alock is None:
        _wh_app_alock = asyncio.Lock()
    async with _wh_app_alock:
        if _wh_app is not None:
            return _wh_app
        from telegram.ext import Application
        ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
        _build_tg_handlers(ptb_app)
        await ptb_app.initialize()
        _wh_app = ptb_app
        log.info("Webhook Application hazir.")
        return _wh_app

async def _do_process_webhook_update(data: dict):
    from telegram import Update
    ptb_app = await _ensure_wh_app()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)

def process_webhook_update(data: dict):
    """Flask /telegram_webhook route'undan cagrilir -- bot loop'unda async isler."""
    loop = _get_wh_loop()
    future = asyncio.run_coroutine_threadsafe(_do_process_webhook_update(data), loop)
    try:
        future.result(timeout=55)
    except Exception as e:
        log.error("Webhook update isleme hatasi: %s", e)


def start_telegram_bot():
    """Standalone polling fallback. Uretimde start.py webhook modu kullanir
    (DISABLE_EMBEDDED_BOT=1) - bu fonksiyon sadece lokal calistirma / 'python app.py
    --telegram-only' icin var, ayni anda ikisi calismamali (Telegram tek getUpdates
    poller'a izin verir)."""
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN ayarli degil."); return
    if not acquire_telegram_bot_lock():
        log.info("Telegram bot zaten baska bir surecte aktif; ikinci polling baslatilmadi.")
        return
    try:
        from telegram.ext import Application
    except ImportError:
        release_telegram_bot_lock()
        log.warning("python-telegram-bot kurulu degil."); return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app2 = Application.builder().token(TELEGRAM_TOKEN).build()
    _build_tg_handlers(app2)
    log.info("Telegram bot aktif: web sitesinden bagimsiz veri takibi basladi.")
    tg_touch_heartbeat('running', 'telegram polling started') if 'tg_touch_heartbeat' in globals() else None
    try:
        app2.run_polling(drop_pending_updates=True, stop_signals=None)
    finally:
        tg_touch_heartbeat('stopped', 'telegram polling stopped') if 'tg_touch_heartbeat' in globals() else None
        release_telegram_bot_lock()


def ensure_food_registry():
    conn = get_db()
    # Create table with full schema
    conn.execute('''
        CREATE TABLE IF NOT EXISTS food_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT DEFAULT '',
            name TEXT NOT NULL,
            official_name TEXT DEFAULT '',
            calories_per_100 REAL DEFAULT 0,
            protein_per_100 REAL DEFAULT 0,
            carbs_per_100 REAL DEFAULT 0,
            fat_per_100 REAL DEFAULT 0,
            unit TEXT DEFAULT 'g',
            base_unit TEXT DEFAULT '100g',
            serving_size REAL DEFAULT 100,
            serving_unit TEXT DEFAULT 'g',
            is_raw INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            aliases TEXT DEFAULT '',
            source TEXT DEFAULT '',
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migrate old schema
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(food_registry)").fetchall()}
    if 'calories_per_100g' in cols and 'calories_per_100' not in cols:
        for col in ['calories_per_100','protein_per_100','carbs_per_100','fat_per_100']:
            try: conn.execute(f"ALTER TABLE food_registry ADD COLUMN {col} REAL DEFAULT 0")
            except: pass
        try:
            conn.execute("UPDATE food_registry SET calories_per_100=COALESCE(calories_per_100g,0), protein_per_100=COALESCE(protein_per_100g,0), carbs_per_100=COALESCE(carbs_per_100g,0), fat_per_100=COALESCE(fat_per_100g,0)")
        except: pass
    for col, defval in [('aliases',"TEXT DEFAULT ''"),('unit',"TEXT DEFAULT 'g'"),('serving_size','REAL DEFAULT 100'),
                        ('product_id',"TEXT DEFAULT ''"),('official_name',"TEXT DEFAULT ''"),
                        ('base_unit',"TEXT DEFAULT '100g'"),('is_raw','INTEGER DEFAULT 0'),('source',"TEXT DEFAULT ''"),
                        ('category',"TEXT DEFAULT ''")]:
        if col not in cols:
            try: conn.execute(f"ALTER TABLE food_registry ADD COLUMN {col} {defval}")
            except: pass
    conn.commit()
    conn.close()

@app.route('/api/food-registry', methods=['GET'])
def api_food_registry_list():
    ensure_food_registry()
    conn = get_db()
    rows = conn.execute("SELECT * FROM food_registry ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/food-registry', methods=['POST'])
def api_food_registry_add():
    try:
        ensure_food_registry()
        data = request.get_json(force=True) or {}
        if not data.get('name','').strip():
            return jsonify({'error':'name required'}), 400
        conn = get_db()
        conn.execute("""INSERT INTO food_registry (name,calories_per_100,protein_per_100,carbs_per_100,fat_per_100,unit,serving_size,serving_unit,notes,aliases,category) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (data.get('name','').strip(),data.get('calories_per_100') or 0,data.get('protein_per_100') or 0,data.get('carbs_per_100') or 0,data.get('fat_per_100') or 0,data.get('unit','g'),data.get('serving_size') or 100,data.get('serving_unit') or 'g',data.get('notes',''),data.get('aliases',''),data.get('category','')))
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({'ok':True,'id':new_id})
    except Exception as e:
        import traceback
        return jsonify({'error':str(e),'trace':traceback.format_exc()}), 500

@app.route('/api/food-registry/<int:fid>', methods=['PUT'])
def api_food_registry_update(fid):
    ensure_food_registry()
    data = request.get_json(force=True) or {}
    fields = ['name','calories_per_100','protein_per_100','carbs_per_100','fat_per_100','unit','serving_size','serving_unit','notes','aliases','category']
    sent = {k: data[k] for k in fields if k in data}
    if not sent:
        return jsonify({'ok': False, 'error': 'Güncellenecek alan yok'}), 400
    conn = get_db()
    conn.execute(f"UPDATE food_registry SET {','.join(k+'=?' for k in sent)} WHERE id=?", (*sent.values(), fid))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/food-registry/<int:fid>', methods=['DELETE'])
def api_food_registry_delete(fid):
    ensure_food_registry()
    conn = get_db()
    conn.execute("DELETE FROM food_registry WHERE id=?", (fid,))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

def _meal_stack_calc_item(conn, item):
    """meal_stack_items satirindan (food_id/food_name/amount/unit) kcal/makro hesapla."""
    food = None
    if item['food_id']:
        food = conn.execute("SELECT * FROM food_registry WHERE id=?", (item['food_id'],)).fetchone()
    if not food and item['food_name']:
        food = conn.execute("SELECT * FROM food_registry WHERE name=? OR official_name=?",
                             (item['food_name'], item['food_name'])).fetchone()
    if not food:
        return None
    food = dict(food)
    ratio = (item['amount'] or 0) / 100.0
    return {
        'food_id': food['id'],
        'name': food.get('official_name') or food.get('name'),
        'amount': item['amount'], 'unit': item['unit'],
        'kcal': round((food.get('calories_per_100') or 0) * ratio, 1),
        'protein': round((food.get('protein_per_100') or 0) * ratio, 1),
        'carbs': round((food.get('carbs_per_100') or 0) * ratio, 1),
        'fat': round((food.get('fat_per_100') or 0) * ratio, 1),
    }

@app.route('/api/meal-stacks', methods=['GET'])
def api_meal_stacks_list():
    conn = get_db()
    stacks = [dict(r) for r in conn.execute("SELECT * FROM meal_stacks ORDER BY name").fetchall()]
    for s in stacks:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM meal_stack_items WHERE stack_id=? ORDER BY order_num, id", (s['id'],)).fetchall()]
        s['items'] = items
        s['item_count'] = len(items)
    conn.close()
    return jsonify(stacks)

@app.route('/api/meal-stacks', methods=['POST'])
def api_meal_stacks_add():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    items = data.get('items') or []
    if not name:
        return jsonify({'ok': False, 'error': 'name gerekli'}), 400
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO meal_stacks (name) VALUES (?)", (name,))
        sid = cur.lastrowid
        for i, it in enumerate(items):
            conn.execute(
                "INSERT INTO meal_stack_items (stack_id,food_id,food_name,amount,unit,order_num) VALUES (?,?,?,?,?,?)",
                (sid, it.get('food_id'), (it.get('food_name') or '').strip(), it.get('amount') or 100, it.get('unit') or 'g', i))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'ok': False, 'error': 'Bu isimde bir stack zaten var'}), 400
    conn.close()
    return jsonify({'ok': True, 'id': sid})

@app.route('/api/meal-stacks/<int:sid>', methods=['PUT'])
def api_meal_stacks_update(sid):
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    items = data.get('items') or []
    conn = get_db()
    if name:
        conn.execute("UPDATE meal_stacks SET name=? WHERE id=?", (name, sid))
    conn.execute("DELETE FROM meal_stack_items WHERE stack_id=?", (sid,))
    for i, it in enumerate(items):
        conn.execute(
            "INSERT INTO meal_stack_items (stack_id,food_id,food_name,amount,unit,order_num) VALUES (?,?,?,?,?,?)",
            (sid, it.get('food_id'), (it.get('food_name') or '').strip(), it.get('amount') or 100, it.get('unit') or 'g', i))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/meal-stacks/<int:sid>', methods=['DELETE'])
def api_meal_stacks_delete(sid):
    conn = get_db()
    conn.execute("DELETE FROM meal_stack_items WHERE stack_id=?", (sid,))
    conn.execute("DELETE FROM meal_stacks WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/meal-stacks/<int:sid>/quick-add', methods=['POST'])
def api_meal_stacks_quick_add(sid):
    """Stack'teki tum urunleri o gunun meal_entries'ine tek seferde ekle."""
    data = request.get_json(force=True) or {}
    d = data.get('date', operation_today())
    slot = (data.get('slot') or '').strip()
    if not slot:
        return jsonify({'ok': False, 'error': 'slot gerekli'}), 400
    conn = get_db()
    stack = conn.execute("SELECT * FROM meal_stacks WHERE id=?", (sid,)).fetchone()
    if not stack:
        conn.close()
        return jsonify({'ok': False, 'error': 'stack bulunamadi'}), 404
    items = conn.execute("SELECT * FROM meal_stack_items WHERE stack_id=? ORDER BY order_num, id", (sid,)).fetchall()
    added = []
    for it in items:
        calc = _meal_stack_calc_item(conn, it)
        if not calc:
            continue
        description = f"{calc['amount']} {calc['unit']} {calc['name']}"
        conn.execute("""
            INSERT INTO meal_entries (date,slot,title,description,calories,protein_g,carbs_g,fat_g,source)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (d, slot, calc['name'], description, calc['kcal'], calc['protein'], calc['carbs'], calc['fat'], 'meal_stack'))
        added.append(calc)
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d, 'slot': slot, 'stack_name': stack['name'], 'added': added})

@app.route('/api/food-registry/migrate-spec', methods=['POST'])
def api_food_registry_migrate_spec():
    """Master spec v1.0 ürün düzeltmeleri — isimleri, macroları ve meta alanları güncelle."""
    ensure_food_registry()
    conn = get_db()
    # product_id, official_name, base_unit, is_raw, source, correct macros
    SPEC = [
        # (match_by_name, updates_dict)
        ('Yasmin Pirinç', {'product_id':'PROD-010','official_name':'YASMİN Pirinci','name':'YASMİN Pirinci',
                           'calories_per_100':346,'protein_per_100':7.6,'carbs_per_100':77,'fat_per_100':0.5,
                           'base_unit':'100g_raw','is_raw':1,'source':'etiket',
                           'aliases':'pirinç,pirinc,yasmin,yasmin pirinci,yasemin,jasmin rice,rice'}),
        ('Patates',       {'product_id':'PROD-011','official_name':'Mączyste Patates','name':'Mączyste Patates',
                           'calories_per_100':77,'protein_per_100':2,'carbs_per_100':17,'fat_per_100':0.1,
                           'base_unit':'100g_raw','is_raw':1,'source':'manual',
                           'aliases':'patates,potato,maczyste,mączyste patates'}),
        ('Skyr Yogurt',   {'product_id':'PROD-013','official_name':'Skyr Yoğurt','name':'Skyr Yoğurt',
                           'calories_per_100':64,'protein_per_100':12,'carbs_per_100':4.1,'fat_per_100':0,
                           'base_unit':'100g','is_raw':0,'source':'etiket',
                           'aliases':'yoğurt,yogurt,skyr,skyr yogurt'}),
        ('Sıvı Yumurta Beyazı', {'product_id':'PROD-006','official_name':'Sıvı Yumurta Akı','name':'Sıvı Yumurta Akı',
                                  'calories_per_100':58,'protein_per_100':10.3,'carbs_per_100':1.2,'fat_per_100':0.8,
                                  'base_unit':'100g','is_raw':0,'source':'etiket',
                                  'aliases':'syb,sıvı yumurta,liquid egg white,egg white,sivi yumurta,sıvı yumurta akı,likit yumurta'}),
        ('Kakao',         {'product_id':'PROD-009','official_name':'Kakao','name':'Kakao',
                           'calories_per_100':309,'protein_per_100':24,'carbs_per_100':13,'fat_per_100':11,
                           'base_unit':'100g','is_raw':0,'source':'etiket','aliases':'cocoa,cacao,kakao'}),
        ('Carrefour Tost Ekmeği', {'product_id':'PROD-007','official_name':'Carrefour Tam Tahıllı Tost Ekmeği',
                                    'name':'Carrefour Tam Tahıllı Tost Ekmeği',
                                    'calories_per_100':252,'protein_per_100':9.5,'carbs_per_100':45,'fat_per_100':2.1,
                                    'base_unit':'100g','serving_size':23,'is_raw':0,'source':'etiket',
                                    'aliases':'tost ekmeği,ekmek,carrefour tost,tost,tost ekmegi,tam tahıllı tost'}),
        ('Donmus Patates',{'product_id':'PROD-001','official_name':'Dondurulmuş Patates','name':'Dondurulmuş Patates',
                           'calories_per_100':99,'protein_per_100':1.9,'carbs_per_100':15,'fat_per_100':3.1,
                           'base_unit':'100g','is_raw':0,'source':'etiket',
                           'aliases':'donmuş patates,donmus patates,frozen potato,kartofle'}),
        ('GymBeam Sprey Yag', {'product_id':'PROD-002','official_name':'GymBeam Sprey Yağ','name':'GymBeam Sprey Yağ',
                                'calories_per_100':15,'protein_per_100':0,'carbs_per_100':0,'fat_per_100':1.65,
                                'base_unit':'1fis','serving_size':1,'serving_unit':'fıs','is_raw':0,'source':'etiket',
                                'aliases':'gymbeam,sprey yağ,sprey yag,olive oil spray,fıs,yağ'}),
        ('Şekersiz Badem Sütü', {'product_id':'PROD-003','official_name':'Şekersiz Badem Sütü',
                                   'calories_per_100':14,'protein_per_100':0.5,'carbs_per_100':0,'fat_per_100':1.1,
                                   'base_unit':'100ml','is_raw':0,'source':'etiket',
                                   'aliases':'şekersiz badem sütü,badem sutu,almond milk,sekersiz badem sutu'}),
        ('Kornişon Turşu', {'product_id':'PROD-004','official_name':'Salatalık Turşusu','name':'Salatalık Turşusu',
                             'calories_per_100':18,'protein_per_100':0.9,'carbs_per_100':1.92,'fat_per_100':0,
                             'base_unit':'100g','is_raw':0,'source':'etiket',
                             'aliases':'turşu,kornişon,pickle,tursu,kornison,salatalık turşusu'}),
        ('Keto Ketçap',   {'product_id':'PROD-005','official_name':'Keto Ketçap',
                           'calories_per_100':41,'protein_per_100':2,'carbs_per_100':6.2,'fat_per_100':0.5,
                           'base_unit':'100g','is_raw':0,'source':'etiket','aliases':'ketçap,keto ketçap,ketchup,ketcap'}),
        ('Carrefour BIO Yumurta', {'product_id':'PROD-008','official_name':'Carrefour BIO Yumurta',
                                    'calories_per_100':137,'protein_per_100':12.4,'carbs_per_100':1.3,'fat_per_100':9.1,
                                    'base_unit':'1adet','serving_size':1,'serving_unit':'adet','is_raw':0,'source':'manual',
                                    'aliases':'yumurta,carrefour yumurta,bio yumurta,tam yumurta,carrefour bio'}),
        ('Go On Nutrition Protein 33% Bar Sutlu', {'product_id':'PROD-012','official_name':'Çikolatalı Protein Bar 33%',
                                                    'name':'Çikolatalı Protein Bar 33%',
                                                    'calories_per_100':386,'protein_per_100':33,'carbs_per_100':23,'fat_per_100':18,
                                                    'base_unit':'1bar','serving_size':50,'serving_unit':'g','is_raw':0,'source':'etiket',
                                                    'aliases':'protein bar,go on,go on nutrition,bar,çikolatalı protein bar,1 bar'}),
        ('Tavuk Baharati', {'product_id':'PROD-014','official_name':'Tavuk Baharatı','name':'Tavuk Baharatı',
                            'calories_per_100':286,'protein_per_100':18.1,'carbs_per_100':50.4,'fat_per_100':8.2,
                            'base_unit':'100g','serving_size':5,'serving_unit':'g','is_raw':0,'source':'etiket',
                            'aliases':'tavuk baharatı,baharat,tavuk baharati'}),
    ]
    updated = []
    for match_name, upd in SPEC:
        row = conn.execute("SELECT id FROM food_registry WHERE name=?", (match_name,)).fetchone()
        if row:
            sets = ', '.join(f"{k}=?" for k in upd)
            vals = list(upd.values()) + [row['id']]
            conn.execute(f"UPDATE food_registry SET {sets} WHERE id=?", vals)
            updated.append(upd.get('official_name', match_name))
        else:
            updated.append(f'MISSING:{match_name}')
    conn.commit(); conn.close()
    return jsonify({'ok':True, 'updated': updated})

# âââ SUPPLEMENT SYSTEM âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def ensure_supplement_tables():
    """Supplement tablolarinin varligini garanti et."""
    conn = get_db()
    cols_products = {r['name'] for r in conn.execute("PRAGMA table_info(supplement_products)").fetchall()}
    cols_stacks   = {r['name'] for r in conn.execute("PRAGMA table_info(supplement_stacks)").fetchall()}
    # Tables created in init_db; here we just verify
    conn.close()

def seed_supplement_data():
    """Master Spec v1.0 supplement ürünleri ve stacklerini yükle (yoksa)."""
    conn = get_db()
    # Check if already seeded
    existing = conn.execute("SELECT COUNT(*) as c FROM supplement_stacks").fetchone()['c']
    if existing > 0:
        conn.close()
        return

    # Products
    PRODUCTS = [
        ('Whey Protein',                   '',                    'olcek',   1, 'ölçek'),
        ('NOW NAC 600mg',                  'NOW',                 'kapsul',  1, 'kapsul'),
        ('Garden of Life Probiotic',       'Garden of Life',      'kapsul',  1, 'kapsul'),
        ('Life Extension Mega EPA/DHA (Omega-3)', 'Life Extension', 'kapsul', 3, 'kapsul'),
        ('Thorne Vitamin D + K2',          'Thorne',              'damla',   4, 'damla'),
        ('Life Extension B-Complex',       'Life Extension',      'kapsul',  1, 'kapsul'),
        ('Life Extension MacuGuard',       'Life Extension',      'kapsul',  1, 'kapsul'),
        ('California Gold Nutrition C',    'California Gold Nutrition', 'kapsul', 1, 'kapsul'),
        ('NOW Magtein',                    'NOW',                 'kapsul',  1, 'kapsul'),
        ('NOW L-Theanine Double Strength', 'NOW',                 'kapsul',  1, 'kapsul'),
        ('Optimum Nutrition Collagen',     'Optimum Nutrition',   'olcek',   1, 'ölçek'),
        ('NOW Zinc Picolinate 50mg',       'NOW',                 'kapsul',  1, 'kapsul'),
        ('Elektrolit',                     '',                    'g',       8, 'g'),
        ('Citrulline',                     '',                    'g',       8, 'g'),
        ('Taurine',                        '',                    'g',       2, 'g'),
        ('Beta Alanine',                   '',                    'g',       2, 'g'),
        ('Creatine Monohydrate',           '',                    'g',       5, 'g'),
        ('Magnesium Glycinate',            '',                    'kapsul',  3, 'kapsul'),
        ('KSM-66 Ashwagandha',            '',                    'kapsul',  1, 'kapsul'),
        ('Glycine',                        '',                    'kapsul',  3, 'kapsul'),
        ('Melatonin',                      '',                    'kapsul',  3, 'kapsul'),
    ]
    for name, brand, form, dose, unit in PRODUCTS:
        try:
            conn.execute("INSERT INTO supplement_products (name,brand,form,default_dose,default_unit) VALUES (?,?,?,?,?)",
                         (name, brand, form, dose, unit))
        except: pass

    # Stacks
    STACKS = [
        ('Aç Karna Stack',   'fasted',     1, [
            ('NOW NAC 600mg',                  1, 'kapsul'),
            ('Garden of Life Probiotic',       1, 'kapsul'),
        ]),
        ('Sabah Stack',      'morning',    2, [
            ('Life Extension Mega EPA/DHA (Omega-3)', 3, 'kapsul'),
            ('Thorne Vitamin D + K2',          4, 'damla'),
            ('Life Extension B-Complex',       1, 'kapsul'),
            ('Life Extension MacuGuard',       1, 'kapsul'),
            ('California Gold Nutrition C',    1, 'kapsul'),
            ('NOW Magtein',                    1, 'kapsul'),
            ('NOW L-Theanine Double Strength', 1, 'kapsul'),
            ('Optimum Nutrition Collagen',     1, 'ölçek'),
            ('NOW Zinc Picolinate 50mg',       1, 'kapsul'),
        ]),
        ('Pre Workout Stack','preworkout', 3, [
            ('Elektrolit',    8, 'g'),
            ('Citrulline',    8, 'g'),
            ('Taurine',       2, 'g'),
            ('Beta Alanine',  2, 'g'),
        ]),
        ('Post Workout Stack','postworkout',4, [
            ('Creatine Monohydrate', 5, 'g'),
            ('Whey Protein',         1, 'ölçek'),
        ]),
        ('Gece Stack',       'night',      5, [
            ('Magnesium Glycinate',            3, 'kapsul'),
            ('KSM-66 Ashwagandha',            1, 'kapsul'),
            ('Glycine',                        3, 'kapsul'),
            ('Melatonin',                      3, 'kapsul'),
            ('NOW L-Theanine Double Strength', 1, 'kapsul'),
        ]),
    ]
    for sname, cat, order, items in STACKS:
        conn.execute("INSERT INTO supplement_stacks (name,category,active,order_num) VALUES (?,?,1,?)",
                     (sname, cat, order))
        sid = conn.execute("SELECT id FROM supplement_stacks WHERE name=?", (sname,)).fetchone()['id']
        for i, (pname, dose, unit) in enumerate(items):
            conn.execute("INSERT INTO supplement_stack_items (stack_id,product_name,dose,unit,order_num) VALUES (?,?,?,?,?)",
                         (sid, pname, dose, unit, i+1))

    # Rules
    conn.execute("INSERT OR IGNORE INTO supplement_rules (product_name,rule_type,rule_data) VALUES (?,?,?)",
                 ('NOW Zinc Picolinate 50mg', 'every_other_day', '{}'))

    conn.commit()
    conn.close()

def fix_mojibake_supplement_names():
    """Onceki bozuk-encoding ile seed edilmis kayitlari duzelt (ornek: 'Aç Karna Stack')."""
    FIXES = {
        'AÃ§ Karna Stack': 'Aç Karna Stack',
    }
    UNIT_FIXES = {
        'Ã¶lÃ§ek': 'ölçek',
    }
    NAME_FIXES = {
        # daha net/tanidik isim (kullanici "omega 3" olarak arayacak)
        'Life Extension Mega EPA/DHA': 'Life Extension Mega EPA/DHA (Omega-3)',
    }
    conn = get_db()
    try:
        for bad, good in FIXES.items():
            conn.execute("UPDATE supplement_stacks SET name=? WHERE name=?", (good, bad))
        for bad, good in UNIT_FIXES.items():
            conn.execute("UPDATE supplement_stack_items SET unit=? WHERE unit=?", (good, bad))
            conn.execute("UPDATE supplement_products SET default_unit=? WHERE default_unit=?", (good, bad))
        for bad, good in NAME_FIXES.items():
            conn.execute("UPDATE supplement_products SET name=? WHERE name=?", (good, bad))
            conn.execute("UPDATE supplement_stack_items SET product_name=? WHERE product_name=?", (good, bad))
        # Whey Protein onceki seed'lerde yoktu - eksikse kataloga ve Post Workout Stack'e ekle
        has_whey = conn.execute("SELECT id FROM supplement_products WHERE name='Whey Protein'").fetchone()
        if not has_whey:
            conn.execute("INSERT INTO supplement_products (name,brand,form,default_dose,default_unit) VALUES (?,?,?,?,?)",
                         ('Whey Protein', '', 'olcek', 1, 'ölçek'))
        pw_stack = conn.execute("SELECT id FROM supplement_stacks WHERE name='Post Workout Stack'").fetchone()
        if pw_stack:
            has_whey_item = conn.execute(
                "SELECT id FROM supplement_stack_items WHERE stack_id=? AND product_name='Whey Protein'",
                (pw_stack['id'],)).fetchone()
            if not has_whey_item:
                max_order = conn.execute(
                    "SELECT COALESCE(MAX(order_num),0) AS m FROM supplement_stack_items WHERE stack_id=?",
                    (pw_stack['id'],)).fetchone()['m']
                conn.execute(
                    "INSERT INTO supplement_stack_items (stack_id,product_name,dose,unit,order_num) VALUES (?,?,?,?,?)",
                    (pw_stack['id'], 'Whey Protein', 1, 'ölçek', max_order + 1))
        conn.commit()
    except Exception as e:
        log.warning(f"fix_mojibake_supplement_names failed: {e}")
    finally:
        conn.close()

def import_real_besin_supplement_db():
    """Kullanicinin verdigi gercek Besin DB + Supplement DB verisini yukle (upsert by name).
    Onceki yer-tutucu (placeholder) supplement stack'lerinin yerini alir, food_registry'ye
    eksik urunleri ekler/gunceller. Loglar (vitamin_logs/supplement_log_items) urun adini
    snapshot olarak tuttugu icin bu degisiklik gecmis kayitlari etkilemez."""
    conn = get_db()
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(food_registry)").fetchall()}
    if 'quick_amounts' not in cols:
        conn.execute("ALTER TABLE food_registry ADD COLUMN quick_amounts TEXT DEFAULT ''")

    CAT_LABELS = {'protein':'Protein','sebze':'Sebze','meyve':'Meyve','tahil':'Tahıl',
                  'sut':'Süt Ürünü','yag':'Yağ','sos':'Sos/Baharat','atistirmalik':'Atıştırmalık'}

    FOODS = [
        ('Alpro Badem Sütü Şekersiz', 24, 0.4, 3.1, 1.1, 'ml', 'sut', 'Şekersiz. 100ml=24kcal P0.4 K3.1 Y1.1', [100,200,250]),
        ('Badem', 606, 20, 7.6, 52, 'g', 'atistirmalik', 'Almond entry restored from old blank record.', [10,30,100]),
        ('Carrefour BIO Yumurta', 137, 12.4, 1.3, 9.1, 'g', 'protein', '[ETIKETLI] Kullanici etiketi: 100g = 137 kcal, P12.4, K1.3, Y9.1.', [50,100,150]),
        ('Carrefour Tam Tahıllı Tost Ekmeği', 252, 9.5, 45, 2.1, 'g', 'tahil', '1 kromka ~23g / etiket: 252kcal P9.5 K45 Y2.1 per 100g', [23,46,69]),
        ('Domates', 18, 0.9, 3.9, 0.2, 'g', 'sebze', 'Genel domates - çeri/salkım/normal', [100,150,200]),
        ('Genç Patates', 70, 1.9, 15, 0.1, 'g', 'sebze', 'Ziemniaki Młode - Carrefour, çiğ ağırlık, airfryer uygun', [100,150,200]),
        ('GymBeam Sprey Yağ', 1500, 0, 0, 165, 'fış', 'yag', '[KULLANICI ONAYLI] 1 fış/basış = 15 kcal, 1.65Y.', [1,2,5]),
        ('Hindi Göğüs', 104, 22, 0, 2, 'g', 'protein', 'Carrefour taze hindi göğüs fileto. Ham çiğ.', [100,150,200]),
        ('Kaju', 553, 18, 30, 44, 'g', 'atistirmalik', 'Çiğ kaju', [10,30,100]),
        ('Kakao', 309, 24, 13, 11, 'g', 'atistirmalik', 'Saf kakao tozu şekersiz', [5,10,15]),
        ('Karışık Yeşillik', 20, 2, 3, 0.3, 'g', 'sebze', 'Marul roka ıspanak', [50,100,150]),
        ('Keto Ketçap', 41, 2, 6.2, 0.5, 'g', 'sos', 'Şekersiz keto / etiket: P2.0 K6.2 Y<0.5 per 100g', [5,10,20]),
        ('Kırmızı Elma', 52, 0.3, 14, 0.2, 'g', 'meyve', '', [100,150,200]),
        ('Marul', 14, 1.4, 2.9, 0.2, 'g', 'sebze', 'İç marul / iceberg.', [50,100,150]),
        ('Muz', 89, 1.1, 23, 0.3, 'g', 'meyve', '', [100,120,200]),
        ('Salatalık', 15, 0.7, 3.6, 0.1, 'g', 'sebze', '', [100,150,200]),
        ('Skyr Yoğurt', 64, 12, 4.1, 0, 'g', 'sut', '', [100,150,200]),
        ('Tavuk Baharatı', 286, 18.1, 50.4, 8.2, 'g', 'sos', '', [3,5,10]),
        ('Tavuk Gogsu', 115, 23, 0, 1.5, 'g', 'protein', 'Kullanıcı onaylı: 100g çiğ tavuk göğsü = 115 kcal, P23, K0, Y1.5', [100,150,200]),
        ('Turşu', 11, 0.8, 1.5, 0.1, 'g', 'sebze', 'Salatalık turşusu.', [50,100,150]),
        ('Valio PROfeel Protein Pudding Chocolate', 85, 11, 6.2, 1.6, 'g', 'sut', '[ETIKETLI/FOTOGRAF] 1 kap ~182g: ~155 kcal, P20, K11.3, Y2.9.', [182]),
        ('Yasmin Pirinc', 360, 7, 79, 0.6, 'g', 'tahil', 'Kuru çiğ. Kullanıcı onaylı: 100g = 360 kcal, P7, K79, Y0.6', [60,80,100]),
        ('Yulaf', 371, 13, 58, 7, 'g', 'tahil', 'Tam yulaf', [20,40,60]),
        ('Yumurta Akı', 58, 10.3, 1.2, 0.8, 'g', 'protein', 'etiket: 58kcal P10.3 K1.2 Y0.8 per 100g', [100,150,250]),
        ('Çikolatalı Protein Bar 33%', 386, 33, 23, 18, 'g', 'atistirmalik', '[ETIKETLI/FOTOGRAF] 1 paket = 50g: 193 kcal, P16.5, K11.5, Y9.', [50]),
        ('Çilek', 32, 0.7, 7.7, 0.3, 'g', 'meyve', '', [100,150,200]),
    ]
    for name, kcal, prot, carb, fat, unit, cat_key, note, quick in FOODS:
        cat = CAT_LABELS.get(cat_key, '')
        qa = json.dumps(quick)
        existing = conn.execute("SELECT id FROM food_registry WHERE name=?", (name,)).fetchone()
        if existing:
            conn.execute("""UPDATE food_registry SET calories_per_100=?,protein_per_100=?,carbs_per_100=?,
                            fat_per_100=?,unit=?,category=?,notes=?,quick_amounts=? WHERE id=?""",
                         (kcal, prot, carb, fat, unit, cat, note, qa, existing['id']))
        else:
            conn.execute("""INSERT INTO food_registry (name,calories_per_100,protein_per_100,carbs_per_100,fat_per_100,unit,category,notes,quick_amounts)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (name, kcal, prot, carb, fat, unit, cat, note, qa))
    conn.commit()

    # Ogun sablonu: Kahvalti Stack
    kahvalti_items = [('Carrefour BIO Yumurta', 100, 'g'), ('Carrefour Tam Tahıllı Tost Ekmeği', 46, 'g'), ('Domates', 100, 'g')]
    ms = conn.execute("SELECT id FROM meal_stacks WHERE name='Kahvaltı Stack'").fetchone()
    if not ms:
        cur = conn.execute("INSERT INTO meal_stacks (name) VALUES (?)", ('Kahvaltı Stack',))
        msid = cur.lastrowid
        for i, (n, a, u) in enumerate(kahvalti_items):
            food = conn.execute("SELECT id FROM food_registry WHERE name=?", (n,)).fetchone()
            conn.execute("INSERT INTO meal_stack_items (stack_id,food_id,food_name,amount,unit,order_num) VALUES (?,?,?,?,?,?)",
                         (msid, food['id'] if food else None, n, a, u, i))
        conn.commit()

    # Supplement products (22 - gercek marka isimleri)
    PRODUCTS = [
        ('NOW NAC 600mg', 1, 'kapsül'), ('Garden of Life Probiyotik', 1, 'doz'),
        ('Optimum Nutrition Collagen Peptides', 1, 'ölçek'), ('Thorne Vitamin D + K2', 1, 'damla'),
        ('Life Extension Mega EPA/DHA', 1, 'kapsül'), ('NOW Magtein Magnesium L-Threonate', 1, 'kapsül'),
        ('Life Extension MacuGuard with Saffron', 1, 'kapsül'), ('Life Extension BioActive Complete B-Complex', 1, 'kapsül'),
        ('California Gold Nutrition C 1000mg', 1, 'tablet'), ('NOW L-Theanine Double Strength', 1, 'kapsül'),
        ('NOW Zinc Picolinate 50mg', 1, 'kapsül'), ('NOW Astaxanthin 10mg', 1, 'kapsül'),
        ('NOW Magnesium Glycinate', 1, 'kapsül'), ('NOW Melatonin 1mg', 1, 'tablet'),
        ('NOW Glycine 1000mg', 1, 'kapsül'), ('KSM-66 Ashwagandha', 1, 'kapsül'), ('L-Theanine', 1, 'kapsül'),
        ('5% Nutrition L-Citrulline 3000', 1, 'ölçek'), ('KFD Premium Beta-Alanin', 1, 'ölçek'),
        ('Optimum Nutrition Elektrolit', 1, 'ölçek'), ('Swedish Supplements Taurine', 1, 'ölçek'),
        ('KFD Creatine Monohydrate', 1, 'ölçek'),
    ]
    for name, dose, unit in PRODUCTS:
        existing = conn.execute("SELECT id FROM supplement_products WHERE name=?", (name,)).fetchone()
        if not existing:
            conn.execute("INSERT INTO supplement_products (name,default_dose,default_unit) VALUES (?,?,?)", (name, dose, unit))
    conn.commit()

    # Eski genel/yer-tutucu urun isimleri artik daha spesifik gercek marka isimleriyle var - eskisini sil
    SUPERSEDED_PRODUCT_NAMES = [
        'Garden of Life Probiotic', 'Life Extension Mega EPA/DHA (Omega-3)', 'Life Extension B-Complex',
        'Life Extension MacuGuard', 'California Gold Nutrition C', 'NOW Magtein', 'Elektrolit',
        'Citrulline', 'Taurine', 'Beta Alanine', 'Creatine Monohydrate', 'Magnesium Glycinate',
        'Glycine', 'Melatonin', 'Optimum Nutrition Collagen',
    ]
    for old_name in SUPERSEDED_PRODUCT_NAMES:
        conn.execute("DELETE FROM supplement_products WHERE name=?", (old_name,))
    conn.commit()

    # Eski yer-tutucu stack'leri sil, gercek 5 stack ile degistir (loglar snapshot oldugu icin gecmis etkilenmez)
    OLD_PLACEHOLDER_STACKS = ['Aç Karna Stack', 'Sabah Stack', 'Pre Workout Stack', 'Post Workout Stack', 'Gece Stack']
    for old_name in OLD_PLACEHOLDER_STACKS:
        row = conn.execute("SELECT id FROM supplement_stacks WHERE name=?", (old_name,)).fetchone()
        if row:
            conn.execute("DELETE FROM supplement_stack_items WHERE stack_id=?", (row['id'],))
            conn.execute("DELETE FROM supplement_stacks WHERE id=?", (row['id'],))
    conn.commit()

    STACKS_REAL = [
        ('Aç Karna', 1, [
            ('NOW NAC 600mg', 1, 'kapsül'), ('Garden of Life Probiyotik', 1, 'doz'),
        ]),
        ('Sabah/Kahvaltı', 2, [
            ('Optimum Nutrition Collagen Peptides', 1, 'ölçek'), ('Thorne Vitamin D + K2', 1, 'damla'),
            ('Life Extension Mega EPA/DHA', 1, 'kapsül'), ('NOW Magtein Magnesium L-Threonate', 1, 'kapsül'),
            ('Life Extension MacuGuard with Saffron', 1, 'kapsül'), ('Life Extension BioActive Complete B-Complex', 1, 'kapsül'),
            ('California Gold Nutrition C 1000mg', 1, 'tablet'), ('NOW L-Theanine Double Strength', 1, 'kapsül'),
            ('NOW Zinc Picolinate 50mg', 1, 'kapsül'), ('NOW Astaxanthin 10mg', 1, 'kapsül'),
        ]),
        ('Gece', 3, [
            ('NOW Magnesium Glycinate', 1, 'kapsül'), ('NOW Melatonin 1mg', 1, 'tablet'),
            ('NOW Glycine 1000mg', 1, 'kapsül'), ('KSM-66 Ashwagandha', 1, 'kapsül'), ('L-Theanine', 1, 'kapsül'),
        ]),
        ('Pre-workout', 4, [
            ('5% Nutrition L-Citrulline 3000', 1, 'ölçek'), ('KFD Premium Beta-Alanin', 1, 'ölçek'),
            ('Optimum Nutrition Elektrolit', 1, 'ölçek'), ('Swedish Supplements Taurine', 1, 'ölçek'),
        ]),
        ('Post-workout', 5, [
            ('KFD Creatine Monohydrate', 1, 'ölçek'),
        ]),
    ]
    for sname, order, items in STACKS_REAL:
        existing = conn.execute("SELECT id FROM supplement_stacks WHERE name=?", (sname,)).fetchone()
        if existing:
            continue
        cur = conn.execute("INSERT INTO supplement_stacks (name,category,active,order_num) VALUES (?,?,1,?)", (sname, 'custom', order))
        sid = cur.lastrowid
        for j, (pname, dose, unit) in enumerate(items):
            conn.execute("INSERT INTO supplement_stack_items (stack_id,product_name,dose,unit,order_num) VALUES (?,?,?,?,?)",
                         (sid, pname, dose, unit, j))
    conn.commit()
    conn.close()

try:
    seed_supplement_data()
    fix_mojibake_supplement_names()
    import_real_besin_supplement_db()
except Exception as e:
    import logging; logging.getLogger('daily').warning(f"supplement seed failed: {e}")

# ââ API ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.route('/api/supplements/stacks', methods=['GET'])
def api_supplement_stacks():
    conn = get_db()
    stacks = [dict(r) for r in conn.execute(
        "SELECT * FROM supplement_stacks WHERE active=1 ORDER BY order_num, name").fetchall()]
    for s in stacks:
        items = conn.execute(
            "SELECT id, product_name, dose, unit FROM supplement_stack_items WHERE stack_id=? ORDER BY order_num, id",
            (s['id'],)).fetchall()
        s['items'] = [dict(i) for i in items]
    conn.close()
    return jsonify(stacks)


@app.route('/api/supplements/reorder', methods=['POST'])
def api_supplements_reorder():
    """Log'daki Takviye Sirasi kartindan stack/urun sirasini kalicilastirir.
    {stack_ids:[...]} tum stack sirasini, {stack_id, item_ids:[...]} bir stack'in
    urun sirasini yeni listedeki konuma gore yazar."""
    data = request.get_json(force=True) or {}
    conn = get_db()
    if isinstance(data.get('stack_ids'), list):
        for i, sid in enumerate(data['stack_ids'], start=1):
            conn.execute("UPDATE supplement_stacks SET order_num=? WHERE id=?", (i, int(sid)))
    if data.get('stack_id') and isinstance(data.get('item_ids'), list):
        for i, iid in enumerate(data['item_ids'], start=1):
            conn.execute("UPDATE supplement_stack_items SET order_num=? WHERE id=? AND stack_id=?",
                         (i, int(iid), int(data['stack_id'])))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/supplements/today', methods=['GET'])
def api_supplements_today():
    today = request.args.get('date', operation_today())
    conn = get_db()
    logs = [dict(r) for r in conn.execute(
        "SELECT * FROM supplement_logs WHERE date=? ORDER BY ts", (today,)).fetchall()]
    for log in logs:
        items = conn.execute(
            "SELECT * FROM supplement_log_items WHERE log_id=?", (log['id'],)).fetchall()
        log['items'] = [dict(i) for i in items]
    # Zinc programi: Pzt/Car/Cum (ZINC_DAYS). last_date bilgi amacli vitamin_logs'tan.
    zrow = conn.execute(
        "SELECT date FROM vitamin_logs WHERE name=? AND date<=? ORDER BY date DESC, id DESC LIMIT 1",
        (ZINC_PRODUCT_NAME, today)).fetchone()
    conn.close()
    last_zinc = zrow['date'] if zrow else None
    zinc_today = date.fromisoformat(today).weekday() in ZINC_DAYS
    zinc_rest = not zinc_today                     # cinko gunu degil -> eksik sayilmaz
    return jsonify({'date': today, 'logs': logs,
                    'zinc': {'take_today': zinc_today, 'rest_today': zinc_rest, 'last_date': last_zinc}})

_VIT_STATUS_KEYS = {'taken', 'missed', 'eod_skipped', 'eod_taken', 'half_dose', 'on_break'}

def vit_status_of(status, notes):
    """templates/index.html:3029 vitStatusOf()'un sunucu tarafi birebir eşleniği -
    Telegram/web loglarindaki eski status'suz kayitlarin ayni sekilde yorumlanmasi icin."""
    if status and status in _VIT_STATUS_KEYS:
        return status
    raw = (notes or '').lower()
    is_gun = bool(re.search(r'gün aşırı|gun asiri', raw))
    is_alindi = bool(re.search(r'al[ıi]nd[ıi]', raw))
    if re.search(r'eksik al[ıi]nd[ıi]|alinmadi|alınmadı', raw):
        return 'missed'
    if is_gun and is_alindi:
        return 'eod_taken'
    if is_gun:
        return 'eod_skipped'
    if re.search(r'yar[ıi]m', raw):
        return 'half_dose'
    return 'taken'


def backfill_vitamin_status():
    """status'u bos eski vitamin_logs kayitlarina vit_status_of sonucunu yazar (idempotent,
    her boot). SUPP_TAKEN_COUNT_SQL gibi SQL sayaclari '' status'lu satirlari goremiyordu -
    web 'stack alindi' butonu eskiden status yazmadigi icin o gunler 0/23 sayiliyordu."""
    conn = get_db()
    rows = conn.execute("SELECT id, notes FROM vitamin_logs WHERE status IS NULL OR status=''").fetchall()
    for r in rows:
        conn.execute("UPDATE vitamin_logs SET status=? WHERE id=?", (vit_status_of('', r['notes']), r['id']))
    if rows:
        conn.commit()
        log.info("vitamin_logs status backfill: %d satir dolduruldu", len(rows))
    conn.close()


backfill_vitamin_status()


@app.route('/api/supplements/breaks', methods=['GET'])
def api_supplements_breaks():
    """Su an aktif olan 'ara veriliyor' durumlarini doner (stack veya urun bazli).
    Dashboard/Ozet gibi ayri render yollarinin hepsi bunu kullanabilsin diye tek yer."""
    expire_due_supplement_breaks()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, target_type, target_name, since_date, end_date, note FROM supplement_breaks WHERE active=1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/supplements/breaks', methods=['POST'])
def api_supplements_break_start():
    """Ayarlar sayfasindan manuel 'ara ver' - Telegram'daki tg_supplement_break_actions_from_text
    ile ayni start_supplement_break() fonksiyonunu kullanir, davranis birebir ayni."""
    data = request.get_json(force=True) or {}
    ttype = (data.get('target_type') or '').strip()
    tname = (data.get('target_name') or '').strip()
    if ttype not in ('stack', 'product') or not tname:
        return jsonify({'ok': False, 'error': "target_type ('stack' veya 'product') ve target_name gerekli"}), 400
    start_supplement_break(ttype, tname, data.get('since_date'), data.get('note') or 'Ayarlar sayfasından manuel',
                           end_date=(data.get('end_date') or '').strip() or None)
    return jsonify({'ok': True})

@app.route('/api/supplements/breaks/end', methods=['POST'])
def api_supplements_break_end():
    """Ayarlar sayfasindan manuel 'tekrar basla' - karsi granulariteyi de kapatan
    end_supplement_break() ile ayni, Telegram'daki 'tekrar basladim' ile birebir tutarli."""
    data = request.get_json(force=True) or {}
    ttype = (data.get('target_type') or '').strip()
    tname = (data.get('target_name') or '').strip()
    if ttype not in ('stack', 'product') or not tname:
        return jsonify({'ok': False, 'error': "target_type ('stack' veya 'product') ve target_name gerekli"}), 400
    end_supplement_break(ttype, tname)
    return jsonify({'ok': True})

@app.route('/api/supplements/range', methods=['GET'])
def api_supplements_range():
    """Tarih araligi icin, her gun icin her aktif stack'in taken/total + item-bazli durumunu doner.
    Kaynak vitamin_logs (name==supplement_stack_items.product_name birebir eslesme) - hem web UI'nin
    hem Telegram botunun gercekte yazdigi tablo bu, supplement_logs/supplement_log_items degil
    (o tablolar sadece web UI'nin 'stack alindi' butonuyla doluyor, Telegram onlari hic yazmiyor)."""
    start = request.args.get('start')
    end = request.args.get('end', start)
    if not start:
        return jsonify({'error': 'start is required'}), 400
    expire_due_supplement_breaks()
    conn = get_db()
    stacks = [dict(r) for r in conn.execute(
        "SELECT * FROM supplement_stacks WHERE active=1 ORDER BY order_num, name").fetchall()]
    for s in stacks:
        items = conn.execute(
            "SELECT product_name, dose, unit FROM supplement_stack_items WHERE stack_id=? ORDER BY order_num",
            (s['id'],)).fetchall()
        s['items'] = [dict(i) for i in items]

    vlogs = conn.execute(
        "SELECT date, name, status, notes FROM vitamin_logs WHERE date>=? AND date<=?", (start, end)).fetchall()
    # TUM break'ler (kapali olanlar dahil): kapanan bir ara, kendi tarih araligindaki gecmis
    # gunlerde 'ara verildi' olarak gorunmeye DEVAM etmeli - eskiden sadece active=1 okununca
    # ara bitince tarihsel gunler geriye donuk 'kacirilmis'a donuyordu; end_date sinirsiz
    # sayildigi icin gelecek gunler de yanlis 'ara veriliyor' cikiyordu.
    breaks = conn.execute(
        "SELECT target_type, target_name, since_date, end_date, active, ended_at FROM supplement_breaks").fetchall()
    conn.close()
    # (date, name) -> log listesi; web loglari notes'ta 'stack:<ad>' tasir. Ayni urun iki
    # stack'te olunca (NAC, L-Theanine) once ayni stack'e etiketli log aranir - eskiden tek
    # sabah NAC kaydi Gece stack'ini de 'alindi' gosteriyordu. Etiketsiz (Telegram) loglar
    # fallback olarak her iki stack'e de sayilir (bilinen sinirlama).
    by_date_name = {}
    for r in vlogs:
        nt = r['notes'] or ''
        by_date_name.setdefault((r['date'], r['name']), []).append({
            'status': vit_status_of(r['status'], r['notes']),
            'stack': nt[6:].strip() if nt.startswith('stack:') else None,
        })

    def _pick_log(logs_, stack_name):
        tagged = [l for l in logs_ if l['stack'] == stack_name]
        untagged = [l for l in logs_ if l['stack'] is None]
        pool = tagged if tagged else untagged
        for l in pool:
            if l['status'] in ('taken', 'eod_taken', 'half_dose'):
                return l
        return pool[0] if pool else None

    def _break_map(rows_):
        m = {}
        for r in rows_:
            m.setdefault(r['target_name'], []).append({
                'since': r['since_date'] or '0000-00-00', 'end': r['end_date'],
                'active': bool(r['active']), 'ended_at': r['ended_at'],
            })
        return m
    broken_stacks = _break_map([r for r in breaks if r['target_type'] == 'stack'])
    broken_products = _break_map([r for r in breaks if r['target_type'] == 'product'])

    def _covering_break(brs, ds):
        """ds gununu kapsayan break kaydini doner. Pencere: since <= ds <= end_date (varsa);
        elle bitirilen aralarda (active=0, ended_at) bitis gunu HARIC - o gun tekrar baslanmistir."""
        for b in brs or []:
            if ds < b['since']:
                continue
            if b['end'] and ds > b['end']:
                continue
            if not b['active'] and not (b['ended_at'] and ds < b['ended_at']):
                continue
            return b
        return None


    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    days = []
    d = d0
    while d <= d1:
        ds = d.isoformat()
        day_stacks = []
        for s in stacks:
            stack_break = _covering_break(broken_stacks.get(s['name']), ds)
            items_out = []
            taken_n = 0
            total_n = 0
            item_break_ends = []
            for it in s['items']:
                br = _covering_break(broken_products.get(it['product_name']), ds) or stack_break
                on_break = br is not None
                if on_break:
                    ui_status = 'on_break'
                    item_break_ends.append(br['end'])
                else:
                    chosen = _pick_log(by_date_name.get((ds, it['product_name'])) or [], s['name'])
                    st = chosen['status'] if chosen else None
                    if (it['product_name'] == ZINC_PRODUCT_NAME and st is None
                            and d.weekday() not in ZINC_DAYS):
                        # cinko gunu degil (Pzt/Car/Cum disi) -> beklenmez, toplam sayilmaz
                        items_out.append({'name': it['product_name'], 'dose': it['dose'],
                                          'unit': it['unit'], 'status': 'eod_rest'})
                        continue
                    total_n += 1
                    if st in ('taken', 'eod_taken', 'half_dose'):
                        taken_n += 1
                        ui_status = st
                    elif st in ('missed', 'eod_skipped'):
                        ui_status = st
                    else:
                        ui_status = 'pending'
                item_out = {'name': it['product_name'], 'dose': it['dose'], 'unit': it['unit'], 'status': ui_status}
                if on_break:
                    item_out['break_until'] = br['end']  # None = suresiz
                items_out.append(item_out)
            stack_on_break = len(s['items']) > 0 and total_n == 0
            # Stack rozetindeki geri sayac: stack-seviyeli break varsa onun bitisi; degilse
            # tum urunler ayri ayri paused demektir - biri suresizse (None) rozet de suresiz,
            # hepsinin tarihi varsa en gec bitis gosterilir.
            stack_until = None
            if stack_on_break:
                if stack_break:
                    stack_until = stack_break['end']
                elif item_break_ends and all(e for e in item_break_ends):
                    stack_until = max(item_break_ends)
            day_stacks.append({
                'stack_id': s['id'], 'name': s['name'], 'taken': taken_n, 'total': total_n,
                # Stack rozeti hem gercek stack-seviyeli aralara hem de "stack'teki TEK urun
                # ayri ayri paused edildi ama sonucta hepsi kapandi" durumuna gore tetiklenir
                # (ornek: tek urunlu Post-workout'ta urun bazli break de ayni gorseli vermeli).
                'on_break': stack_on_break,
                'break_until': stack_until,
                'items': items_out,
            })
        days.append({'date': ds, 'stacks': day_stacks})
        d += timedelta(days=1)

    return jsonify({'start': start, 'end': end, 'days': days})

@app.route('/api/supplements/log', methods=['POST'])
def api_supplements_log():
    """Stack tamamlandı olarak kaydet. Override ve ekstra destekler."""
    import json as _json
    data = request.get_json(force=True) or {}
    today = data.get('date', operation_today())
    stack_name = data.get('stack_name', '').strip()
    overrides  = data.get('overrides', {})   # {product_name: {dose, unit, taken}}
    extras     = data.get('extras', [])       # [{name, dose, unit}]
    notes      = data.get('notes', '')

    conn = get_db()
    stack = conn.execute("SELECT * FROM supplement_stacks WHERE name=?", (stack_name,)).fetchone()
    if not stack:
        conn.close()
        return jsonify({'ok': False, 'error': f'Stack bulunamadı: {stack_name}'}), 404

    # Snapshot log
    conn.execute("INSERT INTO supplement_logs (date,stack_id,stack_name_snapshot,completed,notes) VALUES (?,?,?,1,?)",
                 (today, stack['id'], stack_name, notes))
    log_id = conn.execute("SELECT last_insert_rowid() as lid").fetchone()['lid']

    # Items (snapshot with overrides)
    items = conn.execute("SELECT * FROM supplement_stack_items WHERE stack_id=? ORDER BY order_num", (stack['id'],)).fetchall()
    for item in items:
        pname = item['product_name']
        ov = overrides.get(pname, {})
        dose   = ov.get('dose',  item['dose'])
        unit   = ov.get('unit',  item['unit'])
        taken  = ov.get('taken', 1)
        ov_note = ov.get('note', '')
        conn.execute("INSERT INTO supplement_log_items (log_id,product_name_snapshot,dose_snapshot,unit_snapshot,taken,override_note) VALUES (?,?,?,?,?,?)",
                     (log_id, pname, dose, unit, taken, ov_note))
        # Zinc last date update
        if pname == 'NOW Zinc Picolinate 50mg' and taken:
            rd = _json.dumps({'last_date': today})
            conn.execute("UPDATE supplement_rules SET rule_data=? WHERE product_name='NOW Zinc Picolinate 50mg'", (rd,))
        # Also log to vitamin_logs for backward compat
        if taken:
            conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes,status) VALUES (?,?,?,?,?,'taken')",
                         (today, pname, str(dose), unit, f'stack:{stack_name}'))

    # Extras
    for ex in extras:
        ename = ex.get('name', '')
        edose = ex.get('dose', '')
        eunit = ex.get('unit', '')
        conn.execute("INSERT INTO supplement_log_items (log_id,product_name_snapshot,dose_snapshot,unit_snapshot,taken,override_note) VALUES (?,?,?,?,1,'extra')",
                     (log_id, ename, edose, eunit))
        conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes,status) VALUES (?,?,?,?,?,'taken')",
                     (today, ename, str(edose), eunit, f'extra:{stack_name}'))

    conn.commit(); conn.close()
    return jsonify({'ok': True, 'log_id': log_id, 'stack': stack_name, 'date': today})

@app.route('/api/supplements/log/<int:lid>', methods=['DELETE'])
def api_supplement_log_delete(lid):
    """Bir supplement log kaydını ve item'larını sil (re-log için)."""
    conn = get_db()
    conn.execute("DELETE FROM supplement_log_items WHERE log_id=?", (lid,))
    conn.execute("DELETE FROM supplement_logs WHERE id=?", (lid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'deleted_log_id': lid})

@app.route('/api/supplements/log-items/<int:iid>', methods=['PATCH'])
def api_supplement_log_item_patch(iid):
    """Supplement log item'ını güncelle (taken, override_note)."""
    data = request.get_json(force=True) or {}
    conn = get_db()
    if 'taken' in data:
        conn.execute("UPDATE supplement_log_items SET taken=? WHERE id=?", (data['taken'], iid))
    if 'override_note' in data:
        conn.execute("UPDATE supplement_log_items SET override_note=? WHERE id=?", (data['override_note'], iid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/supplements/zinc', methods=['GET'])
def api_supplements_zinc():
    import json as _json
    conn = get_db()
    row = conn.execute("SELECT rule_data FROM supplement_rules WHERE product_name='NOW Zinc Picolinate 50mg'").fetchone()
    conn.close()
    last_date = None
    if row:
        try: last_date = _json.loads(row['rule_data'] or '{}').get('last_date')
        except: pass
    today = operation_today()
    take_today = True
    if last_date:
        from datetime import date as _date
        diff = (_date.fromisoformat(today) - _date.fromisoformat(last_date)).days
        take_today = diff >= 2
    return jsonify({'take_today': take_today, 'last_date': last_date, 'today': today})

@app.route('/api/supplements/stacks', methods=['POST'])
def api_supplement_stack_create():
    data = request.get_json(force=True) or {}
    name = data.get('name','').strip()
    if not name:
        return jsonify({'ok':False, 'error':'Stack adı gerekli'}), 400
    items = data.get('items', [])
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO supplement_stacks (name,category,active,order_num) VALUES (?,?,1,99)",
                 (name, data.get('category','custom')))
    sid = conn.execute("SELECT id FROM supplement_stacks WHERE name=?", (name,)).fetchone()['id']
    for i, item in enumerate(items):
        conn.execute("INSERT INTO supplement_stack_items (stack_id,product_name,dose,unit,order_num) VALUES (?,?,?,?,?)",
                     (sid, item.get('product_name', item.get('name','')), item.get('dose',1), item.get('unit','kapsul'), i+1))
    conn.commit(); conn.close()
    return jsonify({'ok':True, 'stack_id': sid})

@app.route('/api/supplements/stacks/<int:sid>/items', methods=['PUT'])
def api_supplement_stack_items_replace(sid):
    """Bir stack'in tum item'larini yenisiyle degistir."""
    data = request.get_json(force=True) or {}
    items = data.get('items', [])
    conn = get_db()
    conn.execute("DELETE FROM supplement_stack_items WHERE stack_id=?", (sid,))
    for i, item in enumerate(items):
        conn.execute(
            "INSERT INTO supplement_stack_items (stack_id,product_name,dose,unit,order_num) VALUES (?,?,?,?,?)",
            (sid, item.get('product_name', item.get('name','')), item.get('dose',1), item.get('unit','kapsul'), i+1)
        )
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'stack_id': sid, 'items_count': len(items)})

@app.route('/api/supplements/stacks/<int:sid>', methods=['PUT'])
def api_supplement_stack_rename(sid):
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name gerekli'}), 400
    conn = get_db()
    conn.execute("UPDATE supplement_stacks SET name=? WHERE id=?", (name, sid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/supplements/stacks/<int:sid>', methods=['DELETE'])
def api_supplement_stack_delete(sid):
    conn = get_db()
    conn.execute("DELETE FROM supplement_stack_items WHERE stack_id=?", (sid,))
    conn.execute("DELETE FROM supplement_stacks WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/supplement-products', methods=['GET'])
def api_supplement_products_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM supplement_products ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/supplement-products', methods=['POST'])
def api_supplement_products_add():
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name gerekli'}), 400
    conn = get_db()
    try:
        conn.execute("INSERT INTO supplement_products (name,brand,form,default_dose,default_unit,notes) VALUES (?,?,?,?,?,?)",
                     (name, data.get('brand',''), data.get('form','kapsul'), data.get('default_dose') or 1,
                      data.get('default_unit','kapsul'), data.get('notes','')))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'ok': False, 'error': 'Bu isimde bir supplement zaten var'}), 400
    new_id = conn.execute("SELECT id FROM supplement_products WHERE name=?", (name,)).fetchone()['id']
    conn.close()
    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/supplement-products/<int:pid>', methods=['PUT'])
def api_supplement_products_update(pid):
    data = request.get_json(force=True) or {}
    fields = ['name','brand','form','default_dose','default_unit','notes']
    sent = {k: data[k] for k in fields if k in data}
    if 'name' in sent and not sent['name'].strip():
        return jsonify({'ok': False, 'error': 'name gerekli'}), 400
    if not sent:
        return jsonify({'ok': False, 'error': 'Güncellenecek alan yok'}), 400
    conn = get_db()
    try:
        conn.execute(f"UPDATE supplement_products SET {','.join(k+'=?' for k in sent)} WHERE id=?", (*sent.values(), pid))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'ok': False, 'error': 'Bu isimde bir supplement zaten var'}), 400
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/supplement-products/<int:pid>', methods=['DELETE'])
def api_supplement_products_delete(pid):
    conn = get_db()
    conn.execute("DELETE FROM supplement_products WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/supplements/zinc/last-date', methods=['PATCH'])
def api_supplement_zinc_last_date():
    """supplement_rules tablosunda cinkonun son alinma tarihini guncelle."""
    import json as _json
    data = request.get_json(force=True) or {}
    last_date = data.get('last_date', '').strip()
    if not last_date:
        return jsonify({'ok': False, 'error': 'last_date gerekli'}), 400
    conn = get_db()
    row = conn.execute("SELECT id, rule_data FROM supplement_rules WHERE product_name='NOW Zinc Picolinate 50mg'").fetchone()
    if row:
        rd = _json.loads(row['rule_data'] or '{}')
        rd['last_date'] = last_date
        conn.execute("UPDATE supplement_rules SET rule_data=? WHERE id=?", (_json.dumps(rd), row['id']))
    else:
        conn.execute("INSERT INTO supplement_rules (product_name,rule_data) VALUES (?,?)",
                     ('NOW Zinc Picolinate 50mg', _json.dumps({'last_date': last_date})))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'last_date': last_date})


@app.route('/api/supplements/compliance', methods=['GET'])
def api_supplements_compliance():
    """Son 7 günlük stack uyum oranı."""
    conn = get_db()
    from datetime import date as _date, timedelta as _td
    today = _date.fromisoformat(operation_today())
    result = []
    for i in range(7):
        d = (today - _td(days=i)).isoformat()
        logs = conn.execute("SELECT stack_name_snapshot FROM supplement_logs WHERE date=?", (d,)).fetchall()
        result.append({'date': d, 'stacks_logged': [r['stack_name_snapshot'] for r in logs], 'count': len(logs)})
    conn.close()
    return jsonify(result)

# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.route('/api/ai-insights', methods=['POST'])
def api_ai_insights():
    try:
        data = request.get_json(silent=True) or {}
        date_str = data.get('date', operation_today())
        conn = get_db()
        meals = [dict(r) for r in conn.execute("SELECT * FROM meal_entries WHERE date=? ORDER BY display_order, id", (date_str,)).fetchall()]
        vitamins = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY id", (date_str,)).fetchall()]
        sleep_row = conn.execute("SELECT * FROM sleep_logs WHERE date=?", (date_str,)).fetchone()
        exercise_row = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (date_str,)).fetchone()
        mood_row = conn.execute("SELECT * FROM mood_logs WHERE date=?", (date_str,)).fetchone()
        water_row = conn.execute("SELECT SUM(water_ml) as total FROM nutrition_logs WHERE date=?", (date_str,)).fetchone()
        step_row = conn.execute("SELECT * FROM step_logs WHERE date=?", (date_str,)).fetchone()
        # last 7 days summary for context (summary is not a real table, query actual tables)
        _start7 = (operation_date() - timedelta(days=7)).isoformat()
        _meal_week = conn.execute(
            "SELECT date, SUM(calories) as calories, SUM(protein_g) as protein_g FROM meal_entries WHERE date>=? AND date<? GROUP BY date ORDER BY date",
            (_start7, date_str)
        ).fetchall()
        _water_week = conn.execute(
            "SELECT date, SUM(water_ml) as water_ml FROM nutrition_logs WHERE date>=? AND date<? GROUP BY date ORDER BY date",
            (_start7, date_str)
        ).fetchall()
        _water_map = {r['date']: r['water_ml'] for r in _water_week}
        week = [{'date': r['date'], 'calories': r['calories'] or 0, 'protein_g': r['protein_g'] or 0, 'water_ml': _water_map.get(r['date'], 0)} for r in _meal_week]
        conn.close()
        totals = {
            'cal': sum(m.get('calories',0) or 0 for m in meals),
            'prot': sum(m.get('protein_g',0) or 0 for m in meals),
            'carb': sum(m.get('carbs_g',0) or 0 for m in meals),
            'fat': sum(m.get('fat_g',0) or 0 for m in meals),
            'water_ml': int((water_row['total'] or 0) if water_row else 0),
            'steps': dict(step_row)['steps'] if step_row else 0,
        }
        try:
            settings_conn = get_db()
            s_rows = {r['key']: r['value'] for r in settings_conn.execute("SELECT key,value FROM settings").fetchall()}
            settings_conn.close()
        except Exception:
            s_rows = {}
        targets = {
            'cal': int(s_rows.get('target_calories', 1800)),
            'prot': int(s_rows.get('target_protein', 160)),
            'water': int(s_rows.get('target_water_ml', 5000)),
            'steps': 10000,
        }
        ctx_str = (
            f"Tarih: {date_str} ({training_day(date_str)} günü)\n"
            f"Kalori: {totals['cal']} / {targets['cal']} kcal\n"
            f"Protein: {round(totals['prot'])}g / {targets['prot']}g\n"
            f"Karb: {round(totals['carb'])}g | Yağ: {round(totals['fat'])}g\n"
            f"Su: {totals['water_ml']}ml / {targets['water']}ml\n"
            f"Adım: {totals['steps']} / {targets['steps']}\n"
            f"Öğünler: {len(meals)} kayıt\n"
            f"Takviyeler: {len(vitamins)} kayıt\n"
        )
        if sleep_row:
            sl = dict(sleep_row)
            ctx_str += f"Uyku: {sl.get('hours','?')}s kalite {sl.get('quality','?')}/10\n"
        if exercise_row:
            ex = dict(exercise_row)
            ctx_str += f"Antrenman: {ex.get('type','?')} {ex.get('duration','?')}dk\n"
        if mood_row:
            mo = dict(mood_row)
            ctx_str += f"Enerji: {mo.get('energy','?')}/10 | Mood: {mo.get('mood','?')}/10\n"
        if week:
            ctx_str += f"Son 7 gün kalori ort: {round(sum(w.get('calories',0) or 0 for w in week)/max(len(week),1))} kcal\n"
            ctx_str += f"Son 7 gün protein ort: {round(sum(w.get('protein_g',0) or 0 for w in week)/max(len(week),1))}g\n"

        if not ANTHROPIC_API_KEY:
            return jsonify({'insight': 'AI modu aktif değil.', 'ok': False})

        import urllib.request, urllib.error
        body = {
            'model': ANTHROPIC_MODEL,
            'max_tokens': 200,
            'system': (
                "Sen Taha Serdem'in kişisel performans koçusun. "
                "Günlük veri özetine bakarak 2-3 cümle, samimi, net ve motive edici bir insight ver. "
                "Olumlu olanı vurgula, eksik varsa kısa belirt. Türkçe yaz. "
                "Emoji kullanabilirsin. Sadece insight metni döndür, başka hiçbir şey yazma."
            ),
            'messages': [{'role': 'user', 'content': ctx_str}]
        }
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps(body).encode('utf-8'),
            headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        insight = payload['content'][0]['text']
        return jsonify({'insight': insight, 'ok': True})
    except Exception as e:
        log.exception("ai-insights error")
        err_msg = str(e)
        import urllib.error as _ue
        if isinstance(e, _ue.HTTPError):
            try: err_msg = e.read().decode('utf-8', errors='ignore')[:300]
            except: pass
        return jsonify({'insight': f'Analiz yuklenemedi: {err_msg}', 'ok': False, 'error': err_msg})


if __name__ == '__main__':
    init_db()
    log.info(f"DB: {DB_PATH}")
    import sys as _sys
    telegram_only = ('--telegram-only' in _sys.argv) or (os.environ.get('TELEGRAM_ONLY') == '1')
    if telegram_only:
        log.info("Telegram-only mod: site acilmadan bot calisiyor.")
        start_telegram_bot()
    else:
        if TELEGRAM_TOKEN and os.environ.get('DISABLE_EMBEDDED_BOT') != '1':
            threading.Thread(target=start_telegram_bot, daemon=True).start()
        log.info(f"http://localhost:{PORT}")
        app.run(host='0.0.0.0', port=PORT, debug=False)
