"""Canonical metric names, units and source tags. Every importer, the ingest API, the analysis
pipeline and the MCP server speak these names — nothing else goes into `samples.metric`."""

from __future__ import annotations

# --- raw / vendor metrics -------------------------------------------------------------
HR = "hr"                          # bpm
STRESS_GARMIN = "stress_garmin"    # 0..100 (Garmin/Firstbeat), ~3-min; negative "unmeasurable" codes are dropped
STRESS_NOW = "stress_now"          # 0..100 ActivityMonitor.stressScore (30-s rolling), snapshots from the watch
BODY_BATTERY = "body_battery"      # 0..100
SPO2 = "spo2"                      # %
RESP_RATE = "resp_rate"            # breaths/min (Garmin)
STEPS_PM = "steps_pm"              # steps per minute (importers convert cumulative counters)
ACTIVE_KCAL_PM = "active_kcal_pm"  # kcal per minute
INTENSITY = "intensity"            # Garmin activity intensity class 0..n
TEMP_C = "temp_c"                  # skin/device temperature °C
PRESSURE_PA = "pressure_pa"        # barometric pressure Pa
ALTITUDE_M = "altitude_m"
HRV_GARMIN = "hrv_garmin"          # ms, Garmin overnight HRV readings (RMSSD, 5-min)
HRV_GARMIN_NIGHT = "hrv_garmin_night_avg"  # ms, Garmin nightly average
RESTING_HR = "resting_hr"          # bpm, daily
ENMO = "enmo"                      # milli-g, 1-s epochs from our accelerometer chunks
MAD = "mad"                        # milli-g, 1-s epochs

# --- derived (source = DERIVED) ---------------------------------------------------------
ADDITIONAL_HR = "additional_hr"    # bpm above the movement/circadian expectation
AROUSAL_Z = "arousal_z"            # fused latent arousal (z units)
AROUSAL_SD = "arousal_sd"
RMSSD = "rmssd"                    # ms, from our cleaned RR, 2-min windows stepped 1 min
LN_RMSSD = "ln_rmssd"
SDNN = "sdnn"
DFA_A1 = "dfa_a1"
RESP_RSA = "resp_rsa"              # breaths/min estimated from RR (HF peak)
HR_RR = "hr_rr"                    # bpm from RR windows
RR_QUALITY = "rr_quality"          # 0..1 untouched beats in the window

# --- sources ---------------------------------------------------------------------------
SRC_CIQ_FACE = "ciq:face"          # watch face background service (SensorHistory, 2-5 min data)
SRC_CIQ_CAPTURE = "ciq:capture"    # capture app via makeWebRequest
SRC_BLE = "ble:bridge"             # capture app via the BLE bridge
SRC_FIT = "fit"                    # FIT files from the watch (monitoring, sleep, activities)
SRC_GC = "gc"                      # Garmin Connect API
DERIVED = "derived:v1"

# preference order when the same metric exists from several sources (first wins per minute)
HR_PREFERENCE = (SRC_BLE, SRC_CIQ_CAPTURE, SRC_FIT, SRC_GC, SRC_CIQ_FACE)
