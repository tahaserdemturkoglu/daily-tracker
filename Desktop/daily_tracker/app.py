#!/usr/bin/env python3
"""Taha Serdem Daily Rapor â Flask + Telegram Bot"""

import os, sqlite3, threading, asyncio, json, logging, re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template

_TZ_ISTANBUL = ZoneInfo('Europe/Istanbul')

def now_istanbul() -> datetime:
    """Şu anki Istanbul saatini döndürür. Railway UTC'de çalışır, bu fonksiyon TR saatini verir."""
    return datetime.now(_TZ_ISTANBUL)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
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
CYCLE_START = _cfg.get('CYCLE_START', date.today().isoformat())


# OPERATION_DAY_CUTOFF_V1
OPERATION_DAY_CUTOFF_HOUR = int(os.environ.get('OPERATION_DAY_CUTOFF_HOUR', _cfg.get('OPERATION_DAY_CUTOFF_HOUR', 6)))

# SHIFT_AWARE_OPERATION_DAY_V1
SHIFT_TRANSITION_DATE = date(2026, 6, 22)
SHIFT_BLOCK_DAYS = 14

def current_shift_info(now=None):
    """
    Sabit 18:00-03:00 vardiyasi (2026-07-06 itibariyle).
    Uyanis ~14:15, uyku ~06:45. Gun siniri 14:00.
    """
    return {
        'name': '18:00-03:00',
        'label': 'aksam vardiyasi',
        'start': '18:00',
        'end': '03:00',
        'cutoff_hour': 14,
        'late_window': '00:00-13:59',
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

# WHOOP entegrasyonu (whoop_integration.py kendi DB_PATH'ini DATABASE_PATH env'inden okur -
# ana app'in gerçek DB_PATH'iyle her zaman aynı dosyaya işaret etsin diye burada eşitliyoruz).
os.environ.setdefault('DATABASE_PATH', DB_PATH)
from whoop_integration import whoop_bp, init_whoop_tables
app.register_blueprint(whoop_bp)
init_whoop_tables()

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
        CREATE TABLE IF NOT EXISTS daily_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE, note TEXT,
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
        'vitamin_logs': {'status': "TEXT DEFAULT ''"},
        'mood_logs': {'recovery': 'REAL', 'strain': 'REAL'},
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
    vitamins = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY id", (today,)).fetchall()]
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

@app.route('/api/new-day', methods=['POST'])
def api_new_day():
    """Gunaydın: operation tarihi bugunun takvim tarihine ayarla (DB'ye kaydet)"""
    from datetime import date as _dt
    today = _dt.today().isoformat()
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
    slot = data.get('slot', '').strip() or 'extra'
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
    start = operation_date() - timedelta(days=days-1)
    conn = get_db()
    result = []
    for i in range(days):
        ds = (start + timedelta(days=i)).isoformat()
        meals = meal_macro_totals(ds)
        row = conn.execute("SELECT SUM(water_ml) AS water_ml FROM nutrition_logs WHERE date=?", (ds,)).fetchone()
        water_ml = float((row['water_ml'] if row else 0) or 0)
        result.append({
            'date': ds,
            'calories': meals.get('calories', 0),
            'protein_g': meals.get('protein_g', 0),
            'carbs_g': meals.get('carbs_g', 0),
            'fat_g': meals.get('fat_g', 0),
            'fiber_g': meals.get('fiber_g', 0),
            'water_ml': water_ml,
            'water_l': round(water_ml / 1000, 2),
        })
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

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    """Telegram webhook endpoint."""
    if not TELEGRAM_TOKEN:
        return 'no token', 200
    data = request.get_json(force=True) or {}
    if not data:
        return 'ok', 200
    try:
        from bot import process_webhook_update
        import threading as _thr
        t = _thr.Thread(target=process_webhook_update, args=(data,), daemon=True)
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
        slot=a[0] if a else 'ara'
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
        'su_gecmisi': water_history,
    }


