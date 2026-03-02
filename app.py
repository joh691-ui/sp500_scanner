import os
from flask import Flask, send_from_directory, jsonify
from scanner import scan_in_background, set_status, get_status

app = Flask(__name__, static_folder=".")

# ── Auto-start scan on boot ────────────────────────────────────────────────
# When Render deploys/restarts, the generated HTML is gone (not in git).
# We kick off a background scan immediately so users see fresh data ASAP.
def _auto_start_scan():
    status = get_status()
    if not status.get("is_running", False):
        set_status("Auto-starting scan on server boot...", True)
        scan_in_background()

_auto_start_scan()
# ──────────────────────────────────────────────────────────────────────────

LOADING_PAGE = """<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="5">
  <title>S&amp;P 500 Scanner — Loading...</title>
  <style>
    body { background: #0a0e17; color: #e2e8f0; font-family: sans-serif;
           display: flex; flex-direction: column; align-items: center;
           justify-content: center; min-height: 100vh; margin: 0; }
    h1   { font-size: 24px; margin-bottom: 12px; }
    #msg { font-size: 14px; color: #94a3b8; font-family: monospace; margin-top: 8px; }
    .spinner { width: 40px; height: 40px; border: 4px solid #1e293b;
               border-top-color: #3b82f6; border-radius: 50%;
               animation: spin 0.8s linear infinite; margin-bottom: 20px; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="spinner"></div>
  <h1>S&amp;P 500 Scanner</h1>
  <p>Generating dashboard for the first time&hellip;</p>
  <p id="msg">Connecting to server…</p>
  <script>
    function poll() {
      fetch('/api/status').then(r => r.json()).then(d => {
        document.getElementById('msg').innerText = d.message || 'Working…';
        if (!d.is_running) { location.reload(); return; }
        setTimeout(poll, 2000);
      }).catch(() => setTimeout(poll, 3000));
    }
    poll();
  </script>
</body>
</html>"""

@app.route('/')
def serve_dashboard():
    if not os.path.exists('sp500_scanner_dashboard.html'):
        return LOADING_PAGE
    response = send_from_directory('.', 'sp500_scanner_dashboard.html')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/update', methods=['POST'])
def update_data():
    status = get_status()
    if not status.get("is_running", False):
        set_status("Starting background task...", True)
        scan_in_background()
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"})

@app.route('/api/status', methods=['GET'])
def api_get_status():
    status = get_status()
    return jsonify({
        "is_running": status.get("is_running", False),
        "message": status.get("message", "")
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
