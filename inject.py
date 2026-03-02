import os

old_html_path = r"C:\Users\joh69\Downloads\sp500_scanner_dashboard.html"
new_html_path = r"C:\Users\joh69\.gemini\antigravity\scratch\sp500_scanner_app\sp500_scanner_dashboard.html"

if not os.path.exists(old_html_path):
    print("Old HTML not found.")
    exit(1)

with open(old_html_path, 'r', encoding='utf-8') as f:
    html = f.read()

# Replace section title
old_title = '<div class="section-title">TOP 25 — STARKASTE KÖP-SIGNALER</div>'
new_title = """<div class="section-title">
    <div>TOP 25 — STARKASTE KÖP-SIGNALER</div>
    <div>
        <button id="updateBtn" class="btn" onclick="startUpdate()">Uppdatera Data</button>
        <span id="statusMsg"></span>
    </div>
</div>"""
html = html.replace(old_title, new_title)

# Replace head style to add .btn style
style_to_add = """
.btn { background-color: var(--accent-blue); color: white; border: none; padding: 8px 16px; font-size: 14px; border-radius: 6px; cursor: pointer; font-family: 'JetBrains Mono', monospace; font-weight: 600; }
.btn:hover { background-color: #2563eb; }
.btn:disabled { background-color: #1e3a8a; cursor: not-allowed; }
#statusMsg { font-size: 12px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; margin-left: 10px; }
</style>
"""
html = html.replace("</style>", style_to_add)

# Add script before </body>
script_to_add = """
<script>
function startUpdate() {
    const btn = document.getElementById('updateBtn');
    const statusMsg = document.getElementById('statusMsg');
    btn.disabled = true;
    btn.innerText = "Startar...";
    
    fetch('/api/update', {method: 'POST'})
        .then(res => res.json())
        .then(data => {
            if (data.status === 'started' || data.status === 'already_running') {
                pollStatus();
            } else {
                statusMsg.innerText = "Fel vid start.";
                btn.disabled = false;
                btn.innerText = "Uppdatera Data";
            }
        })
        .catch(() => {
            statusMsg.innerText = "Kan inte nå servern.";
            btn.disabled = false;
            btn.innerText = "Uppdatera Data";
        });
}

function pollStatus() {
    const btn = document.getElementById('updateBtn');
    const statusMsg = document.getElementById('statusMsg');
    
    fetch('/api/status')
        .then(res => res.json())
        .then(data => {
            if (data.is_running) {
                btn.innerText = "Uppdaterar...";
                statusMsg.innerText = data.message || "Arbetar...";
                setTimeout(pollStatus, 3000);
            } else {
                btn.innerText = "Uppdatera Data";
                btn.disabled = false;
                statusMsg.innerText = "Uppdatering klar. Sidan laddas om...";
                setTimeout(() => location.reload(), 2000);
            }
        });
}

document.addEventListener("DOMContentLoaded", () => {
    fetch('/api/status')
        .then(res => res.json())
        .then(data => {
            if (data.is_running) {
                document.getElementById('updateBtn').disabled = true;
                pollStatus();
            }
        });
});
</script>
</body>
"""
html = html.replace("</body>", script_to_add)

with open(new_html_path, 'w', encoding='utf-8') as f:
    f.write(html)

print("HTML modified and saved.")