TAHA_COACHING_POLICY = """
TAHA ICIN KALICI KOCLUK HAFIZASI:
- Turkce, kisa, net ve profesyonel sporcu kocu gibi yaz. Gereksiz tekrar yapma.
- Hatalari durustce belirt ama panik yaptirma.
- Hedefler: yag kaybi, kas korunumu/kazanimi, performans, akne takibi, sindirim ve genel saglik.

KONUSMA TARZI â EN ONEMLI KURAL:
- Normal mesajlarda (slash komut degil) KISA ve DOGAL yaz. Madde listesi yapma. 1-3 cumle yeter.
- Veri kaydedildiyse tek satirda onayla: "Kaydettim." veya "Tamam, islendi." yeterli.
- Sadece /bugun, /rapor gibi ozel komutlarda yapilandirilmis format kullan.
- Koç gibi konuş, anket dolduruyor gibi değil. Örnek iyi: "Ağır geçmiş, kaç set yaptın?" Örnek kötü: "Antrenmanın kaydedildi. Detaylar: ..."
- Veri eksikse tek, kisa bir soru sor. Birden fazla soru sorma.

GENEL HESAP KURALLARI:
- Tum gramajlar aksi belirtilmedikce cig gramdir.
- Tavuk, pirinc, patates, et ve hindi cig agirlik uzerinden hesaplanir.
- Pismis agirlik kullanma; kullanici ozellikle pismis derse belirt.
- Ekstra yag belirtilmedikce eklenmez.
- GymBeam Olive Oil Spray yalniz kullanici fis/basis sayisi soylerse eklenir.

SABIT URUNLER:
- Carrefour BIO Organik Yumurta: 1 adet = 80 kcal, 7.5P, 0.3K, 4.7Y. (Open Food Facts dogrulandi)
- Sivi Yumurta Beyazi: 100g = 58 kcal, 10.3P, 1.2K, 0.8Y.
- Cig Derisiz Tavuk Gogsu: 100g = 115 kcal, 23P, 0K, 1.5Y. (kullanici onayli deger)
- Marine tavuk sis: altta kalan yag/sos tuketilmiyor; 300g cig = 390 kcal, 68P, 3K, 10Y.
- Yasmin Pirinc: 100g cig = 360 kcal, 7P, 79K, 0.6Y.
- Patates: 100g cig = 77 kcal, 2P, 17K, 0.1Y.
- Carrefour Tost Ekmegi: 100g = 252 kcal, 9.5P, 45K, 2.1Y. 69g = 174 kcal, 6.6P, 31.1K, 1.4Y.
- Cilek: 100g = 32 kcal, 0.7P, 7.7K, 0.3Y.
- Salatalik: 100g = 15 kcal, 0.7P, 3.6K, 0.1Y.
- Sekersiz Badem Sutu: 100ml = 14 kcal, 0.5P, 0K, 1.1Y.
- GymBeam Olive Oil Spray: 1 fis = 1.5g yag = ~13.5 kcal, 0P, 0K, 1.5Y. (her fis hafifce basilir)
- Keto Ketcap: 100g = 41 kcal, 2P, 6.2K, 0.5Y; 20-30g kullanim ihmal edilebilir.

STANDART PANCAKE V2:
- 4 yumurta, 200g sivi yumurta beyazi, 25g yulaf, 50g kuru kayisi, 200g cilek, 50ml sekersiz badem sutu, 6g kakao, 2 fis GymBeam.

SUPPLEMENT SISTEMI:
- Ac karna stack: NAC 600mg + Garden of Life Once Daily Men's Probiotic.
- Sabah/kahvalti stack: Kolajen, D3+K2 4000 IU, Omega-3 3 kapsul, Magtein, Goz vitamini, B Complex, C Vitamini 1000mg, Theanine, gerektiginde Cinko.
- Gece stack: Magnesium Glycinate, Glycine, Melatonin, gerektiginde Theanine, KSM-66 Ashwagandha.
- Cinko 50mg yuksek doz; gun asiri takip edilir, her gun sart gibi yazma.
- Kullanici 'stack alindi' derse ilgili stackteki urunleri tek tek vitamin kaydi olarak isle.
- Kullanici 'haric/eksik/yok' derse o supplementi stackten dus.

AKNE VE CILT:
- Whey, yogurt, protein puding ve yuksek seker akne acisindan takip edilir.
- Kreatin su an kullanilmiyor; akne gozlemi icin bunu koru.
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
    system_prompt = (
        TAHA_COACHING_POLICY + "\n" + besin_db_ctx + "\n" +
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
        '{"type":"note","date":"YYYY-MM-DD","note":"..."}'
        ']}\n'
        f'Tarih kuralı: Kullanıcı tarih belirtmemişse date={operation_today()} (bugün). '
        f'"Dün" derse date={(operation_date()-timedelta(days=1)).isoformat()}. '
        '"X gün önce" veya "X Haziran" gibi ifadeleri doğru tarihe çevir. '
        f"Saat baglami: Simdiki yerel saat {now_istanbul().strftime('%H:%M')}. Aktif vardiya: {current_shift_info().get('name')} ({current_shift_info().get('label')}). Operasyon gunu kapanisi: {operation_cutoff_hour()}:00. Bu kapanis saatinden onceki kayitlari, kullanici aksini soylemedikce onceki operasyon gunune bagla; sabah gibi davranma.\n"
        f"Gece/vardiya kayit kurali: aktif gec pencere {current_shift_info().get('late_window')}. Bu pencerede yatmadan once stack, vitamin, ogun, su, adim, kilo ve gun sonu notlari kullanici aksini soylemedikce bir onceki operasyon gunune aittir. 03:30da uyuyacagim/yatacagim gibi ifadeler uyku suresi degildir; sleep hours olarak 3.3 kaydetme. Uyku kaydi icin ancak uyudum/kalktim/uyandim veya baslangic-bitis netse action uret.\n"
        'Bugün: ' + operation_today() + '\n'
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
        return _json_from_text(txt)
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        log.error("Anthropic HTTP hatasi: %s", detail)
        try:
            msg = json.loads(detail).get('error', {}).get('message', detail[:200])
        except Exception:
            msg = detail[:200]
        return {'reply': f'Claude hatası: {msg}', 'actions': []}
    except Exception:
        log.exception("Claude cevap hatasi")
        return {'reply': 'Bağlantı sorunu. Tekrar dener misin?', 'actions': []}


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


def tg_template_name_from_text(raw_text):
    text = (raw_text or '').strip()
    if not text:
        return ''
    m = re.search(r'ad[ıi]\s+(.+?)\s+olsun', text, flags=re.I)
    if m:
        name = m.group(1).strip(" .,!?:;")
        return name[:60]
    m = re.search(r'ismi\s+(.+?)\s+olsun', text, flags=re.I)
    if m:
        name = m.group(1).strip(" .,!?:;")
        return name[:60]
    return ''

def tg_meal_category_from_text(raw_text, slot=''):
    text = (raw_text or '').lower()
    if any(w in text for w in ['sabah', 'kahvalt', 'breakfast']):
        return 'kahvaltı'
    if any(w in text for w in ['pre', 'antrenman öncesi', 'idman öncesi']):
        return 'pre-antrenman'
    if any(w in text for w in ['post', 'antrenman sonrası', 'idman sonrası']):
        return 'post-antrenman'
    if any(w in text for w in ['öğle', 'ogle', 'lunch']):
        return 'öğle'
    if any(w in text for w in ['akşam', 'aksam', 'dinner']):
        return 'akşam'
    return slot or 'extra'

def tg_should_save_template(raw_text):
    text = (raw_text or '').lower()
    return any(w in text for w in ['fiks', 'fix', 'sabit', 'şablon', 'sablon', 'favori', 'hep kullan', 'kaydet'])

def tg_save_meal_template_from_actions(raw_text, actions):
    if not tg_should_save_template(raw_text):
        return ''
    meal = None
    for a in actions or []:
        if (a.get('type') or '').strip() == 'meal':
            meal = a
            break
    if not meal:
        return ''
    title = tg_template_name_from_text(raw_text) or meal.get('title') or meal.get('slot') or 'Sabit Öğün'
    if 'kahvalt' in tg_meal_category_from_text(raw_text, meal.get('slot') or '') and 'kahvalt' not in title.lower():
        title = title.strip() + ' Kahvaltısı'
    category = tg_meal_category_from_text(raw_text, meal.get('slot') or '')
    desc = meal.get('description') or title
    conn = get_db()
    existing = conn.execute("SELECT id FROM quick_templates WHERE kind='meal' AND lower(title)=lower(?)", (title,)).fetchone()
    payload = (
        category, title, desc,
        meal.get('calories'), meal.get('protein_g'), meal.get('carbs_g'), meal.get('fat_g'), meal.get('fiber_g')
    )
    if existing:
        conn.execute("""
            UPDATE quick_templates
            SET category=?, title=?, description=?, calories=?, protein_g=?, carbs_g=?, fat_g=?, fiber_g=?
            WHERE id=?
        """, payload + (existing['id'],))
    else:
        conn.execute("""
            INSERT INTO quick_templates
                (kind, category, title, description, calories, protein_g, carbs_g, fat_g, fiber_g, amount, unit, notes)
            VALUES ('meal',?,?,?,?,?,?,?,?,?,?,?)
        """, payload + ('', '', 'telegram-ai sabit öğün'))
    conn.commit(); conn.close()
    return title


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

def ai_apply_actions(actions):
    saved = []
    today = operation_today()
    for a in actions or []:
        typ = (a.get('type') or '').strip()
        action_date = (a.get('date') or today)
        try:
            if typ == 'meal':
                conn = get_db()
                slot = a.get('slot') or 'extra'
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
                conn.execute("INSERT INTO vitamin_logs (date, name, amount, unit, notes) VALUES (?,?,?,?,?)",
                             (action_date, vname, vamount, vunit, vnotes))
                conn.commit(); conn.close()
                saved.append('supplement')
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

def tg_water_actions_from_text(raw_text):
    text = raw_text or ''
    norm = _tg_norm(text) if '_tg_norm' in globals() else text.lower()
    if not any(w in norm for w in ['su', 'water', 'ml', 'litre', 'lt']):
        return []
    # "su 3 litre oldu/toplam" -> total set, otherwise "200ml su içildi" -> add
    m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(ml|l|lt|litre)?', norm)
    if not m:
        return []
    val = float(m.group(1).replace(',', '.'))
    unit = (m.group(2) or '').lower()
    ml = int(val * 1000) if unit in ('l', 'lt', 'litre') or (not unit and val <= 10) else int(val)
    if ml <= 0:
        return []
    date = tg_effective_log_date(text, 'water') if 'tg_effective_log_date' in globals() else operation_today()
    is_total = any(w in norm for w in ['toplam', 'olsun', 'olarak', 'yap', 'duzelt', 'düzelt', 'set'])
    return [{'type': 'water_set' if is_total else 'water', 'date': date, 'water_ml': ml}]

def tg_basic_actions_from_text(raw_text):
    """Extract critical records even when the AI provider is temporarily unavailable."""
    text = raw_text or ''
    low = text.lower()
    trans = str.maketrans({
        'ı': 'i', 'İ': 'i', 'ğ': 'g', 'Ğ': 'g', 'ü': 'u', 'Ü': 'u',
        'ş': 's', 'Ş': 's', 'ö': 'o', 'Ö': 'o', 'ç': 'c', 'Ç': 'c',
        'Ä±': 'i', 'Ä°': 'i', 'ÄŸ': 'g', 'Äž': 'g', 'Ã¼': 'u', 'Ãœ': 'u',
        'ÅŸ': 's', 'Åž': 's', 'Ã¶': 'o', 'Ã–': 'o', 'Ã§': 'c', 'Ã‡': 'c'
    })
    norm = low.translate(trans)
    today = operation_today()
    actions = []

    kg_match = re.search(r'(?:kilo|weight|kg)\s*[:\-]?\s*(\d{2,3}(?:[\.,]\d+)?)', norm)
    if kg_match:
        actions.append({'type': 'weight', 'date': today, 'weight_kg': float(kg_match.group(1).replace(',', '.')), 'notes': 'telegram-basic'})

    if 'su' in norm or 'water' in norm:
        wm = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:l|lt|litre)', norm)
        if wm:
            actions.append({'type': 'water', 'date': today, 'water_ml': int(round(float(wm.group(1).replace(',', '.')) * 1000))})
        else:
            wm = re.search(r'(\d{3,5})\s*ml', norm)
            if wm:
                actions.append({'type': 'water', 'date': today, 'water_ml': int(wm.group(1))})

    if 'adim' in norm or 'step' in norm:
        step_nums = [int(x) for x in re.findall(r'\b\d{3,6}\b', norm)]
        if step_nums:
            actions.append({'type': 'steps', 'date': today, 'steps': max(step_nums), 'notes': 'telegram-basic'})

    if any(w in norm for w in ['kahvalti', 'ogle', 'aksam', 'pre', 'post', 'ogun']):
        cal = re.search(r'(?:kalori|kcal|calories)\s*[:~â ]+\s*(\d{2,5})', norm)
        pro = re.search(r'(?:protein|p)\s*[:~â ]+\s*(\d+(?:[\.,]\d+)?)\s*g?', norm)
        carb = re.search(r'(?:karbonhidrat|karb|carb|k)\s*[:~â ]+\s*(\d+(?:[\.,]\d+)?)\s*g?', norm)
        fat = re.search(r'(?:yag|yağ|fat|y)\s*[:~≈ ]+\s*(\d+(?:[\.,]\d+)?)\s*g?', norm)
        if cal or pro or carb or fat:
            slot = 'extra'
            if 'kahvalti' in norm:
                slot = 'kahvaltı'
            elif 'ogle' in norm:
                slot = 'öğle'
            elif 'aksam' in norm:
                slot = 'akşam'
            elif 'pre' in norm:
                slot = 'pre-workout'
            elif 'post' in norm:
                slot = 'post-workout'
            actions.append({
                'type': 'meal', 'date': today, 'slot': slot,
                'description': text[:900],
                'calories': int(cal.group(1)) if cal else None,
                'protein_g': float(pro.group(1).replace(',', '.')) if pro else None,
                'carbs_g': float(carb.group(1).replace(',', '.')) if carb else None,
                'fat_g': float(fat.group(1).replace(',', '.')) if fat else None,
            })
    return actions




# TG_FULL_DAY_REVIEW_V1
def tg_full_day_actions_from_text(raw_text):
    """Build meal/step/water/weight actions from a full-day Turkish food dump."""
    text = raw_text or ''
    low = text.lower()
    trans = str.maketrans({
        'ı': 'i', 'İ': 'i', 'ğ': 'g', 'Ğ': 'g', 'ü': 'u', 'Ü': 'u',
        'ş': 's', 'Ş': 's', 'ö': 'o', 'Ö': 'o', 'ç': 'c', 'Ç': 'c',
        'Ä±': 'i', 'Ä°': 'i', 'ÄŸ': 'g', 'Äž': 'g', 'Ã¼': 'u', 'Ãœ': 'u',
        'ÅŸ': 's', 'Åž': 's', 'Ã¶': 'o', 'Ã–': 'o', 'Ã§': 'c', 'Ã‡': 'c'
    })
    norm = low.translate(trans)
    if not any(x in norm for x in ['kahvalti', 'ogle', 'aksam']):
        return []
    today = operation_today()
    actions = []

    def section(name, start_words, stop_words):
        start = min([norm.find(w) for w in start_words if norm.find(w) >= 0] or [-1])
        if start < 0:
            return ''
        end_candidates = [norm.find(w, start + 1) for w in stop_words if norm.find(w, start + 1) >= 0]
        end = min(end_candidates) if end_candidates else len(norm)
        return text[start:end]

    sections = [
        ('kahvaltı', section('kahvaltı', ['kahvalti'], ['ogle', 'aksam', 'gun totali'])),
        ('öğle', section('öğle', ['ogle'], ['aksam', 'gun totali'])),
        ('akşam', section('akşam', ['aksam'], ['gun totali'])),
    ]

    def add(a, b):
        return {
            'cal': a['cal'] + b.get('cal', 0),
            'p': a['p'] + b.get('p', 0),
            'c': a['c'] + b.get('c', 0),
            'f': a['f'] + b.get('f', 0),
        }

    def estimate_line(line):
        ln = line.lower().translate(trans)
        out = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
        # Explicit line format: "... 288 kcal 25.2 g 1.6 g 19.2 g"
        m = re.search(r'(\d+(?:[\.,]\d+)?)\s*kcal.*?(\d+(?:[\.,]\d+)?)\s*g.*?(\d+(?:[\.,]\d+)?)\s*g.*?(\d+(?:[\.,]\d+)?)\s*g', ln)
        if m:
            return {
                'cal': float(m.group(1).replace(',', '.')),
                'p': float(m.group(2).replace(',', '.')),
                'c': float(m.group(3).replace(',', '.')),
                'f': float(m.group(4).replace(',', '.')),
            }
        gm = re.search(r'(\d{2,4})\s*g?', ln)
        grams = float(gm.group(1)) if gm else 0.0
        if grams:
            if 'tavuk' in ln:
                out = {'cal': grams * 1.65, 'p': grams * 0.31, 'c': 0.0, 'f': grams * 0.036}
            elif 'yulaf' in ln:
                out = {'cal': grams * 3.89, 'p': grams * 0.169, 'c': grams * 0.663, 'f': grams * 0.069}
            elif 'cilek' in ln:
                out = {'cal': grams * 0.32, 'p': grams * 0.007, 'c': grams * 0.077, 'f': grams * 0.003}
            elif 'mercimek' in ln:
                out = {'cal': 115.0, 'p': 9.0, 'c': 20.0, 'f': 0.5}
        if 'yarim kase mercimek' in ln or 'yarım kase mercimek' in line.lower():
            out = add(out, {'cal': 115.0, 'p': 9.0, 'c': 20.0, 'f': 0.5})
        if 'gymbeam' in ln and any(w in ln for w in ['fis', 'basis', 'basış', 'spray']):
            fm = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:fis|basis|basış|spray)', ln)
            if fm:
                sprays = float(fm.group(1).replace(',', '.'))
                out = add(out, {'cal': sprays * 15.0, 'p': 0.0, 'c': 0.0, 'f': sprays * 1.65})
        return out

    total = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
    for slot, body in sections:
        if not body.strip():
            continue
        subtotal = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
        for line in body.splitlines():
            subtotal = add(subtotal, estimate_line(line))
        if subtotal['cal'] or subtotal['p'] or subtotal['c'] or subtotal['f']:
            total = add(total, subtotal)
            actions.append({
                'type': 'meal', 'date': today, 'slot': slot,
                'description': body.strip()[:900],
                'calories': int(round(subtotal['cal'])),
                'protein_g': round(subtotal['p'], 1),
                'carbs_g': round(subtotal['c'], 1),
                'fat_g': round(subtotal['f'], 1),
            })

    kg = re.search(r'(?:kilo|kg)\s*[:\-]?\s*(\d{2,3}(?:[\.,]\d+)?)', norm)
    if kg:
        actions.append({'type': 'weight', 'date': today, 'weight_kg': float(kg.group(1).replace(',', '.')), 'notes': 'telegram full day'})
    water = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:l|lt|litre)\s*su', norm)
    if water:
        actions.append({'type': 'water', 'date': today, 'water_ml': int(round(float(water.group(1).replace(',', '.')) * 1000))})
    steps = re.search(r'(\d{4,6})\s*adim', norm)
    if steps:
        actions.append({'type': 'steps', 'date': today, 'steps': int(steps.group(1)), 'notes': 'telegram full day'})
    if total['cal']:
        actions.append({'type': 'note', 'date': today, 'note': f"Telegram tam gun ozeti: ~{int(round(total['cal']))} kcal | P {round(total['p'],1)}g | K {round(total['c'],1)}g | Y {round(total['f'],1)}g"})
    return actions

def tg_full_day_reply(actions):
    meals = [a for a in actions if a.get('type') == 'meal']
    if len(meals) < 2:
        return ''
    cal = sum(float(a.get('calories') or 0) for a in meals)
    p = sum(float(a.get('protein_g') or 0) for a in meals)
    c = sum(float(a.get('carbs_g') or 0) for a in meals)
    f = sum(float(a.get('fat_g') or 0) for a in meals)
    lines = [
        'Taha, bu tam gun kaydini rapor gibi isledim.',
        '',
        f'Gun toplami yaklasik: {int(round(cal))} kcal | Protein {round(p,1)}g | Karb {round(c,1)}g | Yag {round(f,1)}g',
        '',
    ]
    for m in meals:
        lines.append(f"- {m.get('slot')}: {m.get('calories')} kcal | P {m.get('protein_g')}g | K {m.get('carbs_g')}g | Y {m.get('fat_g')}g")
    water = next((a for a in actions if a.get('type') == 'water'), None)
    steps = next((a for a in actions if a.get('type') in ('steps', 'step')), None)
    weight = next((a for a in actions if a.get('type') in ('weight', 'body_weight', 'kilo')), None)
    extra = []
    if water: extra.append(f"Su {round((water.get('water_ml') or 0)/1000,2)}L")
    if steps: extra.append(f"Adim {steps.get('steps')}")
    if weight: extra.append(f"Kilo {weight.get('weight_kg')}kg")
    if extra:
        lines += ['', 'Ek takip: ' + ' | '.join(extra)]
    lines += ['', 'Yorum: protein tarafi guclu. Tavuk miktari yuksek oldugu icin kas koruma iyi; yag spreyi ve yumurta yaglarini takipte tutacagiz. Bir sonraki revizede hedefe gore karbonhidrati antrenman gunlerine daha stratejik dagitabiliriz.']
    return '\n'.join(lines)
















































































































































































































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

def tg_full_day_actions_from_text(raw_text):
    text = raw_text or ''
    norm = tg_ascii_text(text)
    if not any(x in norm for x in ['kahvalti', 'kahvalt?', 'kahvalt', 'ogle', '??le', '?gle', 'aksam', 'ak?am']):
        return []
    today = operation_today()
    actions = []

    def sec(start_words, stop_words):
        starts = [norm.find(w) for w in start_words if norm.find(w) >= 0]
        if not starts:
            return ''
        start = min(starts)
        ends = [norm.find(w, start + 1) for w in stop_words if norm.find(w, start + 1) >= 0]
        end = min(ends) if ends else len(norm)
        return text[start:end]

    sections = [
        ('kahvalti', sec(['kahvalti', 'kahvalt?', 'kahvalt'], ['ogle', '??le', '?gle', 'aksam', 'ak?am', 'gun totali'])),
        ('ogle', sec(['ogle', '??le', '?gle'], ['aksam', 'ak?am', 'gun totali'])),
        ('aksam', sec(['aksam', 'ak?am'], ['gun totali'])),
    ]

    def add(a, b):
        return {'cal': a['cal'] + b['cal'], 'p': a['p'] + b['p'], 'c': a['c'] + b['c'], 'f': a['f'] + b['f']}

    def est(line):
        ln = tg_ascii_text(line)
        out = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
        m = re.search(r'(\d+(?:[\.,]\d+)?)\s*kcal.*?(\d+(?:[\.,]\d+)?)\s*g.*?(\d+(?:[\.,]\d+)?)\s*g.*?(\d+(?:[\.,]\d+)?)\s*g', ln)
        if m:
            return {'cal': float(m.group(1).replace(',', '.')), 'p': float(m.group(2).replace(',', '.')), 'c': float(m.group(3).replace(',', '.')), 'f': float(m.group(4).replace(',', '.'))}
        gm = re.search(r'(\d{2,4})\s*g?', ln)
        grams = float(gm.group(1)) if gm else 0.0
        if grams:
            if 'tavuk' in ln:
                out = {'cal': grams * 1.65, 'p': grams * 0.31, 'c': 0.0, 'f': grams * 0.036}
            elif 'yulaf' in ln:
                out = {'cal': grams * 3.89, 'p': grams * 0.169, 'c': grams * 0.663, 'f': grams * 0.069}
            elif 'cilek' in ln or '?ilek' in ln:
                out = {'cal': grams * 0.32, 'p': grams * 0.007, 'c': grams * 0.077, 'f': grams * 0.003}
        if 'yarim kase mercimek' in ln or 'yar?m kase mercimek' in ln:
            out = add(out, {'cal': 115.0, 'p': 9.0, 'c': 20.0, 'f': 0.5})
        return out

    total = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
    for slot, body in sections:
        if not body.strip():
            continue
        sub = {'cal': 0.0, 'p': 0.0, 'c': 0.0, 'f': 0.0}
        for line in body.splitlines():
            sub = add(sub, est(line))
        if any(sub.values()):
            total = add(total, sub)
            actions.append({'type': 'meal', 'date': today, 'slot': slot, 'description': body.strip()[:900], 'calories': int(round(sub['cal'])), 'protein_g': round(sub['p'], 1), 'carbs_g': round(sub['c'], 1), 'fat_g': round(sub['f'], 1)})

    kg = re.search(r'(?:kilo|kg)\s*[:\-]?\s*(\d{2,3}(?:[\.,]\d+)?)', norm)
    if kg:
        actions.append({'type': 'weight', 'date': today, 'weight_kg': float(kg.group(1).replace(',', '.')), 'notes': 'telegram full day'})
    water = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:l|lt|litre)\s*su', norm)
    if water:
        actions.append({'type': 'water', 'date': today, 'water_ml': int(round(float(water.group(1).replace(',', '.')) * 1000))})
    steps = re.search(r'(\d{4,6})\s*(?:adim|ad\?m)', norm)
    if steps:
        actions.append({'type': 'steps', 'date': today, 'steps': int(steps.group(1)), 'notes': 'telegram full day'})
    if total['cal']:
        actions.append({'type': 'note', 'date': today, 'note': f"Telegram tam gun ozeti: ~{int(round(total['cal']))} kcal | P {round(total['p'],1)}g | K {round(total['c'],1)}g | Y {round(total['f'],1)}g"})
    return actions

def tg_basic_actions_from_text(raw_text):
    actions = []
    norm = tg_ascii_text(raw_text)
    today = operation_today()
    kg = re.search(r'(?:kilo|weight|kg)\s*[:\-]?\s*(\d{2,3}(?:[\.,]\d+)?)', norm)
    if kg:
        actions.append({'type': 'weight', 'date': today, 'weight_kg': float(kg.group(1).replace(',', '.')), 'notes': 'telegram-basic'})
    water = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:l|lt|litre)\s*su', norm)
    if water:
        actions.append({'type': 'water', 'date': today, 'water_ml': int(round(float(water.group(1).replace(',', '.')) * 1000))})
    steps = re.search(r'(\d{4,6})\s*(?:adim|ad\?m)', norm)
    if steps:
        actions.append({'type': 'steps', 'date': today, 'steps': int(steps.group(1)), 'notes': 'telegram-basic'})
    return actions

def tg_full_day_reply(actions):
    meals = [a for a in actions if a.get('type') == 'meal']
    if len(meals) < 2:
        return ''
    cal = sum(float(a.get('calories') or 0) for a in meals)
    p = sum(float(a.get('protein_g') or 0) for a in meals)
    c = sum(float(a.get('carbs_g') or 0) for a in meals)
    f = sum(float(a.get('fat_g') or 0) for a in meals)
    lines = ['Taha, bu tam gun kaydini rapor gibi isledim.', '', f'Gun toplami yaklasik: {int(round(cal))} kcal | Protein {round(p,1)}g | Karb {round(c,1)}g | Yag {round(f,1)}g', '']
    for m in meals:
        lines.append(f"- {m.get('slot')}: {m.get('calories')} kcal | P {m.get('protein_g')}g | K {m.get('carbs_g')}g | Y {m.get('fat_g')}g")
    water = next((a for a in actions if a.get('type') == 'water'), None)
    steps = next((a for a in actions if a.get('type') in ('steps', 'step')), None)
    weight = next((a for a in actions if a.get('type') in ('weight', 'body_weight', 'kilo')), None)
    extra = []
    if water: extra.append(f"Su {round((water.get('water_ml') or 0)/1000,2)}L")
    if steps: extra.append(f"Adim {steps.get('steps')}")
    if weight: extra.append(f"Kilo {weight.get('weight_kg')}kg")
    if extra: lines += ['', 'Ek takip: ' + ' | '.join(extra)]
    lines += ['', 'Yorum: protein tarafi guclu. Tavuk miktari yuksek oldugu icin kas koruma iyi; yag spreyi ve yumurta yaglarini takipte tutacagiz. Bir sonraki revizede hedefe gore karbonhidrati antrenman gunlerine daha stratejik dagitabiliriz.']
    return '\n'.join(lines)




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
    """Telegram'da gece 00:00 sonrası yazılan gün-sonu kayıtlarını önceki güne bağla."""
    norm = tg_ascii_text(raw_text) if 'tg_ascii_text' in globals() else (raw_text or '').lower()
    now = now_istanbul()
    today = now.date()
    if any(w in norm for w in ['dun', 'dunku', 'dün']):
        return (today - timedelta(days=1)).isoformat()
    if any(w in norm for w in ['bugun', 'bug??nk??', 'bugunku']):
        return operation_today() if 'operation_today' in globals() else today.isoformat()
    late_types = {'meal', 'vitamin', 'supplement', 'water', 'steps', 'step', 'weight', 'body_weight', 'kilo', 'note', 'mood'}
    day_end_words = [
        'gece', 'yatmadan', 'uyku oncesi', 'uyku öncesi', 'gun sonu', 'gün sonu',
        'stack', 'vitamin', 'takviye', 'supplement', 'aksam', 'akşam', 'bugun yediklerim'
    ]
    if 0 <= now.hour < operation_cutoff_hour(now) and ((action_type or '') in late_types or any(w in norm for w in day_end_words)):
        return (today - timedelta(days=1)).isoformat()
    return today.isoformat()

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

    if grams:
        if 'marine' in n and 'tavuk' in n:
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
    if 'keto ketcap' in n or 'keto ketchup' in n:
        km = re.search(r'(\d{1,3})\s*(?:g|gr|gram)', n)
        if km and float(km.group(1)) > 30:
            kgrams = float(km.group(1))
            add(kgrams * 0.41, kgrams * 0.02, kgrams * 0.062, kgrams * 0.005)
    if 'gymbeam' in n and any(w in n for w in ['fis', 'basis', 'basıs', 'spray']):
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

