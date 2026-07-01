#!/usr/bin/env python3
"""Taha Serdem — Standalone Telegram Bot (Flask gerekmez)"""

import os, sys, sqlite3, asyncio, json, logging, re
from datetime import datetime, date, timedelta

# Windows asyncio fix
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.environ.get('DATA_DIR', BASE_DIR)
DB_PATH     = os.path.join(DATA_DIR, 'tracker.db')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# Railway env override for API keys
if os.environ.get('TELEGRAM_TOKEN'):
    pass  # will be read below
if os.environ.get('ANTHROPIC_API_KEY'):
    pass  # will be read below

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

# CONFIG
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding='utf-8-sig') as f:
            return json.load(f)
    return {}

_cfg = load_config()
TELEGRAM_TOKEN    = os.environ.get('TELEGRAM_TOKEN',    _cfg.get('TELEGRAM_TOKEN', ''))
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', _cfg.get('ANTHROPIC_API_KEY', ''))
OPENAI_API_KEY    = os.environ.get('OPENAI_API_KEY', _cfg.get('OPENAI_API_KEY', ''))
OPENAI_MODEL      = os.environ.get('OPENAI_MODEL', _cfg.get('OPENAI_MODEL', 'gpt-4o'))
ANTHROPIC_MODEL   = 'claude-haiku-4-5-20251001'
CYCLE_START       = _cfg.get('CYCLE_START', date.today().isoformat())
TRAINING_CYCLE    = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
OPERATION_DAY_CUTOFF_HOUR = int(os.environ.get('OPERATION_DAY_CUTOFF_HOUR', _cfg.get('OPERATION_DAY_CUTOFF_HOUR', 6)))

# ─── VARSAYILAN ÜRÜN SİSTEMİ ─────────────────────────────────────────────────
# Kullanıcı generic isim yazarsa otomatik resmi ürüne yönlendir
DEFAULT_PRODUCTS = {
    'patates':  'Mączyste Patates',
    'potato':   'Mączyste Patates',
    'pirinç':   'YASMİN Pirinci',
    'pirinc':   'YASMİN Pirinci',
    'rice':     'YASMİN Pirinci',
    'yoğurt':   'Skyr Yoğurt',
    'yogurt':   'Skyr Yoğurt',
    'yumurta':  'Carrefour BIO Yumurta',
    'egg':      'Carrefour BIO Yumurta',
    'tavuk':    'Tavuk Göğsü',
    'chicken':  'Tavuk Göğsü',
}

# Çiğ gramaj gerektiren ürünler (kullanıcı "pişmiş" demediği sürece)
RAW_BY_DEFAULT = {'pirinç','pirinc','rice','patates','potato','tavuk','chicken','et','meat','hindi','turkey','balik','balık','fish'}
# ──────────────────────────────────────────────────────────────────────────────

# SHIFT_AWARE_OPERATION_DAY_V1
SHIFT_TRANSITION_DATE = date(2026, 6, 22)
SHIFT_BLOCK_DAYS = 14

