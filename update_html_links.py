import re

html_path = 'sp500_scanner_dashboard.html'
with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

# Replace ticker with link
html = re.sub(
    r'<td class="ticker">([A-Z-]+)<br>',
    r'<td class="ticker"><a href="https://finance.yahoo.com/chart/\1" target="_blank" style="color:inherit;text-decoration:none;">\1</a><br>',
    html
)

html = html.replace('setTimeout(pollStatus, 3000);', 'setTimeout(pollStatus, 1000);')
with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)
