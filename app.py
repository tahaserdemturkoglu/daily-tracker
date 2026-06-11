#!/usr/bin/env python3
"""Taha Serdem Daily Rapor â Flask + Telegram Bot"""

import os, sqlite3, threading, asyncio, json, logging, re, re, re, re, re, re, re, re, re, re, re, re, re, re, re, re, re
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, render_template

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.environ.get('DATA_DIR', BASE_DIR)
DB_PATH     = os.path.join(DATA_DIR, 'tracker.db')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
PORT        = int(os.environ.get('PORT', 5000))

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding='utf-8-sig') as f:
            return json.load(f)
    return {}

_cfg = load_config()
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', _cfg.get('TELEGRAM_TOKEN', ''))
# Antrenman dÃ¶ngÃ¼sÃ¼ baÅlangÄ±Ã§ tarihi (Push gÃ¼nÃ¼). BugÃ¼n baÅlar.
CYCLE_START = _cfg.get('CYCLE_START', date.today().isoformat())

app = Flask(__name__, template_folder='templates')
logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

# âââ ANTRENMAN DÃNGÃSÃ âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
TRAINING_CYCLE = ['Push', 'Pull', 'Leg', 'Upper', 'Lower', 'Off', 'Off']
TRAINING_COLORS = {
    'Push':  '#cc0000',
    'Pull':  '#990000',
    'Leg':   '#ff2222',
    'Upper': '#aa1111',
    'Lower': '#881111',
    'Off':   '#333333',
}