def tg_supplement_actions_from_text_legacy(raw_text):
    text = raw_text or ''
    norm = tg_ascii_text(text) if 'tg_ascii_text' in globals() else text.lower()
    if not any(w in norm for w in ['nac', 'omega', 'd3', 'k2', 'b-complex', 'b complex', 'probiyotik', 'probiotic', 'goz', 'göz', 'cinko', 'zinc', 'vitamin', 'takviye', 'supplement']):
        return []
    today = tg_effective_log_date(text, 'vitamin') if 'tg_effective_log_date' in globals() else operation_today()
    catalog = [
        ('nac', 'NAC', '1', 'kapsul', 'NOW NAC 600 mg'),
        ('probiyotik', 'Probiyotik', '1', 'doz', 'Garden of Life probiotic'),
        ('probiotic', 'Probiyotik', '1', 'doz', 'Garden of Life probiotic'),
        ('omega', 'Omega-3', '3', 'kapsul', 'Life Extension Mega EPA/DHA'),
        ('d3', 'D3+K2', '4', 'damla', 'Thorne Vitamin D + K2'),
        ('k2', 'D3+K2', '4', 'damla', 'Thorne Vitamin D + K2'),
        ('b-complex', 'B-Complex', '1', 'doz', 'Life Extension BioActive Complete B-Complex'),
        ('b complex', 'B-Complex', '1', 'doz', 'Life Extension BioActive Complete B-Complex'),
        ('goz', 'Goz Vitamini', '1', 'doz', 'Life Extension MacuGuard with Saffron'),
        ('göz', 'Goz Vitamini', '1', 'doz', 'Life Extension MacuGuard with Saffron'),
        ('cinko', 'Cinko', '1', 'kapsul', 'NOW Zinc Picolinate 50 mg'),
        ('zinc', 'Cinko', '1', 'kapsul', 'NOW Zinc Picolinate 50 mg'),
    ]
    actions = []
    seen = set()
    for key, name, default_amount, default_unit, note in catalog:
        if key not in norm or name in seen:
            continue
        line = next((ln for ln in text.splitlines() if key in ((tg_ascii_text(ln) if 'tg_ascii_text' in globals() else ln.lower()))), text)
        amount = default_amount
        unit = default_unit
        local = line.lower()
        if key in ['nac', 'probiyotik', 'probiotic', 'b-complex', 'b complex', 'goz', 'göz', 'cinko', 'zinc'] and '\n' not in text and len(text.split()) > 3:
            local = key
        m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(kapsul|kapsül|damla|doz|tablet|olcek|ölcek|ölçek|g|mg|iu)?', local)
        if name == 'D3+K2':
            m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(damla|drop)', line.lower()) or m
        if m:
            amount = m.group(1).replace(',', '.')
            if m.group(2):
                unit = m.group(2).replace('kapsül', 'kapsul').replace('ölçek', 'olcek').replace('ölcek', 'olcek')
        notes = note
        if 'gun asiri' in norm or 'gün aşırı' in text.lower() or 'asiri' in norm:
            if name == 'Cinko':
                notes += ' | gun asiri'
        actions.append({'type': 'vitamin', 'date': today, 'name': name, 'amount': amount, 'unit': unit, 'notes': notes})
        seen.add(name)
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

