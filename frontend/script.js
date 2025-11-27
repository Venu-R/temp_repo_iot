const API_URL = "http://localhost:8000/api";

// Helper: Render the dashboard summary
function renderSummary(devices) {
  const summary = document.getElementById("status-summary");
  let online = 0, offline = 0, threats = 0;

  devices.forEach(d => {
    if (d.power) online++;
    else offline++;
    if (d.threat === "Threat Detected") threats++;
  });

  summary.innerHTML = `
    <span class="badge bg-success me-2">${online} Online</span>
    <span class="badge bg-secondary me-2">${offline} Offline</span>
    <span class="badge bg-danger">${threats} Threat Detected</span>
  `;
}

// Core: Fetch data from Backend and render
async function fetchAndRender() {
  try {
    const response = await fetch(`${API_URL}/devices`);
    const devices = await response.json();
    
    renderSummary(devices);
    const deviceGrid = document.getElementById("device-grid");
    deviceGrid.innerHTML = "";

    devices.forEach((device) => {
      const card = document.createElement("div");
      card.className = "col-md-4";
      
      // Database stores boolean as 1/0
      const isPowerOn = Boolean(device.power);
      const status = isPowerOn ? "Online" : "Offline";
      const statusClass = isPowerOn ? "bg-success" : "bg-secondary";
      const threatClass = device.threat === "No Threat" ? "no-threat" : "threat";

      card.innerHTML = `
        <div class="device-card">
          <h5>${device.name}</h5>
          <span class="badge ${statusClass} status-badge">${status}</span>
          <p class="mt-2"><strong>Type:</strong> ${device.type}</p>
          <p><strong>Data:</strong> ${device.data}</p>
          <p class="${threatClass}">${device.threat}</p>
          <p><strong>Location:</strong> ${device.location}</p>
          <p><strong>Last Seen:</strong> ${device.last_seen}</p>
          <p class="power-status"><strong>Power:</strong> ${isPowerOn ? "ON" : "OFF"}</p>
          
          <div class="d-flex mt-3">
            <button class="btn btn-outline-primary me-2 control-btn" onclick="togglePower(${device.id})">
              ${isPowerOn ? "Turn Off" : "Turn On"}
            </button>
            <div class="dropdown">
              <button class="btn btn-outline-secondary dropdown-toggle" type="button" data-bs-toggle="dropdown">
                Settings
              </button>
              <ul class="dropdown-menu">
                <li><a class="dropdown-item" href="#" onclick="deleteDevice(${device.id})">Delete Device</a></li>
              </ul>
            </div>
          </div>
        </div>
      `;
      deviceGrid.appendChild(card);
    });

    // After rendering, run the threat scan (ensures immediate popup if a threat is present)
    scanDomForThreat();
  } catch (error) {
    console.error("Failed to fetch devices:", error);
  }
}

// Device Actions
async function togglePower(id) {
  await fetch(`${API_URL}/devices/${id}/toggle`, { method: "POST" });
  fetchAndRender();
}

async function deleteDevice(id) {
  if(confirm("Are you sure you want to delete this device?")) {
    await fetch(`${API_URL}/devices/${id}`, { method: "DELETE" });
    fetchAndRender();
  }
}

// Add Device Handler
document.getElementById("add-device").addEventListener("click", async () => {
  const newDevice = {
    name: "New Sensor",
    type: "Generic Sensor"
  };
  await fetch(`${API_URL}/devices`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(newDevice)
  });
  fetchAndRender();
});

// Emergency Check (Calls AI)
const emergencyBtn = document.querySelector(".btn-outline-danger");
if(emergencyBtn) {
    emergencyBtn.addEventListener("click", async () => {
      const originalText = emergencyBtn.innerText;
      emergencyBtn.innerText = "Analyzing...";
      emergencyBtn.disabled = true;

      try {
        const res = await fetch(`${API_URL}/emergency-check`, { method: "POST" });
        const data = await res.json();
        alert(data.message + ` (${data.devices_checked} devices scanned)`);
        fetchAndRender(); // Refresh UI to show new threat statuses
      } catch (e) {
        alert("Error connecting to AI Check");
      } finally {
        emergencyBtn.innerText = originalText;
        emergencyBtn.disabled = false;
      }
    });
}

// ---------------------- SOCKET.IO init (minimal) ----------------------
let socket = null;
try {
  // If your browser is not on the same machine as the server, replace 'localhost' with the server LAN IP printed by app.py
  socket = io("http://10.235.221.112:8000");

  socket.on("connect", () => console.log("Socket connected:", socket.id));
  socket.on("connect_error", (err) => console.error("Socket connect_error:", err));
  socket.on("device_update", (payload) => {
    console.log("Real-time update:", payload);
    fetchAndRender(); // refresh UI when server pushes updates
    // NOTE: we intentionally don't show popup directly from socket payload here;
    // we rely on server->DB->fetchAndRender so popup logic remains purely tied to rendered DOM text.
  });
} catch (e) {
  console.warn("Socket.IO init failed (fallback to polling):", e);
  // optional: fallback polling if needed
  setInterval(fetchAndRender, 3000);
}
// --------------------------------------------------------------------

// Initial Load
fetchAndRender();



/* ---------- Threat popup detection (STRICT: device-card text only) ---------- */
/* Only shows popup when a rendered device card contains the exact string "Threat Detected".
   It will NOT trigger from the summary badge or any other page text. */

(function(){
  // create/remove popup helpers
  function createPopup(text) {
    removePopup();
    const p = document.createElement('div');
    p.id = 'threat-popup';
    p.innerHTML = `<div>⚠️ Threat Detected</div><span class="sub">${text || 'Potential malicious activity detected'}</span>`;
    document.body.appendChild(p);
  }
  function removePopup() {
    const ex = document.getElementById('threat-popup');
    if (ex) ex.remove();
  }

  // STRICT check: only inspect .threat elements inside #device-grid and require exact phrase
  function existsThreatDetectedInCards() {
    try {
      const threatNodes = document.querySelectorAll('#device-grid .threat');
      for (let i = 0; i < threatNodes.length; i++) {
        const txt = (threatNodes[i].innerText || '').trim();
        if (/^Threat Detected$/i.test(txt)) {
          return true;
        }
      }
      return false;
    } catch (e) {
      return false;
    }
  }

  // main scan function (used by render completion and observer)
  window.scanDomForThreat = function scanDomForThreat() {
    if (existsThreatDetectedInCards()) {
      createPopup('');
    } else {
      removePopup();
    }
  };

  // Observe changes to the device grid only (so summary updates won't trigger this)
  const deviceGrid = document.getElementById('device-grid');
  if (deviceGrid) {
    const obs = new MutationObserver(() => {
      scanDomForThreat();
    });
    obs.observe(deviceGrid, { childList: true, subtree: true, characterData: true });
  } else {
    // fallback: scan periodically (rare case)
    setInterval(scanDomForThreat, 1500);
  }

  // expose helpers for testing
  window.__triggerThreatPopupForTest = function(msg){
    createPopup(msg || 'Simulated threat for testing');
  };
  window.__removeThreatPopup = removePopup;

  // initial quick check
  window.addEventListener('load', scanDomForThreat);
})();