def current_shift_info(now=None):
    """
    Vardiyaya gore operasyon gunu kapanisini belirler.
    - 2026-06-22'ye kadar: 15:00-00:00, gun 06:00'da kapanir.
    - 2026-06-22 itibariyle 2 hafta 06:00-15:00, gun 03:00'te kapanir.
    - Sonraki 2 hafta 21:00-06:00, gun 12:00'de kapanir.
    Sonra 2 haftalik sabah/gece dongusu devam eder.
    """
    now = now or datetime.now()
    base = now.date()
    if base < SHIFT_TRANSITION_DATE:
        return {
            'name': '15:00-00:00',
            'label': 'aksam vardiyasi',
            'start': '15:00',
            'end': '00:00',
            'cutoff_hour': 6,
            'late_window': '00:00-05:59',
        }

    block = ((base - SHIFT_TRANSITION_DATE).days // SHIFT_BLOCK_DAYS) % 2
    if block == 0:
        return {
            'name': '06:00-15:00',
            'label': 'sabah vardiyasi',
            'start': '06:00',
            'end': '15:00',
            'cutoff_hour': 3,
            'late_window': '00:00-02:59',
        }
    return {
        'name': '21:00-06:00',
        'label': 'gece vardiyasi',
        'start': '21:00',
        'end': '06:00',
        'cutoff_hour': 12,
        'late_window': '00:00-11:59',
    }

def operation_cutoff_hour(now=None):
    return int(current_shift_info(now).get('cutoff_hour') or OPERATION_DAY_CUTOFF_HOUR)


def operation_date(now=None):
    """Vardiyaya gore Taha'nin operasyon/log gununu hesaplar."""
    now = now or datetime.now()
    d = now.date()
    if 0 <= now.hour < operation_cutoff_hour(now):
        d = d - timedelta(days=1)
    return d

def operation_today():
    return operation_date().isoformat()

# DB
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
        CREATE TABLE IF NOT EXISTS daily_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE, note TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS meal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, slot TEXT NOT NULL, title TEXT, description TEXT,
            calories INTEGER, protein_g REAL, carbs_g REAL, fat_g REAL, fiber_g REAL,
            source TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS quick_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, category TEXT, title TEXT NOT NULL,
            description TEXT, calories INTEGER, protein_g REAL, carbs_g REAL,
            fat_g REAL, fiber_g REAL, water_ml INTEGER, amount TEXT, unit TEXT,
            notes TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS step_logs (
            date TEXT PRIMARY KEY, steps INTEGER DEFAULT 0,
            notes TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS body_metrics (
            date TEXT PRIMARY KEY, weight_kg REAL, waist_cm REAL,
            chest_cm REAL, arm_cm REAL, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS training_day_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, training_day TEXT NOT NULL,
            exercise TEXT NOT NULL, program_exercise_id INTEGER,
            sets_json TEXT, notes TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL, chat_id TEXT, username TEXT,
            message TEXT NOT NULL, actions TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, training_day TEXT, exercise TEXT NOT NULL,
            set_num INTEGER, weight TEXT, reps TEXT, notes TEXT, set_type TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_profile (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    ''')
    conn.commit(); conn.close()

def water_consolidate(conn, date_val, new_total_ml):
    """Tum water_ml satirlarini sifirla, ilk satirda toplamı yaz. SUM hatasi olmaz."""
    conn.execute("UPDATE nutrition_logs SET water_ml=0 WHERE date=?", (date_val,))
    row = conn.execute("SELECT id FROM nutrition_logs WHERE date=?", (date_val,)).fetchone()
    if row:
        conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE id=?", (new_total_ml, row['id']))
    else:
        conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (date_val, new_total_ml))

def water_get_total(conn, date_val):
    row = conn.execute("SELECT SUM(water_ml) as total FROM nutrition_logs WHERE date=?", (date_val,)).fetchone()
    return int(row['total'] or 0)

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

def weekly_ai_context():
    """Son 7 gunun ozet verisini Claude'a ver — trend analizi icin."""
    conn = get_db()
    today = operation_date()
    days = []
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        sl = conn.execute("SELECT hours, quality FROM sleep_logs WHERE date=?", (d,)).fetchone()
        ex = conn.execute("SELECT type, duration FROM exercise_logs WHERE date=?", (d,)).fetchone()
        nu = conn.execute("SELECT SUM(water_ml) as wml FROM nutrition_logs WHERE date=?", (d,)).fetchone()
        mo = conn.execute("SELECT energy, mood, stress FROM mood_logs WHERE date=?", (d,)).fetchone()
        bm = conn.execute("SELECT weight_kg FROM body_metrics WHERE date=?", (d,)).fetchone()
        mac = meal_macro_totals(d)
        days.append({
            'tarih': d,
            'uyku_s': round(float(sl['hours']),1) if sl and sl['hours'] else None,
            'uyku_kalite': int(sl['quality']) if sl and sl['quality'] else None,
            'egzersiz': ex['type'] if ex and ex['type'] else None,
            'kalori': mac['calories'] or None,
            'protein': mac['protein_g'] or None,
            'su_l': round((nu['wml'] or 0)/1000, 2) if nu else 0,
            'enerji': int(mo['energy']) if mo and mo['energy'] else None,
            'kilo': float(bm['weight_kg']) if bm and bm['weight_kg'] else None,
        })
    conn.close()

    # Ozet istatistikler
    def avg(key):
        vals = [d[key] for d in days if d.get(key) is not None]
        return round(sum(vals)/len(vals), 1) if vals else None

    training_days = sum(1 for d in days if d['egzersiz'])
    return {
        'son_7_gun': days,
        'ortalama': {
            'uyku_s': avg('uyku_s'),
            'kalori': avg('kalori'),
            'protein': avg('protein'),
            'su_l': avg('su_l'),
            'enerji': avg('enerji'),
        },
        'antrenman_gun': training_days,
        'son_kilo': next((d['kilo'] for d in reversed(days) if d['kilo']), None),
    }

def extended_context():
    """Agirlik trendi, antrenman PR'lari, ogun kaliplari, uyku ortalaması — bot zekası için."""
    conn = get_db()
    today = operation_date()
    lines = []

    # 1. Ağırlık trendi (son 30 gün)
    weights = conn.execute(
        "SELECT date, weight_kg, weight_kg_night FROM body_metrics "
        "WHERE date >= ? AND (weight_kg IS NOT NULL OR weight_kg_night IS NOT NULL) ORDER BY date DESC LIMIT 20",
        ((today - timedelta(days=30)).isoformat(),)
    ).fetchall()
    if weights:
        lines.append('KILO TRENDI (son 30 gun, en yeni once):')
        for w in weights[:7]:
            sabah = f"{w['weight_kg']}kg" if w['weight_kg'] else '—'
            gece  = f"{w['weight_kg_night']}kg" if w['weight_kg_night'] else ''
            lines.append(f"  {w['date']}: sabah={sabah}" + (f" gece={gece}" if gece else ''))
        if len(weights) >= 3:
            first_w = next((w['weight_kg'] for w in reversed(weights) if w['weight_kg']), None)
            last_w  = next((w['weight_kg'] for w in weights if w['weight_kg']), None)
            if first_w and last_w:
                diff = round(last_w - first_w, 1)
                lines.append(f"  Trend: {'+' if diff>0 else ''}{diff}kg son 30 gunde")

    # 2. Antrenman PR'ları (her harekette en yüksek ağırlık)
    try:
        prs = conn.execute(
            "SELECT exercise, MAX(CAST(REPLACE(REPLACE(weight,' kg',''),'kg','') AS REAL)) as max_w, "
            "MAX(CAST(reps AS INTEGER)) as max_r "
            "FROM workout_logs WHERE weight IS NOT NULL AND weight != '' "
            "GROUP BY lower(exercise) ORDER BY COUNT(*) DESC LIMIT 12"
        ).fetchall()
        if prs:
            lines.append('ANTRENMAN PR / EN YUK AGIRLIKLAR:')
            for pr in prs:
                if pr['max_w']:
                    lines.append(f"  {pr['exercise']}: {pr['max_w']}kg x {pr['max_r'] or '?'} tekrar")
    except Exception:
        pass

    # 3. En sık yenen öğünler (öğün kalıpları)
    try:
        common_meals = conn.execute(
            "SELECT slot, title, COUNT(*) as cnt, AVG(calories) as avg_cal, "
            "AVG(protein_g) as avg_p, AVG(carbs_g) as avg_k, AVG(fat_g) as avg_y "
            "FROM meal_entries WHERE calories > 0 GROUP BY lower(title) "
            "ORDER BY cnt DESC LIMIT 15"
        ).fetchall()
        if common_meals:
            lines.append('EN SIK YENILEN OGUNLER (aliskanlik profili):')
            for m in common_meals:
                lines.append(
                    f"  [{m['slot']}] {m['title']} ({m['cnt']}x): "
                    f"~{round(m['avg_cal'])}kcal P:{round(m['avg_p'] or 0)}g "
                    f"K:{round(m['avg_k'] or 0)}g Y:{round(m['avg_y'] or 0)}g"
                )
    except Exception:
        pass

    # 4. Uyku ortalaması (son 14 gün)
    try:
        sleep_avg = conn.execute(
            "SELECT AVG(hours) as avg_h, AVG(quality) as avg_q FROM sleep_logs "
            "WHERE date >= ?", ((today - timedelta(days=14)).isoformat(),)
        ).fetchone()
        if sleep_avg and sleep_avg['avg_h']:
            lines.append(f"UYKU ORTALAMA (son 14 gun): {round(sleep_avg['avg_h'],1)}s | kalite {round(sleep_avg['avg_q'] or 0,1)}/10")
    except Exception:
        pass

    # 5. Bu hafta antrenman yaptığı günler
    try:
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        ex_week = conn.execute(
            "SELECT date, type FROM exercise_logs WHERE date >= ? ORDER BY date",
            (week_start,)
        ).fetchall()
        if ex_week:
            lines.append(f"BU HAFTA ANTRENMAN ({len(ex_week)} gun): " +
                         ', '.join(f"{r['date'][-5:]} {r['type']}" for r in ex_week))
        else:
            lines.append('BU HAFTA ANTRENMAN: Henüz yok')
    except Exception:
        pass

    conn.close()
    return ('\n'.join(lines) + '\n') if lines else ''

def user_profile_context():
    """Kullanici profili, hedefler ve site ayarlarini DB'den oku."""
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS user_profile (
        key TEXT PRIMARY KEY, value TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)""")
    profile_rows = conn.execute("SELECT key, value FROM user_profile").fetchall()
    # Sitedeki hedefleri de oku
    try:
        settings_rows = conn.execute("SELECT key, value FROM user_settings").fetchall()
    except Exception:
        settings_rows = []
    conn.close()

    lines = []
    settings = {r['key']: r['value'] for r in settings_rows}
    if settings:
        name_map = {'cal': 'kalori_hedef', 'prot': 'protein_hedef_g',
                    'carb': 'karb_hedef_g', 'fat': 'yag_hedef_g',
                    'water': 'su_hedef_ml', 'weight_goal': 'kilo_hedef_kg'}
        lines.append('HEDEFLER (siteden):')
        for k, v in settings.items():
            label = name_map.get(k, k)
            lines.append(f"  {label}: {v}")

    profile = {r['key']: r['value'] for r in profile_rows}
    if profile:
        lines.append('KULLANICI PROFILI:')
        for k, v in profile.items():
            lines.append(f"  {k}: {v}")

    return ('\n'.join(lines) + '\n') if lines else ''

def user_profile_set(key, value):
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profile (
            key TEXT PRIMARY KEY, value TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT OR REPLACE INTO user_profile (key, value) VALUES (?,?)", (key, value))
    conn.commit(); conn.close()

def seed_food_db_from_history():
    """meal_entries gecmisinden quick_templates'i doldur (bir kez, startup'ta)."""
    conn = get_db()
    # Sadece kalori bilinen, tekrar eden yemekleri al
    rows = conn.execute("""
        SELECT title, description,
               ROUND(AVG(calories)) as cal,
               ROUND(AVG(protein_g),1) as prot,
               ROUND(AVG(carbs_g),1) as carb,
               ROUND(AVG(fat_g),1) as fat,
               slot, COUNT(*) as cnt
        FROM meal_entries
        WHERE title IS NOT NULL AND calories > 0
        GROUP BY lower(title)
        HAVING cnt >= 1
    """).fetchall()
    added = 0
    for r in rows:
        existing = conn.execute(
            "SELECT id FROM quick_templates WHERE kind='meal' AND lower(title)=lower(?)", (r['title'],)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO quick_templates (kind, category, title, description, calories, protein_g, carbs_g, fat_g, notes) "
                "VALUES ('meal', ?, ?, ?, ?, ?, ?, ?, 'history-seed')",
                (r['slot'] or 'extra', r['title'], r['description'] or '',
                 r['cal'], r['prot'], r['carb'], r['fat'])
            )
            added += 1
    conn.commit()
    conn.close()
    if added:
        log.info("Food DB: %d yemek gecmisten ogrenildi", added)

try:
    init_db()
except Exception:
    pass
try:
    seed_food_db_from_history()
except Exception as e:
    log.warning("seed_food_db_from_history skip: %s", e)

def seed_supplement_stack():
    """Kullanicinin bilinen supplement protokolunu quick_templates'e ekle (yoksa)."""
    DEFAULT_SUPPS = [
        ('Probiyotik',      '1',  'kapsul'),
        ('B Complex',       '1',  'kapsul'),
        ('Omega 3',         '3',  'kapsul'),
        ('D3+K2',           '1',  'kapsul'),  # 4000 IU
        ('Göz Vitamini',    '1',  'kapsul'),
        ('C Vitamini',      '1',  'tablet'),  # 1000 mg
    ]
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) as c FROM quick_templates WHERE kind='supplement'").fetchone()['c']
    if existing == 0:
        for title, amount, unit in DEFAULT_SUPPS:
            conn.execute(
                "INSERT INTO quick_templates (kind, category, title, amount, unit, notes) VALUES (?,?,?,?,?,?)",
                ('supplement', 'supplement', title, amount, unit, 'default-stack')
            )
        conn.commit()
        log.info("Supplement stack varsayilan olarak yuklendi.")
    conn.close()

seed_supplement_stack()

def get_owner_chat_id():
    """Kayitli owner chat_id'yi user_profile'den al."""
    conn = get_db()
    row = conn.execute("SELECT value FROM user_profile WHERE key='owner_chat_id'").fetchone()
    conn.close()
    return int(row['value']) if row else None

def save_owner_chat_id(chat_id):
    user_profile_set('owner_chat_id', str(chat_id))

# TRAINING
def training_day(date_str):
    # Weekday-based: Mon=Push, Tue=Pull, Wed=Leg, Thu=Upper, Fri=Lower, Sat=Off, Sun=Off
    WEEKDAY_CYCLE = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
    d = date.fromisoformat(date_str)
    return WEEKDAY_CYCLE[d.weekday()]

# HELPERS
def norm_tr(text):
    t = (text or '').lower()
    for a, b in [('ı','i'),('İ','i'),(chr(287),'g'),(chr(286),'g'),
                 (chr(252),'u'),(chr(220),'u'),(chr(351),'s'),(chr(350),'s'),
                 (chr(246),'o'),(chr(214),'o'),(chr(231),'c'),(chr(199),'c')]:
        t = t.replace(a, b)
    return t

def streak_count():
    conn = get_db()
    n, d = 0, operation_date()
    tables = ('sleep_logs','exercise_logs','nutrition_logs','work_logs','coaching_logs','mood_logs')
    while True:
        found = any(conn.execute(f"SELECT id FROM {t} WHERE date=?", (d.isoformat(),)).fetchone() for t in tables)
        if not found: break
        n += 1; d -= timedelta(days=1)
    conn.close()
    return n

def meal_macro_totals(date_str):
    conn = get_db()
    rows = conn.execute("SELECT calories, protein_g, carbs_g, fat_g FROM meal_entries WHERE date=?", (date_str,)).fetchall()
    conn.close()
    totals = {'calories': 0, 'protein_g': 0.0, 'carbs_g': 0.0, 'fat_g': 0.0}
    for r in rows:
        for k in totals:
            totals[k] += float(r[k] or 0)
    return {k: round(v, 1) for k, v in totals.items()}

def today_summary():
    today = operation_today()
    conn = get_db()
    sl  = conn.execute("SELECT * FROM sleep_logs   WHERE date=?", (today,)).fetchone()
    ex  = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
    nu  = conn.execute("SELECT SUM(water_ml) as water_ml FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    w   = conn.execute("SELECT * FROM work_logs    WHERE date=?", (today,)).fetchone()
    co  = conn.execute("SELECT * FROM coaching_logs WHERE date=?", (today,)).fetchone()
    mo  = conn.execute("SELECT * FROM mood_logs    WHERE date=?", (today,)).fetchone()
    conn.close()
    totals = meal_macro_totals(today)
    td = training_day(today)
    sr = streak_count()
    lines = [f"BUGUN {today} | {sr} gun seri | {td}\n"]
    lines.append("Uyku: "        + (f"{sl['hours']}s kalite {sl['quality']}/10" if sl and sl['hours'] else "-"))
    lines.append("Egzersiz: "    + (f"{ex['type']} {ex['duration']}dk" if ex and ex['type'] else "-"))
    lines.append("Kalori: "      + (f"{totals['calories']} kcal | P {totals['protein_g']}g K {totals['carbs_g']}g Y {totals['fat_g']}g" if totals['calories'] else "-"))
    lines.append("Su: "          + (f"{(nu['water_ml'] or 0)/1000:.1f}L" if nu and nu['water_ml'] else "-"))
    lines.append("Is: "          + (f"{w['hours']}s" if w and w['hours'] else "-"))
    lines.append("Antrenorluk:" + (f" {co['sessions']} seans" if co and co['sessions'] else " -"))
    lines.append("Ruh hali: "    + (f"enerji {mo['energy']} mood {mo['mood']} stres {mo['stress']}" if mo and mo['energy'] else "-"))
    return '\n'.join(lines)

# AI
def today_ai_context():
    today = operation_today()
    totals = meal_macro_totals(today)
    conn = get_db()
    sl  = conn.execute("SELECT * FROM sleep_logs    WHERE date=?", (today,)).fetchone()
    ex  = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
    nu  = conn.execute("SELECT SUM(water_ml) as water_ml FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    mo  = conn.execute("SELECT * FROM mood_logs     WHERE date=?", (today,)).fetchone()
    vs  = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY ts", (today,)).fetchall()]
    conn.close()
    return {
        'date': today,
        'training_day': training_day(today),
        'macros': totals,
        'water_l': round(((dict(nu).get('water_ml') or 0) if nu else 0) / 1000, 2),
        'sleep': dict(sl) if sl else {},
        'exercise': dict(ex) if ex else {},
        'mood': dict(mo) if mo else {},
        'vitamins': vs,
    }



TAHA_COACHING_POLICY = """
TAHA ICIN KALICI KOCLUK HAFIZASI:
- Turkce, kisa, net ve profesyonel sporcu kocu gibi yaz. Gereksiz tekrar yapma.
- Her ogunu ayri hesapla: once besin kalemleri, sonra ogun toplami, sonra kisa yorum.
- Gun sonunda kalori, protein, karbonhidrat ve yag toplamini ver; tahminse tahmin oldugunu soyle.
- Hatalari durustce belirt ama panik yaptirma.
- Hedefler: yag kaybi, kas korunumu/kazanimi, performans, akne takibi, sindirim ve genel saglik.

GENEL HESAP KURALLARI:
- Tum gramajlar aksi belirtilmedikce CIG gramdir. Pisirmis diyene kadar cig hesapla.
- Cig kabul edilenler: pirinc, patates, tavuk, et, hindi, balik. Kullanici "pismis" demezse cig kullan.
- Ekstra yag belirtilmedikce eklenmez.
- GymBeam Sprey Yag yalniz kullanici fis/basis sayisi soylerse eklenir.

VARSAYILAN URUN ESLESTIRMESI (kullanici generic isim yazarsa bu urune yon):
- "pirinc" / "pirinç" / "rice" = YASMiN Pirinci (PROD-010)
- "patates" / "potato" = Maczyste Patates (PROD-011)
- "yogurt" / "yoğurt" = Skyr Yogurt (PROD-013)
- "yumurta" / "egg" = Carrefour BIO Yumurta (PROD-008)
- "tavuk" / "chicken" = Tavuk Gogsu

KAYITLI URUNLER VE RESMI MAKROLARI (MASTER SPEC v1.0):
PROD-001 Dondurulmus Patates: 100g = 99 kcal, 1.9P, 15K, 3.1Y
PROD-002 GymBeam Sprey Yag: 1 fis = 15 kcal, 0P, 0K, 1.65Y
PROD-003 Sekersiz Badem Sutu: 100ml = 14 kcal, 0.5P, 0K, 1.1Y
PROD-004 Salatalik Tursusu: 100g = 18 kcal, 0.9P, 1.92K, 0Y
PROD-005 Keto Ketcap: 100g = 41 kcal, 2P, 6.2K, 0.5Y (20-30g kullanim ihmal edilebilir)
PROD-006 Sivi Yumurta Aki: 100g = 58 kcal, 10.3P, 1.2K, 0.8Y
PROD-007 Carrefour Tam Tahilli Tost Ekmegi: 100g = 252 kcal, 9.5P, 45K, 2.1Y
PROD-008 Carrefour BIO Yumurta: 1 adet = 70 kcal, 6P, 0.5K, 5Y [source=manual]
PROD-009 Kakao: 100g = 309 kcal, 24P, 13K, 11Y
PROD-010 YASMiN Pirinci: 100g CIG = 346 kcal, 7.6P, 77K, 0.5Y
PROD-011 Maczyste Patates: 100g CIG = 77 kcal, 2P, 17K, 0.1Y [source=manual]
PROD-012 Cikolatali Protein Bar 33%: 1 bar (50g) = 193 kcal, 16.5P, 11.5K, 9Y
PROD-013 Skyr Yogurt: 100g = 64 kcal, 12P, 4.1K, 0Y | 150g = 96 kcal, 18P, 6.2K, 0Y
PROD-014 Tavuk Baharati: 100g = 286 kcal, 18.1P, 50.4K, 8.2Y | 5g = 14 kcal (gram belirtilmezse hesaplama yapma)
Tavuk Gogsu (cig): 100g = 115 kcal, 23P, 0K, 1.5Y
Cilek: 100g = 32 kcal, 0.7P, 7.7K, 0.3Y
Salatalik: 100g = 15 kcal, 0.7P, 3.6K, 0.1Y

STANDART PANCAKE V2:
- 4 yumurta, 200g sivi yumurta beyazi, 25g yulaf, 50g kuru kayisi, 200g cilek, 50ml sekersiz badem sutu, 6g kakao, 2 fis GymBeam.

SUPPLEMENT SISTEMI (MASTER SPEC v1.0):
Stackler:
1. Ac Karna Stack: NOW NAC 600mg (1 kapsul), Garden of Life Probiotic (1 kapsul)
2. Sabah Stack: Life Extension Mega EPA/DHA (3 kapsul), Thorne D+K2 (4 damla), Life Extension B-Complex (1 kapsul), Life Extension MacuGuard (1 kapsul), California Gold C (1 kapsul), NOW Magtein (1 kapsul), NOW L-Theanine Double Strength (1 kapsul), ON Collagen (1 olcek), NOW Zinc Picolinate 50mg (1 kapsul - GUN ASIRI)
3. Pre Workout Stack: Elektrolit (8g), Citrulline (8g), Taurine (2g), Beta Alanine (2g)
4. Post Workout Stack: Creatine Monohydrate (5g)
5. Gece Stack: Magnesium Glycinate (3 kapsul), KSM-66 Ashwagandha (1 kapsul), Glycine (3 kapsul), Melatonin (3 kapsul), NOW L-Theanine Double Strength (1 kapsul)
CINKO: Gun asiri; kullanici acikca "cinko alindı/almadim" demezse Sabah Stack'te cinkoyu exclude et.
OVERRIDE: "ama/fakat X kapsul/g URUN" → o urun icin doz override.
EKSTRA: "+ X g/kapsul URUN" → stack'e ekstra ekle.
Snapshot: Gecmis kayitlar degismez.

AKNE VE CILT:
- Whey, yogurt, protein puding ve yuksek seker akne acisindan takip edilir.
- Kreatin su an kullanilmiyor; akne gozlemi icin bunu koru.
- Cilt bariyeri hassas. Is sonrasi dus: nemlendirici. Gece: CeraVe temizleyici, Akneroxid, nemlendirici.

ANTRENMAN:
- Dongu: Pzt=Push, Sal=Pull, Car=Leg, Per=Upper, Cum=Lower, Cmt/Paz=Off (haftalik sabit).
- Sistem tarafindaki resmi antrenman gunu esas alinir; foto veya AI tahminiyle degistirme.

OGUN SLOT SISTEMI (guncel):
- kahvalti = KAHVALTI
- snack = SNACK (ara ogun 1)
- meal1 = MEAL1 (ana ogun)
- pre-workout-meal = PRE-WORKOUT MEAL
- post-workout-meal = POST-WORKOUT MEAL
- snack2 = SNACK 2 (gece/son ara ogun)
Eski slot adlari (ogle, aksam, ara, atistirma, gece) KALDIRILDI. Bunlari kullanma.

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


def food_db_search(text, limit=8):
    """Kullanicinin mesajinda gecen yiyecekleri quick_templates'ten fuzzy ara."""
    words = [w for w in norm_tr(text).split() if len(w) >= 3]
    if not words:
        return []
    conn = get_db()
    # Tum meal template'lerini cek, kelime overlap ile skora gore sirala
    all_foods = conn.execute(
        "SELECT title, description, calories, protein_g, carbs_g, fat_g, notes FROM quick_templates WHERE kind='meal'"
    ).fetchall()
    conn.close()
    scored = []
    for r in all_foods:
        title_n = norm_tr(r['title'] or '')
        desc_n  = norm_tr(r['description'] or '')
        score = 0
        for w in words:
            if w in title_n:
                score += 3  # title match daha degerli
            elif w in desc_n:
                score += 1
            # Fuzzy: kelimenin ilk 4 harfi eslesiyor mu
            elif any(w[:4] in part for part in (title_n + ' ' + desc_n).split() if len(part) >= 4):
                score += 1
        if score > 0:
            # Kullanicinin sabit marka urunleri genel/eski sablonlardan once gelir.
            if 'brand-fixed' in (r['notes'] or ''):
                score += 100
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]

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


def stack_slot_from_text(raw_text):
    norm = norm_tr(raw_text or "")
    if any(w in norm for w in ["ac karna", "ackarna", "fasted", "sabah ac"]):
        return "ac-karna"
    if any(w in norm for w in ["gece", "uyku", "yatmadan"]):
        return "gece"
    if any(w in norm for w in ["pre stack", "pre ", "pre workout", "pre-workout", "preworkout", "idman oncesi", "antrenman oncesi"]):
        return "pre-workout"
    if any(w in norm for w in ["post stack", "post ", "post workout", "post-workout", "postworkout", "idman sonrasi", "antrenman sonrasi"]):
        return "post-workout"
    if any(w in norm for w in ["ogle", "oglen"]):
        return "ogle"
    if any(w in norm for w in ["sabah", "kahvalti"]):
        return "sabah"
    return ""

def stack_label(slot):
    return {
        "ac-karna": "Aç Karna Stack",
        "sabah": "Sabah Stack",
        "kahvalti": "Sabah Stack",
        "ogle": "Öğle Stack",
        "gece": "Gece Stack",
        "pre-workout": "Pre-Workout Stack",
        "post-workout": "Post-Workout",
    }.get(slot, "Supplement Stack")

def supplement_catalog():
    return [
        {"keys":["nac"], "name":"NAC", "amount":"1", "unit":"kapsul", "note":"NOW NAC 600 mg"},
        {"keys":["probiyotik","probiotic"], "name":"Probiyotik", "amount":"1", "unit":"kapsul", "note":"Garden of Life probiotic"},
        {"keys":["omega","epa","dha"], "name":"Omega-3", "amount":"3", "unit":"kapsul", "note":"Life Extension Mega EPA/DHA"},
        {"keys":["d3","k2","d+k"], "name":"D3+K2", "amount":"4", "unit":"damla", "note":"Thorne Vitamin D + K2"},
        {"keys":["b-complex","b complex","bcomplex"], "name":"B-Complex", "amount":"1", "unit":"kapsul", "note":"Life Extension BioActive Complete B-Complex"},
        {"keys":["goz","macuguard","saffron"], "name":"Goz Vitamini", "amount":"1", "unit":"kapsul", "note":"Life Extension MacuGuard with Saffron"},
        {"keys":["vitamin c","c vitamini","gold c"], "name":"Vitamin C", "amount":"1", "unit":"kapsul", "note":"California Gold Nutrition Gold C 1000 mg"},
        {"keys":["cinko","zinc"], "name":"Cinko", "amount":"1", "unit":"kapsul", "note":"NOW Zinc Picolinate 50 mg | gun asiri"},
        {"keys":["magtein","threonate","l-threonate"], "name":"Magtein Magnesium L-Threonate", "amount":"1", "unit":"kapsul", "note":"NOW Magtein Magnesium L-Threonate"},
        {"keys":["magnesium glycinate","magnezyum glisinat","glycinate"], "name":"Magnesium Glycinate", "amount":"3", "unit":"kapsul", "note":"NOW Magnesium Glycinate"},
        {"keys":["ashwagandha","ksm","ksm-66"], "name":"KSM-66 Ashwagandha", "amount":"1", "unit":"kapsul", "note":"NutraBio KSM-66 Ashwagandha"},
        {"keys":["glycine","glisin"], "name":"Glycine", "amount":"3", "unit":"kapsul", "note":"NOW Glycine 1000 mg"},
        {"keys":["melatonin"], "name":"Melatonin", "amount":"3", "unit":"kapsul", "note":"NOW Melatonin 1 mg"},
        {"keys":["theanine","l-theanine","l theanine"], "name":"L-Theanine", "amount":"1", "unit":"kapsul", "note":"NOW L-Theanine Double Strength 200 mg"},
        {"keys":["creatine","kreatin"], "name":"Creatine", "amount":"5", "unit":"g", "note":"KFD Creatine"},
        {"keys":["collagen","kolajen"], "name":"Collagen Peptides", "amount":"1", "unit":"olcek", "note":"Optimum Nutrition Collagen Peptides"},
        {"keys":["hydration","hydrationup","elektrolit","electrolyte"], "name":"HydrationUP", "amount":"1", "unit":"paket", "note":"California Gold Nutrition HydrationUP"},
        {"keys":["citrulline","sitrulin","l-citrulline"], "name":"L-Citrulline", "amount":"6", "unit":"g", "note":"L-Citrulline pre-workout"},
        {"keys":["beta alanine","beta-alanine"], "name":"Beta Alanine", "amount":"3", "unit":"g", "note":"Beta Alanine pre-workout"},
    ]

def stack_preset(slot):
    return {
        "ac-karna": ["NAC", "Probiyotik"],
        "sabah": ["Collagen Peptides", "D3+K2", "Omega-3", "Magtein Magnesium L-Threonate", "Goz Vitamini", "B-Complex", "Vitamin C", "L-Theanine", "Cinko"],
        "kahvalti": ["Collagen Peptides", "D3+K2", "Omega-3", "Magtein Magnesium L-Threonate", "Goz Vitamini", "B-Complex", "Vitamin C", "L-Theanine", "Cinko"],
        "ogle": ["Creatine", "Collagen Peptides", "HydrationUP"],
        "gece": ["L-Theanine", "Magnesium Glycinate", "KSM-66 Ashwagandha", "Glycine", "Melatonin"],
        "pre-workout": ["L-Citrulline", "Beta Alanine", "HydrationUP"],
        "post-workout": ["Creatine"],
    }.get(slot, [])

def zinc_due_for_date(today):
    try:
        target = date.fromisoformat(today)
        conn = get_db()
        row = conn.execute("""
            SELECT date FROM vitamin_logs
            WHERE lower(name) IN ('cinko', 'zinc')
              AND date < ?
            ORDER BY date DESC, id DESC
            LIMIT 1
        """, (today,)).fetchone()
        conn.close()
        if not row or not row["date"]:
            return True
        return (target - date.fromisoformat(row["date"])).days >= 2
    except Exception:
        return True

def zinc_explicitly_taken(norm):
    has_zinc = any(k in norm for k in ['cinko', 'zinc'])
    if not has_zinc:
        return False
    negative = [
        r'(cinko|zinc).{0,50}(almadim|alinmadi|alma|yok|haric|eksik)',
        r'(almadim|alinmadi|alma|yok|haric|eksik).{0,50}(cinko|zinc)'
    ]
    if any(re.search(p, norm) for p in negative):
        return False
    positive = [
        r'(cinko|zinc).{0,50}(aldim|alindi|ictim|tamam)',
        r'(aldim|alindi|ictim|tamam).{0,50}(cinko|zinc)'
    ]
    return any(re.search(p, norm) for p in positive)

def item_missing_in_text(item, norm):
    for key in [item["name"].lower()] + item["keys"]:
        k = norm_tr(key)
        if re.search(re.escape(k) + r".{0,28}(eksik|icmedim|almadim|alma|haric|yok)", norm):
            return True
        if re.search(r"(eksik|icmedim|almadim|haric|yok).{0,28}" + re.escape(k), norm):
            return True
    return False



def profile_get(key, default=''):
    try:
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS user_profile (key TEXT PRIMARY KEY, value TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)")
        row = conn.execute("SELECT value FROM user_profile WHERE key=?", (key,)).fetchone()
        conn.close()
        return row['value'] if row else default
    except Exception:
        return default

def profile_set(key, value):
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS user_profile (key TEXT PRIMARY KEY, value TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT OR REPLACE INTO user_profile (key,value) VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()

def stack_apply_overrides(slot, item):
    if not slot or not item:
        return item
    name = item.get('name') or ''
    amount = profile_get(f"stack:{slot}:{name}:amount", item.get('amount') or '')
    unit = profile_get(f"stack:{slot}:{name}:unit", item.get('unit') or '')
    updated = dict(item)
    updated['amount'] = amount
    updated['unit'] = unit
    return updated

def stack_update_from_text(raw_text):
    text = raw_text or ''
    norm = norm_tr(text)
    update_words = ['degis', 'duzelt', 'guncelle', 'ayarla', 'sistem']
    consume_words = ['aldim', 'alindi', 'ictim', 'kullandim', 'tamam']
    has_stack = 'stack' in norm
    has_update_word = any(w in norm for w in update_words)
    has_consume_word = any(w in norm for w in consume_words)
    dose_like = re.search(r'(\d+(?:[\.,]\d+)?)\s*(kapsul|kapsül|capsule|tablet|damla|drop|olcek|ölcek|ölçek|g|gr|mg|ml|iu)', norm)
    if not has_stack or (not has_update_word and (has_consume_word or not dose_like)):
        return ''
    slot = stack_slot_from_text(text)
    if not slot:
        return ''
    catalog = supplement_catalog()
    item = None
    for cand in catalog:
        if cand.get('name') and norm_tr(cand['name']) in norm:
            item = cand
            break
        if any(norm_tr(k) in norm for k in cand.get('keys', [])):
            item = cand
            break
    if not item:
        return ''
    unit_pat = r'(kapsul|tablet|damla|drop|doz|olcek|paket|g|mg|iu)'
    m = re.search(r'(\d+(?:[\.,]\d+)?)\s*' + unit_pat, norm)
    if not m:
        return 'Hangi doza cekeyim? Ornek: gece stack KSM 1 kapsul.'
    amount = m.group(1).replace(',', '.')
    unit = m.group(2).replace('drop', 'damla')
    name = item['name']
    profile_set(f"stack:{slot}:{name}:amount", amount)
    profile_set(f"stack:{slot}:{name}:unit", unit)
    today = operation_today()
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM vitamin_logs WHERE date=? AND lower(name)=lower(?) ORDER BY id DESC LIMIT 1",
        (today, name),
    ).fetchone()
    if row:
        conn.execute("UPDATE vitamin_logs SET amount=?, unit=?, notes=? WHERE id=?", (amount, unit, f"{stack_label(slot)} | sistem guncellendi", row['id']))
        conn.commit()
    conn.close()
    suffix = ' Bugunku kaydi da guncelledim.' if row else ''
    return f"✅ {stack_label(slot)} guncellendi: {name} artık {amount} {unit}.{suffix}"


def supplement_actions_from_stack_text(raw_text):
    text = raw_text or ""
    norm = norm_tr(text)
    slot = stack_slot_from_text(text)
    if not slot or "stack" not in norm:
        return []
    today = operation_today()
    wanted = set(stack_preset(slot))
    actions = []
    for item in supplement_catalog():
        if item["name"] not in wanted:
            continue
        is_missing = item_missing_in_text(item, norm)
        if not is_missing and item["name"] == "Cinko" and slot in ("sabah", "kahvalti"):
            if not zinc_explicitly_taken(norm):
                continue
        item = stack_apply_overrides(slot, item)
        if is_missing:
            actions.append({
                "type": "vitamin",
                "date": today,
                "name": item["name"],
                "amount": "0",
                "unit": item["unit"],
                "notes": f"eksik alındı | {stack_label(slot)}",
                "stack": slot,
            })
        else:
            actions.append({
                "type": "vitamin",
                "date": today,
                "name": item["name"],
                "amount": item["amount"],
                "unit": item["unit"],
                "notes": f"{stack_label(slot)} | {item['note']}",
                "stack": slot,
            })
    return actions

STACK_NAME_MAP = {
    'ac karna': 'Aç Karna Stack',
    'ackarna':  'Aç Karna Stack',
    'fasted':   'Aç Karna Stack',
    'sabah':    'Sabah Stack',
    'kahvalti': 'Sabah Stack',
    'pre':      'Pre Workout Stack',
    'preworkout':'Pre Workout Stack',
    'antrenman oncesi': 'Pre Workout Stack',
    'post':     'Post Workout Stack',
    'postworkout':'Post Workout Stack',
    'antrenman sonrasi':'Post Workout Stack',
    'creatine': 'Post Workout Stack',
    'kreatin':  'Post Workout Stack',
    'gece':     'Gece Stack',
    'night':    'Gece Stack',
}

def detect_stack_name(norm_text):
    """Metinden stack ismini belirle."""
    for key, val in STACK_NAME_MAP.items():
        if key in norm_text:
            return val
    return None

def parse_stack_overrides(norm_text, stack_name):
    """Override ve ekstra ürünleri metin'den çıkar.
    Örnek: 'gece stack ama 2 kapsül melatonin' → {Melatonin: {dose:2}}
    Örnek: 'sabah stack + 2 g taurine' → extras: [{name:Taurine, dose:2, unit:g}]
    Örnek: 'sabah stack + çinko alınmadı' → {Zinc: {taken:0}}
    """
    overrides = {}
    extras = []

    # Herhangi bir ürün için "X hariç/eksik/almadım/yok" algıla
    neg_suffixes = r".{0,30}(haric|eksik|almadim|alinmadi|alma|yok|atladi|atlandi)"
    neg_prefixes = r"(haric|eksik|almadim|alinmadi|alma|yok|atladi|atlandi).{0,30}"
    for cat_item in supplement_catalog():
        all_keys = [norm_tr(cat_item["name"])] + [norm_tr(k) for k in cat_item.get("keys", [])]
        for k in all_keys:
            if (re.search(re.escape(k) + neg_suffixes, norm_text) or
                    re.search(neg_prefixes + re.escape(k), norm_text)):
                overrides[cat_item["name"]] = {'taken': 0, 'note': 'eksik alındı'}
                break

    # "ama/fakat/ancak X kapsül/g URUN" → override
    import re as _re
    ama_pattern = _re.findall(r'(?:ama|fakat|ancak|sadece)\s+(\d+(?:[.,]\d+)?)\s*(kapsul|kapsül|g|gr|damla|ml|tablet)\s+(\w+(?:\s+\w+)?)', norm_text)
    for m in ama_pattern:
        dose_val, unit_val, prod_hint = m
        prod_hint = prod_hint.strip()
        overrides[prod_hint] = {'dose': float(dose_val.replace(',','.')), 'unit': unit_val, 'note': 'override'}

    # "+ X g/kapsul URUN" → ekstra
    extra_pattern = _re.findall(r'\+\s*(\d+(?:[.,]\d+)?)\s*(kapsul|kapsül|g|gr|damla|ml|tablet)\s+(\w+(?:\s+\w+)?)', norm_text)
    for m in extra_pattern:
        dose_val, unit_val, prod_hint = m
        extras.append({'name': prod_hint.strip(), 'dose': float(dose_val.replace(',','.')), 'unit': unit_val})

    return overrides, extras

async def _handle_stack_shortcut(raw_text, norm_text, today):
    """Stack kısa yolunu işle. Kayıt başarılıysa cevap metni döner, değilse None."""
    import aiohttp as _aio
    stack_name = detect_stack_name(norm_text)
    if not stack_name or 'stack' not in norm_text:
        return None

    overrides, extras = parse_stack_overrides(norm_text, stack_name)

    # API çağrısı
    try:
        import json as _json, urllib.request as _ur
        payload = _json.dumps({'stack_name': stack_name, 'date': today,
                               'overrides': overrides, 'extras': extras}).encode()
        req = _ur.Request('http://localhost:5000/api/supplements/log',
                          data=payload, headers={'Content-Type':'application/json'}, method='POST')
        with _ur.urlopen(req, timeout=5) as resp:
            result = _json.loads(resp.read())
    except Exception as e:
        # Fallback: direkt DB
        result = _log_stack_direct(stack_name, today, overrides, extras)

    if not result.get('ok'):
        return None

    # Zinc durumu kontrol
    zinc_note = ''
    try:
        conn = get_db()
        row = conn.execute("SELECT rule_data FROM supplement_rules WHERE product_name='NOW Zinc Picolinate 50mg'").fetchone()
        conn.close()
        if row:
            import json as _json
            last_d = _json.loads(row['rule_data'] or '{}').get('last_date')
            if last_d:
                from datetime import date as _date, timedelta as _td
                diff = (_date.fromisoformat(today) - _date.fromisoformat(last_d)).days
                if diff == 1:
                    zinc_note = '\n⚠️ Çinko: Dün alındı, yarın al.'
                elif diff >= 2:
                    zinc_note = '\n💊 Çinko: Bugün alma günü ✓'
    except: pass

    # Stack items listele
    conn = get_db()
    stack_row = conn.execute("SELECT id FROM supplement_stacks WHERE name=?", (stack_name,)).fetchone()
    items = []
    if stack_row:
        items = [dict(r) for r in conn.execute(
            "SELECT product_name,dose,unit FROM supplement_stack_items WHERE stack_id=? ORDER BY order_num",
            (stack_row['id'],)).fetchall()]
    conn.close()

    lines = [f'✅ {stack_name} kaydedildi:']
    for item in items:
        pname = item['product_name']
        ov = overrides.get(pname, {})
        if ov.get('taken') == 0:
            lines.append(f'  ✗ {pname} — alınmadı')
        else:
            dose = ov.get('dose', item['dose'])
            unit = ov.get('unit', item['unit'])
            suffix = ' ⟵ override' if pname in overrides else ''
            lines.append(f'  💊 {pname}: {dose} {unit}{suffix}')
    for ex in extras:
        lines.append(f'  ➕ {ex["name"]}: {ex["dose"]} {ex["unit"]} (ekstra)')

    if zinc_note:
        lines.append(zinc_note)

    return '\n'.join(lines)

def _log_stack_direct(stack_name, today, overrides, extras):
    """API erişilmezse direkt DB'ye yaz (fallback)."""
    try:
        conn = get_db()
        stack = conn.execute("SELECT * FROM supplement_stacks WHERE name=?", (stack_name,)).fetchone()
        if not stack:
            conn.close()
            return {'ok': False}
        conn.execute("INSERT INTO supplement_logs (date,stack_id,stack_name_snapshot,completed) VALUES (?,?,?,1)",
                     (today, stack['id'], stack_name))
        log_id = conn.execute("SELECT last_insert_rowid() as lid").fetchone()['lid']
        items = conn.execute("SELECT * FROM supplement_stack_items WHERE stack_id=? ORDER BY order_num", (stack['id'],)).fetchall()
        import json as _json
        for item in items:
            pname = item['product_name']
            ov = overrides.get(pname, {})
            taken = ov.get('taken', 1)
            dose = ov.get('dose', item['dose'])
            unit = ov.get('unit', item['unit'])
            conn.execute("INSERT INTO supplement_log_items (log_id,product_name_snapshot,dose_snapshot,unit_snapshot,taken,override_note) VALUES (?,?,?,?,?,?)",
                         (log_id, pname, dose, unit, taken, ov.get('note','')))
            if taken:
                conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
                             (today, pname, str(dose), unit, f'stack:{stack_name}'))
            else:
                # Eksik alındı — siteye görünür not olarak yaz
                conn.execute("INSERT OR IGNORE INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
                             (today, pname, '0', unit, f'eksik alındı | {stack_name}'))
            if pname == 'NOW Zinc Picolinate 50mg' and taken:
                conn.execute("UPDATE supplement_rules SET rule_data=? WHERE product_name=?",
                             (_json.dumps({'last_date': today}), pname))
        for ex in extras:
            conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
                         (today, ex['name'], str(ex.get('dose','')), ex.get('unit',''), f'extra:{stack_name}'))
        conn.commit(); conn.close()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def save_stack_actions(actions):
    saved = []
    conn = get_db()
    try:
        for a in actions:
            name = a.get("name")
            d = a.get("date") or operation_today()
            if not name:
                continue
            # Cinko gun asiri — uyar ama yine de kaydet
            if any(k in name.lower() for k in ('cinko', 'zinc')):
                if not zinc_due_for_date(d):
                    saved.append("⚠️ Cinko: bugun almana gerek yoktu (gun asiri), ama kaydediyorum")
            already = conn.execute(
                "SELECT id FROM vitamin_logs WHERE date=? AND lower(name)=lower(?)",
                (d, name)
            ).fetchone()
            if already:
                continue
            conn.execute(
                "INSERT INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
                (d, name, str(a.get("amount") or ""), a.get("unit") or "", a.get("notes") or "")
            )
            saved.append(f"{name} {a.get('amount','')} {a.get('unit','')}".strip())
        conn.commit()
    finally:
        conn.close()
    return saved


def supplement_stack_context():
    """Kullanicinin bilinen supplement sablonlarini sistem promptuna inject et."""
    conn = get_db()
    supps = conn.execute(
        "SELECT title, amount, unit FROM quick_templates WHERE kind='supplement' ORDER BY ts DESC"
    ).fetchall()
    conn.close()
    if not supps:
        return ''
    lines = ['KULLANICININ BILINEN SUPPLEMENT PROTOKOLU (quick_templates):']
    for s in supps:
        amount = s.get('amount') or ''
        unit   = s.get('unit') or 'kapsul'
        line   = f"  - {s['title']}"
        if amount:
            line += f": {amount} {unit}"
        lines.append(line)
    lines.append('"tum vitaminler tamam" veya "hepsini aldim" denilirse YUKARIDAKI HEPSINI vitamin action olarak kaydet.')
    return '\n'.join(lines) + '\n'

def food_db_context(text):
    """Mesajdaki yiyecekler icin bilinen makrolari sistem promptuna ekle."""
    foods = food_db_search(text)
    if not foods:
        return ''
    lines = [
        'BILINEN BESINLER (kendi gecmis verilerinden):',
        'KESIN KURAL: brand-fixed kayit varsa ayni besin icin eski/genel sablonlari kullanma. Miktari adet veya grama gore brand-fixed degerden hesapla.'
    ]
    for f in foods:
        cal = f.get('calories') or '?'
        p = f.get('protein_g') or '?'
        k = f.get('carbs_g') or '?'
        y = f.get('fat_g') or '?'
        desc = f.get('description') or ''
        line = f"  - {f['title']}: {cal} kcal | P:{p}g K:{k}g Y:{y}g"
        if desc:
            line += f" ({desc[:50]})"
        lines.append(line)
    lines.append('BU DEGERLERI KULLAN, tahmin yapma.')
    return '\n'.join(lines) + '\n'

def openfoodfacts_lookup(food_name: str, amount_g: float):
    """Query OpenFoodFacts and return macros for given amount. Returns dict or None."""
    import urllib.parse, urllib.request, urllib.error
    try:
        query = urllib.parse.quote(food_name)
        url = (
            "https://world.openfoodfacts.org/cgi/search.pl"
            f"?search_terms={query}&search_simple=1&action=process"
            "&json=1&page_size=5&fields=product_name,nutriments&lc=tr"
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'DailyTrackerBot/1.0'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        for p in data.get('products', []):
            n = p.get('nutriments', {})
            protein = n.get('proteins_100g')
            fat = n.get('fat_100g') or 0
            carbs = n.get('carbohydrates_100g') or 0
            if protein is None:
                continue
            kcal_100g = float(n.get('energy-kcal_100g') or (n.get('energy_100g', 0) or 0) / 4.184)
            # Sanity check: kcal must be consistent with macros (within 30%)
            expected_kcal = 9 * float(fat) + 4 * float(protein) + 4 * float(carbs)
            if kcal_100g > 0 and expected_kcal > 0:
                ratio = expected_kcal / kcal_100g
                if ratio < 0.6 or ratio > 1.6:
                    log.debug("OpenFoodFacts: rejected inconsistent product '%s' (%s)", p.get('product_name'), food_name)
                    continue
            # Reject implausibly high kcal for non-oil whole foods (> 500 kcal/100g likely wrong)
            if kcal_100g > 500 and float(fat) < 40:
                log.debug("OpenFoodFacts: rejected high-kcal non-fat product '%s'", food_name)
                continue
            factor = amount_g / 100
            return {
                'protein_g': round(float(protein) * factor, 1),
                'carbs_g':   round(float(carbs) * factor, 1),
                'fat_g':     round(float(fat) * factor, 1),
                'calories':  round(kcal_100g * factor),
            }
    except Exception as e:
        log.debug("OpenFoodFacts lookup failed for '%s': %s", food_name, e)
    return None


def extract_meal_ingredients_api(user_text: str):
    """Quick Claude call: extract meal ingredients as JSON.
    Returns {"is_meal": bool, "user_macros": bool, "ingredients": [{name, amount_g}]}
    or None on error."""
    import urllib.request, urllib.error
    n = user_text.lower()
    meal_kw = ['yedim', 'içtim', 'kahvaltı', 'kahvalti', 'öğle', 'ogle',
               'akşam', 'aksam', 'sisteme kaydet', 'kaydet', ' g ', 'gram',
               'adet', 'dilim', 'öğün', 'ogun', 'atıştırma', 'atistirma']
    if not any(kw in n for kw in meal_kw):
        return None

    extraction_prompt = (
        "Mesajdan yemek malzemelerini çıkar. Sadece JSON döndür:\n"
        "Yemek kaydı DEĞİLSE: {\"is_meal\": false}\n"
        "Kullanıcı makroları KENDİSİ BELİRTTİYSE (protein/yağ/karbonhidrat sayıları varsa): "
        "{\"is_meal\": true, \"user_macros\": true, \"ingredients\": []}\n"
        "Makro belirtilmemişse: {\"is_meal\": true, \"user_macros\": false, "
        "\"ingredients\": [{\"name\": \"yumurta\", \"amount_g\": 240}]}\n"
        "Birim: 1 tam yumurta=60g, 1 dilim ekmek=30g, 100ml=100g. Sadece JSON."
    )
    body = json.dumps({
        'model': ANTHROPIC_MODEL,
        'max_tokens': 400,
        'system': extraction_prompt,
        'messages': [{'role': 'user', 'content': user_text}]
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        text = payload['content'][0]['text'].strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        log.warning("extract_meal_ingredients_api error: %s", e)
    return None


def openfoodfacts_context(user_text: str) -> str:
    """Build nutrition context from OpenFoodFacts for meal messages."""
    meal_data = extract_meal_ingredients_api(user_text)
    if not meal_data or not meal_data.get('is_meal'):
        return ''
    if meal_data.get('user_macros'):
        return '\nKULLANICI MAKROLARI KENDİSİ BELİRTTİ: Bu değerleri direkt kullan, yeniden hesaplama.\n'
    ingredients = meal_data.get('ingredients') or []
    if not ingredients:
        return ''
    lines = ['BESIN DEĞERLERİ (OpenFoodFacts API — doğrulanmış, kullan):']
    found = False
    for item in ingredients:
        name = item.get('name', '')
        amount_g = float(item.get('amount_g') or 100)
        result = openfoodfacts_lookup(name, amount_g)
        if result:
            lines.append(
                f"  {name} ({amount_g:.0f}g): "
                f"P={result['protein_g']}g K={result['carbs_g']}g Y={result['fat_g']}g"
                f" = {result['calories']} kcal"
            )
            found = True
        else:
            log.debug("OpenFoodFacts: no result for '%s'", name)
    if not found:
        return ''
    lines.append('Bu satırları toplayarak toplam makroyu hesapla. Kullanıcının kendi şablonları varsa onlar önceliklidir.')
    return '\n'.join(lines) + '\n'


def json_from_text(txt):
    txt = (txt or '').strip()
    if txt.startswith('```'):
        txt = txt.strip('`')
        if txt.lower().startswith('json'):
            txt = txt[4:].strip()
    s = txt.find('{'); e = txt.rfind('}')
    if s >= 0 and e > s:
        try:
            return json.loads(txt[s:e+1])
        except json.JSONDecodeError:
            pass
    # Claude düz metin döndürdü — crash etme, reply olarak göster
    return {'reply': txt or 'Anlaşılamadı.', 'actions': []}



def openai_json_call(system_prompt, user_text, max_tokens=1800):
    import urllib.request, urllib.error
    body = {
        'model': OPENAI_MODEL,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_text},
        ],
        'max_tokens': max_tokens,
    }
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + OPENAI_API_KEY, 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    content = payload.get('choices', [{}])[0].get('message', {}).get('content', '')
    return json_from_text(content)

def openai_vision_json_call(system_prompt, caption, img_b64, max_tokens=2000):
    import urllib.request, urllib.error
    content = []
    if caption:
        content.append({'type': 'text', 'text': caption})
    content.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + img_b64}})
    body = {
        'model': OPENAI_MODEL,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': content},
        ],
        'max_tokens': max_tokens,
    }
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + OPENAI_API_KEY, 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    content = payload.get('choices', [{}])[0].get('message', {}).get('content', '')
    return json_from_text(content)


def claude_call(user_text, history=None):
    import urllib.request, urllib.error
    ctx = today_ai_context()
    today = operation_today()
    yesterday = (operation_date() - timedelta(days=1)).isoformat()

    # Gecmis mesajlari ozet olarak hazirla
    history_text = ''
    if history:
        lines = []
        for h in history[-5:]:
            prefix = 'Kullanici' if h['role'] == 'user' else 'Bot'
            lines.append(f"{prefix}: {h['text']}")
        history_text = '\nSON MESAJLAR:\n' + '\n'.join(lines) + '\n'

    # Yiyecek veritabani — bilinen makrolari inject et
    food_ctx = food_db_context(user_text)

    # OpenFoodFacts API — gercek besin degerleri (kullanici makro belirtmemisse)
    try:
        off_ctx = openfoodfacts_context(user_text)
    except Exception:
        off_ctx = ''

    # USDA sabit referans degerleri — AI tahmin hatasini onler
    usda_ref = (
        "\nSTANDART YEDEK REFERANSLAR (yalnizca daha ust oncelikli kaynak yoksa kullan):\n"
        "  Carrefour Organik 0 Numara yumurta (1 adet): P=6g K=0.5g Y=5g = 70 kcal\n"
        "  Yumurta akı (1 adet, ~30g): P=3.6g K=0.2g Y=0g = 17 kcal\n"
        "  Bizim tost ekmegi (100g): P=9.5g K=45g Y=2.1g = 252 kcal\n"
        "  Çilek (100g): P=0.7g K=7.7g Y=0.3g = 32 kcal\n"
        "  Muz (100g): P=1.1g K=23g Y=0.3g = 89 kcal\n"
        "  Elma (100g): P=0.3g K=14g Y=0.2g = 52 kcal\n"
        "  Tavuk göğsü (100g, pişmiş): P=31g K=0g Y=3.6g = 165 kcal\n"
        "  Pirinç pilavı (100g, pişmiş): P=2.7g K=28g Y=0.3g = 130 kcal\n"
        "  Tam yağlı süt (100ml): P=3.2g K=4.8g Y=3.3g = 61 kcal\n"
        "  Yoğurt (tam yağlı, 100g): P=3.5g K=4.7g Y=3.3g = 61 kcal\n"
        "  Zeytinyağı (1 yemek kaşığı, 14g): P=0g K=0g Y=14g = 126 kcal\n"
        "  Peynir (beyaz, 100g): P=14g K=1.5g Y=20g = 250 kcal\n"
        "  Fıstık ezmesi (100g): P=25g K=20g Y=50g = 588 kcal\n"
        "  Nutella (100g): P=6g K=57g Y=30g = 535 kcal\n"
        "Miktar çarpımı: her zaman (değer * gram/100) hesapla.\nGymBeam Olive Oil Spray: sadece kullanıcı fıs/basış sayısı yazarsa hesapla; 1 fıs/basış = 15 kcal ve 1.65g yağ. Sayı yoksa yağ ekleme.\n"
    )

    # Supplement stack
    try:
        supp_ctx = supplement_stack_context()
    except Exception:
        supp_ctx = ''

    # Genişletilmiş kişisel veri (PR'lar, kilo trendi, öğün kalıpları, uyku)
    try:
        ext_ctx = extended_context()
    except Exception:
        ext_ctx = ''

    # Haftalik trend ve kullanici profili
    try:
        week_ctx  = weekly_ai_context()
        week_text = '\nSON 7 GUN OZET:\n' + json.dumps(week_ctx, ensure_ascii=False) + '\n'
    except Exception:
        week_text = ''
    profile_text = user_profile_context()

    system_prompt = (
        "Sen Taha Serdem'in kisisel antrenman ve gunluk performans kocusun. "
        "Turkce, samimi, net ve motive edici konus.\n"
        + TAHA_COACHING_POLICY
        + NUTRITION_ANALYSIS_POLICY
        + "Mesaji analiz et. Kayit iceriyorsa actions listesini doldur. "
        "Birden fazla kayit varsa hepsini ayri action olarak ekle.\n"
        f"BUGUNUN RESMI ANTRENMAN GUNU: {ctx.get('training_day')}. "
        "Bugun icin program onerisi veya antrenman kaydi yaparken bunu esas al; "
        "foto, kas grubu veya tahmine gore Push/Pull/Leg uydurma. "
        "Kullanici duzeltirse sistem gercegi olarak kabul et.\n"
        "REPLY FORMATI (yemek log edilince):\n"
        "  - Her ogunu emoji ile listele: 🍽️ [Ad]: ~X kcal | P:Xg K:Xg Y:Xg\n"
        "  - Toplam makroyu yaz: 🔥 Toplam: ~X kcal | P:Xg K:Xg Y:Xg\n"
        "  - 2-3 cumle kisa koçluk yorum yap (gunun durumuna gore)\n"
        "  - Kisa, vurucu, samimi. Makale yazma.\n"
        "\nSLOT ISIMLERI (GUNCEL):\n"
        "DB slot key -> gorunen isim:\n"
        "  kahvalti -> KAHVALTI\n"
        "  snack -> SNACK\n"
        "  meal1 -> MEAL1\n"
        "  pre-workout-meal -> PRE-WORKOUT MEAL\n"
        "  post-workout-meal -> POST-WORKOUT MEAL\n"
        "  snack2 -> SNACK 2\n"
        "Kullanici MEAL1/PRE WORKOUT MEAL/POST WORKOUT MEAL/SNACK 2 yazarsa dogru key'e cevir.\n"
        "Eski slot adlari (ogle, aksam, ara, atistirma, gece) artik yok.\n"
        "\nKULLANICI YAZMA STILI:\n"
        "Format: SLOT / gramaj besin / gramaj besin / ...\n"
        "Ornek: MEAL1 / 250g tavuk / bol salata / 20g ketcap\n"
        "Her / ile ayrilan parca = ayri meal action.\n"
        "\nKAPIL KURALLAR:\n"
        "- MEAL ACTION: title=SADECE besin adi, description=SADECE gramaj ('250g' veya '4 adet'). ASLA title icine gramaj yazma.\n"
        "- Default CIG: kullanici 'pismis' demedikce et/pirinc/patates cig gram baz al.\n"
        "- fis/fis = GymBeam Sprey Yag. 1 fis=1.8ml=15kcal Y:1.65g.\n"
        "- Supplement/vitamin: kapsul/tablet sayisini amount olarak kaydet\n"
        "- Su: ml veya L, 5.2L=5200ml\n"
        "- Gecmis tarih: dun/onceki gun -> date=dun tarihi\n"
        "- Kalori/makro bilinmiyorsa makul tahmin yap\n"
        "- Antrenman set: bench press 80kg 8 tekrar -> workout_set; weight='80', reps='8', set_type='Working Set'|'Warm-up'|'Back-off'\n"
        "- Birden fazla set: 3 set -> 3 ayri workout_set action\n"
        "- Hem exercise hem workout_set uret\n"
        "YEMEK KAYDI MUTLAK KURAL: Her farkli yiyecek = ayri meal action. ASLA birlestirme.\n"
        "DOGRU ORNEK - MEAL1 / 250g tavuk / bol salata:\n"
        '  {"type":"meal","slot":"meal1","title":"Tavuk Gogsu","description":"250g","calories":285}\n'
        '  {"type":"meal","slot":"meal1","title":"Salata","description":"bol","calories":30}\n'
        "REPLY FORMATI (yemek loglaninca):\n"
        "[slot emoji] Slot Adi  (KAHVALTI->krem  SNACK->elma  MEAL1->yesil  PRE-WORKOUT->yildirim  POST-WORKOUT->kas  SNACK2->ay)\n"
        "Makrolar: kcal, P, K, Y\nYorum: 2-3 cumle kisisel koçluk\n"
        "\nDUZELTME/SILME KURALLARI (ASLA SORU SORMA — direkt isle):\n"
        "- 'su toplam X yaz', 'su X olsun', 'suyu X yap' -> water_set action\n"
        "- 'son yemegi sil', 'az once girdimi sil', '[isim] sil' -> delete_meal action, title varsa doldur\n"
        "- 'son seti sil', '[hareket] setini sil', 'yanlis set' -> delete_workout_set action\n"
        "- 'vitamini sil', '[ad] vitamini sil' -> delete_vitamin action\n"
        "- '[yemek] kalorisini X yap', '[yemek] proteini X gdi' -> update_meal action, title + guncellenen alanlar\n"
        "- '[hareket] agirligini X yap', 'tekrar sayisi yanlis Xdi' -> update_workout_set action\n"
        "- '[takviye] dozunu X yap', '[supplement] miktarini degistir' -> update_vitamin action\n"
        "- 'hayir', 'yanlis', 'duzelt', 'benim hatam' -> SON MESAJ kontekstinden ne kastedildigini anla, "
        "ilgili delete/update action'i uret, SORU SORMA\n"
        "- BUGUNUN TAM LOGUNA bak: hangi kaydin silinecegini/duzeltilecegini oradan anla\n"
        "\nSADECE gecerli JSON dondur:\n"
        '{"reply":"...","actions":['
        '{"type":"sleep","date":"YYYY-MM-DD","hours":7.5,"quality":8},'
        '{"type":"exercise","date":"YYYY-MM-DD","exercise_type":"Push","duration":60,"intensity":8},'
        '{"type":"workout_set","date":"YYYY-MM-DD","exercise":"Bench Press","weight":"80 kg","reps":"8","set_type":"Working Set"},'
        '{"type":"meal","date":"YYYY-MM-DD","slot":"kahvalti","title":"Pankek Kahvaltisi","description":"3 yumurta, peynir","calories":450,"protein_g":32,"carbs_g":10,"fat_g":28,"estimated":false,"source":"brand-fixed"},'
        '{"type":"water","date":"YYYY-MM-DD","water_ml":500},'
        '{"type":"water_set","date":"YYYY-MM-DD","water_ml":400},{"type":"delete_water","date":"YYYY-MM-DD"},'
        '{"type":"mood","date":"YYYY-MM-DD","energy":8,"mood":7,"stress":3},'
        '{"type":"vitamin","date":"YYYY-MM-DD","name":"D3","amount":"2","unit":"kapsul"},'
        '{"type":"weight","date":"YYYY-MM-DD","weight_kg":90.5},'
        '{"type":"steps","date":"YYYY-MM-DD","steps":8500},'
        '{"type":"work","date":"YYYY-MM-DD","hours":8.0,"tasks":"proje X","notes":"..."},'
        '{"type":"coaching","date":"YYYY-MM-DD","sessions":3,"clients":"Ali, Veli","notes":"..."},'
        '{"type":"note","date":"YYYY-MM-DD","note":"..."},{"type":"delete_note","date":"YYYY-MM-DD"},'
        '{"type":"delete_meal","date":"YYYY-MM-DD","title":"Pankek"},'
        '{"type":"update_meal","date":"YYYY-MM-DD","title":"Pankek","calories":500,"protein_g":35},'
        '{"type":"delete_workout_set","date":"YYYY-MM-DD","exercise":"Bench Press"},'
        '{"type":"update_workout_set","date":"YYYY-MM-DD","exercise":"Bench Press","weight":"90 kg","reps":"8"},'
        '{"type":"delete_vitamin","date":"YYYY-MM-DD","name":"D3"},'
        '{"type":"update_vitamin","date":"YYYY-MM-DD","name":"D3","amount":"3","unit":"kapsul"},'
        '{"type":"profile","key":"kalori_hedef","value":"2200"}'
        ']}\n'
        "- 'X saat calistim', 'is saati X', 'X saat ofis' -> work action, hours=X\n"
        "- 'X seans antrenorluk yaptim', 'X musteriye baktim' -> coaching action, sessions=X\n"
        "- Kullanici hedef/tercih belirtirse (kalori hedefi, protein hedefi, uyku hedefi vb.) -> profile action ile kaydet\n"
        "- Haftalik trend gorunce (SON 7 GUN verisine bakarak) proaktif yorum yap: eksik kalan alanlar, iyilesen alanlar\n"
        f'Tarih: bugun={today}, dun={yesterday}, suan={datetime.now().strftime("%H:%M")}.\n'
        f'Kullanici tarih belirtmemisse date={today}.\n'
        + profile_text
        + supp_ctx
        + ext_ctx
        + usda_ref
        + off_ctx
        + food_ctx
        + week_text
        + history_text
        + 'BUGUNUN VERISI: ' + json.dumps(ctx, ensure_ascii=False) + '\n'
        + 'BUGUNUN TAM LOGU (silme/duzeltme icin): ' + json.dumps(today_full_log(), ensure_ascii=False)
    )
    if OPENAI_API_KEY:
        try:
            return openai_json_call(system_prompt, user_text, 1800)
        except Exception:
            log.exception("OpenAI cevap hatasi; Claude fallback deneniyor")
            if not ANTHROPIC_API_KEY:
                return {'reply': 'OpenAI baglanti sorunu. Tekrar dener misin?', 'actions': []}

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
        return json_from_text(payload['content'][0]['text'])
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        log.error("Anthropic hata: %s", detail)
        try:
            msg = json.loads(detail).get('error', {}).get('message', detail[:200])
        except Exception:
            msg = detail[:200]
        return {'reply': f'Claude hatasi: {msg}', 'actions': []}
    except Exception as e:
        log.exception("Claude cevap hatasi")
        return {'reply': f'Baglanti sorunu: {e}', 'actions': []}

def last_split_workout(split_name):
    """Son aynı split gününün antrenmanını DB'den çek, formatlanmış Telegram metni döndür."""
    WEEKDAY_CYCLE = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT date FROM workout_logs WHERE training_day=? ORDER BY date DESC LIMIT 1",
        (split_name,)
    ).fetchall()
    if not rows:
        conn.close()
        return None, None
    last_date = rows[0]['date']
    sets = conn.execute(
        "SELECT exercise, set_type, weight, reps, notes FROM workout_logs WHERE date=? ORDER BY id",
        (last_date,)
    ).fetchall()
    conn.close()

    # Egzersiz sırasını koru, gruplama
    seen_order = []
    groups = {}
    for s in sets:
        ex = s['exercise']
        if ex not in groups:
            groups[ex] = []
            seen_order.append(ex)
        groups[ex].append(s)

    SPLIT_EMOJI = {'Push':'💪','Pull':'🏋️','Leg':'🦵','Upper':'⬆️','Lower':'⬇️'}
    emoji = SPLIT_EMOJI.get(split_name, '🏋️')
    lines = [f"{emoji} Son {split_name} | {last_date}"]
    lines.append('━' * 28)

    for i, ex in enumerate(seen_order, 1):
        ex_sets = groups[ex]
        # SS partner varsa başlığa ekle
        ss_partner = next((s['notes'].replace('SS: ','') for s in ex_sets if s['notes'] and s['notes'].startswith('SS: ')), None)
        ss_b = any(s['notes'] == 'SS-B' for s in ex_sets if s['notes'])
        if ss_b:
            continue  # SS-B'yi ayrı yazdırmıyoruz, A ile birlikte çıkıyor
        header = f"{i}. {ex}"
        if ss_partner:
            header += f" 🔗 {ss_partner}"
        lines.append(header)

        wu, ws, bo, drop = [], [], [], []
        ss_b_sets = groups.get(ss_partner, []) if ss_partner else []
        ss_idx = 0
        for s in ex_sets:
            w = s['weight']
            r = s['reps']
            n = s['notes'] or ''
            tag = f"{w}×{r}" if w else f"BW×{r}"
            if s['set_type'] == 'Warm Up':
                wu.append(tag)
            elif s['set_type'] == 'Back Off Set':
                bo.append(tag)
            elif s['set_type'] == 'Drop Set':
                drop.append(f"↓{tag}")
            else:
                if ss_partner and ss_idx < len(ss_b_sets):
                    sb = ss_b_sets[ss_idx]
                    sb_tag = f"{sb['weight']}×{sb['reps']}" if sb['weight'] else f"BW×{sb['reps']}"
                    ws.append(f"{tag}/{sb_tag}")
                    ss_idx += 1
                else:
                    ws.append(tag)

        if wu:   lines.append(f"  WU: {' | '.join(wu)}")
        if ws:   lines.append(f"  WS: {' | '.join(ws)}{' '+' '.join(drop) if drop else ''}")
        if bo:   lines.append(f"  BO: {' | '.join(bo)}")

    return last_date, '\n'.join(lines)


def today_full_log():
    """Bugün kaydedilen tüm girdileri Claude'a ver — silme/düzeltme için."""
    today = operation_today()
    conn = get_db()
    meals = [dict(r) for r in conn.execute(
        "SELECT id, slot, title, calories, protein_g, carbs_g, fat_g, ts FROM meal_entries WHERE date=? ORDER BY id", (today,)).fetchall()]
    sets  = [dict(r) for r in conn.execute(
        "SELECT id, exercise, set_num, weight, reps, set_type, ts FROM workout_logs WHERE date=? ORDER BY id", (today,)).fetchall()]
    vits  = [dict(r) for r in conn.execute(
        "SELECT id, name, amount, unit, ts FROM vitamin_logs WHERE date=? ORDER BY id", (today,)).fetchall()]
    conn.close()
    return {'ogunler': meals, 'setler': sets, 'vitaminler': vits}


def tg_water_actions_from_text(raw_text):
    text = raw_text or ''
    norm = _tg_norm(text) if '_tg_norm' in globals() else text.lower()
    if not any(w in norm for w in ['su', 'water', 'ml', 'litre', 'lt']):
        return []
    m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(ml|l|lt|litre)?', norm)
    if not m:
        return []
    val = float(m.group(1).replace(',', '.'))
    unit = (m.group(2) or '').lower()
    ml = int(val * 1000) if unit in ('l', 'lt', 'litre') or (not unit and val <= 10) else int(val)
    if ml <= 0:
        return []
    date = operation_today()
    if 'tg_effective_log_date' in globals():
        try:
            date = tg_effective_log_date(text, 'water')
        except Exception:
            date = operation_today()
    is_total = any(w in norm for w in ['toplam', 'olsun', 'olarak', 'yap', 'duzelt', 'düzelt', 'set'])
    return [{'type': 'water_set' if is_total else 'water', 'date': date, 'water_ml': ml}]


def tg_slot_from_text(raw_text):
    """Kullanicinin yazdigi slot adini DB key'ine cevir — G_SLOT_ORDER canonical adlari kullan."""
    n = norm_tr(raw_text) if 'norm_tr' in globals() else (raw_text or '').lower()
    if any(w in n for w in ['kahvalti', 'kahvaltı', 'sabah yemek', 'breakfast']):
        return 'kahvalti'
    if any(w in n for w in ['meal 1', 'meal1', '1. ogun', 'birinci ogun', 'ana ogun', 'ogle']):
        return 'meal1'
    if any(w in n for w in ['pre snack', 'presnack', 'antrenman oncesi atistirma']):
        return 'pre_snack'
    if any(w in n for w in ['pre meal', 'premeal', 'pre workout meal', 'antrenman oncesi yemek']):
        return 'pre_meal'
    if any(w in n for w in ['post snack', 'postsnack', 'antrenman sonrasi atistirma']):
        return 'post_snack'
    if any(w in n for w in ['post meal', 'postmeal', 'post workout meal', 'antrenman sonrasi yemek']):
        return 'post_meal'
    if any(w in n for w in ['snack 2', 'snack2', '2. snack', 'ikinci snack']):
        return 'snack2'
    if any(w in n for w in ['snack', 'atistirma', 'ara ogun']):
        return 'snack'
    if any(w in n for w in ['gece', 'aksam', 'akşam']):
        return 'aksam'
    return 'extra'
def _macro_for_known_food(name, amount):
    name_n = _tg_norm(name) if '_tg_norm' in globals() else str(name).lower()
    amount = float(amount or 0)
    if amount <= 0:
        return None
    # returns kcal, protein, carbs, fat, title
    if 'yumurta' in name_n and not any(w in name_n for w in ['beyaz', 'likit', 'sivi', 'sıvı']):
        return (round(amount * 70), round(amount * 6, 1), round(amount * 0.5, 1), round(amount * 5, 1), f"Carrefour Organik 0 Numara Yumurta ({int(amount)} adet)")
    if any(w in name_n for w in ['tost', 'ekmek']):
        g = amount
        return (round(g * 252 / 100), round(g * 9.5 / 100, 1), round(g * 45 / 100, 1), round(g * 2.1 / 100, 1), f"Carrefour Tost Ekmegi ({g:g}g)")
    if 'cilek' in name_n or 'çilek' in name_n:
        g = amount
        return (round(g * 32 / 100), round(g * 0.7 / 100, 1), round(g * 7.7 / 100, 1), round(g * 0.3 / 100, 1), f"Cilek ({g:g}g)")
    if 'salatalik' in name_n or 'salatalık' in name_n:
        g = amount
        return (round(g * 15 / 100), round(g * 0.7 / 100, 1), round(g * 3.6 / 100, 1), round(g * 0.1 / 100, 1), f"Salatalik ({g:g}g)")
    if 'pirinc' in name_n or 'pirinç' in name_n:
        g = amount
        return (round(g * 360 / 100), round(g * 7 / 100, 1), round(g * 79 / 100, 1), round(g * 0.6 / 100, 1), f"Yasmin Pirinc ({g:g}g cig)")
    if 'tavuk' in name_n:
        g = amount
        return (round(g * 120 / 100), round(g * 23 / 100, 1), 0, round(g * 2 / 100, 1), f"Tavuk Gogsu ({g:g}g cig)")
    if 'muz' in name_n:
        g = amount
        return (round(g * 89 / 100), round(g * 1.1 / 100, 1), round(g * 22.8 / 100, 1), round(g * 0.3 / 100, 1), f"Muz ({g:g}g)")
    if 'gymbeam' in name_n or 'spray' in name_n or 'fis' in name_n or 'fıs' in name_n:
        adet = amount
        return (round(adet * 15), 0, 0, round(adet * 1.65, 1), f"GymBeam Olive Oil Spray ({adet:g} fis)")
    return None


def tg_known_food_actions_from_text(raw_text):
    """Sabit ürünleri AI cevabindan bagimsiz meal action'a cevirir."""
    raw = raw_text or ''
    n = _tg_norm(raw) if '_tg_norm' in globals() else raw.lower()
    if not any(w in n for w in ['yumurta', 'tost', 'ekmek', 'cilek', 'çilek', 'salatalik', 'salatalık', 'pirinc', 'pirinç', 'tavuk', 'muz', 'gymbeam', 'spray', 'fis', 'fıs']):
        return []
    slot = tg_slot_from_text(raw)
    date = tg_effective_log_date(raw, 'meal') if 'tg_effective_log_date' in globals() else operation_today()
    patterns = [
        (r'(\d+(?:[\.,]\d+)?)\s*(?:tam\s*)?(?:adet\s*)?yumurta', 'yumurta'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*(?:bizim\s*)?(?:tost\s*)?ekmek', 'tost ekmegi'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*tost', 'tost ekmegi'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*cilek', 'cilek'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*çilek', 'cilek'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*salatal', 'salatalik'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*(?:yasmin\s*)?pirin', 'pirinc'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*(?:marine\s*)?tavuk', 'tavuk'),
        (r'(\d+(?:[\.,]\d+)?)\s*g(?:r|ram)?\s*muz', 'muz'),
        (r'(\d+(?:[\.,]\d+)?)\s*(?:fis|fıs)\s*(?:gymbeam|spray|yag|yağ)?', 'gymbeam spray'),
    ]
    actions = []
    seen = set()
    for pat, name in patterns:
        for m in re.finditer(pat, n):
            amt = float(m.group(1).replace(',', '.'))
            macro = _macro_for_known_food(name, amt)
            if not macro:
                continue
            key = (name, amt)
            if key in seen:
                continue
            seen.add(key)
            kcal, p, c, f, title = macro
            actions.append({
                'type': 'meal', 'date': date, 'slot': slot, 'title': title,
                'description': f'{amt:g} {"adet" if name == "yumurta" else "g/fis"} sabit hesap',
                'calories': kcal, 'protein_g': p, 'carbs_g': c, 'fat_g': f,
                'source': 'telegram-fixed-food'
            })
    return actions


def tg_known_food_update_from_text(raw_text):
    """Orn: 'tost ekmegini 54 gr yedim' -> mevcut tost kaydini 54g ile guncelle."""
    raw = raw_text or ''
    n = _tg_norm(raw) if '_tg_norm' in globals() else raw.lower()
    if not any(w in n for w in ['yedim', 'yap', 'duzelt', 'düzelt', 'cikar', 'çıkar', 'az', 'fazla']):
        return []
    date = tg_effective_log_date(raw, 'meal') if 'tg_effective_log_date' in globals() else operation_today()
    food_hints = [
        ('tost ekmegi', 'Tost', ['tost', 'ekmek']),
        ('cilek', 'Cilek', ['cilek', 'çilek']),
        ('pirinc', 'Pirinc', ['pirinc', 'pirinç']),
        ('tavuk', 'Tavuk', ['tavuk']),
        ('muz', 'Muz', ['muz']),
    ]
    actions = []
    for food, title_hint, hints in food_hints:
        if not any(h in n for h in hints):
            continue
        grams = re.findall(r'(\d+(?:[\.,]\d+)?)\s*(?:g|gr|gram)\b', n)
        if not grams:
            continue
        # Duzeltme cumlelerinde son gram genelde net tuketilen miktardir:
        # "77g yerine 54g", "23gr cikar 54gr yedim".
        amt = float(grams[-1].replace(',', '.'))
        macro = _macro_for_known_food(food, amt)
        if not macro:
            continue
        kcal, p, c, f, title = macro
        actions.append({
            'type': 'update_meal', 'date': date, 'title': title_hint,
            'description': f'{amt:g}g sabit hesapla duzeltildi',
            'calories': kcal, 'protein_g': p, 'carbs_g': c, 'fat_g': f
        })

    if 'yumurta' in n:
        adetler = re.findall(r'(\d+(?:[\.,]\d+)?)\s*(?:tam\s*)?(?:adet\s*)?yumurta', n)
        if adetler:
            amt = float(adetler[-1].replace(',', '.'))
            macro = _macro_for_known_food('yumurta', amt)
            if macro:
                kcal, p, c, f, title = macro
                actions.append({
                    'type': 'update_meal', 'date': date, 'title': 'Yumurta',
                    'description': f'{amt:g} adet sabit hesapla duzeltildi',
                    'calories': kcal, 'protein_g': p, 'carbs_g': c, 'fat_g': f
                })
    return actions


def merge_actions_no_duplicates(primary, extra):
    out = list(primary or [])
    seen = set()
    for a in out:
        if isinstance(a, dict):
            seen.add((a.get('type'), a.get('date'), a.get('slot'), str(a.get('title') or a.get('name') or '').lower()))
    for a in (extra or []):
        if not isinstance(a, dict):
            continue
        key = (a.get('type'), a.get('date'), a.get('slot'), str(a.get('title') or a.get('name') or '').lower())
        if key not in seen:
            out.append(a)
            seen.add(key)
    return out

# ACTIONS
def apply_actions(actions):
    saved = []
    today = operation_today()
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        typ = (a.get('type') or '').strip()
        d   = a.get('date') or today
        try:
            if typ == 'meal':
                slot  = a.get('slot') or 'extra'
                title = a.get('title') or a.get('name') or a.get('description') or slot
                if len(title) > 80: title = title[:80]
                conn = get_db()
                conn.execute(
                    "INSERT INTO meal_entries (date,slot,title,description,calories,protein_g,carbs_g,fat_g,fiber_g,source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (d, slot, title, a.get('description') or '',
                     a.get('calories'), a.get('protein_g'), a.get('carbs_g'),
                     a.get('fat_g'), a.get('fiber_g'), 'telegram-ai'))
                conn.commit(); conn.close()
                saved.append(f'ogun ({title})')

            elif typ == 'water':
                ml = int(a.get('water_ml') or 0)
                if ml > 0:
                    conn = get_db()
                    cur = water_get_total(conn, d)
                    expected_total = cur + ml
                    water_consolidate(conn, d, expected_total)
                    conn.commit()
                    verified_total = water_get_total(conn, d)
                    conn.close()
                    if verified_total != expected_total:
                        raise RuntimeError(f'Su kaydi dogrulanamadi: beklenen={expected_total}, bulunan={verified_total}')
                    saved.append(f'su (+{ml}ml, toplam={verified_total/1000:.2f}L)')

            elif typ == 'water_set':
                ml = int(a.get('water_ml') or 0)
                conn = get_db()
                water_consolidate(conn, d, ml)
                conn.commit()
                verified_total = water_get_total(conn, d)
                conn.close()
                if verified_total != ml:
                    raise RuntimeError(f'Su toplam kaydi dogrulanamadi: beklenen={ml}, bulunan={verified_total}')
                saved.append(f'su (toplam={verified_total}ml)')

            elif typ in ('delete_water',):
                conn = get_db()
                water_consolidate(conn, d, 0)
                conn.commit(); conn.close()
                saved.append('su silindi')

            elif typ == 'sleep':
                db_upsert('sleep_logs', d, {'hours': a.get('hours'), 'quality': a.get('quality')})
                saved.append('uyku')

            elif typ == 'mood':
                db_upsert('mood_logs', d, {'energy': a.get('energy'), 'mood': a.get('mood'), 'stress': a.get('stress')})
                saved.append('ruh hali')

            elif typ == 'exercise':
                ex_type = a.get('exercise_type') or a.get('exercise') or a.get('name') or ''
                db_upsert('exercise_logs', d, {
                    'type': ex_type,
                    'duration': a.get('duration'),
                    'intensity': a.get('intensity'),
                    'notes': a.get('notes') or ''
                })
                saved.append('egzersiz')

            elif typ == 'workout_set':
                exercise = (a.get('exercise') or a.get('name') or '').strip()
                if exercise:
                    td_name = training_day(d)
                    conn = get_db()
                    # auto-increment set_num for this exercise on this date
                    row = conn.execute(
                        "SELECT COALESCE(MAX(set_num),0) as mx FROM workout_logs WHERE date=? AND exercise=?",
                        (d, exercise)
                    ).fetchone()
                    set_num = (row['mx'] if row else 0) + 1
                    weight = a.get('weight') or ''
                    if weight and not str(weight).endswith('kg') and str(weight).replace('.','').isdigit():
                        weight = str(weight) + ' kg'
                    conn.execute(
                        "INSERT INTO workout_logs (date, training_day, exercise, set_num, weight, reps, notes, set_type) VALUES (?,?,?,?,?,?,?,?)",
                        (d, td_name, exercise, set_num, str(weight) if weight else '',
                         str(a.get('reps') or ''), a.get('notes') or '',
                         a.get('set_type') or 'Working Set')
                    )
                    conn.commit(); conn.close()
                    saved.append(f'set ({exercise} {weight})')

            elif typ in ('vitamin', 'supplement', 'takviye'):
                name = (a.get('name') or '').strip()
                if name:
                    # Cinko gun asiri — uyar ama yine de kaydet
                    if any(k in name.lower() for k in ('cinko', 'zinc')):
                        if not zinc_due_for_date(d):
                            saved.append("⚠️ Cinko: bugun almana gerek yoktu (gun asiri), ama kaydediyorum")
                    conn = get_db()
                    # Aynı gün aynı isimde zaten varsa ekleme (duplikasyon engeli)
                    already = conn.execute(
                        "SELECT id FROM vitamin_logs WHERE date=? AND lower(name)=lower(?)",
                        (d, name)).fetchone()
                    if not already:
                        conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
                                     (d, name, str(a.get('amount') or ''),
                                      a.get('unit') or '', a.get('notes') or ''))
                        conn.commit()
                        saved.append(f"supplement ({name})")
                    conn.close()

            elif typ in ('weight', 'body_weight', 'kilo'):
                kg = float(a.get('weight_kg') or a.get('kg') or 0)
                if kg:
                    now_hour = datetime.now().hour
                    is_night = (now_hour >= 20 or now_hour < OPERATION_DAY_CUTOFF_HOUR)
                    conn = get_db()
                    existing = conn.execute("SELECT weight_kg, weight_kg_night FROM body_metrics WHERE date=?", (d,)).fetchone()
                    if existing:
                        if is_night:
                            # Gece tartisi -> weight_kg_night'a yaz, weight_kg'yi koruyoruz
                            conn.execute("UPDATE body_metrics SET weight_kg_night=?, notes=? WHERE date=?", (kg, 'telegram-ai', d))
                            saved.append(f'gece kilosi ({kg}kg)')
                        else:
                            # Sabah tartisi -> weight_kg'yi guncelle
                            conn.execute("UPDATE body_metrics SET weight_kg=?, notes=? WHERE date=?", (kg, 'telegram-ai', d))
                            saved.append(f'kilo ({kg}kg)')
                    else:
                        conn.execute(
                            "INSERT INTO body_metrics (date, weight_kg, notes) VALUES (?,?,?)",
                            (d, kg, 'telegram-ai'))
                        saved.append(f'kilo ({kg}kg)')
                    conn.commit(); conn.close()

            elif typ == 'steps':
                steps = int(a.get('steps') or 0)
                if steps:
                    conn = get_db()
                    conn.execute("INSERT OR REPLACE INTO step_logs (date,steps,notes) VALUES (?,?,?)", (d, steps, 'telegram-ai'))
                    conn.commit(); conn.close()
                    saved.append(f'adim ({steps})')

            elif typ in ('update_steps',):
                steps = int(a.get('steps') or a.get('value') or 0)
                conn = get_db()
                conn.execute("INSERT OR REPLACE INTO step_logs (date,steps,notes) VALUES (?,?,?)", (d, max(0, steps), 'telegram-ai düzeltme'))
                conn.commit(); conn.close()
                saved.append(f'adim düzeltildi ({steps})')

            elif typ in ('delete_steps',):
                conn = get_db()
                conn.execute("DELETE FROM step_logs WHERE date=?", (d,))
                conn.commit(); conn.close()
                saved.append('adim silindi')

            elif typ in ('work', 'is', 'calisma'):
                db_upsert('work_logs', d, {
                    'hours': a.get('hours'),
                    'tasks': a.get('tasks') or a.get('notes') or '',
                    'notes': a.get('notes') or ''
                })
                saved.append(f"is ({a.get('hours', '?')}s)")

            elif typ in ('coaching', 'antrenorluk'):
                db_upsert('coaching_logs', d, {
                    'sessions': a.get('sessions') or a.get('count') or 1,
                    'clients': a.get('clients') or '',
                    'notes': a.get('notes') or ''
                })
                saved.append(f"antrenorluk ({a.get('sessions', 1)} seans)")

            elif typ == 'delete_meal':
                title = (a.get('title') or '').strip()
                conn = get_db()
                if title:
                    row = conn.execute(
                        "SELECT id, title FROM meal_entries WHERE date=? AND lower(title) LIKE lower(?) ORDER BY id DESC LIMIT 1",
                        (d, f'%{title}%')).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id, title FROM meal_entries WHERE date=? ORDER BY id DESC LIMIT 1", (d,)).fetchone()
                if row:
                    conn.execute("DELETE FROM meal_entries WHERE id=?", (row['id'],))
                    saved.append(f"ogun silindi ({row['title']})")
                conn.commit(); conn.close()

            elif typ == 'update_meal':
                title = (a.get('title') or '').strip()
                conn = get_db()
                if title:
                    row = conn.execute(
                        "SELECT id FROM meal_entries WHERE date=? AND lower(title) LIKE lower(?) ORDER BY id DESC LIMIT 1",
                        (d, f'%{title}%')).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id FROM meal_entries WHERE date=? ORDER BY id DESC LIMIT 1", (d,)).fetchone()
                if row:
                    fields = {k: a[k] for k in ('calories','protein_g','carbs_g','fat_g','description','slot') if a.get(k) is not None}
                    if fields:
                        sets_sql = ', '.join(f"{k}=?" for k in fields)
                        conn.execute(f"UPDATE meal_entries SET {sets_sql} WHERE id=?", list(fields.values()) + [row['id']])
                        # Food DB'yi de guncelle
                        if a.get('title') and a.get('calories'):
                            food_db_auto_learn([{**a, 'type': 'meal'}])
                        saved.append(f"ogun guncellendi")
                conn.commit(); conn.close()

            elif typ == 'delete_workout_set':
                exercise = (a.get('exercise') or '').strip()
                conn = get_db()
                if exercise:
                    row = conn.execute(
                        "SELECT id, exercise, weight FROM workout_logs WHERE date=? AND lower(exercise) LIKE lower(?) ORDER BY id DESC LIMIT 1",
                        (d, f'%{exercise}%')).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id, exercise FROM workout_logs WHERE date=? ORDER BY id DESC LIMIT 1", (d,)).fetchone()
                if row:
                    conn.execute("DELETE FROM workout_logs WHERE id=?", (row['id'],))
                    saved.append(f"set silindi ({row['exercise']} {row.get('weight','')})")
                conn.commit(); conn.close()

            elif typ == 'update_workout_set':
                exercise = (a.get('exercise') or '').strip()
                conn = get_db()
                if exercise:
                    row = conn.execute(
                        "SELECT id FROM workout_logs WHERE date=? AND lower(exercise) LIKE lower(?) ORDER BY id DESC LIMIT 1",
                        (d, f'%{exercise}%')).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id FROM workout_logs WHERE date=? ORDER BY id DESC LIMIT 1", (d,)).fetchone()
                if row:
                    fields = {k: a[k] for k in ('weight','reps','set_type','notes') if a.get(k) is not None}
                    if fields:
                        sets_sql = ', '.join(f"{k}=?" for k in fields)
                        conn.execute(f"UPDATE workout_logs SET {sets_sql} WHERE id=?", list(fields.values()) + [row['id']])
                        saved.append(f"set guncellendi ({exercise})")
                conn.commit(); conn.close()

            elif typ == 'update_vitamin':
                name = (a.get('name') or '').strip()
                conn = get_db()
                row = conn.execute(
                    "SELECT id FROM vitamin_logs WHERE date=? AND lower(name) LIKE lower(?) ORDER BY id DESC LIMIT 1",
                    (d, f'%{name}%')).fetchone() if name else \
                    conn.execute("SELECT id FROM vitamin_logs WHERE date=? ORDER BY id DESC LIMIT 1", (d,)).fetchone()
                if row:
                    fields = {k: a[k] for k in ('amount','unit','notes') if a.get(k) is not None}
                    if fields:
                        sets_sql = ', '.join(f"{k}=?" for k in fields)
                        conn.execute(f"UPDATE vitamin_logs SET {sets_sql} WHERE id=?", list(fields.values()) + [row['id']])
                        saved.append(f"takviye guncellendi ({name})")
                conn.commit(); conn.close()

            elif typ == 'delete_vitamin':
                name = (a.get('name') or '').strip()
                conn = get_db()
                if name:
                    row = conn.execute(
                        "SELECT id, name FROM vitamin_logs WHERE date=? AND lower(name) LIKE lower(?) ORDER BY id DESC LIMIT 1",
                        (d, f'%{name}%')).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id, name FROM vitamin_logs WHERE date=? ORDER BY id DESC LIMIT 1", (d,)).fetchone()
                if row:
                    conn.execute("DELETE FROM vitamin_logs WHERE id=?", (row['id'],))
                    saved.append(f"vitamin silindi ({row['name']})")
                conn.commit(); conn.close()

            elif typ in ('update_weight',):
                kg = float(a.get('weight_kg') or a.get('kg') or a.get('value') or 0)
                if kg:
                    conn = get_db()
                    conn.execute("""
                        INSERT INTO body_metrics (date, weight_kg, notes)
                        VALUES (?,?,?)
                        ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, notes=excluded.notes
                    """, (d, kg, 'telegram-ai düzeltme'))
                    conn.commit(); conn.close()
                    saved.append(f'kilo düzeltildi ({kg}kg)')

            elif typ in ('delete_weight',):
                conn = get_db()
                conn.execute("DELETE FROM body_metrics WHERE date=?", (d,))
                conn.commit(); conn.close()
                saved.append('kilo silindi')

            elif typ == 'note':
                db_upsert('daily_notes', d, {'note': a.get('note') or ''})
                saved.append('not')

            elif typ in ('delete_note',):
                conn = get_db()
                conn.execute("DELETE FROM daily_notes WHERE date=?", (d,))
                conn.commit(); conn.close()
                saved.append('not silindi')

            elif typ == 'profile':
                key = (a.get('key') or '').strip()
                val = (a.get('value') or '').strip()
                blocked_profile_keys = {'training_day', 'antrenman_gunu', 'current_training_day', 'bugunun_antrenmani'}
                if key in blocked_profile_keys:
                    saved.append('profil reddedildi (resmi antrenman günü korunuyor)')
                elif key and val:
                    user_profile_set(key, val)
                    saved.append(f'profil ({key}={val})')

        except Exception:
            log.exception("Action kaydedilemedi: %s", typ)
    return saved

# TEMPLATES
def should_save_template(text):
    n = norm_tr(text)
    return any(w in n for w in ['sablon', 'sabit', 'fiks', 'fix', 'favori',
                                  'suplemente kaydet', 'ogunlere kaydet',
                                  'yemeklere kaydet', 'supplementlere kaydet',
                                  'takviyeye kaydet'])

def save_template_from_actions(raw_text, actions):
    if not should_save_template(raw_text):
        return ''
    n = norm_tr(raw_text)
    titles = []
    name_hint = ''
    for pat in [r'ad[ii]\s+(.+?)\s+olsun', r'ismi\s+(.+?)\s+olsun', r'(.{2,60}?)\s+olarak\s+kaydet']:
        m = re.search(pat, raw_text, flags=re.I)
        if m:
            name_hint = m.group(1).strip(" .,!?:;\"'")[:70]
            break
    meals    = [a for a in actions if isinstance(a, dict) and a.get('type') == 'meal']
    vitamins = [a for a in actions if isinstance(a, dict) and a.get('type') == 'vitamin']
    is_supp  = any(w in n for w in ['supplement', 'suplement', 'takviye', 'vitamin'])
    is_meal  = any(w in n for w in ['ogun', 'yemek', 'kahvalti', 'ogle', 'aksam']) or (not is_supp)

    def upsert_template(kind, category, title, desc, cal, p, c, f, amount='', unit=''):
        title = (title or '').strip()[:90]
        if not title: return ''
        conn = get_db()
        existing = conn.execute("SELECT id FROM quick_templates WHERE kind=? AND lower(title)=lower(?)", (kind, title)).fetchone()
        if existing:
            conn.execute("UPDATE quick_templates SET category=?,description=?,calories=?,protein_g=?,carbs_g=?,fat_g=?,amount=?,unit=? WHERE id=?",
                         (category, desc, cal, p, c, f, amount, unit, existing['id']))
        else:
            conn.execute("INSERT INTO quick_templates (kind,category,title,description,calories,protein_g,carbs_g,fat_g,amount,unit,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (kind, category, title, desc, cal, p, c, f, amount, unit, 'telegram-ai'))
        conn.commit(); conn.close()
        return title

    if is_meal and meals:
        for meal in meals:
            slot  = meal.get('slot') or 'extra'
            title = name_hint or meal.get('title') or f"{slot} sabit ogun"
            cat   = 'kahvalti' if 'kahvalti' in n or 'sabah' in n else \
                    'ogle' if 'ogle' in n else \
                    'aksam' if 'aksam' in n else slot
            saved = upsert_template('meal', cat, title, meal.get('description') or title,
                                    meal.get('calories'), meal.get('protein_g'),
                                    meal.get('carbs_g'), meal.get('fat_g'))
            if saved: titles.append(saved)

    if (is_supp or not is_meal) and vitamins:
        for v in vitamins:
            title = name_hint or v.get('name') or 'Supplement'
            saved = upsert_template('supplement', 'supplement', title, '',
                                    None, None, None, None,
                                    str(v.get('amount') or ''), v.get('unit') or '')
            if saved: titles.append(saved)

    return ', '.join(dict.fromkeys(titles))

# KOMUTLAR
async def cmd_start(u, c):
    await u.message.reply_text(
        "Taha Serdem Daily Rapor\n\n"
        "Dogal dil yaz: '7.5 saat uyudum' veya 'kahvaltida 3 yumurta yedim'\n\n"
        "/uyku 7.5 8   uyku + kalite\n"
        "/su 2.5        su (litre)\n"
        "/mood 8 7 3   enerji mood stres\n"
        "/vitamin D3 5000 IU\n"
        "/bugun        ozet\n"
        "/rapor        detayli rapor\n"
        "/hafta        7 gunluk ozet\n"
        "/antrenman    program\n"
        "/streak       seri"
    )

async def cmd_uyku(u, c):
    try:
        a = c.args
        db_upsert('sleep_logs', operation_today(), {
            'hours':   float(a[0]) if a else None,
            'quality': int(a[1])   if len(a) > 1 else None
        })
        await u.message.reply_text(f"Uyku kaydedildi: {a[0] if a else '?'}s")
    except Exception:
        await u.message.reply_text("Kullanim: /uyku 7.5 8")

async def cmd_su(u, c):
    try:
        l = float(c.args[0])
        today = operation_today()
        conn = get_db()
        cur = water_get_total(conn, today)
        expected = cur + int(l*1000)
        water_consolidate(conn, today, expected)
        conn.commit()
        verified = water_get_total(conn, today)
        conn.close()
        if verified != expected:
            raise RuntimeError(f"Su kaydi dogrulanamadi: beklenen={expected}, bulunan={verified}")
        await u.message.reply_text(f"Su: +{l}L eklendi. Toplam: {verified/1000:.2f}L")
    except Exception:
        await u.message.reply_text("Kullanim: /su 2.5")

async def cmd_mood(u, c):
    try:
        a = c.args
        db_upsert('mood_logs', operation_today(), {
            'energy': int(a[0]) if a else None,
            'mood':   int(a[1]) if len(a) > 1 else None,
            'stress': int(a[2]) if len(a) > 2 else None
        })
        await u.message.reply_text("Ruh hali kaydedildi")
    except Exception:
        await u.message.reply_text("Kullanim: /mood 8 7 3")

async def cmd_vitamin(u, c):
    try:
        a = c.args
        conn = get_db()
        conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit) VALUES (?,?,?,?)",
                     (operation_today(), a[0] if a else '?',
                      a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else ''))
        conn.commit(); conn.close()
        await u.message.reply_text(f"Vitamin: {' '.join(a)}")
    except Exception:
        await u.message.reply_text("Kullanim: /vitamin D3 5000 IU")

async def cmd_bugun(u, c):
    await u.message.reply_text(today_summary())

async def cmd_rapor(u, c):
    today = operation_today()
    conn = get_db()
    sl    = conn.execute("SELECT * FROM sleep_logs    WHERE date=?", (today,)).fetchone()
    ex    = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
    nu    = conn.execute("SELECT SUM(water_ml) as water_ml FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    w     = conn.execute("SELECT * FROM work_logs     WHERE date=?", (today,)).fetchone()
    mo    = conn.execute("SELECT * FROM mood_logs     WHERE date=?", (today,)).fetchone()
    vs    = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=?", (today,)).fetchall()]
    meals = [dict(r) for r in conn.execute("SELECT * FROM meal_entries WHERE date=? ORDER BY id", (today,)).fetchall()]
    conn.close()
    totals = meal_macro_totals(today)
    td = training_day(today)
    sr = streak_count()
    lines = [
        f"=== GUNLUK RAPOR {today} ===",
        f"Seri: {sr} gun | Antrenman: {td}", "",
        "[ UYKU ]",
        f"  {sl['hours']}s kalite {sl['quality']}/10" if sl and sl['hours'] else "  -", "",
        "[ EGZERSIZ ]",
        f"  {ex['type']} {ex['duration']}dk yogunluk {ex['intensity']}/10" if ex and ex['type'] else "  -", "",
        f"[ BESLENME ] {totals['calories']} kcal | P {totals['protein_g']}g | K {totals['carbs_g']}g | Y {totals['fat_g']}g",
    ]
    for m in meals:
        lines.append(f"  {m['title'] or m['slot']}: {m['calories'] or '?'} kcal")
    if not meals:
        lines.append("  Ogun kaydi yok")
    lines += [
        "", "[ SU ]",
        f"  {(nu['water_ml'] or 0)/1000:.1f}L" if nu and nu['water_ml'] else "  -", "",
        "[ IS ]",
        f"  {w['hours']}s" if w and w['hours'] else "  -", "",
        "[ RUH HALI ]",
        f"  Enerji {mo['energy']}/10 | Mood {mo['mood']}/10 | Stres {mo['stress']}/10" if mo and mo['energy'] else "  -", "",
        "[ VITAMINLER ]",
    ]
    for v in vs:
        lines.append(f"  {v['name']} {v['amount']} {v['unit']}")
    if not vs:
        lines.append("  -")
    await u.message.reply_text('\n'.join(lines))

async def cmd_hafta(u, c):
    start = operation_date() - timedelta(days=6)
    conn = get_db()
    sl_rows = conn.execute("SELECT * FROM sleep_logs    WHERE date>=? ORDER BY date", (start.isoformat(),)).fetchall()
    ex_rows = conn.execute("SELECT * FROM exercise_logs WHERE date>=? ORDER BY date", (start.isoformat(),)).fetchall()
    w_rows  = conn.execute("SELECT * FROM work_logs     WHERE date>=? ORDER BY date", (start.isoformat(),)).fetchall()
    mo_rows = conn.execute("SELECT * FROM mood_logs     WHERE date>=? ORDER BY date", (start.isoformat(),)).fetchall()
    conn.close()
    def avg(rows, key):
        v = [float(r[key]) for r in rows if r[key] is not None]
        return round(sum(v)/len(v), 1) if v else '-'
    await u.message.reply_text(
        f"7 GUNLUK OZET\n"
        f"Uyku: ort {avg(sl_rows,'hours')}s | kalite {avg(sl_rows,'quality')}/10\n"
        f"Egzersiz: {len(ex_rows)}/7 gun\n"
        f"Is: ort {avg(w_rows,'hours')}s/gun\n"
        f"Enerji: {avg(mo_rows,'energy')}/10 | Mood: {avg(mo_rows,'mood')}/10 | Stres: {avg(mo_rows,'stress')}/10"
    )

async def cmd_antrenman(u, c):
    today = operation_date()
    lines = ["ANTRENMAN TAKVIMI\n"]
    for i in range(-1, 8):
        d = today + timedelta(days=i)
        td = training_day(d.isoformat())
        prefix = ">>> BUGUN: " if i == 0 else ("DUN:       " if i == -1 else "           ")
        lines.append(f"{prefix}{d.strftime('%a %d/%m')} -- {td}")
    await u.message.reply_text('\n'.join(lines))

async def morning_briefing(context):
    """Her sabah 07:00'de otomatik mesaj (Turkey = UTC+3, Railway UTC'de calisir)."""
    chat_id = get_owner_chat_id()
    if not chat_id:
        return
    today      = operation_today()
    yesterday  = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    conn       = get_db()
    gun_adi    = ['Pazartesi','Salı','Çarşamba','Perşembe','Cuma','Cumartesi','Pazar'][date.fromisoformat(today).weekday()]
    # Dünkü özet
    cal_row    = conn.execute("SELECT SUM(calories) as c FROM meal_entries WHERE date=?", (yesterday,)).fetchone()
    cal_dun    = int(cal_row['c'] or 0) if cal_row else 0
    water_row  = conn.execute("SELECT SUM(water_ml) as w FROM nutrition_logs WHERE date=?", (yesterday,)).fetchone()
    water_dun  = int(water_row['w'] or 0) if water_row else 0
    sleep_row  = conn.execute("SELECT hours, quality FROM sleep_logs WHERE date=?", (yesterday,)).fetchone()
    ex_row     = conn.execute("SELECT type FROM exercise_logs WHERE date=?", (today,)).fetchone()
    kilo_row   = conn.execute("SELECT weight_kg FROM body_metrics WHERE date=?", (today,)).fetchone()
    # Antrenman serisi — son 2 gün bak
    no_train_days = 0
    for i in range(1, 5):
        d = (date.fromisoformat(today) - timedelta(days=i)).isoformat()
        ex = conn.execute("SELECT id FROM exercise_logs WHERE date=?", (d,)).fetchone()
        if not ex:
            no_train_days += 1
        else:
            break
    # Bugünün antrenman programı
    antrenman_bugun = training_day(today)
    conn.close()

    lines = [f"☀️ Günaydın Taha! {gun_adi}, {today}"]
    lines.append("")
    if sleep_row:
        emoji = "😴" if (sleep_row['hours'] or 0) < 6 else "💤"
        lines.append(f"{emoji} Uyku: {sleep_row['hours']}s | Kalite: {sleep_row['quality'] or '?'}/10")
    if cal_dun > 0:
        lines.append(f"🍽️ Dün: {cal_dun} kcal | Su: {water_dun/1000:.1f}L")
    if kilo_row and kilo_row['weight_kg']:
        lines.append(f"⚖️ Kilo: {kilo_row['weight_kg']} kg (aç karna girdiysen iyi)")
    lines.append("")
    lines.append(f"📅 Bugün: {antrenman_bugun} günü")
    if no_train_days >= 2:
        lines.append(f"⚠️ {no_train_days} gündür antrenman yok — bugün salonun var!")
    # Supplement hatırlatma
    lines.append("💊 Supplementlerini aldın mı? 'Tüm vitaminler tamam' de loglayayım.")
    lines.append("")
    lines.append("Günün planı ne? 👊")

    await context.bot.send_message(chat_id=chat_id, text='\n'.join(lines))

async def night_check(context):
    """Gece 22:00'de — gün özeti + ertesi gün hatırlatması."""
    chat_id = get_owner_chat_id()
    if not chat_id:
        return
    today  = operation_today()
    conn   = get_db()
    cal    = conn.execute("SELECT SUM(calories) as c FROM meal_entries WHERE date=?", (today,)).fetchone()
    water  = conn.execute("SELECT SUM(water_ml) as w FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    ex     = conn.execute("SELECT type FROM exercise_logs WHERE date=?", (today,)).fetchone()
    kilo   = conn.execute("SELECT weight_kg, weight_kg_night FROM body_metrics WHERE date=?", (today,)).fetchone()
    vit    = conn.execute("SELECT COUNT(*) as c FROM vitamin_logs WHERE date=?", (today,)).fetchone()
    settings = {r['key']: r['value'] for r in conn.execute("SELECT key, value FROM user_settings").fetchall()}
    conn.close()

    cal_val   = int(cal['c'] or 0) if cal else 0
    water_val = int(water['w'] or 0) if water else 0
    cal_hedef = int(settings.get('cal', 2500))
    su_hedef  = int(settings.get('water', 3000))

    lines = [f"🌙 Gece Özeti — {today}"]
    lines.append("")
    lines.append(f"🍽️ Kalori: {cal_val} / {cal_hedef} kcal {'✅' if cal_val >= cal_hedef*0.9 else '⚠️'}")
    lines.append(f"💧 Su: {water_val/1000:.1f}L / {su_hedef/1000:.1f}L {'✅' if water_val >= su_hedef*0.9 else '⚠️'}")
    lines.append(f"🏋️ Antrenman: {'✅ ' + ex['type'] if ex else '❌ Yok'}")
    lines.append(f"💊 Supplement: {vit['c']} kayıt {'✅' if vit['c'] >= 4 else '⚠️'}")
    if kilo:
        if not kilo['weight_kg_night']:
            lines.append("⚖️ Gece tartısı girilmedi — 'gece kilom X kg' yaz.")
    lines.append("")
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    ant_yarn = training_day(tomorrow)
    lines.append(f"📅 Yarın: {ant_yarn} günü")
    lines.append("Akşam yemeğini logladın mı? 🍽️")

    await context.bot.send_message(chat_id=chat_id, text='\n'.join(lines))

async def cmd_streak(u, c):
    await u.message.reply_text(f"{streak_count()} gunluk seri!")

def parse_workout_log(raw_text):
    """
    Taha'nin antrenman formatini parse eder:
      DD.MM.YYYY [optional text]
      [N-]Egzersiz Adi (notlar)
      Warm up / Working set / Back off set
      agirlik-tekrar [agirlik-tekrar ...]
      ...
    Returns (date_str, result_sets) or None.
    result_sets: list of {'exercise', 'weight', 'reps', 'set_type', 'set_num'}
    """
    # Tek satir gelirse numarali egzersiz + set tipi kelimelerinden once newline ekle
    raw_text = raw_text.strip()
    if '\n' not in raw_text and re.match(r'\d{2}\.\d{2}\.\d{4}', raw_text):
        raw_text = re.sub(r'\s+(\d+)-((?!\d)[A-Za-z])', r'\n\1-\2', raw_text)
        for kw in ['Back off set','Back off','Working set','Workimg set','Working','Warm up']:
            raw_text = re.sub(r'\s+(' + re.escape(kw) + r')(\s|$)', r'\n\1\2', raw_text, flags=re.IGNORECASE)

    lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
    if not lines:
        return None

    # Ilk satir tarih icermeli: DD.MM.YYYY
    date_match = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', lines[0])
    if not date_match:
        return None
    day, month, year = date_match.groups()
    date_str = f"{year}-{month}-{day}"

    # Ilk satirin geri kalanini isle (tarihten sonra egzersiz numarasi gelebilir)
    first_rest = lines[0][date_match.end():].strip()
    # "Warm up" / "isınma" vs. kelimesini bas kisimdan at
    first_rest = re.sub(r'^(warm\s*up|isinma|ısınma)\s*', '', first_rest, flags=re.IGNORECASE).strip()
    processing = ([first_rest] if first_rest else []) + lines[1:]

    SET_TYPE_MAP = [
        ('back off set', 'Back Off Set'), ('back off', 'Back Off Set'),
        ('backoff set',  'Back Off Set'), ('backoff',  'Back Off Set'),
        ('working set',  'Working Set'),  ('workimg set', 'Working Set'),
        ('work set',     'Working Set'),  ('working',  'Working Set'),
        ('warm up',      'Warm Up'),      ('warmup',   'Warm Up'),
        ('isinma',       'Warm Up'),      ('ısınma',   'Warm Up'),
        ('drop set',     'Drop Set'),
    ]

    current_exercise = None
    current_set_type = 'Working Set'
    set_counters = {}   # exercise -> count
    result_sets = []

    for line in processing:
        norm = norm_tr(line.lower().strip())

        # Set tipi mi? (tam eslesme veya inline: "Warm up 12-10")
        matched_type = None
        inline_rest = None
        for key, val in SET_TYPE_MAP:
            if norm == key or norm == key + ':':
                matched_type = val
                break
            if norm.startswith(key + ' ') or norm.startswith(key + '\t'):
                matched_type = val
                inline_rest = line[len(key):].strip()
                break
        if matched_type:
            current_set_type = matched_type
            if not inline_rest:
                continue
            # inline set verisi var, asagida isle
            norm = norm_tr(inline_rest.lower())

        # Numarali egzersiz: "1-Chest Supported Row" / "1. ..." / "1) ..."
        ex_match = re.match(r'^(\d+)[\-\.\)]\s*(.+)', line)
        if ex_match:
            ex_raw = ex_match.group(2).strip()
            # Parantez icindeki aciklamayi at: "(Upper Back)" vs.
            ex_name = re.sub(r'\s*\([^)]*\)', '', ex_raw).strip()
            current_exercise = ex_name
            current_set_type = 'Working Set'
            continue

        # Set verisi: agirlik-tekrar ciftleri
        # Format: "20-10 22-10" veya superset "6-10/12-10"
        if current_exercise is None:
            continue

        set_iter = re.finditer(r'(\d+(?:\.\d+)?)-(\d+)(?:/(\d+(?:\.\d+)?)-(\d+))?', norm)
        for m in set_iter:
            w1, r1 = m.group(1), m.group(2)
            w2, r2 = m.group(3), m.group(4)

            set_counters[current_exercise] = set_counters.get(current_exercise, 0) + 1
            result_sets.append({
                'exercise': current_exercise,
                'weight': w1,
                'reps': r1,
                'set_type': current_set_type,
                'set_num': set_counters[current_exercise]
            })

            # Superset: ikinci egzersiz
            if w2 and r2:
                if '/' in current_exercise:
                    ex2 = current_exercise.split('/', 1)[1].strip()
                else:
                    ex2 = current_exercise + ' B'
                set_counters[ex2] = set_counters.get(ex2, 0) + 1
                result_sets.append({
                    'exercise': ex2,
                    'weight': w2,
                    'reps': r2,
                    'set_type': current_set_type,
                    'set_num': set_counters[ex2]
                })

    if not result_sets:
        return None
    return date_str, result_sets


def save_workout_log(date_str, result_sets):
    """
    Parse edilmis antrenman setlerini workout_logs tablosuna kaydeder.
    Ayni gun icin mevcut kayitlari once siler (temiz reimport).
    Returns (summary_lines, training_day_name).
    """
    td_name = training_day(date_str)
    conn = get_db()

    # Ayni gun icin mevcut workout kayitlarini temizle
    conn.execute("DELETE FROM workout_logs WHERE date=?", (date_str,))

    saved_exercises = {}
    for s in result_sets:
        ex = s['exercise']
        weight_str = f"{s['weight']} kg" if s['weight'] else ''
        conn.execute(
            "INSERT INTO workout_logs (date, training_day, exercise, set_num, weight, reps, set_type) VALUES (?,?,?,?,?,?,?)",
            (date_str, td_name, ex, s['set_num'], weight_str, s['reps'], s['set_type'])
        )
        saved_exercises[ex] = saved_exercises.get(ex, 0) + 1

    conn.commit()
    conn.close()

    summary = [f"  💪 {ex}: {cnt} set" for ex, cnt in saved_exercises.items()]
    return summary, td_name


def bulk_log_parse(raw_text):
    """
    Cok satirli toplu log mesajini parse eder.
    Her satiri ayri tanir: adim, su, stack, supplement, vb.
    Returns (actions, stack_results, descriptions, unhandled) veya None.
    """
    lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
    if len(lines) < 2:
        return None

    today = operation_today()
    actions = []
    stack_results = []
    descriptions = []
    unhandled = []

    for line in lines:
        norm = norm_tr(line)
        handled = False

        # Adim: "9000 adim", "9.000 adim", "9bin adim"
        m = re.search(r'(\d[\d.]*)\s*(bin\s*)?adim', norm)
        if m:
            try:
                steps_str = m.group(1).replace('.', '')
                steps = int(float(steps_str))
                if m.group(2):
                    steps *= 1000
                actions.append({'type': 'steps', 'date': today, 'steps': steps})
                descriptions.append(f"\U0001f463 Adim: {steps:,}")
                handled = True
            except (ValueError, OverflowError):
                pass

        # Su: "5l su", "2.5 litre", "500ml"
        if not handled:
            wm = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:l\b|lt\b|litre\b|liter\b)', norm)
            if wm:
                val = float(wm.group(1).replace(',', '.'))
                ml = int(val * 1000)
                actions.append({'type': 'water_set', 'date': today, 'water_ml': ml})
                descriptions.append(f"\U0001f4a7 Su: {val}L")
                handled = True
            elif re.search(r'\d+\s*ml', norm):
                wm2 = re.search(r'(\d+)\s*ml', norm)
                ml = int(wm2.group(1))
                actions.append({'type': 'water', 'date': today, 'water_ml': ml})
                descriptions.append(f"\U0001f4a7 Su: +{ml}ml")
                handled = True

        # Stack: "gece stack alindi", "pre-workout stack tamam"
        if not handled and 'stack' in norm:
            stack_acts = supplement_actions_from_stack_text(line)
            if stack_acts:
                saved = save_stack_actions(stack_acts)
                lbl = stack_label(stack_acts[0].get('stack', ''))
                stack_results.append((lbl, saved))
                if saved:
                    descriptions.append(f"\U0001f48a {lbl}: {', '.join(saved)}")
                else:
                    descriptions.append(f"\U0001f48a {lbl}: zaten kayitli")
                handled = True

        # Tekil supplement: "5g creatine", "kreatin 5gr alindi"
        if not handled:
            for item in supplement_catalog():
                keys_to_check = item['keys'] + [norm_tr(item['name'])]
                if any(norm_tr(k) in norm for k in keys_to_check):
                    dose_m = re.search(r'(\d+(?:[.,]\d+)?)\s*(g|gr|gram|mg|kapsul|tablet|damla)', norm)
                    if dose_m:
                        amount = dose_m.group(1).replace(',', '.')
                        unit_raw = dose_m.group(2)
                        unit = 'g' if unit_raw in ('g', 'gr', 'gram') else unit_raw
                    else:
                        amount = item['amount']
                        unit = item['unit']
                    actions.append({
                        'type': 'vitamin',
                        'date': today,
                        'name': item['name'],
                        'amount': str(amount),
                        'unit': unit,
                        'notes': 'toplu log'
                    })
                    descriptions.append(f"\U0001f48a {item['name']}: {amount} {unit}")
                    handled = True
                    break

        # Adim alternatif: "X steps"
        if not handled:
            m2 = re.search(r'(\d+)\s*step', norm)
            if m2:
                steps = int(m2.group(1))
                actions.append({'type': 'steps', 'date': today, 'steps': steps})
                descriptions.append(f"\U0001f463 Adim: {steps:,}")
                handled = True

        if not handled:
            unhandled.append(line)

    if not descriptions:
        return None
    return actions, stack_results, descriptions, unhandled


# Konusma gecmisi (chat_id -> son 6 mesaj)
_chat_history: dict = {}

def get_history(chat_id):
    return _chat_history.get(str(chat_id), [])

def add_history(chat_id, role, text):
    h = _chat_history.setdefault(str(chat_id), [])
    h.append({'role': role, 'text': text[:400]})
    if len(h) > 6:
        h.pop(0)

# AI CHAT
async def cmd_chat_ai(u, c):
    raw = (u.message.text or '').strip()
    if not raw:
        return

    chat_id = u.message.chat_id
    # İlk mesajda owner chat_id'yi kaydet
    if not get_owner_chat_id():
        save_owner_chat_id(chat_id)
    await u.message.chat.send_action('typing')
    n = norm_tr(raw)

    # GUN SONU RAPORU — dogal dil tetikleyicileri
    _gun_sonu_triggers = [
        'gun sonu', 'gunu kapat', 'gunu bitir', 'gunun ozeti', 'bugunun ozeti',
        'bugun nasıldi', 'nasıldi bugun', 'bugun nasildi', 'nasildi bugun',
        'bugun nasil gecti', 'gun nasil gecti', 'gun nasıldi',
        'gunluk rapor', 'gunluk ozet', 'rapor ver', 'ozet ver',
        'bugunum nasil', 'bugunku ozet', 'ne kadar yedim', 'bugun ne yedim',
        'gunu degerlendir', 'gunu degerlendır', 'gunu kapat', 'gunu sonlandir',
        'kapat gunu', 'gece raporu', 'aksam raporu', 'sabah raporu',
        'gun bitti', 'gunumu kapat', 'gunumu bitir', 'gunumu degerlendir',
        'bugun nasil oldu', 'nasil oldu bugun', 'gun sonu ozet',
        'daily summary', 'todays summary', 'how was today',
        'bugun iyi miydi', 'bugun kotu muydu', 'bugun iyiydi mi',
    ]
    if any(t in n for t in _gun_sonu_triggers):
        reply = today_summary()
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', reply)
        await u.message.reply_text(reply)
        return

    stack_update_reply = stack_update_from_text(raw)
    if stack_update_reply:
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', stack_update_reply)
        await u.message.reply_text(stack_update_reply)
        return

    # "Hepsini sil" / "bugünkü kayıtları temizle" — tüm öğün+takviye sil
    _del_all = any(w in n for w in [
        'hepsini sil', 'hepsini kaldir', 'bugunku kayitlari sil',
        'bugunkuleri sil', 'tum kayitlari sil', 'tum ogunleri sil',
        'temizle bugun', 'bugun temizle', 'kayitlari temizle',
        'sil hepsini', 'kaldir hepsini',
        'yemek verilerini sil', 'yemekleri sil', 'yemek kayitlarini sil',
        'ogunleri sil', 'ogun kayitlarini sil',
        'bugunku yemekleri sil', 'sil yemekleri', 'tum yemekleri sil'
    ])
    if _del_all:
        today = operation_today()
        conn  = get_db()
        meal_count = conn.execute("SELECT COUNT(*) as c FROM meal_entries WHERE date=?", (today,)).fetchone()['c']
        vit_count  = conn.execute("SELECT COUNT(*) as c FROM vitamin_logs WHERE date=?", (today,)).fetchone()['c']
        conn.execute("DELETE FROM meal_entries WHERE date=?", (today,))
        conn.execute("DELETE FROM vitamin_logs WHERE date=?", (today,))
        conn.commit(); conn.close()
        reply = f"✅ Bugünün tüm kayıtları silindi: {meal_count} öğün + {vit_count} takviye. Temiz sayfa!"
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', reply)
        await u.message.reply_text(reply)
        return

    # ANTRENMAN LOG: DD.MM.YYYY + numarali egzersizler formatini yakala
    if '\n' in raw:
        workout = parse_workout_log(raw)
        if workout:
            w_date, w_sets = workout
            w_summary, w_tdname = save_workout_log(w_date, w_sets)
            ex_count = len({s['exercise'] for s in w_sets})
            set_count = len(w_sets)
            reply_lines = [
                f"✅ Antrenman kaydedildi — {w_date} ({w_tdname})",
                f"📊 {ex_count} egzersiz, {set_count} set\n"
            ] + w_summary
            reply = '\n'.join(reply_lines)
            add_history(chat_id, 'user', raw)
            add_history(chat_id, 'bot', reply)
            await u.message.reply_text(reply)
            return

    # TOPLU LOG: 2+ satirli mesajlari her satir icin ayri isle
    if '\n' in raw:
        bulk = bulk_log_parse(raw)
        if bulk:
            bulk_actions, _bulk_stacks, bulk_descs, unhandled = bulk
            apply_actions(bulk_actions)
            reply_lines = ['✅ Toplu log kaydedildi:\n']
            reply_lines.extend(bulk_descs)
            if unhandled:
                reply_lines.append(f"\n⚠️ Tanimlanamadi: {', '.join(unhandled[:3])}")
            reply = '\n'.join(reply_lines)
            add_history(chat_id, 'user', raw)
            add_history(chat_id, 'bot', reply)
            await u.message.reply_text(reply)
            return

    # "Tüm vitaminler tamam" kisayolu — template'lerden direkt log at
    # Net stack kisayolu: ac karna/sabah/gece/pre/post stack AI'ya birakilmaz.
    # Yeni supplement sistem: API tabanlı snapshot + override + ekstra
    _stack_result = await _handle_stack_shortcut(raw, n, operation_today())
    if _stack_result:
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', _stack_result)
        await u.message.reply_text(_stack_result)
        return

    # "TÃ¼m vitaminler tamam" kisayolu â€” template'lerden direkt log at
    _all_vitamins = any(w in n for w in ['tum vitaminler','hepsini aldim','vitaminler tamam',
                                          'suppler tamam','suppler aldim','supplement tamam',
                                          'stacki aldim','stack aldim','sabah stacki tamam'])
    if _all_vitamins:
        today = operation_today()
        conn  = get_db()
        supps = conn.execute(
            "SELECT title, amount, unit FROM quick_templates WHERE kind='supplement' ORDER BY ts DESC"
        ).fetchall()
        if supps:
            saved_names = []
            for s in supps:
                conn.execute(
                    "INSERT INTO vitamin_logs (date, name, amount, unit, notes) VALUES (?,?,?,?,?)",
                    (today, s['title'], str(s.get('amount') or ''), s.get('unit') or 'kapsul', 'telegram-toplu')
                )
                amt = f"{s.get('amount','')} {s.get('unit','kapsul')}".strip() if s.get('amount') else ''
                saved_names.append(f"{s['title']}{' ('+amt+')' if amt else ''}")
            conn.commit(); conn.close()
            lines = ['✅ Tüm supplementler kaydedildi:'] + [f"  💊 {x}" for x in saved_names]
            reply = '\n'.join(lines)
        else:
            conn.close()
            reply = "Henüz kayıtlı supplement şablonu yok. Önce bir kez 'Probiyotik 1 kapsül aldım' gibi gir, öğreneyim."
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', reply)
        await u.message.reply_text(reply)
        return

    # Su sifirla kisayolu
    if any(w in n for w in ['su','suyu','suyumu']) and any(w in n for w in ['sifirla','temizle','bosalt','hepsini sil','kaydi sil']):
        today = operation_today()
        conn = get_db()
        water_consolidate(conn, today, 0)
        conn.commit(); conn.close()
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', 'Su sifirlandı: 0L')
        await u.message.reply_text("Su kaydı sıfırlandı: 0L")
        return

    # Su TOPLAM SET kisayolu — "toplam X ml/L", "su X olsun", "suyu X yap", "X ml olarak kaydet" vb.
    # Herhangi bir Claude cagrisından ONCE yakala — en güvenilir yol
    _su_set_trigger = (
        any(w in n for w in ['toplam','hepsini sil','oncekini sil','sil toplam']) and
        any(w in n for w in ['su','ml','litre','lt']) and
        re.search(r'\d', n)
    ) or (
        any(w in n for w in ['su','suyu','suyumu']) and
        any(w in n for w in ['olsun','olarak kaydet','yap ','yaz ','degistir','guncelle','set et','sadece']) and
        re.search(r'\d', n)
    )
    if _su_set_trigger:
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|l\b|litre|lt)?', n)
        if m:
            val  = float(m.group(1).replace(',','.'))
            unit = (m.group(2) or '').lower().strip()
            ml   = int(val * 1000) if unit in ('l','litre','lt') or (not unit and val <= 10) else int(val)
            if ml > 0:
                today = operation_today()
                conn  = get_db()
                water_consolidate(conn, today, ml)
                conn.commit(); conn.close()
                reply = f"Su toplam olarak {ml}ml ({ml/1000:.2f}L) ayarlandı. ✅"
                add_history(chat_id, 'user', raw)
                add_history(chat_id, 'bot', reply)
                await u.message.reply_text(reply)
                return

    # Su azalt kisayolu
    if any(w in n for w in ['su','suyu']) and any(w in n for w in ['azalt','cikart','eksilt','yanlis']):
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|l|litre|lt)?', n)
        if m:
            val  = float(m.group(1).replace(',','.'))
            unit = (m.group(2) or '').lower()
            ml   = int(val * 1000) if unit in ('l','litre','lt') or (not unit and val <= 10) else int(val)
            today = operation_today()
            conn = get_db()
            cur  = water_get_total(conn, today)
            new  = max(0, cur - ml)
            water_consolidate(conn, today, new)
            conn.commit(); conn.close()
            add_history(chat_id, 'user', raw)
            add_history(chat_id, 'bot', f'Su {ml}ml azaltildi. Toplam: {new/1000:.2f}L')
            await u.message.reply_text(f"Su {ml}ml azaltıldı. Toplam: {new/1000:.2f}L")
            return

    # ── SILME KISAYOLLARI (Claude'a gitmeden direkt DB) ──────────────────────
    SLOT_MAP = {'kahvalti':'kahvalti','sabah':'kahvalti','ogle':'ogle','aksam':'aksam',
                'ara':'ara','atistirma':'ara','gece':'gece','extra':'extra'}

    # "X'i sil" / "son yemeği sil" / "kahvaltıyı sil" vb.
    _del_meal = any(w in n for w in ['yemegi sil','ogunu sil','kahvaltiyi sil','kahvalti sil',
                                      'oglen sil','aksami sil','ara ogun sil',
                                      'son ogun','son yemegi sil','son kaydi sil',
                                      'kaydi kaldir','kaydi iptal','sil bunu','bunu sil',
                                      'kaldir bunu','yanlis girdim','yanlis kayit',
                                      'ogunleri siler', 'kayitlari siler', 'yemegi siler',
                                      'siler misin', 'kaldirir misin'])
    _del_meal = _del_meal or (any(w in n for w in ['sil','kaldir','iptal','siler']) and
                               any(w in n for w in ['ogun','yemek','kahvalti','ogle','aksam','atistirma']))
    if _del_meal:
        today = operation_today()
        conn  = get_db()
        slot_filter = next((v for k, v in SLOT_MAP.items() if k in n), None)
        if slot_filter:
            row = conn.execute(
                "SELECT id, title, calories FROM meal_entries WHERE date=? AND slot=? ORDER BY id DESC LIMIT 1",
                (today, slot_filter)).fetchone()
            if not row:  # slot bulunamazsa son ogun dene
                row = conn.execute(
                    "SELECT id, title, calories FROM meal_entries WHERE date=? ORDER BY id DESC LIMIT 1",
                    (today,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id, title, calories FROM meal_entries WHERE date=? ORDER BY id DESC LIMIT 1",
                (today,)).fetchone()
        if row:
            conn.execute("DELETE FROM meal_entries WHERE id=?", (row['id'],))
            conn.commit(); conn.close()
            reply = f"✅ {row['title']} silindi ({row['calories'] or '?'} kcal)"
        else:
            conn.close()
            reply = "Bugün silinecek öğün kaydı bulunamadı."
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', reply)
        await u.message.reply_text(reply)
        return  # HER DURUMDA return — Claude'a düşme

    # "son seti sil" / "X setini sil" / "bench'i sil"
    _del_set = (any(w in n for w in ['seti sil','set sil','son seti','antrenman sil','egzersiz sil']) or
                (any(w in n for w in ['sil','kaldir']) and any(w in n for w in ['set','tekrar','agirlik'])))
    if _del_set:
        today = operation_today()
        conn  = get_db()
        # Egzersiz ismi geciyorsa bul
        ex_words = [w for w in norm_tr(raw).split() if len(w) >= 4 and w not in
                    ('sil','kaldir','iptal','bunu','yanlış','hatam','yanlis')]
        row = None
        for ew in ex_words:
            row = conn.execute(
                "SELECT id, exercise, weight, reps FROM workout_logs WHERE date=? AND lower(exercise) LIKE ? ORDER BY id DESC LIMIT 1",
                (today, f'%{ew}%')).fetchone()
            if row: break
        if not row:
            row = conn.execute(
                "SELECT id, exercise, weight, reps FROM workout_logs WHERE date=? ORDER BY id DESC LIMIT 1",
                (today,)).fetchone()
        if row:
            conn.execute("DELETE FROM workout_logs WHERE id=?", (row['id'],))
            conn.commit(); conn.close()
            reply = f"✅ Set silindi: {row['exercise']} {row.get('weight','')} × {row.get('reps','')}"
        else:
            conn.close()
            reply = "Bugün silinecek set kaydı bulunamadı."
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', reply)
        await u.message.reply_text(reply)
        return

    # "vitamini sil" / "D3'ü sil"
    _del_vit = (any(w in n for w in ['vitamin sil','takviye sil','supplement sil','son vitamini sil']) or
                (any(w in n for w in ['sil','kaldir']) and any(w in n for w in ['vitamin','takviye','supplement','kapsul'])))
    if _del_vit:
        today = operation_today()
        conn  = get_db()
        # İsim belirtilmişse ara
        vit_name = next((w for w in n.split() if len(w) >= 3 and w not in
                         ['sil','kaldir','vitamini','takviye','supplement']), None)
        row = None
        if vit_name:
            row = conn.execute(
                "SELECT id, name FROM vitamin_logs WHERE date=? AND lower(name) LIKE ? ORDER BY id DESC LIMIT 1",
                (today, f'%{vit_name}%')).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id, name FROM vitamin_logs WHERE date=? ORDER BY id DESC LIMIT 1", (today,)).fetchone()
        if row:
            conn.execute("DELETE FROM vitamin_logs WHERE id=?", (row['id'],))
            conn.commit(); conn.close()
            reply = f"✅ {row['name']} silindi"
        else:
            conn.close()
            reply = "Bugün silinecek takviye/vitamin kaydı bulunamadı."
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', reply)
        await u.message.reply_text(reply)
        return
    # ── ANTRENMAN GEÇMİŞİ KISAYOLU ───────────────────────────────────────────
    # "antrenman", "idman", "ne yapıyorum" → loglamak değil sorgu → direkt DB'den getir
    _is_workout_query = (
        any(w in n for w in ['antrenman', 'idman', 'training', 'bugun ne yapiyorum', 'bugun ne var'])
        and not any(w in n for w in ['yaptim', 'yapti', 'kg', 'tekrar', 'set ', 'rep ', 'kaydet', 'kayit'])
    )
    if _is_workout_query:
        WEEKDAY_CYCLE = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
        today_split = WEEKDAY_CYCLE[operation_date().weekday()]
        if today_split == 'Off':
            reply = "🛌 Bugün Off day. Dinlen."
        else:
            last_date, formatted = last_split_workout(today_split)
            if formatted:
                reply = formatted
            else:
                reply = f"📋 {today_split} day için henüz geçmiş kayıt yok. İlk antrenman bu olacak!"
        add_history(chat_id, 'user', raw)
        add_history(chat_id, 'bot', reply)
        await u.message.reply_text(reply)
        return
    # ─────────────────────────────────────────────────────────────────────────

    history = get_history(chat_id)
    add_history(chat_id, 'user', raw)

    result   = claude_call(raw, history) if ANTHROPIC_API_KEY else {'reply': 'API key yok.', 'actions': []}
    actions  = result.get('actions') or []
    fixed_actions = []
    try:
        fixed_actions.extend(tg_known_food_update_from_text(raw))
        if not fixed_actions:
            fixed_actions.extend(tg_known_food_actions_from_text(raw))
        fixed_actions.extend(tg_water_actions_from_text(raw))
    except Exception:
        log.exception("Deterministik Telegram aksiyonlari basarisiz")
    actions = merge_actions_no_duplicates(actions, fixed_actions)
    saved    = apply_actions(actions)

    template_title = ''
    try:
        template_title = save_template_from_actions(raw, actions)
    except Exception:
        log.exception("Template kaydi basarisiz")

    try:
        food_db_auto_learn(actions)
    except Exception:
        log.exception("Food DB ogrenme basarisiz")

    reply = result.get('reply') or 'Anladim.'
    if template_title:
        reply += f"\n\nSablon kaydedildi: {template_title}"
    if saved:
        reply += f"\n\n✅ Kaydedildi: {', '.join(saved)}"
    add_history(chat_id, 'bot', reply)

    # Telegram mesajlarini DB'ye kaydet (site senkronizasyonu)
    try:
        username = (u.message.from_user.username or '') if u.message.from_user else ''
        conn = get_db()
        conn.execute(
            "INSERT INTO telegram_messages (direction,chat_id,username,message,actions) VALUES (?,?,?,?,?)",
            ('in', str(chat_id), username, raw, json.dumps(actions, ensure_ascii=False))
        )
        conn.execute(
            "INSERT INTO telegram_messages (direction,chat_id,username,message,actions) VALUES (?,?,?,?,?)",
            ('out', str(chat_id), username, reply, None)
        )
        conn.commit(); conn.close()
    except Exception:
        log.exception("Telegram mesaj kaydi basarisiz")

    await u.message.reply_text(reply)


def enforce_training_day_on_actions(actions, date_val=None):
    """AI yanilsa bile kayitlar resmi sistem gunune baglanir."""
    official = training_day(date_val or operation_today())
    valid_days = {"Push", "Pull", "Leg", "Upper", "Lower", "Off"}
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

# PHOTO / IMAGE ANALYSIS
async def cmd_photo(u, c):
    """Kullanici fotograf gonderdiyse Claude vision ile analiz et"""
    msg = u.message
    caption = (msg.caption or '').strip()
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
        ctx = today_ai_context()
        official_training = ctx.get('training_day') or training_day(today)
        photo_food_ctx = food_db_context(caption) if caption else ''
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
            + photo_food_ctx
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
        result = json_from_text(payload["content"][0]["text"])
        actions = enforce_training_day_on_actions(result.get("actions") or [], today)
        saved = apply_actions(actions)
        food_db_auto_learn(actions)
        reply = result.get("reply") or "Fotografi inceledim."
        if official_training and official_training.lower() not in reply.lower():
            reply += f"\n\nSistem notu: Bugunun resmi antrenman gunu {official_training}."
        if saved:
            reply += f"\n\n✅ Kaydedildi: {', '.join(saved)}"
        await msg.reply_text(reply)
    except Exception as e:
        log.exception("Fotograf analizi basarisiz")
        await msg.reply_text(f"Fotografi analiz edemedim: {e}")

# ─── BOT RUNNER ───────────────────────────────────────────────────────────────
async def _run_bot():
    from telegram.ext import Application, CommandHandler, MessageHandler, filters
    retry_delay = 15  # saniye — 409 conflict sonrasi bekleme
    while True:
        try:
            from telegram.ext import Application, CommandHandler, MessageHandler, filters
            app = (Application.builder()
                   .token(TELEGRAM_TOKEN)
                   .build())
            app.add_handler(CommandHandler("start",     cmd_start))
            app.add_handler(CommandHandler("uyku",      cmd_uyku))
            app.add_handler(CommandHandler("su",        cmd_su))
            app.add_handler(CommandHandler("mood",      cmd_mood))
            app.add_handler(CommandHandler("vitamin",   cmd_vitamin))
            app.add_handler(CommandHandler("bugun",     cmd_bugun))
            app.add_handler(CommandHandler("rapor",     cmd_rapor))
            app.add_handler(CommandHandler("hafta",     cmd_hafta))
            app.add_handler(CommandHandler("antrenman", cmd_antrenman))
            app.add_handler(CommandHandler("streak",    cmd_streak))
            app.add_handler(MessageHandler(filters.PHOTO, cmd_photo))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_chat_ai))
            # Sabah brifing: 07:00 Turkey = 04:00 UTC
            from datetime import time as dtime
            jq = app.job_queue
            if jq:
                jq.run_daily(morning_briefing, time=dtime(4, 0, 0),  name='sabah_brifing')
                jq.run_daily(night_check,      time=dtime(19, 0, 0), name='gece_ozeti')  # 22:00 Turkey
                log.info("Job queue: sabah 07:00 + gece 22:00 Turkey kuruldu")
            log.info("Bot baslatiliyor: @taha_serdem_daily_rapor_bot")
            async with app:
                await app.start()                                           # önce start
                await app.updater.start_polling(drop_pending_updates=True) # sonra polling
                log.info("Bot polling aktif.")
                await asyncio.Event().wait()
        except Exception as e:
            log.warning(f"[bot] Hata: {e} \u2014 {retry_delay}s sonra yeniden deneniyor...")
            await asyncio.sleep(retry_delay)

def main():
    asyncio.run(_run_bot())


if __name__ == "__main__":
    main()
