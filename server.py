"""
Basit yerel haber proxy sunucusu.
Hem bu klasördeki dosyaları (haber-akisi.html gibi) sunar,
hem de /proxy?url=... adresine gelen istekleri kendi tarafında
(Python üzerinden) çekip tarayıcıya CORS izniyle geri verir.

Aynı beslemeyi kısa süre içinde tekrar tekrar çekmemek için
basit bir bellek-içi önbellek (cache) kullanır (CACHE_TTL saniye).

/data uç noktası (GET/POST) kaydedilen ve okunan haberleri
bu klasördeki kaydedilenler.json dosyasında kalıcı olarak saklar.

Kullanım:
    python server.py
Sonra tarayıcıda:
    http://localhost:8000/haber-akisi.html
"""

import http.server
import socketserver
import urllib.request
import urllib.parse
import sys
import time
import json
import os

PORT = 8080
CACHE_TTL = 180  # saniye - bu süre içinde aynı url tekrar istenirse önbellekten verilir

# url -> (kaydedilme_zamani, veri_bytes, content_type)
CACHE = {}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "kaydedilenler.json")


def load_saved_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {
                        "saved": list(data.get("saved", [])),
                        "read": list(data.get("read", [])),
                    }
        except Exception as e:
            print(f"[veri okuma hatası] {e}")
    return {"saved": [], "read": []}


def write_saved_data(data):
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, DATA_FILE)


# Sunucu açıldığında dosyadan yükle, sonrasında bellekte tutup her POST'ta diske yaz
SAVED_DATA = load_saved_data()

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/proxy":
            self.handle_proxy(parsed)
        elif parsed.path == "/data":
            self.handle_data_get()
        else:
            # Normal dosya sunumu (haber-akisi.html gibi)
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/data":
            self.handle_data_post()
        else:
            self.send_error(404, "Bulunamadi")

    def handle_data_get(self):
        body = json.dumps(SAVED_DATA, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle_data_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            incoming = json.loads(raw.decode("utf-8") or "{}")

            SAVED_DATA["saved"] = list(incoming.get("saved", []))
            SAVED_DATA["read"] = list(incoming.get("read", []))
            write_saved_data(SAVED_DATA)

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        except Exception as e:
            print(f"[veri yazma hatası] {e}")
            self.send_response(500)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    def handle_proxy(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        target_url = query.get("url", [None])[0]

        if not target_url:
            self.send_error(400, "url parametresi eksik")
            return

        now = time.time()
        cached = CACHE.get(target_url)
        if cached and (now - cached[0] < CACHE_TTL):
            _, data, content_type = cached
            print(f"[onbellek] {target_url}")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Cache", "HIT")
            self.end_headers()
            self.wfile.write(data)
            return

        try:
            req = urllib.request.Request(
                target_url,
                headers={
                    # Bazı siteler tarayıcı gibi görünmeyen istekleri reddediyor,
                    # bu yüzden gerçek bir tarayıcı User-Agent'ı gönderiyoruz.
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                },
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "application/xml")

            CACHE[target_url] = (now, data, content_type)

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Cache", "MISS")
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            print(f"[proxy hatası] {target_url} -> {e}")
            self.send_response(502)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Proxy hatasi: {e}".encode("utf-8"))

    def log_message(self, format, *args):
        # Konsolu daha sade tutmak icin varsayilan loglamayi kisaltiyoruz
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), ProxyHandler) as httpd:
        print(f"Sunucu calisiyor: http://localhost:{PORT}/")
        print(f"Haber sayfasi:    http://localhost:{PORT}/haber-akisi.html")
        print("Durdurmak icin bu pencerede Ctrl+C'ye basabilirsin.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nSunucu durduruldu.")
            sys.exit(0)