def tg_supplement_catalog():
    return [
        {'keys':['nac'], 'name':'NAC', 'amount':'1', 'unit':'kapsul', 'note':'NOW NAC 600 mg'},
        {'keys':['probiyotik','probiotic'], 'name':'Probiyotik', 'amount':'1', 'unit':'kapsul', 'note':'Garden of Life probiotic'},
        {'keys':['omega','epa','dha'], 'name':'Omega-3', 'amount':'3', 'unit':'kapsul', 'note':'Life Extension Mega EPA/DHA'},
        {'keys':['d3','k2','d+k'], 'name':'D3+K2', 'amount':'4', 'unit':'damla', 'note':'Thorne Vitamin D + K2'},
        {'keys':['b-complex','b complex','bcomplex'], 'name':'B-Complex', 'amount':'1', 'unit':'kapsul', 'note':'Life Extension BioActive Complete B-Complex'},
        {'keys':['goz','macuguard','saffron'], 'name':'Goz Vitamini', 'amount':'1', 'unit':'kapsul', 'note':'Life Extension MacuGuard with Saffron'},
        {'keys':['vitamin c','c vitamini','gold c'], 'name':'Vitamin C', 'amount':'1', 'unit':'kapsul', 'note':'California Gold Nutrition Gold C 1000 mg'},
        {'keys':['cinko','zinc'], 'name':'Cinko', 'amount':'1', 'unit':'kapsul', 'note':'NOW Zinc Picolinate 50 mg | gun asiri'},
        {'keys':['magtein','threonate','l-threonate'], 'name':'Magtein Magnesium L-Threonate', 'amount':'1', 'unit':'kapsul', 'note':'NOW Magtein Magnesium L-Threonate'},
        {'keys':['magnesium glycinate','magnezyum glisinat','glycinate'], 'name':'Magnesium Glycinate', 'amount':'3', 'unit':'kapsul', 'note':'NOW Magnesium Glycinate'},
        {'keys':['ashwagandha','ksm','ksm-66'], 'name':'KSM-66 Ashwagandha', 'amount':'1', 'unit':'kapsul', 'note':'NutraBio KSM-66 Ashwagandha'},
        {'keys':['glycine','glisin'], 'name':'Glycine', 'amount':'3', 'unit':'kapsul', 'note':'NOW Glycine 1000 mg'},
        {'keys':['melatonin'], 'name':'Melatonin', 'amount':'3', 'unit':'kapsul', 'note':'NOW Melatonin 1 mg'},
        {'keys':['theanine','l-theanine','l theanine'], 'name':'L-Theanine', 'amount':'1', 'unit':'kapsul', 'note':'NOW L-Theanine Double Strength 200 mg'},
        {'keys':['creatine','kreatin'], 'name':'Creatine', 'amount':'5', 'unit':'g', 'note':'KFD Creatine'},
        {'keys':['collagen','kolajen'], 'name':'Collagen Peptides', 'amount':'1', 'unit':'olcek', 'note':'Optimum Nutrition Collagen Peptides'},
        {'keys':['hydration','hydrationup','elektrolit','electrolyte'], 'name':'HydrationUP', 'amount':'1', 'unit':'paket', 'note':'California Gold Nutrition HydrationUP'},
        {'keys':['citrulline','sitrulin','l-citrulline'], 'name':'L-Citrulline', 'amount':'6', 'unit':'g', 'note':'L-Citrulline pre-workout'},
        {'keys':['beta alanine','beta-alanine'], 'name':'Beta Alanine', 'amount':'3', 'unit':'g', 'note':'Beta Alanine pre-workout'},
    ]

