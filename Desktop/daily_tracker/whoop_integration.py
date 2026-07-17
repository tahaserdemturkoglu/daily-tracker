# -*- coding: utf-8 -*-
"""
WHOOP v2 API entegrasyonu — daily-tracker
==========================================
Flask blueprint. Mevcut app'e eklemek için:

    from whoop_integration import whoop_bp, init_whoop_tables
    app.register_blueprint(whoop_bp)
    init_whoop_tables()  # DB init'in çağrıldığı yerde

Railway env değişkenleri (zorunlu):
    WHOOP_CLIENT_ID
    WHOOP_CLIENT_SECRET
    WHOOP_REDIRECT_URI = https://web-production-87c2c.up.railway.app/whoop/callback

Notlar:
- WHOOP v2 rotating refresh token kullanır: her refresh'te YENİ refresh token
  döner ve eskisi geçersiz olur. Bu modül her seferinde DB'ye yenisini yazar.
- WHOOP adım saymaz. Otomatik dolan alanlar: Recovery %, Strain, HRV, RHR,
  uyku süresi/performansı, yakılan kalori.
- Tek kullanıcılı sistem varsayımı (mevcut tracker gibi).
"""

import os
import time
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, request, redirect, jsonify

# ---------------------------------------------------------------- config ---
WHOOP_CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID", "").strip()
WHOOP_CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET", "").strip()
WHOOP_REDIRECT_URI = os.environ.get(
    "WHOOP_REDIRECT_URI",
    "https://web-production-87c2c.up.railway.app/whoop/callback",
).strip()

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer/v2"
SCOPES = "offline read:recovery read:sleep read:cycles read:workout read:body_measurement read:profile"

# Türkiye saati (DST yok)
TR_TZ = timezone(timedelta(hours=3))

# DB yolu — mevcut app hangi dosyayı kullanıyorsa onu ver
DB_PATH = os.environ.get("DATABASE_PATH", "tracker.db")

