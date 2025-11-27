# server.py  (cleaned, header-repair enabled)
# Usage:
#   pip install flask pandas scikit-learn joblib python-dateutil
#   python server.py

from flask import Flask, request, jsonify, send_file
from pathlib import Path
import joblib, pandas as pd, numpy as np, time, json, csv, traceback, os
from collections import defaultdict
from datetime import datetime, timedelta
import threading
from queue import Queue, Empty

# optional (fallback) parser
from dateutil import parser as dateparser

APP = Flask(__name__)
BASE = Path(__file__).parent.resolve()

# ---------- Config ----------
MODEL_PATH = BASE / "artifacts_iot_model" / "model.pkl"
SCALER_PATH = BASE / "artifacts_iot_model" / "scaler.pkl"
FEATURE_ORDER_JSON = BASE / "artifacts_iot_model" / "feature_order.json"  # optional
PROCESSED_CSV = BASE / "processed_optionD.csv"   # fallback to derive feature order
PRED_LOG = BASE / "predictions.log"
CSV_LOG = BASE / "incoming_predictions.csv"

# OPTIONAL: header-source CSV (if you want a raw header row above the server header)
EXTRA_HEADER_CSV = BASE / "raw_headers.csv"

def load_extra_headers():
    if not EXTRA_HEADER_CSV.exists():
        return []
    try:
        dfh = pd.read_csv(EXTRA_HEADER_CSV, nrows=0, engine='python')
        return dfh.columns.tolist()
    except Exception:
        try:
            with open(EXTRA_HEADER_CSV, "r", newline="") as _f:
                import csv as _csv
                first = next(_csv.reader(_f), None)
                if first:
                    return first
        except Exception:
            pass
    return []

EXTRA_HEADERS = load_extra_headers()

# ---------- Load artifacts ----------
if not MODEL_PATH.exists() or not SCALER_PATH.exists():
    raise FileNotFoundError("Missing model.pkl or scaler.pkl in artifacts_iot_model/")

model = joblib.load(MODEL_PATH)
scaler = joblib.load(SCALER_PATH)

# ---------- Feature order (load or derive) ----------
def load_feature_order():
    if FEATURE_ORDER_JSON.exists():
        return json.loads(FEATURE_ORDER_JSON.read_text())
    if PROCESSED_CSV.exists():
        _df_sample = pd.read_csv(PROCESSED_CSV, nrows=5, engine='python')
        numeric_cols = _df_sample.select_dtypes(include=[np.number]).columns.tolist()
        if 'attack_type' in numeric_cols:
            numeric_cols.remove('attack_type')
        return numeric_cols
    raise FileNotFoundError("feature_order.json or processed_optionD.csv required to determine feature order.")

feature_order = load_feature_order()

if 'req_count_same_sec' not in feature_order:
    feature_order.append('req_count_same_sec')

FEATURE_ORDER = feature_order

# CSV log header (features + raw timestamp + metadata)
CSV_HEADER = FEATURE_ORDER + ["raw_timestamp", "predicted_label", "predicted_label_idx", "confidence", "pred_time_unix", "pred_time_human"]

# ---------- Robust header ensuring / repair ----------
def looks_numeric(x):
    try:
        float(x)
        return True
    except Exception:
        return False

def ensure_csv_has_headers(csv_path: Path, extra_headers, csv_header):
    try:
        import csv as _csv, tempfile
        if not csv_path.exists():
            with open(csv_path, "w", newline="") as f:
                w = _csv.writer(f)
                if extra_headers:
                    w.writerow(extra_headers)
                w.writerow(csv_header)
            return
        if csv_path.stat().st_size == 0:
            with open(csv_path, "w", newline="") as f:
                w = _csv.writer(f)
                if extra_headers:
                    w.writerow(extra_headers)
                w.writerow(csv_header)
            return
        with open(csv_path, "r", newline="") as f:
            reader = _csv.reader(f)
            first = next(reader, [])
        if not first:
            tmp = csv_path.with_suffix(".tmp")
            with open(tmp, "w", newline="") as out_f:
                w = _csv.writer(out_f)
                if extra_headers:
                    w.writerow(extra_headers)
                w.writerow(csv_header)
                with open(csv_path, "r", newline="") as in_f:
                    for row in _csv.reader(in_f):
                        w.writerow(row)
            os.replace(str(tmp), str(csv_path))
            return
        if any(looks_numeric(c) for c in first):
            tmp = csv_path.with_suffix(".tmp")
            with open(tmp, "w", newline="") as out_f:
                w = _csv.writer(out_f)
                if extra_headers:
                    w.writerow(extra_headers)
                w.writerow(csv_header)
                with open(csv_path, "r", newline="") as in_f:
                    for row in _csv.reader(in_f):
                        w.writerow(row)
            os.replace(str(tmp), str(csv_path))
            return
    except Exception as e:
        with open(PRED_LOG, "a") as lf:
            lf.write(f"ensure_csv_has_headers error: {e}\n")