def tg_stack_preset(slot):
    return {
        'ac-karna': ['NAC', 'Probiyotik'],
        'sabah': ['Collagen Peptides', 'D3+K2', 'Omega-3', 'Magtein Magnesium L-Threonate', 'Goz Vitamini', 'B-Complex', 'Vitamin C', 'L-Theanine', 'Cinko'],
        'kahvalti': ['Collagen Peptides', 'D3+K2', 'Omega-3', 'Magtein Magnesium L-Threonate', 'Goz Vitamini', 'B-Complex', 'Vitamin C', 'L-Theanine', 'Cinko'],
        'ogle': ['Creatine', 'Collagen Peptides', 'HydrationUP'],
        'gece': ['L-Theanine', 'Magnesium Glycinate', 'KSM-66 Ashwagandha', 'Glycine', 'Melatonin'],
        'pre-workout': ['L-Citrulline', 'Beta Alanine', 'HydrationUP'],
        'post-workout': ['Creatine'],
    }.get(slot, [])

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

def tg_zinc_due_for_date(today):
    try:
        target = date.fromisoformat(today)
        conn = get_db()
        row = conn.execute("""
            SELECT date FROM vitamin_logs
            WHERE lower(name) IN ('cinko', 'zinc')
              AND date <= ?
            ORDER BY date DESC, id DESC
            LIMIT 1
        """, (today,)).fetchone()
        conn.close()
        if not row or not row['date']:
            return True
        last = date.fromisoformat(row['date'])
        return (target - last).days >= 2
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
    actions, seen = [], set()
    for item in matched:
        if item['name'] in seen or tg_supplement_item_missing(item, norm):
            continue
        explicit_item = any(k in norm for k in item['keys'])
        if item['name'] == 'Cinko' and slot in ('sabah', 'kahvalti') and not explicit_item and not tg_zinc_due_for_date(today):
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
        if 'Cinko' in wanted_names and 'Cinko' not in seen:
            cinko_item = next((it for it in catalog if it['name'] == 'Cinko'), None)
            explicitly_excluded = cinko_item and tg_supplement_item_missing(cinko_item, norm)
            if not explicitly_excluded:
                actions.append({'type': '_bot_note', 'text': '⚠️ Çinko alınmadı — not edildi'})
    return actions

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
    if 'before_sleep_count' in locals() and before_sleep_count > after_sleep_count:
        reply += "\n\nUyku notu: uyuyacağım/yatacağım ifadesini saat olarak algılamadım; uyku süresi kaydetmedim. Uyandığında kalkış saatini yazarsan gerçek süreyi işleriz."
    if template_title:
        reply += f"\n\nSablon hazir: {template_title}. Sablonlar sayfasinda dogru kategori altinda kullanabilirsin."
    if saved:
        reply += "\n\nKaydedildi: " + ", ".join(saved)
    tg_store_message('out', reply, chat_id, 'AI Coach', actions)
    await u.message.reply_text(reply)

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
        from telegram.ext import Application, CommandHandler, MessageHandler, filters
    except ImportError:
        release_telegram_bot_lock()
        log.warning("python-telegram-bot kurulu degil."); return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app2 = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [("start",cmd_start),("uyku",cmd_uyku),("egzersiz",cmd_egzersiz),
                    ("yemek",cmd_yemek),("su",cmd_su),("is",cmd_is),("antrenor",cmd_antrenor),
                    ("mood",cmd_mood),("vitamin",cmd_vitamin),("bugun",cmd_bugun),
                    ("rapor",cmd_rapor),("antrenman",cmd_antrenman),
                    ("hafta",cmd_hafta),("streak",cmd_streak)]:
        app2.add_handler(CommandHandler(cmd, fn))
    app2.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_chat_ai))
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
            "SELECT product_name, dose, unit FROM supplement_stack_items WHERE stack_id=? ORDER BY order_num",
            (s['id'],)).fetchall()
        s['items'] = [dict(i) for i in items]
    conn.close()
    return jsonify(stacks)