whoop_bp = Blueprint("whoop", __name__)


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_whoop_tables():
    conn = _db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS whoop_tokens (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT,
            refresh_token TEXT,
            expires_at REAL,           -- epoch seconds
            connected_at TEXT
        );
        CREATE TABLE IF NOT EXISTS whoop_daily (
            date TEXT PRIMARY KEY,      -- YYYY-MM-DD (TR lokal)
            recovery_score INTEGER,     -- %
            strain REAL,                -- 0-21
            hrv_ms REAL,
            rhr_bpm REAL,
            spo2 REAL,
            skin_temp_c REAL,
            sleep_hours REAL,
            sleep_performance INTEGER,  -- %
            sleep_efficiency REAL,      -- %
            kcal_burned REAL,
            raw_json TEXT,
            synced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS whoop_workouts (
            id TEXT PRIMARY KEY,        -- WHOOP'un kendi workout id'si
            date TEXT,                  -- YYYY-MM-DD (TR lokal, start'a gore)
            sport_name TEXT,
            start TEXT,                 -- ISO UTC
            end TEXT,                   -- ISO UTC
            duration_min REAL,
            strain REAL,
            kcal_burned REAL,
            avg_hr INTEGER,
            max_hr INTEGER,
            raw_json TEXT,
            synced_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_whoop_workouts_date ON whoop_workouts(date);
        """
    )
    # Eski DB'lerde eksik olabilecek kolonlari sonradan ekle (uyku evreleri + solunum hizi)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(whoop_daily)").fetchall()}
    new_cols = {
        'respiratory_rate': 'REAL',
        'sleep_light_ms': 'INTEGER',
        'sleep_deep_ms': 'INTEGER',
        'sleep_rem_ms': 'INTEGER',
        'sleep_awake_ms': 'INTEGER',
    }
    for col, typ in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE whoop_daily ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- tokens ---
def _save_tokens(access_token, refresh_token, expires_in):
    conn = _db()
    conn.execute(
        """INSERT INTO whoop_tokens (id, access_token, refresh_token, expires_at, connected_at)
           VALUES (1, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             access_token=excluded.access_token,
             refresh_token=excluded.refresh_token,
             expires_at=excluded.expires_at""",
        (access_token, refresh_token, time.time() + expires_in - 60,
         datetime.now(TR_TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def _token_request(data):
    """Token isteği — WHOOP client_secret_post bekler (body auth); olmazsa Basic dener."""
    body = dict(data)
    body["client_id"] = WHOOP_CLIENT_ID
    body["client_secret"] = WHOOP_CLIENT_SECRET
    r = requests.post(TOKEN_URL, data=body, timeout=30)
    if r.status_code == 401:
        body2 = {k: v for k, v in data.items() if k not in ("client_id", "client_secret")}
        r = requests.post(TOKEN_URL, data=body2,
                          auth=(WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET), timeout=30)
    return r


def _get_access_token():
    """Geçerli access token döner; süresi dolduysa refresh eder (rotating)."""
    conn = _db()
    row = conn.execute("SELECT * FROM whoop_tokens WHERE id = 1").fetchone()
    conn.close()
    if not row or not row["refresh_token"]:
        return None
    if row["expires_at"] and time.time() < row["expires_at"]:
        return row["access_token"]
    # refresh — WHOOP her seferinde yeni refresh token döndürür
    r = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": row["refresh_token"],
        "scope": "offline",
    })
    if r.status_code != 200:
        return None
    tok = r.json()
    _save_tokens(tok["access_token"], tok["refresh_token"], tok.get("expires_in", 3600))
    return tok["access_token"]


def _api_get(path, params=None):
    token = _get_access_token()
    if not token:
        return None
    r = requests.get(f"{API_BASE}{path}",
                     headers={"Authorization": f"Bearer {token}"},
                     params=params or {}, timeout=30)
    if r.status_code != 200:
        return None
    return r.json()


def _paged(path, params):
    """Koleksiyon endpoint'lerini next_token ile tüketir."""
    out, token = [], None
    for _ in range(10):  # güvenlik limiti
        p = dict(params)
        if token:
            p["nextToken"] = token
        data = _api_get(path, p)
        if not data:
            break
        out.extend(data.get("records", []))
        token = data.get("next_token")
        if not token:
            break
    return out


# ---------------------------------------------------------------- routes ---
@whoop_bp.route("/whoop/connect")
def whoop_connect():
    """Kullanıcıyı WHOOP OAuth ekranına yönlendirir."""
    url = (f"{AUTH_URL}?response_type=code"
           f"&client_id={WHOOP_CLIENT_ID}"
           f"&redirect_uri={WHOOP_REDIRECT_URI}"
           f"&scope={SCOPES.replace(' ', '%20')}"
           f"&state=daily-tracker")
    return redirect(url)


@whoop_bp.route("/whoop/callback")
def whoop_callback():
    code = request.args.get("code")
    if not code:
        return "WHOOP yetkilendirme reddedildi.", 400
    r = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": WHOOP_REDIRECT_URI,
    })
    if r.status_code != 200:
        return f"Token alınamadı: {r.text}", 400
    tok = r.json()
    _save_tokens(tok["access_token"], tok["refresh_token"], tok.get("expires_in", 3600))
    sync_whoop_data(days=7)
    return redirect("/?whoop=connected")


@whoop_bp.route("/whoop/status")
def whoop_status():
    conn = _db()
    row = conn.execute("SELECT connected_at FROM whoop_tokens WHERE id = 1").fetchone()
    last = conn.execute(
        "SELECT date, synced_at FROM whoop_daily ORDER BY date DESC LIMIT 1").fetchone()
    conn.close()
    return jsonify({
        "connected": bool(row),
        "connected_at": row["connected_at"] if row else None,
        "last_sync_date": last["date"] if last else None,
        "last_synced_at": last["synced_at"] if last else None,
    })


@whoop_bp.route("/whoop/sync", methods=["POST", "GET"])
def whoop_sync_route():
    days = int(request.args.get("days", 3))
    result = sync_whoop_data(days=days)
    if result is None:
        return jsonify({"ok": False, "error": "WHOOP bağlı değil veya token yenilenemedi"}), 401
    return jsonify({"ok": True, "synced_dates": result})


@whoop_bp.route("/whoop/daily/<date>")
def whoop_daily(date):
    """Vücut sekmesi bu endpoint'ten okur. date = YYYY-MM-DD"""
    conn = _db()
    row = conn.execute("SELECT * FROM whoop_daily WHERE date = ?", (date,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"found": False})
    d = dict(row)
    d.pop("raw_json", None)
    d["found"] = True
    return jsonify(d)


def get_workouts_for_date(date):
    """date = YYYY-MM-DD. app.py'nin AI context helper'ları da bunu dogrudan cagirir."""
    conn = _db()
    rows = conn.execute(
        "SELECT id, sport_name, start, end, duration_min, strain, kcal_burned, avg_hr, max_hr "
        "FROM whoop_workouts WHERE date = ? ORDER BY start", (date,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@whoop_bp.route("/whoop/workouts/<date>")
def whoop_workouts_route(date):
    """Antrenman sayfasi bu endpoint'ten o gunun WHOOP-algiladigi antrenman(lar)ini okur."""
    return jsonify({"date": date, "workouts": get_workouts_for_date(date)})


_WHOOP_SUMMARY_METRICS = [
    "recovery_score", "strain", "hrv_ms", "rhr_bpm", "sleep_hours",
    "sleep_performance", "kcal_burned", "spo2", "respiratory_rate",
]


@whoop_bp.route("/whoop/summary")
def whoop_summary():
    """Özet sayfasi haftalik/aylik WHOOP ortalamalari icin: days=N gunun ortalamasi +
    onceki esit uzunluktaki periyotla delta. Veri seyrek olabilir (henuz az gun senkron
    edildi) - null-safe, hic veri yoksa avg alanlari null doner, widget cokmez."""
    days = int(request.args.get("days", 7))
    end_str = request.args.get("end")
    end_date = datetime.strptime(end_str, "%Y-%m-%d").date() if end_str else datetime.now(TR_TZ).date()
    start_date = end_date - timedelta(days=days - 1)
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    def _avg(s, e):
        conn = _db()
        cols = ", ".join(f"AVG({m}) {m}" for m in _WHOOP_SUMMARY_METRICS)
        row = conn.execute(
            f"SELECT COUNT(*) c, {cols} FROM whoop_daily WHERE date >= ? AND date <= ?",
            (s.isoformat(), e.isoformat()),
        ).fetchone()
        conn.close()
        d = dict(row)
        count = d.pop("c")
        return count, {k: (round(v, 1) if v is not None else None) for k, v in d.items()}

    count, avg = _avg(start_date, end_date)
    prev_count, prev_avg = _avg(prev_start, prev_end)

    delta = {}
    for m in _WHOOP_SUMMARY_METRICS:
        a, b = avg.get(m), prev_avg.get(m)
        delta[m] = round(a - b, 1) if (a is not None and b is not None) else None

    return jsonify({
        "days": days, "start": start_date.isoformat(), "end": end_date.isoformat(),
        "count": count, "avg": avg,
        "prev_count": prev_count, "prev_avg": prev_avg, "delta": delta,
    })


# ------------------------------------------------------------------ sync ---
def sync_whoop_data(days=3):
    """Son N günün recovery/sleep/cycle verisini çekip whoop_daily'ye yazar.
    Dönen değer: senkronlanan tarih listesi, ya da None (auth hatası)."""
    if _get_access_token() is None:
        return None

    start = (datetime.now(timezone.utc) - timedelta(days=days + 1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    params = {"limit": 25, "start": start}

    recoveries = _paged("/recovery", params)
    sleeps = _paged("/activity/sleep", params)
    cycles = _paged("/cycle", params)
    workouts = _paged("/activity/workout", params)

    daily = {}  # date -> dict

    def bucket(dt_str):
        """UTC timestamp -> TR lokal tarih (uyanılan gün)."""
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.astimezone(TR_TZ).strftime("%Y-%m-%d")

    # sleep: id -> tarih eşlemesi (recovery sleep_id ile bağlanır)
    sleep_by_id = {}
    for s in sleeps:
        if s.get("nap"):
            continue
        end = s.get("end")
        if not end:
            continue
        d = bucket(end)
        sleep_by_id[s["id"]] = d
        sc = s.get("score") or {}
        stage = sc.get("stage_summary") or {}
        light_ms = stage.get("total_light_sleep_time_milli") or 0
        deep_ms = stage.get("total_slow_wave_sleep_time_milli") or 0
        rem_ms = stage.get("total_rem_sleep_time_milli") or 0
        awake_ms = stage.get("total_awake_time_milli") or 0
        total_ms = light_ms + deep_ms + rem_ms
        rec = daily.setdefault(d, {})
        rec["sleep_hours"] = round(total_ms / 3600000, 2) if total_ms else None
        rec["sleep_performance"] = sc.get("sleep_performance_percentage")
        rec["sleep_efficiency"] = sc.get("sleep_efficiency_percentage")
        rec["respiratory_rate"] = sc.get("respiratory_rate")
        rec["sleep_light_ms"] = light_ms or None
        rec["sleep_deep_ms"] = deep_ms or None
        rec["sleep_rem_ms"] = rem_ms or None
        rec["sleep_awake_ms"] = awake_ms or None

    for r in recoveries:
        score = r.get("score") or {}
        d = sleep_by_id.get(r.get("sleep_id"))
        if not d:
            d = bucket(r.get("updated_at") or r.get("created_at"))
        rec = daily.setdefault(d, {})
        rec["recovery_score"] = score.get("recovery_score")
        rec["hrv_ms"] = score.get("hrv_rmssd_milli")
        rec["rhr_bpm"] = score.get("resting_heart_rate")
        rec["spo2"] = score.get("spo2_percentage")
        rec["skin_temp_c"] = score.get("skin_temp_celsius")

    for c in cycles:
        start_ts = c.get("start")
        if not start_ts:
            continue
        d = bucket(start_ts)
        score = c.get("score") or {}
        rec = daily.setdefault(d, {})
        # gün içinde cycle güncellenir; en son değer kazanır
        rec["strain"] = score.get("strain")
        kj = score.get("kilojoule")
        rec["kcal_burned"] = round(kj * 0.239006, 0) if kj else None

    workout_rows = []
    for w in workouts:
        wid = w.get("id")
        start_ts = w.get("start")
        if not wid or not start_ts:
            continue
        end_ts = w.get("end")
        d = bucket(start_ts)
        score = w.get("score") or {}
        dur_min = None
        if end_ts:
            try:
                sdt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                edt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                dur_min = round((edt - sdt).total_seconds() / 60, 1)
            except (ValueError, TypeError):
                dur_min = None
        kj = score.get("kilojoule")
        workout_rows.append((
            str(wid), d, w.get("sport_name") or (str(w.get("sport_id")) if w.get("sport_id") is not None else None),
            start_ts, end_ts, dur_min, score.get("strain"),
            round(kj * 0.239006, 0) if kj else None,
            score.get("average_heart_rate"), score.get("max_heart_rate"),
            json.dumps(w, ensure_ascii=False),
        ))

    now = datetime.now(TR_TZ).isoformat()
    conn = _db()
    for d, rec in daily.items():
        conn.execute(
            """INSERT INTO whoop_daily
               (date, recovery_score, strain, hrv_ms, rhr_bpm, spo2, skin_temp_c,
                sleep_hours, sleep_performance, sleep_efficiency, kcal_burned,
                respiratory_rate, sleep_light_ms, sleep_deep_ms, sleep_rem_ms, sleep_awake_ms,
                raw_json, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 recovery_score=COALESCE(excluded.recovery_score, recovery_score),
                 strain=COALESCE(excluded.strain, strain),
                 hrv_ms=COALESCE(excluded.hrv_ms, hrv_ms),
                 rhr_bpm=COALESCE(excluded.rhr_bpm, rhr_bpm),
                 spo2=COALESCE(excluded.spo2, spo2),
                 skin_temp_c=COALESCE(excluded.skin_temp_c, skin_temp_c),
                 sleep_hours=COALESCE(excluded.sleep_hours, sleep_hours),
                 sleep_performance=COALESCE(excluded.sleep_performance, sleep_performance),
                 sleep_efficiency=COALESCE(excluded.sleep_efficiency, sleep_efficiency),
                 kcal_burned=COALESCE(excluded.kcal_burned, kcal_burned),
                 respiratory_rate=COALESCE(excluded.respiratory_rate, respiratory_rate),
                 sleep_light_ms=COALESCE(excluded.sleep_light_ms, sleep_light_ms),
                 sleep_deep_ms=COALESCE(excluded.sleep_deep_ms, sleep_deep_ms),
                 sleep_rem_ms=COALESCE(excluded.sleep_rem_ms, sleep_rem_ms),
                 sleep_awake_ms=COALESCE(excluded.sleep_awake_ms, sleep_awake_ms),
                 synced_at=excluded.synced_at""",
            (d, rec.get("recovery_score"), rec.get("strain"), rec.get("hrv_ms"),
             rec.get("rhr_bpm"), rec.get("spo2"), rec.get("skin_temp_c"),
             rec.get("sleep_hours"), rec.get("sleep_performance"),
             rec.get("sleep_efficiency"), rec.get("kcal_burned"),
             rec.get("respiratory_rate"), rec.get("sleep_light_ms"), rec.get("sleep_deep_ms"),
             rec.get("sleep_rem_ms"), rec.get("sleep_awake_ms"),
             json.dumps(rec, ensure_ascii=False), now),
        )

    for row in workout_rows:
        conn.execute(
            """INSERT INTO whoop_workouts
               (id, date, sport_name, start, end, duration_min, strain, kcal_burned,
                avg_hr, max_hr, raw_json, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 date=excluded.date, sport_name=excluded.sport_name, start=excluded.start,
                 end=excluded.end, duration_min=excluded.duration_min, strain=excluded.strain,
                 kcal_burned=excluded.kcal_burned, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,
                 raw_json=excluded.raw_json, synced_at=excluded.synced_at""",
            row + (now,),
        )

    conn.commit()
    conn.close()
    return sorted(set(daily.keys()) | {r[1] for r in workout_rows})