ensure_csv_has_headers(CSV_LOG, EXTRA_HEADERS, CSV_HEADER)

# ---------- Thread-safe counters + writer queue ----------
req_counter = defaultdict(int)
req_lock = threading.Lock()
write_queue = Queue()
REQ_COUNTER_CLEANUP_SECONDS = 300

# ---------- Timestamp parsing (fast path for ISO) ----------
def parse_timestamp_to_second(ts_value):
    if ts_value is None:
        return None
    s = str(ts_value).strip()
    if not s:
        return None
    try:
        if len(s) >= 19 and s[4] == '-' and s[7] == '-' and s[10] in ('T', ' '):
            if s.endswith('Z'):
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
                return dt.strftime("%Y-%m-%dT%H:%M:%S")
            else:
                dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
                return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    try:
        dt = dateparser.parse(s)
        if dt is None:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None

def get_req_count_and_increment(device_id, ts_value):
    ts_sec = parse_timestamp_to_second(ts_value)
    if ts_sec is None:
        key = (str(device_id), "__unknown__")
    else:
        key = (str(device_id), ts_sec)
    with req_lock:
        req_counter[key] += 1
        return req_counter[key]

def req_counter_cleanup_loop():
    while True:
        time.sleep(REQ_COUNTER_CLEANUP_SECONDS)
        allowed_prefixes = set()
        for i in range(0, REQ_COUNTER_CLEANUP_SECONDS, max(1, REQ_COUNTER_CLEANUP_SECONDS // 10)):
            t = (datetime.utcnow() - timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S")
            allowed_prefixes.add(t[:19])
        with req_lock:
            keys_to_remove = [k for k in req_counter.keys() if (k[1] != "__unknown__" and k[1] not in allowed_prefixes)]
            for k in keys_to_remove:
                del req_counter[k]

cleanup_thread = threading.Thread(target=req_counter_cleanup_loop, daemon=True)
cleanup_thread.start()

# ---------- Background CSV writer (robust) ----------
def csv_writer_loop():
    import csv as _csv
    while True:
        batch = []
        try:
            item = write_queue.get(timeout=1.0)
            batch.append(item)
            while True:
                try:
                    item = write_queue.get_nowait()
                    batch.append(item)
                except Empty:
                    break
        except Empty:
            continue

        try:
            ensure_csv_has_headers(CSV_LOG, EXTRA_HEADERS, CSV_HEADER)

            with open(CSV_LOG, "a", newline="") as f:
                writer = _csv.writer(f)
                for feature_log_dict, result in batch:
                    t_unix = time.time()
                    t_human = datetime.fromtimestamp(t_unix).strftime("%Y-%m-%d %H:%M:%S")
                    row = [ feature_log_dict.get(col, 0) for col in FEATURE_ORDER ]
                    raw_ts = feature_log_dict.get('raw_timestamp', None)
                    row += [ raw_ts, result.get("label"), result.get("label_idx"), result.get("confidence"), t_unix, t_human ]
                    writer.writerow(row)
        except Exception as e:
            with open(PRED_LOG, "a") as lf:
                lf.write(f"CSV writer error: {e}\n")

writer_thread = threading.Thread(target=csv_writer_loop, daemon=True)
writer_thread.start()

# ---------- Helpers ----------
def sanitize_input_keys(d: dict):
    out = dict(d)
    if 'req_count' in out and 'req_count_same_sec' not in out:
        out['req_count_same_sec'] = out.get('req_count')
    return out

def build_feature_vector_from_input(input_dict):
    input_dict = sanitize_input_keys(input_dict or {})
    feat = {}
    for f in FEATURE_ORDER:
        if f == 'req_count_same_sec':
            continue
        feat[f] = input_dict.get(f, None)

    device_id_raw = input_dict.get("device_id", "unknown_device")
    raw_ts = input_dict.get("timestamp", None)

    rc_val = None
    if 'req_count_same_sec' in input_dict and input_dict.get('req_count_same_sec') not in (None, ""):
        try:
            rc_val = int(float(input_dict.get('req_count_same_sec')))
        except Exception:
            rc_val = None

    if rc_val is None:
        rc_val = get_req_count_and_increment(device_id_raw, raw_ts)

    feat['req_count_same_sec'] = rc_val

    vec = []
    for f in FEATURE_ORDER:
        v = feat.get(f, None)
        if v is None:
            v = input_dict.get(f, 0)
        try:
            vec.append(float(v))
        except Exception:
            vec.append(0.0)

    log_dict = { k: (input_dict.get(k) if input_dict.get(k) is not None else feat.get(k, 0)) for k in FEATURE_ORDER }
    log_dict['raw_timestamp'] = raw_ts
    return np.array(vec, dtype=float), log_dict

def predict_from_vector(vec):
    X = vec.reshape(1, -1)
    try:
        Xs = scaler.transform(X)
    except Exception as e:
        raise RuntimeError(f"Scaler transform failed: {e}")

    probs = None
    if hasattr(model, "predict_proba"):
        try:
            probs = model.predict_proba(Xs)[0]
        except Exception:
            probs = None

    raw_pred = model.predict(Xs)[0]
    label_idx = None
    try:
        label_idx = int(raw_pred)
    except Exception:
        label_idx = None

    # ---------- Threshold logic ----------
    attack_prob = None
    if probs is not None and len(probs) >= 2:
        attack_prob = float(probs[1])
        normal_prob = float(probs[0])
    else:
        attack_prob = 0.0
        normal_prob = 1.0

    if attack_prob >= 0.987:
        final_label = "1"
        final_label_idx = 1
    else:
        final_label = "0"
        final_label_idx = 0

    confidence = max(normal_prob, attack_prob)
    probs_list = probs.tolist() if probs is not None else None

    return {
        "label": final_label,
        "label_idx": final_label_idx,
        "probs": probs_list,
        "confidence": confidence
    }

def append_row_to_csv_and_log(feature_log_dict, result):
    try:
        write_queue.put_nowait((feature_log_dict, result))
    except Exception:
        with open(PRED_LOG, "a") as f:
            f.write(json.dumps({"time": time.time(), "input": feature_log_dict, "result": result}) + "\n")

    try:
        with open(PRED_LOG, "a") as f:
            f.write(json.dumps({"time": time.time(), "input": feature_log_dict, "result": result}) + "\n")
    except:
        pass

# ---------- Routes ----------
@APP.route("/health", methods=["GET"])
def health():
    info = {"status": "ok", "model": MODEL_PATH.name, "features": FEATURE_ORDER}
    try:
        if hasattr(model, "n_features_in_"):
            info["model_n_features_in"] = int(model.n_features_in_)
    except Exception:
        pass
    return jsonify(info)

@APP.route("/predict", methods=["POST"])
def predict_single():
    try:
        payload = request.get_json(force=True)
        if isinstance(payload, dict) and "features" in payload:
            input_data = payload["features"]
        else:
            input_data = payload if isinstance(payload, dict) else {}
        vec, logdict = build_feature_vector_from_input(input_data)

        res = predict_from_vector(vec)
        res["uncertain"] = (res["confidence"] is not None and res["confidence"] < 0.4)
        append_row_to_csv_and_log(logdict, res)
        return jsonify(res)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 400

@APP.route("/batch_predict", methods=["POST"])
def predict_batch():
    try:
        outputs = []
        if request.files and "file" in request.files:
            f = request.files["file"]
            df_in = pd.read_csv(f, engine='python')
            for _, r in df_in.iterrows():
                input_data = r.to_dict()
                input_data = sanitize_input_keys(input_data)
                vec, logdict = build_feature_vector_from_input(input_data)
                res = predict_from_vector(vec)
                res["uncertain"] = (res["confidence"] is not None and res["confidence"] < 0.4)
                append_row_to_csv_and_log(logdict, res)
                outputs.append({"input": logdict, "result": res})
            return jsonify({"count": len(outputs), "predictions": outputs})
        payload = request.get_json(force=True)
        if isinstance(payload, list):
            for item in payload:
                input_data = item.get("features", item) if isinstance(item, dict) else {}
                input_data = sanitize_input_keys(input_data)
                vec, logdict = build_feature_vector_from_input(input_data)
                res = predict_from_vector(vec)
                res["uncertain"] = (res["confidence"] is not None and res["confidence"] < 0.4)
                append_row_to_csv_and_log(logdict, res)
                outputs.append({"input": logdict, "result": res})
            return jsonify({"count": len(outputs), "predictions": outputs})
        return jsonify({"error": "Unsupported payload for batch_predict. Send multipart CSV (file) or JSON list."}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 400

@APP.route("/download_logs", methods=["GET"])
def download_logs():
    if CSV_LOG.exists():
        return send_file(str(CSV_LOG), as_attachment=True)
    return jsonify({"error": "CSV log not found"}), 404

# ---------- Run ----------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    APP.run(host="0.0.0.0", port=5000)