@app.route('/api/supplements/today', methods=['GET'])
def api_supplements_today():
    today = operation_today()
    conn = get_db()
    logs = [dict(r) for r in conn.execute(
        "SELECT * FROM supplement_logs WHERE date=? ORDER BY ts", (today,)).fetchall()]
    for log in logs:
        items = conn.execute(
            "SELECT * FROM supplement_log_items WHERE log_id=?", (log['id'],)).fetchall()
        log['items'] = [dict(i) for i in items]
    # Zinc status
    zinc_rule = conn.execute("SELECT rule_data FROM supplement_rules WHERE product_name='NOW Zinc Picolinate 50mg' AND rule_type='every_other_day'").fetchone()
    last_zinc = None
    if zinc_rule:
        import json as _json
        try: last_zinc = _json.loads(zinc_rule['rule_data'] or '{}').get('last_date')
        except: pass
    zinc_today = True  # default: take today
    if last_zinc:
        from datetime import date as _date, timedelta as _td
        last_d = _date.fromisoformat(last_zinc)
        diff = (_date.fromisoformat(today) - last_d).days
        zinc_today = diff >= 2  # every other day
    conn.close()
    return jsonify({'date': today, 'logs': logs, 'zinc': {'take_today': zinc_today, 'last_date': last_zinc}})

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
            conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
                         (today, pname, str(dose), unit, f'stack:{stack_name}'))

    # Extras
    for ex in extras:
        ename = ex.get('name', '')
        edose = ex.get('dose', '')
        eunit = ex.get('unit', '')
        conn.execute("INSERT INTO supplement_log_items (log_id,product_name_snapshot,dose_snapshot,unit_snapshot,taken,override_note) VALUES (?,?,?,?,1,'extra')",
                     (log_id, ename, edose, eunit))
        conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
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
