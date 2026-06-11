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
ANTHROPIC_MODEL   = 'claude-haiku-4-5-20251001'
CYCLE_START       = _cfg.get('CYCLE_START', date.today().isoformat())
TRAINING_CYCLE    = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']

# Antrenman programı v1 — 11.06.2026
PROGRAM = {
    'Push': [
        ('Incline DB/Makine Press',      '4×6-10',  'üst göğüs öncelikli'),
        ('High to Low Cable Fly',         '3×12-15', 'stretch hissini koru'),
        ('Lateral Raise',                 '4×12-15', 'kontrollü tempo'),
        ('Overhead Press (makine/DB)',    '3×10-12', 'opsiyonel'),
        ('Triceps Pushdown',              '3×12-15', ''),
        ('Overhead Rope Extension',       '3×12-15', ''),
    ],
    'Pull': [
        ('Pull-Up / Lat Pulldown',        '4×6-10',  'lat genişliği öncelikli'),
        ('Chest Supported Row',           '4×8-12',  'orta sırt kalınlığı'),
        ('Single Arm Cable Pulldown',     '3×12-15', 'lat stretch'),
        ('Rear Delt Fly',                 '4×15-20', 'ilerleme sürüyor'),
        ('EZ Bar Curl',                   '3×10-12', ''),
        ('Hammer Curl',                   '3×12-15', 'brachialis'),
    ],
    'Leg': [
        ('Bulgarian Split Squat',         '4×8-12',  'quad öncelikli'),
        ('Toe Elevated RDL',              '4×8-12',  'hamstring stretch'),
        ('Leg Extension',                 '4×10-15', 'hedef: 55→60 kg'),
        ('Leg Curl',                      '3×10-15', ''),
        ('Standing Calf Raise',           '4×15-20', 'full ROM'),
        ('Glute Bridge',                  '3×15',    'opsiyonel'),
    ],
    'Upper': [
        ('Incline Press',                 '3×8-12',  'Push\'tan farklı varyant'),
        ('Chest Supported Row',           '3×10-12', ''),
        ('Lat Pulldown',                  '3×10-12', ''),
        ('Lateral Raise',                 '3×15-20', 'hafif, pump'),
        ('Rear Delt',                     '3×15-20', ''),
        ('Bayesian Curl',                 '3×12-15', ''),
        ('Triceps Pushdown/Rope',         '3×12-15', ''),
    ],
    'Lower': [
        ('Reverse Lunge',                 '3×10-12', 'her bacak'),
        ('Leg Curl',                      '3×12-15', ''),
        ('Leg Extension',                 '3×12-15', 'heavy günden hafif'),
        ('Calf Raise',                    '3×15-20', ''),
        ('Core: Leg Raise + Plank',       '3-4 set', ''),
    ],
    'Off': [],
}

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
    ''')
    conn.commit(); conn.close()

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

# TRAINING
def training_day(date_str):
    d = date.fromisoformat(date_str)
    start = date.fromisoformat(CYCLE_START)
    diff = (d - start).days % 7
    if diff < 0:
        diff = (diff + 7) % 7
    return TRAINING_CYCLE[diff]

# HELPERS
def norm_tr(text):
    t = (text or '').lower()
    for a, b in [('ı','i'),('İ','i'),(chr(287),'g'),(chr(286),'g'),
                 (chr(252),'u'),(chr(220),'u'),(chr(351),'s'),(chr(350),'s'),
                 (chr(246),'o'),(chr(214),'o'),(chr(231),'c'),(chr(199),'c')]:
        t = t.replace(a, b)
    return t

def get_last_sets_for_day(day_type):
    """O antrenman günü için önceki seanstan ağırlıkları çek."""
    conn = get_db()
    # son tarih
    row = conn.execute(
        "SELECT MAX(date) as last_date FROM workout_logs WHERE training_day=? AND date < ?",
        (day_type, date.today().isoformat())
    ).fetchone()
    if not row or not row['last_date']:
        conn.close()
        return {}
    last_date = row['last_date']
    rows = conn.execute(
        "SELECT exercise, weight, reps, set_num FROM workout_logs WHERE date=? AND training_day=? ORDER BY exercise, set_num",
        (last_date, day_type)
    ).fetchall()
    conn.close()
    # Her egzersiz için son seti al (en yüksek set_num)
    result = {}
    for r in rows:
        result[r['exercise']] = {'weight': r['weight'], 'reps': r['reps'], 'date': last_date}
    return result

def get_today_sets():
    """Bugün kaydedilen setleri çek."""
    today = date.today().isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT exercise, set_num, weight, reps, set_type FROM workout_logs WHERE date=? ORDER BY exercise, set_num",
        (today,)
    ).fetchall()
    conn.close()
    grouped = {}
    for r in rows:
        ex = r['exercise']
        if ex not in grouped:
            grouped[ex] = []
        grouped[ex].append({'set': r['set_num'], 'weight': r['weight'], 'reps': r['reps']})
    return grouped

def streak_count():
    conn = get_db()
    n, d = 0, date.today()
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
    today = date.today().isoformat()
    conn = get_db()
    sl  = conn.execute("SELECT * FROM sleep_logs   WHERE date=?", (today,)).fetchone()
    ex  = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
    nu  = conn.execute("SELECT * FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    w   = conn.execute("SELECT * FROM work_logs    WHERE date=?", (today,)).fetchone()
    co  = conn.execute("SELECT * FROM coaching_logs WHERE date=?", (today,)).fetchone()
    mo  = conn.execute("SELECT * FROM mood_logs    WHERE date=?", (today,)).fetchone()
    conn.close()
    totals = meal_macro_totals(today)
    td = training_day(today)
    sr = streak_count()
    lines = [f"BUGUN {date.today().strftime('%d/%m/%Y')} | {sr} gun seri | {td}\n"]
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
    today = date.today().isoformat()
    totals = meal_macro_totals(today)
    conn = get_db()
    sl  = conn.execute("SELECT * FROM sleep_logs    WHERE date=?", (today,)).fetchone()
    ex  = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
    nu  = conn.execute("SELECT * FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    mo  = conn.execute("SELECT * FROM mood_logs     WHERE date=?", (today,)).fetchone()
    vs  = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY ts", (today,)).fetchall()]
    conn.close()
    td = training_day(today)
    last_sets = get_last_sets_for_day(td)
    today_sets = get_today_sets()
    program_exercises = [(name, sets) for name, sets, _ in PROGRAM.get(td, [])]
    return {
        'date': today,
        'training_day': td,
        'program': program_exercises,
        'last_session_weights': last_sets,
        'todays_logged_sets': today_sets,
        'macros': totals,
        'water_l': round(((dict(nu).get('water_ml') or 0) if nu else 0) / 1000, 2),
        'sleep': dict(sl) if sl else {},
        'exercise': dict(ex) if ex else {},
        'mood': dict(mo) if mo else {},
        'vitamins': vs,
    }

def json_from_text(txt):
    txt = (txt or '').strip()
    if txt.startswith('```'):
        txt = txt.strip('`')
        if txt.lower().startswith('json'):
            txt = txt[4:].strip()
    s = txt.find('{'); e = txt.rfind('}')
    if s >= 0 and e > s:
        txt = txt[s:e+1]
    return json.loads(txt)

def claude_call(user_text):
    import urllib.request, urllib.error
    ctx = today_ai_context()
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    system_prompt = (
        "Sen Taha Serdem'in kisisel antrenman ve gunluk performans kocusun. "
        "Turkce, samimi, net ve motive edici konus.\n"
        "Mesaji analiz et. Kayit iceriyorsa actions listesini doldur. "
        "Birden fazla kayit varsa hepsini ayri action olarak ekle.\n"
        "\nANTRENMAN KOCLUGU KURALLARI:\n"
        "- Kullanici set logu gonderirse (ornek: 'incline 32kg 4x8') -> workout_set action olustur ve kisa geri bildirim ver\n"
        "- Onceki seanstan agirlik varsa (last_session_weights) karsilastir: artti mi, ayni mi, dustu mu belirt\n"
        "- Bugunkü programa (program) gore hangi egzersizlerin loga girmedigini fark edersen nazikce hatirlatabilirsin\n"
        "- Uyku 5-6 saat ise: 'hacmi biraz azalt' uyu\n"
        "- Progressive overload: rep araligi tutulduysa bir sonraki seans icin agirlik artisini oner\n"
        "\nKAPIL KURALLAR:\n"
        "- Supplement/vitamin: kapsul/tablet sayisini amount olarak kaydet, unit='kapsul' veya 'tablet' yaz\n"
        "- Kapsul ornekleri: '2 kapsul NAC' -> amount='2' unit='kapsul' | '1 tablet D3' -> amount='1' unit='tablet'\n"
        "- Su gecmis saat: 'saat 09da 500ml' veya '09:00da 2 bardak' bile olsa bugune ekle, water_ml hesapla\n"
        "- Birden fazla su girisi: her birini ayri water action olarak ekle (hepsi ayni gune toplanir)\n"
        "- Gecmis tarih: 'dun', 'onceki gun', 'dun gece' -> date=dun tarihi\n"
        "- Yemek title: her zaman gercek isim (Panikek, Tavuklu Pilav, Omlet...), asla slot ismi yazma\n"
        "- Kalori/makro bilinmiyorsa makul tahmin yap, reply'da belirt\n"
        "- Egzersiz: exercise_type alani hareketin/gunun adini icersin (Push, Squat, Bench Press...)\n"
        "- Antrenman set: 'bench press 80kg 8 tekrar' veya 'squat 3 set 100kg 5 tekrar' gibi seyler workout_set olarak kaydet\n"
        "- workout_set icin: exercise=hareket adi, weight='80 kg', reps='8', set_type=('Working Set'|'Warm-up'|'Back-off'), sets=kac set varsa her birini ayri action yaz\n"
        "- Birden fazla set: '3 set' yazilmissa 3 ayri workout_set action uret (aynı exercise/weight/reps ile)\n"
        "- Hem exercise hem workout_set uret: exercise(genel gun logu) + workout_set(detayli setler)\n"
        "\nSADECE gecerli JSON dondur:\n"
        '{"reply":"...","actions":['
        '{"type":"sleep","date":"YYYY-MM-DD","hours":7.5,"quality":8},'
        '{"type":"exercise","date":"YYYY-MM-DD","exercise_type":"Push","duration":60,"intensity":8},'
        '{"type":"workout_set","date":"YYYY-MM-DD","exercise":"Bench Press","weight":"80 kg","reps":"8","set_type":"Working Set"},'
        '{"type":"meal","date":"YYYY-MM-DD","slot":"kahvalti","title":"Panikek Kahvaltisi","description":"3 yumurta, peynir","calories":450,"protein_g":32,"carbs_g":10,"fat_g":28},'
        '{"type":"water","date":"YYYY-MM-DD","water_ml":500},'
        '{"type":"mood","date":"YYYY-MM-DD","energy":8,"mood":7,"stress":3},'
        '{"type":"vitamin","date":"YYYY-MM-DD","name":"D3","amount":"2","unit":"kapsul"},'
        '{"type":"weight","date":"YYYY-MM-DD","weight_kg":90.5},'
        '{"type":"steps","date":"YYYY-MM-DD","steps":8500},'
        '{"type":"note","date":"YYYY-MM-DD","note":"..."}'
        ']}\n'
        f'Tarih: bugun={today}, dun={yesterday}, suan={datetime.now().strftime("%H:%M")}.\n'
        f'Kullanici tarih belirtmemisse date={today}.\n'
        'Bugunun verisi: ' + json.dumps(ctx, ensure_ascii=False)
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

# ACTIONS
def apply_actions(actions):
    saved = []
    today = date.today().isoformat()
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
                    row = conn.execute("SELECT water_ml FROM nutrition_logs WHERE date=?", (d,)).fetchone()
                    if row:
                        conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE date=?", ((row['water_ml'] or 0) + ml, d))
                    else:
                        conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (d, ml))
                    conn.commit(); conn.close()
                    saved.append(f'su (+{ml}ml)')

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

            elif typ == 'vitamin':
                conn = get_db()
                conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit,notes) VALUES (?,?,?,?,?)",
                             (d, a.get('name') or '', str(a.get('amount') or ''),
                              a.get('unit') or '', a.get('notes') or ''))
                conn.commit(); conn.close()
                saved.append(f"supplement ({a.get('name','')})")

            elif typ in ('weight', 'body_weight', 'kilo'):
                kg = float(a.get('weight_kg') or a.get('kg') or 0)
                if kg:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO body_metrics (date, weight_kg, notes) VALUES (?,?,?) ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg",
                        (d, kg, 'telegram-ai'))
                    conn.commit(); conn.close()
                    saved.append(f'kilo ({kg}kg)')

            elif typ == 'steps':
                steps = int(a.get('steps') or 0)
                if steps:
                    conn = get_db()
                    conn.execute("INSERT OR REPLACE INTO step_logs (date,steps,notes) VALUES (?,?,?)", (d, steps, 'telegram-ai'))
                    conn.commit(); conn.close()
                    saved.append(f'adim ({steps})')

            elif typ == 'note':
                db_upsert('daily_notes', d, {'note': a.get('note') or ''})
                saved.append('not')

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
        db_upsert('sleep_logs', date.today().isoformat(), {
            'hours':   float(a[0]) if a else None,
            'quality': int(a[1])   if len(a) > 1 else None
        })
        await u.message.reply_text(f"Uyku kaydedildi: {a[0] if a else '?'}s")
    except Exception:
        await u.message.reply_text("Kullanim: /uyku 7.5 8")

async def cmd_su(u, c):
    try:
        l = float(c.args[0])
        today = date.today().isoformat()
        conn = get_db()
        row = conn.execute("SELECT id, water_ml FROM nutrition_logs WHERE date=?", (today,)).fetchone()
        if row:
            conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE date=?", ((row['water_ml'] or 0) + int(l*1000), today))
        else:
            conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (today, int(l*1000)))
        conn.commit(); conn.close()
        await u.message.reply_text(f"Su: +{l}L eklendi")
    except Exception:
        await u.message.reply_text("Kullanim: /su 2.5")

async def cmd_mood(u, c):
    try:
        a = c.args
        db_upsert('mood_logs', date.today().isoformat(), {
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
                     (date.today().isoformat(), a[0] if a else '?',
                      a[1] if len(a) > 1 else '', a[2] if len(a) > 2 else ''))
        conn.commit(); conn.close()
        await u.message.reply_text(f"Vitamin: {' '.join(a)}")
    except Exception:
        await u.message.reply_text("Kullanim: /vitamin D3 5000 IU")

async def cmd_bugun(u, c):
    await u.message.reply_text(today_summary())

async def cmd_rapor(u, c):
    today = date.today().isoformat()
    conn = get_db()
    sl    = conn.execute("SELECT * FROM sleep_logs    WHERE date=?", (today,)).fetchone()
    ex    = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
    nu    = conn.execute("SELECT * FROM nutrition_logs WHERE date=?", (today,)).fetchone()
    w     = conn.execute("SELECT * FROM work_logs     WHERE date=?", (today,)).fetchone()
    mo    = conn.execute("SELECT * FROM mood_logs     WHERE date=?", (today,)).fetchone()
    vs    = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=?", (today,)).fetchall()]
    meals = [dict(r) for r in conn.execute("SELECT * FROM meal_entries WHERE date=? ORDER BY id", (today,)).fetchall()]
    conn.close()
    totals = meal_macro_totals(today)
    td = training_day(today)
    sr = streak_count()
    lines = [
        f"=== GUNLUK RAPOR {date.today().strftime('%d/%m/%Y')} ===",
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
    start = date.today() - timedelta(days=6)
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
    args = c.args
    today_str = date.today().isoformat()

    # /antrenman log — bugünün setleri
    if args and norm_tr(args[0]) in ('log', 'sets', 'setler'):
        sets = get_today_sets()
        if not sets:
            await u.message.reply_text("Bugün henüz set kaydı yok.")
            return
        lines = [f"📋 Bugünün Setleri — {date.today().strftime('%d/%m/%Y')}\n"]
        for ez, s_list in sets.items():
            set_strs = ' | '.join(f"S{s['set']}: {s['weight']} ×{s['reps']}" for s in s_list)
            lines.append(f"• {ex}: {set_strs}")
        await u.message.reply_text('\n'.join(lines))
        return

    # /antrenman [push|pull|leg|upper|lower] veya bugünkü gün
    if args:
        day_map = {'push': 'Push', 'pull': 'Pull', 'leg': 'Leg', 'legs': 'Leg',
                   'bacak': 'Leg', 'upper': 'Upper', 'lower': 'Lower'}
        td = day_map.get(norm_tr(args[0]))
        if not td:
            await u.message.reply_text("Kullanım: /antrenman [push|pull|leg|upper|lower|log]")
            return
    else:
        td = training_day(today_str)

    exercises = PROGRAM.get(td, [])

    if td == 'Off' or not exercises:
        await u.message.reply_text("🛌 Bugün dinlenme günü. İyi recovery!")
        return

    last_sets = get_last_sets_for_day(td)

    emoji = {'Push': '🔥', 'Pull': '💪', 'Leg': '🦵', 'Upper': '⚡', 'Lower': '🏃'}
    lines = [f"{emoji.get(td,'💪')} {td.upper()} DAY — {date.today().strftime('%d/%m/%Y')}\n"]

    for i, (name, reps, note) in enumerate(exercises, start=1):
        # Son seanstan ağırlık var mı?
        # Benzer isim arama (tam eşleşme veya içerme)
        last_weight = ''
        for ex_name, ex_data in last_sets.items():
            if norm_tr(name[:10]) in norm_tr(ex_name) or norm_tr(ex_name[:10]) in norm_tr(name):
                w = ex_data.get('weight', '')
                r = ex_data.get('reps', '')
                if w:
                    last_weight = f"  [son: {w} ×{r}]"
                break

        note_str = f"  ({note})" if note else ''
        lines.append(f"{i}. {name}\n   {reps}{last_weight}{note_str}")

    lines.append("\n📝 Seti kaydetmek için yaz:\n\"incline 32kg 4x8\" veya \"leg ext 55 4 set 12 tekrar\"")
    await u.message.reply_text('\n'.join(lines))

async def cmd_streak(u, c):
    await u.message.reply_text(f"{streak_count()} gunluk seri!")

# AI CHAT
async def cmd_chat_ai(u, c):
    raw = (u.message.text or '').strip()
    if not raw:
        return

    await u.message.chat.send_action('typing')

    # Su duzeltme kisayolu
    n = norm_tr(raw)
    if any(w in n for w in ['su','suyu']) and any(w in n for w in ['azalt','cikart','eksilt','sil','yanlis']):
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|l|litre|lt)?', n)
        if m:
            val  = float(m.group(1).replace(',','.'))
            unit = (m.group(2) or '').lower()
            ml   = int(val * 1000) if unit in ('l','litre','lt') or (not unit and val <= 10) else int(val)
            today = date.today().isoformat()
            conn = get_db()
            row  = conn.execute("SELECT water_ml FROM nutrition_logs WHERE date=?", (today,)).fetchone()
            cur  = int((row['water_ml'] if row else 0) or 0)
            new  = max(0, cur - ml)
            if row:
                conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE date=?", (new, today))
            else:
                conn.execute("INSERT INTO nutrition_logs (date, water_ml) VALUES (?,?)", (today, new))
            conn.commit(); conn.close()
            await u.message.reply_text(f"Su {ml}ml azaltildi. Toplam: {new/1000:.2f}L")
            return

    result   = claude_call(raw) if ANTHROPIC_API_KEY else {'reply': 'API key yok.', 'actions': []}
    actions  = result.get('actions') or []
    saved    = apply_actions(actions)

    template_title = ''
    try:
        template_title = save_template_from_actions(raw, actions)
    except Exception:
        log.exception("Template kaydi basarisiz")

    reply = result.get('reply') or 'Anladim.'
    if template_title:
        reply += f"\n\nSablon kaydedildi: {template_title}"
    if saved:
        reply += f"\n\n✅ Kaydedildi: {', '.join(saved)}"
    await u.message.reply_text(reply)

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
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        ctx = today_ai_context()
        system_prompt = (
            "Sen Taha Serdem'in kisisel antrenman ve gunluk performans kocusun. "
            "Turkce, samimi, net ve motive edici konus.\n"
            "Fotografi analiz et. Antrenman programi, ilerleme, vucuk olcumu, yemek veya "
            "herhangi bir not gorebilirsin.\n"
            "- Antrenman programi/logu fotografiysa: eksiklikleri, iyilestirme onerilerini, "
            "o gune uygun antrenmani yaz\n"
            "- Vucud fotografiysa: durusu, kas gelisimini, genel yorumu paylas\n"
            "- Yemek fotografiysa: tahmini kalori/makro bilgisi ver, isterse kaydet\n"
            "- Kayit iceriyorsa actions listesini doldur.\n"
            "SADECE gecerli JSON dondur:\n"
            '{"reply":"...","actions":[]}'
            f"\nTarih: bugun={today}, dun={yesterday}\n"
            f"Bugunun verisi: {json.dumps(ctx, ensure_ascii=False)}"
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
        actions = result.get("actions") or []
        saved = apply_actions(actions)
        reply = result.get("reply") or "Fotografi inceledim."
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
            app = Application.builder().token(TELEGRAM_TOKEN).build()
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
            log.info("Bot baslatiliyor: @taha_serdem_daily_rapor_bot")
            async with app:
                await app.updater.start_polling(drop_pending_updates=True)
                await app.start()
                await asyncio.Event().wait()
        except Exception as e:
            log.warning(f"[bot] Hata: {e} — {retry_delay}s sonra yeniden deneniyor...")
            await asyncio.sleep(retry_delay)

def main():
    asyncio.run(_run_bot())

if __name__ == "__main__":
    main()
