from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlite3
import requests
import time
from datetime import datetime
import random
from flask_socketio import SocketIO
import socket
import hashlib
import json
from collections import deque, defaultdict

app = Flask(__name__)
CORS(app)

# Initialize SocketIO for real-time pushes
socketio = SocketIO(app, cors_allowed_origins="*")

AI_SERVER_URL = "http://localhost:5000/predict"

# ---------- Replay / burst detection globals (minimal) ----------
# per-device recent history: deque of (payload_hash, timestamp)
_recent_payloads = defaultdict(lambda: deque(maxlen=1000))

# tuning params (tweak to match your environment)
REPLAY_WINDOW_SEC = 5.0        # window to consider repeats
REPLAY_REPEAT_THRESHOLD = 8    # same payload count within window => replay
BURST_RATE_THRESHOLD = 20      # messages per second within window => burst/DoS

# small helper to quantize values for hashing (makes heuristic robust to tiny noise)
def _hash_payload_for_replay(p):
    # Quantize temperature to 0.1 and humidity to integer percent for stable hashing
    key_obj = {
        "temperature": None if p.get("temperature") is None else round(float(p.get("temperature")), 1),
        "humidity": None if p.get("humidity") is None else int(round(float(p.get("humidity")))),
        "motion": int(p.get("motion", 0))
    }
    return hashlib.sha256(json.dumps(key_obj, sort_keys=True).encode()).hexdigest()

