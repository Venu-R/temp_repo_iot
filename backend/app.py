from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlite3
import requests
import time
from datetime import datetime
import random 

app = Flask(__name__)
CORS(app)

AI_SERVER_URL = "http://localhost:5000/predict"

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

    label = str(ai_result.get('label', 'unknown'))
    threat_status = "Threat Detected" if (label.lower() != 'normal' and label != '0') else "No Threat"

    data_str = f"{payload.get('temperature')}°C, {payload.get('humidity')}%"
    conn.execute('UPDATE devices SET threat = ?, data = ?, last_seen = ? WHERE id = ?',
                 (threat_status, data_str, "Just Now", device_id))
    conn.commit()
    conn.close()

    return jsonify({"status": "processed", "threat": threat_status, "ai": ai_result})

# --- THIS IS THE CRITICAL PART ---
if __name__ == '__main__':
    init_db()
    print("Main Backend running on Port 8000...")
    app.run(host="0.0.0.0", port=8000, debug=True)