def training_day(date_str):
    d = date.fromisoformat(date_str)
    start = date.fromisoformat(CYCLE_START)
    diff = (d - start).days % 7
    if diff < 0:
        diff = (diff + 7) % 7
    return TRAINING_CYCLE[diff]

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
    start = (date.today() - timedelta(days=days-1)).isoformat()
    rows = conn.execute(f"SELECT * FROM {table} WHERE date >= ? ORDER BY date ASC", (start,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_today(table):
    conn = get_db()
    row = conn.execute(f"SELECT * FROM {table} WHERE date=? LIMIT 1", (date.today().isoformat(),)).fetchone()
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
    n, d = 0, date.today()
    tables = ('sleep_logs','exercise_logs','nutrition_logs','work_logs','coaching_logs','mood_logs')
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
    return response

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/today')
def api_today():
    today = date.today().isoformat()
    conn = get_db()
    vitamins = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY ts", (today,)).fetchall()]
    note_row = conn.execute("SELECT note FROM daily_notes WHERE date=?", (today,)).fetchone()
    conn.close()
    return jsonify({
        'sleep': db_today('sleep_logs'), 'exercise': db_today('exercise_logs'),
        'nutrition': db_today('nutrition_logs'), 'work': db_today('work_logs'),
        'coaching': db_today('coaching_logs'), 'mood': db_today('mood_logs'),
        'vitamins': vitamins,
        'note': note_row['note'] if note_row else '',
        'training_day': training_day(today),
        'training_color': TRAINING_COLORS[training_day(today)],
        'streak': streak_count(), 'date': today,
    })

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
    d = data.pop('date', date.today().isoformat())
    db_upsert(tables[category], d, data)
    return jsonify({'ok': True, 'date': d})



def _table_count_for_date(conn, table, date_col='date', d=None):
    d = d or date.today().isoformat()
    try:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {date_col}=?", (d,)).fetchone()
        return int(row['c'] if row else 0)
    except Exception:
        return 0

@app.route('/api/system/status')
def api_system_status():
    d = request.args.get('date', date.today().isoformat())
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
    d = request.args.get('date', date.today().isoformat())
    conn = get_db()
    conn.execute(f"DELETE FROM {tables[category]} WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/vitamins/today', methods=['DELETE'])
def api_vitamins_today_delete():
    d = request.args.get('date', date.today().isoformat())
    conn = get_db()
    conn.execute("DELETE FROM vitamin_logs WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/meals/today', methods=['DELETE'])
def api_meals_today_delete():
    d = request.args.get('date', date.today().isoformat())
    conn = get_db()
    conn.execute("DELETE FROM meal_entries WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/note', methods=['DELETE'])
def api_note_delete():
    d = request.args.get('date', date.today().isoformat())
    conn = get_db()
    conn.execute("DELETE FROM daily_notes WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})



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
    d = date_str or date.today().isoformat()
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
    d = data.get('date', date.today().isoformat())
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
    d = request.args.get('date', date.today().isoformat())
    conn = get_db()
    conn.execute("DELETE FROM step_logs WHERE date=?", (d,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/vitamin', methods=['POST'])
def api_vitamin():
    data = request.get_json(force=True) or {}
    d = data.pop('date', date.today().isoformat())
    conn = get_db()
    conn.execute("INSERT INTO vitamin_logs (date, name, amount, unit, notes) VALUES (?,?,?,?,?)",
                 (d, data.get('name',''), data.get('amount',''), data.get('unit',''), data.get('notes','')))
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
    conn.execute("UPDATE vitamin_logs SET name=?, amount=?, unit=?, notes=? WHERE id=?",
                 (data.get('name','').strip(), data.get('amount','').strip(),
                  data.get('unit','').strip(), data.get('notes','').strip(), vid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/meals/today')
def api_meals_today():
    return api_meals_day(date.today().isoformat())

@app.route('/api/meals/<date_str>')
def api_meals_day(date_str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM meal_entries WHERE date=? ORDER BY id", (date_str,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/meals', methods=['POST'])
def api_meal_save():
    data = request.get_json(force=True) or {}
    d = data.get('date', date.today().isoformat())
    slot = data.get('slot', '').strip() or 'extra'
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    calories = data.get('calories') or None
    protein_g = data.get('protein_g') or None
    carbs_g = data.get('carbs_g') or None
    fat_g = data.get('fat_g') or None
    fiber_g = data.get('fiber_g') or None
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


@app.route('/api/meals/<int:mid>', methods=['PUT'])
def api_meal_update(mid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    conn.execute("""
        UPDATE meal_entries
        SET slot=?, title=?, description=?, calories=?, protein_g=?, carbs_g=?, fat_g=?, fiber_g=?, source=?
        WHERE id=?
    """, (
        data.get('slot', '').strip() or 'extra',
        data.get('title', '').strip(),
        data.get('description', '').strip(),
        data.get('calories') or None,
        data.get('protein_g') or None,
        data.get('carbs_g') or None,
        data.get('fat_g') or None,
        data.get('fiber_g') or None,
        data.get('source', '').strip(),
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
    today = date.today().isoformat()
    return jsonify({'date': today, 'totals': meal_macro_totals(today), 'meals': api_meals_day(today).get_json()})


@app.route('/api/macro/range')
def api_macro_range():
    days = int(request.args.get('days', 7))
    days = max(1, min(days, 60))
    start = date.today() - timedelta(days=days-1)
    conn = get_db()
    result = []
    for i in range(days):
        ds = (start + timedelta(days=i)).isoformat()
        meals = meal_macro_totals(ds)
        row = conn.execute("SELECT water_ml FROM nutrition_logs WHERE date=?", (ds,)).fetchone()
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
    start = (date.today() - timedelta(days=days-1)).isoformat()
    conn = get_db()
    rows = conn.execute("SELECT * FROM body_metrics WHERE date>=? ORDER BY date", (start,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/body/metrics/today')
def api_body_metrics_today():
    ensure_body_metrics_table()
    d = request.args.get('date', date.today().isoformat())
    conn = get_db()
    row = conn.execute("SELECT * FROM body_metrics WHERE date=?", (d,)).fetchone()
    conn.close()
    return jsonify(dict(row) if row else {'date': d, 'weight_kg': None, 'waist_cm': None, 'chest_cm': None, 'arm_cm': None, 'notes': ''})

@app.route('/api/body/metrics', methods=['POST'])
def api_body_metrics_save():
    ensure_body_metrics_table()
    data = request.get_json(force=True) or {}
    d = data.get('date') or date.today().isoformat()
    weight = data.get('weight_kg')
    waist = data.get('waist_cm')
    chest = data.get('chest_cm')
    arm = data.get('arm_cm')
    notes = data.get('notes') or ''
    conn = get_db()
    conn.execute("""
        INSERT INTO body_metrics (date, weight_kg, waist_cm, chest_cm, arm_cm, notes)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, waist_cm=excluded.waist_cm, chest_cm=excluded.chest_cm, arm_cm=excluded.arm_cm, notes=excluded.notes
    """, (d, weight, waist, chest, arm, notes))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': d})

@app.route('/api/body/metrics/<date_str>', methods=['DELETE'])
def api_body_metrics_delete(date_str):
    ensure_body_metrics_table()
    conn = get_db()
    conn.execute("DELETE FROM body_metrics WHERE date=?", (date_str,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'date': date_str})

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

@app.route('/api/note', methods=['POST'])
def api_note():
    data = request.get_json(force=True) or {}
    d = data.get('date', date.today().isoformat())
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
    for day in range(1, days_in_month + 1):
        d = f"{year:04d}-{month:02d}-{day:02d}"
        tables = ('sleep_logs','exercise_logs','nutrition_logs','work_logs','coaching_logs','mood_logs')
        has_data = any(conn.execute(f"SELECT id FROM {t} WHERE date=?", (d,)).fetchone() for t in tables)
        note_row = conn.execute("SELECT note FROM daily_notes WHERE date=?", (d,)).fetchone()
        td = training_day(d)
        result.append({
            'date': d, 'day': day,
            'training': td,
            'color': TRAINING_COLORS[td],
            'has_data': has_data,
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
    conn.close()
    td = training_day(date_str)
    return jsonify({
        'sleep': db_date('sleep_logs', date_str),
        'exercise': db_date('exercise_logs', date_str),
        'nutrition': db_date('nutrition_logs', date_str),
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
    })

@app.route('/api/report/today')
def api_report():
    today = date.today().isoformat()
    d = json.loads(api_day(today).get_data())
    sl = d.get('sleep', {}); ex = d.get('exercise', {}); nu = d.get('nutrition', {})
    w = d.get('work', {}); co = d.get('coaching', {}); mo = d.get('mood', {})
    vits = d.get('vitamins', []); sr = streak_count()
    td = d.get('training', '')

    lines = [
        f"=== TAHA SERDEM GUNLUK RAPOR ===",
        f"Tarih: {date.today().strftime('%d %B %Y')} | Seri: {sr} gun | Antrenman: {td}",
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
    """Weekly/monthly summary for the Ãzet page."""
    days = int(request.args.get('days', 7))
    days = max(1, min(days, 90))
    ensure_step_logs_table()
    ensure_body_metrics_table()
    start = date.today() - timedelta(days=days - 1)
    conn = get_db()
    result = []
    for i in range(days):
        ds = (start + timedelta(days=i)).isoformat()
        sl = conn.execute("SELECT * FROM sleep_logs WHERE date=?", (ds,)).fetchone()
        mo = conn.execute("SELECT * FROM mood_logs WHERE date=?", (ds,)).fetchone()
        nu = conn.execute("SELECT * FROM nutrition_logs WHERE date=?", (ds,)).fetchone()
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
    day = data.get('training_day', '').strip() or training_day(date.today().isoformat())
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
        data.get('training_day', '').strip() or training_day(date.today().isoformat()),
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
    'Ä±sÄ±nma': 'Warm up',
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
    pattern = r'(warm\s*up|warmup|isinma|Ä±sÄ±nma|working\s*set|working|ana\s*set|top\s*set|top|back\s*off|backoff|drop\s*set|drop)'
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
    today = date.today().isoformat()
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
    conn.close()
    for r in program_rows:
        r['set_details'] = _training_parse_sets(r)
    for r in logs:
        try:
            r['set_details'] = json.loads(r.get('sets_json') or '[]')
        except Exception:
            r['set_details'] = []
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
    conn.execute("DELETE FROM training_day_logs WHERE id=?", (log_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

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
    today = date.today()
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
        "SELECT * FROM workout_logs WHERE date=? ORDER BY exercise, set_num", (date_str,)
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
    d = data.get('date', date.today().isoformat())
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
    conn.execute("DELETE FROM workout_logs WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

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
    """Son 14 gÃ¼nÃ¼n antrenmanlarini dÃ¶ndÃ¼rÃ¼r â exercise + date listesi."""
    days = int(request.args.get('days', 14))
    from datetime import date as _date, timedelta
    cutoff = (_date.today() - timedelta(days=days)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT date, exercise FROM workout_logs WHERE date >= ? ORDER BY date DESC",
        (cutoff,)
    ).fetchall()
    conn.close()
    return jsonify([{'date': r['date'], 'exercise': r['exercise']} for r in rows])

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
    today = date.today().isoformat()
    d = json.loads(api_day(today).get_data())
    sl=d.get('sleep',{}); ex=d.get('exercise',{}); nu=d.get('nutrition',{})
    w=d.get('work',{}); co=d.get('coaching',{}); mo=d.get('mood',{}); sr=streak_count()
    td=d.get('training','')
    lines = [f"BUGUN {date.today().strftime('%d/%m/%Y')} | {sr} gun | {td}\n"]
    lines.append("Uyku: " + (f"{sl.get('hours','?')}s kalite {sl.get('quality','?')}/10" if sl else "-"))
    lines.append("Egzersiz: " + (f"{ex.get('type','?')} {ex.get('duration','?')}dk" if ex else "-"))
    lines.append("Beslenme: " + (f"{nu.get('calories','?')}kcal su {(nu.get('water_ml',0) or 0)/1000:.1f}L" if nu else "-"))
    lines.append("Is: " + (f"{w.get('hours','?')}s" if w else "-"))
    lines.append("Antrenorluk: " + (f"{co.get('sessions','?')} seans" if co else "-"))
    lines.append("Ruh hali: " + (f"enerji {mo.get('energy','?')} mood {mo.get('mood','?')} stres {mo.get('stress','?')}" if mo else "-"))
    return '\n'.join(lines)

async def cmd_ogun(u,c):
    today=date.today().isoformat(); totals=meal_macro_totals(today)
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
        a=c.args; db_upsert('sleep_logs',date.today().isoformat(),{'hours':float(a[0]) if a else None,'quality':int(a[1]) if len(a)>1 else None})
        await u.message.reply_text(f"Uyku: {a[0] if a else '?'}s")
    except: await u.message.reply_text("Kullanim: /uyku 7.5 8")
async def cmd_egzersiz(u,c):
    try:
        a=c.args; db_upsert('exercise_logs',date.today().isoformat(),{'type':a[0] if a else '?','duration':int(a[1]) if len(a)>1 else None,'intensity':int(a[2]) if len(a)>2 else None})
        await u.message.reply_text(f"Egzersiz: {a[0] if a else '?'}")
    except: await u.message.reply_text("Kullanim: /egzersiz bench 60 9")
async def cmd_yemek(u,c):
    try:
        a=c.args
        today=date.today().isoformat()
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
        l=float(c.args[0]); today=date.today().isoformat(); conn=get_db()
        row=conn.execute("SELECT id,water_ml FROM nutrition_logs WHERE date=?",(today,)).fetchone()
        if row: conn.execute("UPDATE nutrition_logs SET water_ml=? WHERE date=?",((row['water_ml'] or 0)+int(l*1000),today))
        else: conn.execute("INSERT INTO nutrition_logs (date,water_ml) VALUES (?,?)",(today,int(l*1000)))
        conn.commit(); conn.close()
        await u.message.reply_text(f"Su: +{l}L")
    except: await u.message.reply_text("Kullanim: /su 2.5")
async def cmd_is(u,c):
    try:
        a=c.args; db_upsert('work_logs',date.today().isoformat(),{'hours':float(a[0]) if a else None,'notes':' '.join(a[1:])})
        await u.message.reply_text(f"Is: {a[0] if a else '?'}s")
    except: await u.message.reply_text("Kullanim: /is 8 notlar")
async def cmd_antrenor(u,c):
    try:
        a=c.args; db_upsert('coaching_logs',date.today().isoformat(),{'sessions':int(a[0]) if a else None,'notes':' '.join(a[1:])})
        await u.message.reply_text(f"Antrenorluk: {a[0] if a else '?'} seans")
    except: await u.message.reply_text("Kullanim: /antrenor 3 notlar")
async def cmd_mood(u,c):
    try:
        a=c.args; db_upsert('mood_logs',date.today().isoformat(),{'energy':int(a[0]) if a else None,'mood':int(a[1]) if len(a)>1 else None,'stress':int(a[2]) if len(a)>2 else None})
        await u.message.reply_text("Ruh hali kaydedildi")
    except: await u.message.reply_text("Kullanim: /mood 8 7 3")
async def cmd_vitamin(u,c):
    try:
        a=c.args; name=a[0] if a else '?'; amount=a[1] if len(a)>1 else ''; unit=a[2] if len(a)>2 else ''
        conn=get_db(); conn.execute("INSERT INTO vitamin_logs (date,name,amount,unit) VALUES (?,?,?,?)",(date.today().isoformat(),name,amount,unit)); conn.commit(); conn.close()
        await u.message.reply_text(f"Vitamin: {name} {amount} {unit}")
    except: await u.message.reply_text("Kullanim: /vitamin D3 5000 IU")
async def cmd_bugun(u,c): await u.message.reply_text(tg_today_summary())
async def cmd_rapor(u,c): await u.message.reply_text(tg_report())
async def cmd_antrenman(u,c):
    sched = json.loads(api_training_schedule().get_data())['schedule']
    lines = ["ANTRENMAN PROGRAMI\n"]
    for s in sched:
        prefix = ">>> " if s['is_today'] else "    "
        lines.append(f"{prefix}{s['date']} {s['training']}")
    await u.message.reply_text('\n'.join(lines))
async def cmd_hafta(u,c):
    data = json.loads(api_week().get_data())
    def avg(lst,key): v=[r[key] for r in lst if r.get(key) is not None]; return round(sum(v)/len(v),1) if v else '-'
    await u.message.reply_text(
        f"7 GUNLUK OZET\nUyku: ort {avg(data['sleep'],'hours')}s kalite {avg(data['sleep'],'quality')}/10\n"
        f"Egzersiz: {len(data['exercise'])}/7 gun\nIs: ort {avg(data['work'],'hours')}s/gun\n"
        f"Antrenorluk: {sum(r.get('sessions',0) or 0 for r in data['coaching'])} seans\n"
        f"Enerji: {avg(data['mood'],'energy')}/10 Mood: {avg(data['mood'],'mood')}/10 Stres: {avg(data['mood'],'stress')}/10"
    )
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
    today = date.today().isoformat()
    totals = meal_macro_totals(today)
    conn = get_db()
    try:
        sleep = conn.execute("SELECT * FROM sleep_logs WHERE date=?", (today,)).fetchone()
        exercise = conn.execute("SELECT * FROM exercise_logs WHERE date=?", (today,)).fetchone()
        nutrition = conn.execute("SELECT * FROM nutrition_logs WHERE date=?", (today,)).fetchone()
        mood = conn.execute("SELECT * FROM mood_logs WHERE date=?", (today,)).fetchone()
        vitamins = [dict(r) for r in conn.execute("SELECT * FROM vitamin_logs WHERE date=? ORDER BY ts", (today,)).fetchall()]
        note = conn.execute("SELECT note FROM daily_notes WHERE date=?", (today,)).fetchone()
    finally:
        conn.close()
    nutrition_d = dict(nutrition) if nutrition else {}
    ctx = {
        'date': today,
        'training_day': training_day(today),
        'macros': totals,
        'water_l': round(((nutrition_d.get('water_ml') or 0) / 1000), 2),
        'sleep': dict(sleep) if sleep else {},
        'exercise': dict(exercise) if exercise else {},
        'mood': dict(mood) if mood else {},
        'vitamins': vitamins,
        'note': note['note'] if note else '',
    }
    return ctx

def _claude_call(user_text):
    import urllib.request, urllib.error
    ctx = _today_ai_context()
    system_prompt = (
        "Sen Taha Serdem'in kiÅisel antrenman ve gÃ¼nlÃ¼k performans koÃ§usun. "
        "TÃ¼rkÃ§e, samimi, net ve motive edici konuÅ.\n"
        "KullanÄ±cÄ±nÄ±n mesajÄ±nÄ± analiz et. KayÄ±t iÃ§eriyorsa actions listesini doldur. "
        "Eksik bilgi varsa once makul tahminle kaydet ve belirsizligi reply icinde belirt; sadece kritik bilgi tamamen yoksa kisa soru sor. Tam gun beslenme mesajlarinda asla detay ver diye kacma; mevcut gramajlardan yaklasik gun toplamlarini cikar.\n"
        "SADECE geÃ§erli JSON dÃ¶ndÃ¼r:\n"
        '{"reply":"...","actions":['
        '{"type":"sleep","date":"YYYY-MM-DD","hours":7.5,"quality":8},'
        '{"type":"exercise","date":"YYYY-MM-DD","exercise_type":"Upper","duration":60,"intensity":8,"notes":""},'
        '{"type":"meal","date":"YYYY-MM-DD","slot":"kahvaltÄ±","description":"...","calories":500,"protein_g":30,"carbs_g":60,"fat_g":10},'
        '{"type":"water","date":"YYYY-MM-DD","water_ml":500},'
        '{"type":"mood","date":"YYYY-MM-DD","energy":8,"mood":7,"stress":3},'
        '{"type":"vitamin","date":"YYYY-MM-DD","name":"D3","amount":"5000","unit":"IU"},'
        '{"type":"note","date":"YYYY-MM-DD","note":"..."}'
        ']}\n'
        f'Tarih kuralÄ±: KullanÄ±cÄ± tarih belirtmemiÅse date={date.today().isoformat()} (bugÃ¼n). '
        f'"DÃ¼n" derse date={(date.today()-timedelta(days=1)).isoformat()}. '
        '"X gÃ¼n Ã¶nce" veya "X Haziran" gibi ifadeleri doÄru tarihe Ã§evir. '
        'BugÃ¼n: ' + date.today().isoformat() + '\n'
        'BugÃ¼nÃ¼n verisi: ' + json.dumps(ctx, ensure_ascii=False)
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
        return {'reply': f'Claude hatasÄ±: {msg}', 'actions': []}
    except Exception:
        log.exception("Claude cevap hatasi")
        return {'reply': 'BaÄlantÄ± sorunu. Tekrar dener misin?', 'actions': []}


def ai_coach_call(user_text):
    if ANTHROPIC_API_KEY:
        return _claude_call(user_text)
    if not OPENAI_API_KEY:
        return {
            'reply': (
                'AI modu aktif deÄil.\n\n'
                'KomutlarÄ± kullanabilirsin:\n'
                '/uyku /egzersiz /yemek /su /mood /vitamin\n'
                '/bugun /rapor /hafta /antrenman'
            ),
            'actions': []
        }

    import urllib.request, urllib.error
    ctx = _today_ai_context()

    system_prompt = (
        "Sen Taha Serdem'in kiÅisel antrenman ve gÃ¼nlÃ¼k performans koÃ§usun. "
        "TÃ¼rkÃ§e, samimi ve net konuÅ. Motive edici ama gerÃ§ekÃ§i ol.\n"
        "KullanÄ±cÄ±nÄ±n mesajÄ±nÄ± analiz et. KayÄ±t iÃ§eriyorsa actions listesini doldur. "
        "Eksik bilgi varsa once makul tahminle kaydet ve belirsizligi reply icinde belirt; sadece kritik bilgi tamamen yoksa kisa soru sor. Tam gun beslenme mesajlarinda asla detay ver diye kacma; mevcut gramajlardan yaklasik gun toplamlarini cikar.\n"
        "Medikal teÅhis koyma.\n\n"
        "SADECE geÃ§erli JSON dÃ¶ndÃ¼r, baÅka hiÃ§bir Åey yazma:\n"
        '{"reply":"...","actions":['
        '{"type":"sleep","hours":7.5,"quality":8},'
        '{"type":"exercise","exercise_type":"Upper","duration":60,"intensity":8,"notes":""},'
        '{"type":"meal","slot":"kahvaltÄ±","description":"...","calories":500,"protein_g":30,"carbs_g":60,"fat_g":10},'
        '{"type":"water","water_ml":500},'
        '{"type":"mood","energy":8,"mood":7,"stress":3},'
        '{"type":"vitamin","name":"D3","amount":"5000","unit":"IU"},'
        '{"type":"training_exercise","exercise":"Bench press","set_details":[{"type":"Warm up","reps":"12","weight":"40 kg"},{"type":"Working set","reps":"8","weight":"80 kg"},{"type":"Back off","reps":"12","weight":"60 kg"}]},'
        '{"type":"steps","steps":8500},{"type":"body_weight","weight_kg":95.2},{"type":"skin_log","area":"yÃ¼z","name":"Benzoyl peroxide","status":"done"},{"type":"note","note":"..."}'
        ']}'
    )

    body = {
        'model': OPENAI_MODEL,
        'response_format': {'type': 'json_object'},
        'messages': [
            {
                'role': 'system',
                'content': system_prompt + '\n\nBugÃ¼nÃ¼n verisi: ' + json.dumps(ctx, ensure_ascii=False)
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
        return {'reply': f'OpenAI hatasÄ±: {msg}', 'actions': []}
    except Exception:
        log.exception("OpenAI cevap hatasi")
        return {'reply': 'AI cevabÄ±nÄ± iÅlerken sorun Ã§Ä±ktÄ±. Tekrar dener misin?', 'actions': []}


def tg_template_name_from_text(raw_text):
    text = (raw_text or '').strip()
    if not text:
        return ''
    m = re.search(r'ad[Ä±i]\s+(.+?)\s+olsun', text, flags=re.I)
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
        return 'kahvaltÄ±'
    if any(w in text for w in ['pre', 'antrenman Ã¶ncesi', 'idman Ã¶ncesi']):
        return 'pre-antrenman'
    if any(w in text for w in ['post', 'antrenman sonrasÄ±', 'idman sonrasÄ±']):
        return 'post-antrenman'
    if any(w in text for w in ['Ã¶Äle', 'ogle', 'lunch']):
        return 'Ã¶Äle'
    if any(w in text for w in ['akÅam', 'aksam', 'dinner']):
        return 'akÅam'
    return slot or 'extra'

def tg_should_save_template(raw_text):
    text = (raw_text or '').lower()
    return any(w in text for w in ['fiks', 'fix', 'sabit', 'Åablon', 'sablon', 'favori', 'hep kullan', 'kaydet'])

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
    title = tg_template_name_from_text(raw_text) or meal.get('title') or meal.get('slot') or 'Sabit ÃÄÃ¼n'
    if 'kahvalt' in tg_meal_category_from_text(raw_text, meal.get('slot') or '') and 'kahvalt' not in title.lower():
        title = title.strip() + ' KahvaltÄ±sÄ±'
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
        """, payload + ('', '', 'telegram-ai sabit Ã¶ÄÃ¼n'))
    conn.commit(); conn.close()
    return title

def ai_apply_actions(actions):
    saved = []
    today = date.today().isoformat()
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
                saved.append('Ã¶ÄÃ¼n')
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
            elif typ == 'sleep':
                db_upsert('sleep_logs', action_date, {'hours': a.get('hours'), 'quality': a.get('quality')})
                saved.append('uyku')
            elif typ == 'mood':
                db_upsert('mood_logs', action_date, {'energy': a.get('energy'), 'mood': a.get('mood'), 'stress': a.get('stress')})
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
                conn = get_db()
                conn.execute("INSERT INTO vitamin_logs (date, name, amount, unit, notes) VALUES (?,?,?,?,?)",
                             (action_date, a.get('name') or '', str(a.get('amount') or ''), a.get('unit') or '', a.get('notes') or ''))
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
                    saved.append('adÄ±m')
            elif typ in ('skin', 'skin_log'):
                ensure_skin_tables()
                conn = get_db()
                conn.execute("INSERT INTO skin_logs (date, area, name, status, notes) VALUES (?,?,?,?,?)",
                             (action_date, a.get('area') or 'yÃ¼z', a.get('name') or a.get('item') or 'cilt rutini', a.get('status') or 'done', a.get('notes') or 'telegram-ai'))
                conn.commit(); conn.close()
                saved.append('cilt')
            elif typ == 'note':
                db_upsert('daily_notes', action_date, {'note': a.get('note') or ''})
                saved.append('not')
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
        'Ä±': 'i', 'Ä°': 'i', 'Ä': 'g', 'Ä': 'g', 'Ã¼': 'u', 'Ã': 'u',
        'Å': 's', 'Å': 's', 'Ã¶': 'o', 'Ã': 'o', 'Ã§': 'c', 'Ã': 'c'
    })
    n = norm.translate(trans)
    water_words = ('su', 'suyu', 'water')
    correction_words = ('azalt', 'dus', 'dÃ¼Å', 'eksilt', 'geri al', 'yanlis', 'yanlÄ±Å', 'fazla', 'sil')
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

    today = date.today().isoformat()
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
        'reply': f"Tamam, suyu {amount_ml} ml azalttÄ±m. Yeni toplam: {new_total/1000:.2f} L."
    }



# TG_BASIC_NO_AI_FALLBACK_V1
def tg_basic_actions_from_text(raw_text):
    """Extract critical records even when the AI provider is temporarily unavailable."""
    text = raw_text or ''
    low = text.lower()
    trans = str.maketrans({
        'Ä±': 'i', 'Ä°': 'i', 'Ä': 'g', 'Ä': 'g', 'Ã¼': 'u', 'Ã': 'u',
        'Å': 's', 'Å': 's', 'Ã¶': 'o', 'Ã': 'o', 'Ã§': 'c', 'Ã': 'c',
        'ÃÂ±': 'i', 'ÃÂ°': 'i', 'ÃÅ¸': 'g', 'ÃÅ¾': 'g', 'ÃÂ¼': 'u', 'ÃÅ': 'u',
        'ÃÅ¸': 's', 'ÃÅ¾': 's', 'ÃÂ¶': 'o', 'Ãâ': 'o', 'ÃÂ§': 'c', 'Ãâ¡': 'c'
    })
    norm = low.translate(trans)
    today = date.today().isoformat()
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
        fat = re.search(r'(?:yag|yaÄ|fat|y)\s*[:~â ]+\s*(\d+(?:[\.,]\d+)?)\s*g?', norm)
        if cal or pro or carb or fat:
            slot = 'extra'
            if 'kahvalti' in norm:
                slot = 'kahvaltÄ±'
            elif 'ogle' in norm:
                slot = 'Ã¶Äle'
            elif 'aksam' in norm:
                slot = 'akÅam'
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
        'Ä±': 'i', 'Ä°': 'i', 'Ä': 'g', 'Ä': 'g', 'Ã¼': 'u', 'Ã': 'u',
        'Å': 's', 'Å': 's', 'Ã¶': 'o', 'Ã': 'o', 'Ã§': 'c', 'Ã': 'c',
        'ÃÂ±': 'i', 'ÃÂ°': 'i', 'ÃÅ¸': 'g', 'ÃÅ¾': 'g', 'ÃÂ¼': 'u', 'ÃÅ': 'u',
        'ÃÅ¸': 's', 'ÃÅ¾': 's', 'ÃÂ¶': 'o', 'Ãâ': 'o', 'ÃÂ§': 'c', 'Ãâ¡': 'c'
    })
    norm = low.translate(trans)
    if not any(x in norm for x in ['kahvalti', 'ogle', 'aksam']):
        return []
    today = date.today().isoformat()
    actions = []

    def section(name, start_words, stop_words):
        start = min([norm.find(w) for w in start_words if norm.find(w) >= 0] or [-1])
        if start < 0:
            return ''
        end_candidates = [norm.find(w, start + 1) for w in stop_words if norm.find(w, start + 1) >= 0]
        end = min(end_candidates) if end_candidates else len(norm)
        return text[start:end]

    sections = [
        ('kahvaltÄ±', section('kahvaltÄ±', ['kahvalti'], ['ogle', 'aksam', 'gun totali'])),
        ('Ã¶Äle', section('Ã¶Äle', ['ogle'], ['aksam', 'gun totali'])),
        ('akÅam', section('akÅam', ['aksam'], ['gun totali'])),
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
        if 'yarim kase mercimek' in ln or 'yarÄ±m kase mercimek' in line.lower():
            out = add(out, {'cal': 115.0, 'p': 9.0, 'c': 20.0, 'f': 0.5})
        if 'fis' in ln and 'gymbeam' in ln:
            fm = re.search(r'(\d+)\s*fis', ln)
            sprays = float(fm.group(1)) if fm else 1.0
            out = add(out, {'cal': sprays * 1.0, 'p': 0.0, 'c': 0.0, 'f': sprays * 0.1})
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
        ('Ä±','i'),('Ä°','i'),('Ä','g'),('Ä','g'),('Ã¼','u'),('Ã','u'),
        ('Å','s'),('Å','s'),('Ã¶','o'),('Ã','o'),('Ã§','c'),('Ã','c'),
        ('ÃÂ±','i'),('ÃÂ°','i'),('ÃÅ¸','g'),('ÃÅ¾','g'),('ÃÂ¼','u'),('ÃÅ','u'),
        ('ÃÅ¸','s'),('ÃÅ¾','s'),('ÃÂ¶','o'),('Ãâ','o'),('ÃÂ§','c'),('Ãâ¡','c')
    ]
    for a, b in pairs:
        text = text.replace(a, b)
    return text

def tg_full_day_actions_from_text(raw_text):
    text = raw_text or ''
    norm = tg_ascii_text(text)
    if not any(x in norm for x in ['kahvalti', 'kahvalt?', 'kahvalt', 'ogle', '??le', '?gle', 'aksam', 'ak?am']):
        return []
    today = date.today().isoformat()
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
    today = date.today().isoformat()
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
        'baÄlantÄ± sorunu', 'baglanti sorunu', 'tekrar dener misin',
        'detaylarÄ±nÄ± biraz daha aÃ§', 'detaylarini biraz daha ac',
        'tam hesaplayabilmem', 'eksik', 'claude hatasÄ±', 'openai hatasÄ±'
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
    for a, b in [('Ä±','i'),('Ä°','i'),('Ä','g'),('Ã¼','u'),('Å','s'),('Ã¶','o'),('Ã§','c')]:
        text = text.replace(a, b)
    return text

def tg_should_save_template(raw_text):
    norm = tg_template_norm(raw_text)
    return any(w in norm for w in [
        'sablon', 'sabit', 'fiks', 'fix', 'favori', 'hep kullan',
        'ogunlere kaydet', 'ogun olarak kaydet', 'yemeklere kaydet', 'yemek olarak kaydet',
        'supplementlere kaydet', 'supplement olarak kaydet', 'suplementlere kaydet', 'suplemente kaydet',
        'takviyelere kaydet', 'takviye olarak kaydet'
    ])

def tg_template_target_kind(raw_text):
    norm = tg_template_norm(raw_text)
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
        r'ad[Ä±i]\s+(.+?)\s+olsun',
        r'ismi\s+(.+?)\s+olsun',
        r'isimi\s+(.+?)\s+olsun',
        r'bunun\s+ad[Ä±i]\s+(.+?)\s+olsun',
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
        return 'kahvaltÄ±'
    if 'ogle' in norm:
        return 'Ã¶Äle'
    if 'aksam' in norm:
        return 'akÅam'
    if 'pre' in norm:
        return 'pre-antrenman'
    if 'post' in norm:
        return 'post-antrenman'
    return slot or 'extra'

def tg_supp_category_from_text(raw_text):
    norm = tg_template_norm(raw_text)
    if any(w in norm for w in ['uyku', 'melatonin', 'glycine', 'glisin', 'magnesium', 'magnezyum']):
        return 'uyku Ã¶ncesi'
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

async def cmd_chat_ai(u, c):
    raw = (u.message.text or '').strip()
    chat_id = getattr(u.effective_chat, 'id', '') if u else ''
    username = ''
    if getattr(u, 'effective_user', None):
        username = u.effective_user.username or u.effective_user.first_name or ''

    tg_store_message('in', raw, chat_id, username)
    water_correction = tg_try_water_correction(raw) if 'tg_try_water_correction' in globals() else None
    if water_correction:
        reply = water_correction.get('reply') or 'Su kaydÄ± dÃ¼zeltildi.'
        tg_store_message('out', reply, chat_id, 'AI Coach', water_correction)
        await u.message.reply_text(reply)
        return

    result = ai_coach_call(raw)
    actions = result.get('actions') or []
    basic_actions = tg_basic_actions_from_text(raw) if 'tg_basic_actions_from_text' in globals() else []
    full_day_actions = tg_full_day_actions_from_text(raw) if 'tg_full_day_actions_from_text' in globals() else []
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
    saved = ai_apply_actions(actions)
    if (not result.get('actions')) and basic_actions and 'BaÃÅ¸lantÃÂ± sorunu' in (result.get('reply') or ''):
        result['reply'] = 'AI baÃÅ¸lantÃÂ±sÃÂ± anlÃÂ±k takÃÂ±ldÃÂ± ama temel verileri boÃÅ¸a dÃÂ¼ÃÅ¸ÃÂ¼rmedim. Kilo/su/adÃÂ±m ve net makro gÃÂ¶rdÃÂ¼ÃÅ¸ÃÂ¼m kayÃÂ±tlarÃÂ± sisteme iÃÅ¸ledim; detaylÃÂ± koÃÂ§ yorumunu tekrar sorabilirsin.'
    template_title = ''
    try:
        template_title = tg_save_meal_template_from_actions(raw, actions) if 'tg_save_meal_template_from_actions' in globals() else ''
        if template_title:
            saved.append('Åablon')
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
            m = re.search(r'(\d{2,3}(?:[\.,]\d)?)\s*(?:kg|kilo)?', norm)
            if m:
                ensure_body_metrics_table()
                kg = float(m.group(1).replace(',', '.'))
                conn = get_db()
                today = date.today().isoformat()
                conn.execute("""
                    INSERT INTO body_metrics (date, weight_kg, notes)
                    VALUES (?,?,?)
                    ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, notes=excluded.notes
                """, (today, kg, 'telegram'))
                conn.commit(); conn.close()
                saved.append('kilo')
        except Exception:
            log.exception("Telegram kilo fallback kaydi basarisiz")

    if 'adÄ±m' not in saved and 'adim' not in saved and any(w in norm for w in ['adim','step','steps']):
        try:
            import re
            nums = [int(x) for x in re.findall(r'\b\d{3,6}\b', norm)]
            if nums:
                ensure_step_logs_table()
                conn = get_db()
                today = date.today().isoformat()
                conn.execute("INSERT OR REPLACE INTO step_logs (date, steps, notes) VALUES (?,?,?)", (today, nums[-1], 'telegram'))
                conn.commit(); conn.close()
                saved.append('adÄ±m')
        except Exception:
            log.exception("Telegram adim fallback kaydi basarisiz")

    reply = result.get('reply') or 'AnladÄ±m.'
    if template_title:
        reply += f"\n\nSablon hazir: {template_title}. Sablonlar sayfasinda dogru kategori altinda kullanabilirsin."
    if saved:
        reply += "\n\nKaydedildi: " + ", ".join(saved)
    tg_store_message('out', reply, chat_id, 'AI Coach', actions)
    await u.message.reply_text(reply)

def start_telegram_bot():
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN ayarli degil."); return
    try:
        from telegram.ext import Application, CommandHandler, MessageHandler, filters
    except ImportError:
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
    log.info("Telegram bot baslatildi...")
    app2.run_polling(drop_pending_updates=True, stop_signals=None)




# TELEGRAM_STANDALONE_ALWAYS_ON_V1
TELEGRAM_LOCK_PATH = os.path.join(BASE_DIR, 'telegram_bot.lock')
_TELEGRAM_LOCK_HANDLE = None

def acquire_telegram_bot_lock():
    """Avoid two Telegram pollers fighting over the same bot token."""
    global _TELEGRAM_LOCK_HANDLE
    try:
        import msvcrt
        _TELEGRAM_LOCK_HANDLE = open(TELEGRAM_LOCK_PATH, 'a+b')
        _TELEGRAM_LOCK_HANDLE.seek(0)
        msvcrt.locking(_TELEGRAM_LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
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
        import msvcrt
        _TELEGRAM_LOCK_HANDLE.seek(0)
        msvcrt.locking(_TELEGRAM_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        _TELEGRAM_LOCK_HANDLE.close()
    except Exception:
        pass
    _TELEGRAM_LOCK_HANDLE = None