# --- Socket.IO handlers & test endpoint ---
@socketio.on('connect')
def handle_connect():
    try:
        sid = request.sid if hasattr(request, 'sid') else 'unknown'
    except Exception:
        sid = 'unknown'
    print(f"Socket connected: sid={sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print("Socket disconnected")

@app.route('/api/test-emit', methods=['GET'])
def test_emit():
    payload = {
        "device_id": 999,
        "threat": "Test Emit",
        "data": "test-data",
        "last_seen": "Just Now"
    }
    try:
        print(f"[EMIT] about to emit device_update -> device_id={payload.get('device_id')}")
        socketio.emit('device_update', payload)
        print("[EMIT] emit completed")
        return jsonify({"status": "emitted", "payload": payload})
    except Exception as e:
        print("Error emitting test:", e)
        return jsonify({"status": "error", "error": str(e)}), 500

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS devices 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  name TEXT, type TEXT, data TEXT, 
                  threat TEXT, location TEXT, 
                  last_seen TEXT, power BOOLEAN)''')
    
    # Seed initial data if empty
    c.execute('SELECT count(*) FROM devices')
    if c.fetchone()[0] == 0:
        seed_data = [
            ("DHT22 Sensor", "Temperature & Humidity", "24°C, 60%", "No Threat", "Living Room", "Now", 1),
            ("PIR Motion", "Motion Detection", "Motion Detected", "No Threat", "Entrance", "Now", 1)
        ]
        c.executemany("INSERT INTO devices (name, type, data, threat, location, last_seen, power) VALUES (?,?,?,?,?,?,?)", seed_data)
        conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

# --- API Routes ---

@app.route('/api/devices', methods=['GET'])
def get_devices():
    conn = get_db_connection()
    devices = conn.execute('SELECT * FROM devices').fetchall()
    conn.close()
    return jsonify([dict(row) for row in devices])

@app.route('/api/devices', methods=['POST'])
def add_device():
    new_device = request.json
    conn = get_db_connection()
    conn.execute("INSERT INTO devices (name, type, data, threat, location, last_seen, power) VALUES (?,?,?,?,?,?,?)",
                 (new_device['name'], new_device['type'], "N/A", "No Threat", "Unassigned", "Now", 1))
    conn.commit()
    conn.close()
    return jsonify({"message": "Device added"}), 201

@app.route('/api/devices/<int:id>/toggle', methods=['POST'])
def toggle_device(id):
    conn = get_db_connection()
    device = conn.execute('SELECT power FROM devices WHERE id = ?', (id,)).fetchone()
    if device:
        new_state = not device['power']
        conn.execute('UPDATE devices SET power = ? WHERE id = ?', (new_state, id))
        conn.commit()
    conn.close()
    return jsonify({"message": "Toggled"})

@app.route('/api/devices/<int:id>', methods=['DELETE'])
def delete_device(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM devices WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Deleted"})

# --- AI Integration Logic ---
@app.route('/api/emergency-check', methods=['POST'])
def emergency_check():
    conn = get_db_connection()
    devices = conn.execute('SELECT * FROM devices').fetchall()
    
    updated_count = 0
    current_time = datetime.now()

    for dev in devices:
        # Simulate Sensor Data 
        sim_temp = random.uniform(20.0, 35.0)
        sim_humidity = random.uniform(40.0, 90.0)
        sim_motion = 1 if "Motion" in dev['type'] else 0
        
        ai_payload = {
            "features": {
                "device_id": str(dev['id']),
                "timestamp": current_time.strftime("%Y-%m-%dT%H:%M:%S"),
                "temperature": sim_temp,
                "humidity": sim_humidity,
                "motion": sim_motion,
                "motion_detected": sim_motion,
                "hour": current_time.hour,
                "minute": current_time.minute,
                "second": current_time.second
            }
        }

        try:
            response = requests.post(AI_SERVER_URL, json=ai_payload)
            if response.status_code == 200:
                result = response.json()
                predicted_label = str(result.get('label', 'normal'))
                
                if predicted_label.lower() != 'normal' and predicted_label != '0':
                    threat_status = "Threat Detected"
                else:
                    threat_status = "No Threat"
                
                conn.execute('UPDATE devices SET threat = ? WHERE id = ?', (threat_status, dev['id']))
                updated_count += 1

                # Emit update for frontend (real-time)
                try:
                    payload_for_ui = {
                        "device_id": dev['id'],
                        "threat": threat_status,
                        "data": dev['data'],
                        "last_seen": "Just Now"
                    }
                    print(f"[EMIT] emergency_check -> device_id={dev['id']} threat={threat_status}")
                    socketio.emit('device_update', payload_for_ui)
                    print("[EMIT] emergency_check emit completed")
                except Exception as e:
                    print("Socket emit error (emergency_check):", e)

        except Exception as e:
            print(f"AI Server Error for device {dev['id']}: {e}")

    conn.commit()
    conn.close()
    return jsonify({"message": "Check complete", "devices_checked": updated_count})

# To receive external data sent by the AI server 
@app.route('/api/external-data', methods=['POST'])
def receive_external_data():
    """
    Accept sensor JSON from external clients, check DB power state,
    forward to AI server at AI_SERVER_URL, update DB, return status.
    """
    conn = get_db_connection()
    payload = request.get_json(force=True, silent=True)

    # (debug prints removed)
    if not payload:
        conn.close()
        return jsonify({"error": "no json payload"}), 400

    device_id = payload.get('device_id')
    if device_id is None:
        conn.close()
        return jsonify({"error": "missing device_id"}), 400

    device = conn.execute('SELECT * FROM devices WHERE id = ?', (device_id,)).fetchone()
    if not device:
        conn.close()
        return jsonify({"error": "device not found"}), 404

    if not device['power']:
        conn.close()
        return jsonify({"status": "ignored", "message": "Device is turned OFF"}), 200

    # -------------------------
    # QUICK REPLAY / BURST HEURISTIC
    # -------------------------
    try:
        now_ts = time.time()
        payload_hash = _hash_payload_for_replay(payload)

        dq = _recent_payloads[device_id]
        dq.append((payload_hash, now_ts))

        cutoff = now_ts - REPLAY_WINDOW_SEC
        same_count = 0
        total_in_window = 0
        for h, ts in reversed(dq):
            if ts < cutoff:
                break
            total_in_window += 1
            if h == payload_hash:
                same_count += 1

        # replay detection: identical payload repeated many times within window
        if same_count >= REPLAY_REPEAT_THRESHOLD:
            threat_status = "Threat Detected"
            data_str = f"{payload.get('temperature')}°C, {payload.get('humidity')}%"
            conn.execute('UPDATE devices SET threat = ?, data = ?, last_seen = ? WHERE id = ?',
                         (threat_status, data_str, "Just Now", device_id))
            conn.commit()

            try:
                payload_for_ui = {"device_id": device_id, "threat": threat_status, "data": data_str, "last_seen": "Just Now"}
                socketio.emit('device_update', payload_for_ui)
            except Exception as e:
                print("Socket emit error (replay heuristic):", e)

            return jsonify({"status": "processed", "threat": threat_status, "ai": {"label": "replay_detected"}})

        # burst-rate detection (high messages/sec)
        if (total_in_window / max(1.0, REPLAY_WINDOW_SEC)) >= BURST_RATE_THRESHOLD:
            threat_status = "Threat Detected"
            data_str = f"{payload.get('temperature')}°C, {payload.get('humidity')}%"
            conn.execute('UPDATE devices SET threat = ?, data = ?, last_seen = ? WHERE id = ?',
                         (threat_status, data_str, "Just Now", device_id))
            conn.commit()

            try:
                payload_for_ui = {"device_id": device_id, "threat": threat_status, "data": data_str, "last_seen": "Just Now"}
                socketio.emit('device_update', payload_for_ui)
            except Exception as e:
                print("Socket emit error (burst heuristic):", e)

            return jsonify({"status": "processed", "threat": threat_status, "ai": {"label": "burst_detected"}})

    except Exception as e:
        print("Replay heuristic error:", e)
        # continue to AI processing path if heuristic fails

    ts = payload.get('timestamp') or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ai_payload = {
        "features": {
            "device_id": str(device_id),
            "timestamp": ts,
            "temperature": payload.get('temperature'),
            "humidity": payload.get('humidity'),
            "motion": payload.get('motion', 0)
        }
    }

    try:
        ai_resp = requests.post(AI_SERVER_URL, json=ai_payload, timeout=10)
        if ai_resp.status_code == 200:
            ai_result = ai_resp.json()
        else:
            ai_result = {"label": "unknown", "raw_status": ai_resp.status_code}
    except Exception as e:
        ai_result = {"label": "unknown", "error": str(e)}

    # >>> SAFE LABEL LOGIC <<<
    label = str(ai_result.get('label', 'unknown'))
    label_norm = label.strip().lower()

    # Treat explicit normal codes as safe
    if label_norm in ('normal', '0', 'none', '', 'ok'):
        threat_status = "No Threat"
    # If AI couldn't decide or errored, avoid false alarms
    elif label_norm in ('unknown', 'error', 'null'):
        threat_status = "No Threat"
    # Everything else counts as an attack
    else:
        threat_status = "Threat Detected"

    data_str = f"{payload.get('temperature')}°C, {payload.get('humidity')}%"
    conn.execute('UPDATE devices SET threat = ?, data = ?, last_seen = ? WHERE id = ?',
                 (threat_status, data_str, "Just Now", device_id))
    conn.commit()
    conn.close()

    # Emit the update after DB commit so frontend gets the latest info
    try:
        payload_for_ui = {
            "device_id": device_id,
            "threat": threat_status,
            "data": data_str,
            "last_seen": "Just Now"
        }
        print(f"[EMIT] receive_external_data -> device_id={device_id} threat={threat_status} data={data_str}")
        socketio.emit('device_update', payload_for_ui)
        print("[EMIT] receive_external_data emit completed")
    except Exception as e:
        print("Socket emit error (receive_external_data):", e)

    return jsonify({"status": "processed", "threat": threat_status, "ai": ai_result})

# Helper to determine local LAN IP for prettier startup banner
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # route discovery; no data is actually sent
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

# --- THIS IS THE CRITICAL PART ---
if __name__ == '__main__':
    init_db()
    local_ip = get_local_ip()
    print("Main Backend running on Port 8000...")
    print(f" * Local Access:   http://127.0.0.1:8000")
    print(f" * Network Access: http://{local_ip}:8000")
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)
