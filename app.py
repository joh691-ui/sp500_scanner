import os
from flask import Flask, send_from_directory, jsonify
from scanner import scan_in_background, SCAN_STATUS

app = Flask(__name__, static_folder=".")

@app.route('/')
def serve_dashboard():
    # If the file doesn't exist yet, show a simple loading/instructions page
    if not os.path.exists('sp500_scanner_dashboard.html'):
        return """
        <html>
            <head><title>S&P 500 Scanner</title></head>
            <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                <h1>No dashboard has been generated yet.</h1>
                <p>Click the button below to start the initial fetch and optimization (this can take 10-20 minutes).</p>
                <button onclick="start()" style="padding: 10px 20px; font-size: 16px;">Start Scanning</button>
                <p id="msg" style="margin-top: 20px; color: #555;"></p>
                
                <script>
                function start() {
                    document.querySelector('button').disabled = true;
                    fetch('/api/update', {method: 'POST'})
                        .then(res => res.json())
                        .then(() => poll());
                }
                function poll() {
                    fetch('/api/status')
                        .then(res => res.json())
                        .then(data => {
                            document.getElementById('msg').innerText = data.message || "Working...";
                            if (data.is_running) {
                                setTimeout(poll, 1000);
                            } else {
                                location.reload();
                            }
                        });
                }
                // Auto check
                fetch('/api/status').then(r=>r.json()).then(d=>{if(d.is_running) { document.querySelector('button').disabled=true; poll();} });
                </script>
            </body>
        </html>
        """
    response = send_from_directory('.', 'sp500_scanner_dashboard.html')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/update', methods=['POST'])
def update_data():
    if not SCAN_STATUS["is_running"]:
        SCAN_STATUS["is_running"] = True
        SCAN_STATUS["message"] = "Starting background task..."
        scan_in_background()
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"})

@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({
        "is_running": SCAN_STATUS["is_running"],
        "message": SCAN_STATUS["message"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
