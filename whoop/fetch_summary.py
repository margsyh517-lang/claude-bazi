#!/usr/bin/env python3
"""
Fetch a daily WHOOP summary.

Standard library only (no pip installs available in the automation
environment). Reads WHOOP credentials from a local JSON file, refreshes
the OAuth token, pulls the latest cycle/recovery/sleep/workout data from
the WHOOP v2 API, upserts it into the user's own workout-tracker app's
Supabase table (so the app can render it), and prints one JSON object to
stdout:

    {"new_creds": {...}, "summary_doc": {...}, "summary_text": "...",
     "app_sync_error": "..."}  # app_sync_error present only on failure

The caller (a Claude Code session with access to the Artifact tool) is
responsible for persisting new_creds and summary_doc to the artifact
database and for delivering summary_text to the user.

Usage:
    python3 fetch_summary.py /path/to/creds.json

creds.json must contain: client_id, client_secret, refresh_token
(access_token / access_token_expires_at are optional and ignored -- this
script always refreshes at the start of each run).
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.prod.whoop.com"
TOKEN_URL = f"{API_BASE}/oauth/oauth2/token"

# WHOOP sits behind Cloudflare, which blocks the default urllib/curl user
# agent as a bot (HTTP 403, error code 1010). A browser-like UA avoids it.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

KJ_TO_KCAL = 0.239006

# The user's existing workout-tracker app (Netlify + Supabase) stores its
# data as {id, data} rows in a single "workout_data" table -- id="default_user"
# holds the workout log, and this script upserts a second row,
# id="whoop_data", holding {"YYYY-MM-DD": summary_doc, ...} so the app's UI
# can render it. This anon key is already public (it ships in the app's
# client-side JS), so shipping it here too adds no new exposure.
SUPA_URL = "https://kcyznlqkmnmkzwwspbdm.supabase.co"
SUPA_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtjeXpubHFrbW5ta3p3d3NwYmRtIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg1MzY1ODEsImV4cCI6MjA5NDExMjU4MX0.9I7W-HPxiO6Y37SmIZQy1m0oFJsO9tk_Sd-5lAX-DvA"
SUPA_WHOOP_ROW_ID = "whoop_data"
SUPA_MAX_DAYS = 90


def http_post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def http_get_json(url, access_token):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def refresh_access_token(creds):
    resp = http_post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    new_creds = dict(creds)
    new_creds["access_token"] = resp["access_token"]
    # WHOOP may or may not rotate the refresh token; always store whatever
    # it returns, falling back to the existing one if omitted.
    new_creds["refresh_token"] = resp.get("refresh_token", creds["refresh_token"])
    new_creds["access_token_expires_in"] = resp.get("expires_in")
    return new_creds


def ms_to_min(ms):
    if ms is None:
        return None
    return round(ms / 60000, 1)


def local_date(iso_ts, tz_offset):
    """Local calendar date for a WHOOP UTC timestamp + its timezone_offset (e.g. '-07:00')."""
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    sign = 1 if tz_offset.startswith("+") else -1
    oh, om = tz_offset.lstrip("+-").split(":")
    from datetime import timedelta

    dt_local = dt + sign * timedelta(hours=int(oh), minutes=int(om))
    return dt_local.date().isoformat()


def supa_get_whoop_history():
    req = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/workout_data?id=eq.{SUPA_WHOOP_ROW_ID}&select=data",
        headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        rows = json.loads(resp.read().decode())
    return rows[0]["data"] if rows and rows[0].get("data") else {}


def supa_save_whoop_history(history_by_date):
    body = json.dumps(
        {"id": SUPA_WHOOP_ROW_ID, "data": history_by_date, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).encode()
    req = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/workout_data",
        data=body,
        headers={
            "apikey": SUPA_KEY,
            "Authorization": f"Bearer {SUPA_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal,resolution=merge-duplicates",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


def sync_to_app(summary_doc):
    """Merge today's summary into the app's Supabase history row and upsert it."""
    history = supa_get_whoop_history()
    history[summary_doc["date"]] = summary_doc
    keep_dates = sorted(history.keys(), reverse=True)[:SUPA_MAX_DAYS]
    trimmed = {d: history[d] for d in keep_dates}
    supa_save_whoop_history(trimmed)


def fetch_whoop_data(access_token):
    cycles = http_get_json(f"{API_BASE}/developer/v2/cycle?limit=2", access_token)["records"]
    recovery = http_get_json(f"{API_BASE}/developer/v2/recovery?limit=1", access_token)["records"]
    sleep = http_get_json(f"{API_BASE}/developer/v2/activity/sleep?limit=1", access_token)["records"]
    workouts = http_get_json(f"{API_BASE}/developer/v2/activity/workout?limit=10", access_token)["records"]
    return cycles, recovery, sleep, workouts


def build_summary(cycles, recovery, sleep, workouts):
    # cycles[0] is the current (today, still-open) cycle; cycles[1] is the
    # most recently completed one -- WHOOP computes a "day" from wake to
    # wake, not the calendar day, so the completed cycle usually spans
    # yesterday morning through this morning.
    current_cycle = cycles[0] if len(cycles) > 0 else None
    completed_cycle = cycles[1] if len(cycles) > 1 else None
    rec = recovery[0] if recovery else None
    sl = sleep[0] if sleep else None

    if current_cycle:
        today_date = local_date(current_cycle["start"], current_cycle.get("timezone_offset", "+00:00"))
    else:
        today_date = datetime.now(timezone.utc).date().isoformat()

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": today_date,
        "calibrating": bool(rec and rec.get("score", {}).get("user_calibrating")),
        "recovery_score": None,
        "hrv_ms": None,
        "rhr_bpm": None,
        "spo2_pct": None,
        "skin_temp_c": None,
        "recovery_state": rec["score_state"] if rec else None,
        "sleep_duration_min": None,
        "sleep_need_min": None,
        "sleep_efficiency_pct": None,
        "sleep_performance_pct": None,
        "stages": {"light_min": None, "rem_min": None, "sws_min": None, "awake_min": None},
        "strain": None,
        "strain_avg_hr": None,
        "strain_max_hr": None,
        "calories_kcal": None,
        "workouts": [],
    }

    if rec and rec.get("score_state") == "SCORED":
        s = rec["score"]
        doc["recovery_score"] = s.get("recovery_score")
        doc["hrv_ms"] = round(s["hrv_rmssd_milli"], 1) if s.get("hrv_rmssd_milli") is not None else None
        doc["rhr_bpm"] = s.get("resting_heart_rate")
        doc["spo2_pct"] = s.get("spo2_percentage")
        doc["skin_temp_c"] = s.get("skin_temp_celsius")

    if sl and sl.get("score_state") == "SCORED":
        s = sl["score"]
        stage = s["stage_summary"]
        awake_ms = stage.get("total_awake_time_milli", 0)
        nodata_ms = stage.get("total_no_data_time_milli", 0)
        in_bed_ms = stage.get("total_in_bed_time_milli", 0)
        asleep_ms = in_bed_ms - awake_ms - nodata_ms
        doc["sleep_duration_min"] = ms_to_min(asleep_ms)
        need = s.get("sleep_needed", {})
        need_total_ms = sum(
            need.get(k, 0) or 0
            for k in (
                "baseline_milli",
                "need_from_sleep_debt_milli",
                "need_from_recent_strain_milli",
                "need_from_recent_nap_milli",
            )
        )
        doc["sleep_need_min"] = ms_to_min(need_total_ms)
        doc["sleep_efficiency_pct"] = s.get("sleep_efficiency_percentage")
        doc["sleep_performance_pct"] = s.get("sleep_performance_percentage")
        doc["stages"] = {
            "light_min": ms_to_min(stage.get("total_light_sleep_time_milli")),
            "rem_min": ms_to_min(stage.get("total_rem_sleep_time_milli")),
            "sws_min": ms_to_min(stage.get("total_slow_wave_sleep_time_milli")),
            "awake_min": ms_to_min(awake_ms),
        }

    if completed_cycle and completed_cycle.get("score_state") == "SCORED":
        s = completed_cycle["score"]
        doc["strain"] = round(s["strain"], 1) if s.get("strain") is not None else None
        doc["strain_avg_hr"] = s.get("average_heart_rate")
        doc["strain_max_hr"] = s.get("max_heart_rate")
        doc["calories_kcal"] = round(s["kilojoule"] * KJ_TO_KCAL) if s.get("kilojoule") is not None else None

        window_start = completed_cycle.get("start")
        window_end = completed_cycle.get("end")
        for w in workouts:
            if window_start and w.get("start", "") < window_start:
                continue
            if window_end and w.get("start", "") >= window_end:
                continue
            if w.get("score_state") != "SCORED":
                continue
            ws = w["score"]
            start_dt = datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
            doc["workouts"].append(
                {
                    "sport_name": w.get("sport_name"),
                    "strain": round(ws["strain"], 1) if ws.get("strain") is not None else None,
                    "avg_hr": ws.get("average_heart_rate"),
                    "max_hr": ws.get("max_heart_rate"),
                    "calories_kcal": round(ws["kilojoule"] * KJ_TO_KCAL) if ws.get("kilojoule") is not None else None,
                    "duration_min": round((end_dt - start_dt).total_seconds() / 60, 1),
                    "distance_meter": ws.get("distance_meter"),
                }
            )

    return doc


def format_summary_text(doc):
    lines = []
    lines.append(f"WHOOP 每日概览 · {doc['date']}")
    lines.append("")

    if doc["recovery_score"] is not None:
        lines.append(f"恢复 Recovery：{doc['recovery_score']:.0f}%")
        details = []
        if doc["hrv_ms"] is not None:
            details.append(f"HRV {doc['hrv_ms']:.0f}ms")
        if doc["rhr_bpm"] is not None:
            details.append(f"静息心率 {doc['rhr_bpm']:.0f}bpm")
        if doc["spo2_pct"] is not None:
            details.append(f"血氧 {doc['spo2_pct']:.0f}%")
        if details:
            lines.append("　" + "，".join(details))
        if doc["calibrating"]:
            lines.append("　（WHOOP 仍在校准中，恢复分可能还不够准确）")
    else:
        lines.append("恢复 Recovery：今日数据尚未生成")
    lines.append("")

    if doc["sleep_duration_min"] is not None:
        h = int(doc["sleep_duration_min"] // 60)
        m = int(doc["sleep_duration_min"] % 60)
        lines.append(f"睡眠 Sleep：{h}小时{m}分钟" + (f"（表现 {doc['sleep_performance_pct']:.0f}%，效率 {doc['sleep_efficiency_pct']:.0f}%）" if doc.get("sleep_performance_pct") is not None else ""))
        st = doc["stages"]
        stage_bits = []
        if st.get("sws_min") is not None:
            stage_bits.append(f"深睡 {int(st['sws_min'])}分")
        if st.get("rem_min") is not None:
            stage_bits.append(f"REM {int(st['rem_min'])}分")
        if st.get("light_min") is not None:
            stage_bits.append(f"浅睡 {int(st['light_min'])}分")
        if st.get("awake_min") is not None:
            stage_bits.append(f"清醒 {int(st['awake_min'])}分")
        if stage_bits:
            lines.append("　" + "，".join(stage_bits))
    else:
        lines.append("睡眠 Sleep：暂无数据")
    lines.append("")

    if doc["strain"] is not None:
        lines.append(f"压力 Strain：{doc['strain']:.1f}" + (f"（{doc['calories_kcal']} 千卡）" if doc.get("calories_kcal") else ""))
    else:
        lines.append("压力 Strain：暂无数据")
    lines.append("")

    if doc["workouts"]:
        lines.append(f"训练 Workouts（{len(doc['workouts'])}项）：")
        for w in doc["workouts"]:
            bits = [w["sport_name"] or "运动", f"{w['duration_min']:.0f}分钟"]
            if w.get("strain") is not None:
                bits.append(f"strain {w['strain']:.1f}")
            if w.get("avg_hr") is not None:
                bits.append(f"平均心率 {w['avg_hr']:.0f}")
            if w.get("calories_kcal") is not None:
                bits.append(f"{w['calories_kcal']} 千卡")
            lines.append("　· " + "，".join(bits))
    else:
        lines.append("训练 Workouts：无记录")

    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        print("usage: fetch_summary.py <creds.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        creds = json.load(f)

    try:
        new_creds = refresh_access_token(creds)
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": f"token_refresh_failed: {e.code} {e.read().decode()}"}))
        sys.exit(1)

    try:
        cycles, recovery, sleep, workouts = fetch_whoop_data(new_creds["access_token"])
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": f"data_fetch_failed: {e.code} {e.read().decode()}", "new_creds": new_creds}))
        sys.exit(1)

    summary_doc = build_summary(cycles, recovery, sleep, workouts)
    summary_text = format_summary_text(summary_doc)

    app_sync_error = None
    try:
        sync_to_app(summary_doc)
    except Exception as e:
        app_sync_error = str(e)

    result = {"new_creds": new_creds, "summary_doc": summary_doc, "summary_text": summary_text}
    if app_sync_error:
        result["app_sync_error"] = app_sync_error
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
