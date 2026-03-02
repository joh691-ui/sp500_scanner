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
  <title>Stock Scanner — Loading...</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📊</text></svg>">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0a0e17; color: #e2e8f0; font-family: 'Segoe UI', sans-serif;
           display: flex; flex-direction: column; align-items: center;
           justify-content: center; min-height: 100vh; }
    h1   { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; margin-bottom: 6px; }
    .subtitle { font-size: 13px; color: #64748b; margin-bottom: 32px; }
    .progress-wrap { width: 420px; max-width: 90vw; }
    .progress-bg { background: #1e293b; border-radius: 99px; height: 8px; overflow: hidden; }
    .progress-bar { height: 100%; border-radius: 99px;
                    background: linear-gradient(90deg, #2563eb, #3b82f6);
                    transition: width 0.6s ease; width: 0%; }
    .pct { font-size: 12px; color: #3b82f6; font-family: monospace;
           text-align: right; margin-top: 6px; }
    #phase { font-size: 13px; color: #94a3b8; font-family: monospace;
             margin-top: 14px; min-height: 20px; text-align: center; }
    #detail { font-size: 11px; color: #475569; font-family: monospace;
              margin-top: 4px; min-height: 16px; text-align: center; }
  </style>
</head>
<body>
  <h1>📊 STOCK MOMENTUM SCANNER</h1>
  <p class="subtitle">Building dashboard — this takes about 2 minutes</p>
  <div class="progress-wrap">
    <div class="progress-bg"><div class="progress-bar" id="bar"></div></div>
    <div class="pct" id="pct">0%</div>
  </div>
  <p id="phase">Connecting to server…</p>
  <p id="detail"></p>
  <script>
    // Map known status message patterns to overall % ranges
    function parseProgress(msg) {
      if (!msg) return {pct: 1, phase: 'Starting…', detail: ''};
      
      // "Downloading price data: 200/503 completed"
      let m = msg.match(/(\\d+)\\/(\\d+)/);
      
      if (msg.includes('Fetching S&P') || msg.includes('Fetching S%26P') || msg.includes('Wikipedia')) {
        return {pct: 2, phase: '📋 Fetching S&P 500 component list…', detail: ''};
      }
      if (msg.includes('Downloading') && m) {
        let done = parseInt(m[1]), total = parseInt(m[2]);
        let p = Math.round(5 + (done / total) * 40);
        return {pct: p, phase: '⬇️ Downloading price data via Nasdaq API…', detail: m[1] + ' / ' + m[2] + ' stocks'};
      }
      if (msg.includes('Downloading')) {
        return {pct: 5, phase: '⬇️ Downloading price data via Nasdaq API…', detail: ''};
      }
      if (msg.includes('VIX')) {
        return {pct: 46, phase: '📊 Fetching VIX volatility data…', detail: ''};
      }
      if (msg.includes('Optimizing') && m) {
        let done = parseInt(m[1]), total = parseInt(m[2]);
        let p = Math.round(50 + (done / total) * 30);
        return {pct: p, phase: '🔬 Per-stock Sharpe optimisation…', detail: m[1] + ' / ' + m[2] + ' stocks'};
      }
      if (msg.includes('Optimizing')) {
        return {pct: 50, phase: '🔬 Per-stock Sharpe optimisation…', detail: ''};
      }
      if (msg.includes('Calculating')) {
        return {pct: 82, phase: '⚡ Calculating momentum signals…', detail: ''};
      }
      if (msg.includes('Generating')) {
        return {pct: 94, phase: '🖥️ Generating HTML dashboard…', detail: ''};
      }
      if (msg.includes('Done') || msg.includes('done')) {
        return {pct: 100, phase: '✅ Done! Loading dashboard…', detail: ''};
      }
      if (msg.startsWith('Error')) {
        return {pct: 0, phase: '❌ ' + msg, detail: ''};
      }
      return {pct: 1, phase: msg, detail: ''};
    }

    function poll() {
      fetch('/api/status').then(r => r.json()).then(d => {
        let {pct, phase, detail} = parseProgress(d.message || '');
        document.getElementById('bar').style.width = pct + '%';
        document.getElementById('pct').innerText = pct + '%';
        document.getElementById('phase').innerText = phase;
        document.getElementById('detail').innerText = detail;
        if (!d.is_running) { setTimeout(() => location.reload(), 1500); return; }
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
    from flask import request as flask_request
    body = flask_request.get_json(silent=True) or {}
    market = body.get("market", "SP500")
    if market not in ("SP500", "STO"):
        market = "SP500"
    status = get_status()
    if not status.get("is_running", False):
        set_status(f"Starting {market} scan...", True)
        scan_in_background(market)
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
