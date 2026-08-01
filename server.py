"""
Basit yerel haber proxy sunucusu.
Hem bu klasördeki dosyaları (haber-akisi.html gibi) sunar,
hem de /proxy?url=... adresine gelen istekleri kendi tarafında
(Python üzerinden) çekip tarayıcıya CORS izniyle geri verir.

Aynı beslemeyi kısa süre içinde tekrar tekrar çekmemek için
basit bir bellek-içi önbellek (cache) kullanır (CACHE_TTL saniye).

/data uç noktası (GET/POST) kaydedilen ve okunan haberleri
bu klasördeki kaydedilenler.json dosyasında kalıcı olarak saklar.

/x-feed uç noktası, X (Twitter) hesaplarının son gönderilerini X'in
resmi API'si üzerinden çeker. Bunun çalışması için bu klasörde bir
x-ayarlari.json dosyası ve içinde geçerli bir "bearer_token" olması
gerekir (bkz. x-ayarlari.ornek.json). Şifreyle giriş YAPILMAZ; sadece
X Developer Portal'dan alınan bir API anahtarı (Bearer Token) kullanılır.

ÖNEMLİ - MALİYET: Şubat 2026'dan itibaren X API ücretsiz değil, kullandıkça
ödeme (pay-per-use) modeliyle çalışıyor. Her istek gerçek paraya mal olur.
Bu yüzden takip ettiğin hesap sayısını makul tut ve X Developer Console'da
bir harcama limiti (spending limit) belirle.

/nitter-feed uç noktası ise ücretsiz bir alternatif: Nitter (özgür/açık
kaynak bir X arayüzü) üzerinden herkese açık hesapların RSS beslemesini
çeker, hiçbir token/ödeme gerektirmez. Ama şunu bilerek kullan: bu genel
topluluk sunucuları üzerinden çalışıyor, sunucu sahipleri "scraping için
kullanmayın, kendi sunucunuzu kurun" diye rica ediyor, ve bu sunucular
önceden haber vermeden çökebilir/kapanabilir. Garantisi yok.

ÖNEMLİ - HERKESE AÇIK YAYINLAMA HAKKINDA: Bu siteyi internete koyarsan,
görünen HTML sayfasını başkalarının programatik olarak çekmesini (scraping)
%100 engellemek mümkün değil - tarayıcıda görünen her şey teknik olarak
okunabilir. Ama aşağıdaki önlemler bunu zorlaştırıp çoğu otomatik
scraper'ı caydırıyor: basit IP bazlı hız sınırlama (RATE_LIMIT_*), bilinen
scraper araçlarını User-Agent'a göre engelleme (BOT_UA_PATTERNS), ve
robots.txt ile arama motoru botlarına "indeksleme" izni vermeme.

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

# Render (ve benzeri barındırma servisleri) PORT'u kendi ortam
# değişkeniyle veriyor; yerelde çalıştırırken hâlâ 8080 kullanılır.
PORT = int(os.environ.get("PORT", 8080))
CACHE_TTL = 180  # saniye - bu süre içinde aynı url tekrar istenirse önbellekten verilir

# url -> (kaydedilme_zamani, veri_bytes, content_type)
CACHE = {}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "kaydedilenler.json")
X_CONFIG_FILE = os.path.join(BASE_DIR, "x-ayarlari.json")

X_API_BASE = "https://api.x.com/2"
# X API artik ucretsiz degil (Subat 2026'dan itibaren kullandikca odeme).
# Her okuma gercek paraya mal oluyor, bu yuzden onbellek suresini uzun
# tutuyoruz. 1800 saniye = 30 dakika: bu sure icinde ayni hesap tekrar
# istenirse X'e gitmeden onbellekten donuyor.
X_CACHE_TTL = 1800

# handle -> (kaydedilme_zamani, tweet_listesi)
X_CACHE = {}
# handle -> kullanici_id (X API önce handle'i id'ye çevirmeyi gerektiriyor, tekrar
# tekrar sormamak için bunu da bellekte tutuyoruz)
X_USER_ID_CACHE = {}

# Nitter: ucretsiz alternatif. Topluluk sunuculari sik cokup/kapandigi icin
# birden fazla sunucu sirayla deneniyor, biri calismazsa digerine geciliyor.
# Liste status.d420.de uzerinden RSS destegi olan ve saglikli gorunen
# sunuculardan secildi (2026-08-01 itibariyle); zamanla degisebilir,
# guncel durumu status.d420.de'den kontrol edebilirsin.
NITTER_INSTANCES = [
    "https://xcancel.com",
    "https://nitter.net",
    "https://nitter.poast.org",
]
# Topluluk sunucularina nazik davranmak icin onbellegi uzun tutuyoruz.
# Artik cok sayida hesap takip edildigi icin (kurumsal + gazeteci listesi)
# bu sure daha da onemli - 30 dakika.
NITTER_CACHE_TTL = 1800

# handle -> (kaydedilme_zamani, xml_bytes)
NITTER_CACHE = {}


def load_x_config():
    """x-ayarlari.json dosyasindan bearer_token'i okur. Yoksa None doner."""
    if os.path.exists(X_CONFIG_FILE):
        try:
            with open(X_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                token = cfg.get("bearer_token", "").strip()
                return token or None
        except Exception as e:
            print(f"[x-ayarlari okuma hatası] {e}")
    return None


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

# ---------- Basit koruma önlemleri (herkese açık yayında scraping'i zorlaştırmak için) ----------

# ip -> [istek_zamanlari]
RATE_LIMIT_WINDOW = 60  # saniye
RATE_LIMIT_MAX_REQUESTS = 250  # bu pencerede bir IP'den izin verilen azami istek
# Not: normal bir sayfa yüklemesi (RSS + X hesapları) tek başına ~80 istek
# yapıyor, bu yüzden eşiği ona göre yüksek tuttuk - amaç gerçek kullanıcıyı
# değil, sürekli/otomatik toplu çekim yapan botları yavaşlatmak.
RATE_LIMIT_LOG = {}

# Bilinen otomatik scraping araçlarının User-Agent'larında sık geçen ifadeler.
# Not: bu kesin bir engelleme değil (User-Agent kolayca sahtelenebilir),
# sadece varsayılan ayarlarla çalışan basit/gelişigüzel scraper'ları eler.
BOT_UA_PATTERNS = (
    "python-requests", "scrapy", "curl/", "wget/", "httpx", "aiohttp",
    "go-http-client", "libwww-perl", "okhttp", "axios/", "node-fetch",
)


def is_blocked_user_agent(user_agent):
    ua = (user_agent or "").lower()
    if not ua:
        return True  # User-Agent hiç yoksa da şüpheli say
    return any(pattern in ua for pattern in BOT_UA_PATTERNS)


def is_rate_limited(ip):
    now = time.time()
    history = [t for t in RATE_LIMIT_LOG.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    history.append(now)
    RATE_LIMIT_LOG[ip] = history
    return len(history) > RATE_LIMIT_MAX_REQUESTS

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def guard_request(self):
        """Hiz siniri ve bot User-Agent kontrolu. True donerse istek reddedildi demektir."""
        ip = self.client_address[0]
        if is_rate_limited(ip):
            self.send_response(429)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Retry-After", str(RATE_LIMIT_WINDOW))
            self.end_headers()
            self.wfile.write("Cok fazla istek - biraz sonra tekrar dene.".encode("utf-8"))
            return True
        if is_blocked_user_agent(self.headers.get("User-Agent")):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Erisim reddedildi.".encode("utf-8"))
            return True
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/robots.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"User-agent: *\nDisallow: /\n")
            return

        if self.guard_request():
            return

        if parsed.path == "/proxy":
            self.handle_proxy(parsed)
        elif parsed.path == "/data":
            self.handle_data_get()
        elif parsed.path == "/x-feed":
            self.handle_x_feed(parsed)
        elif parsed.path == "/nitter-feed":
            self.handle_nitter_feed(parsed)
        else:
            # Normal dosya sunumu (haber-akisi.html gibi)
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if self.guard_request():
            return

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

    def handle_x_feed(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        handle = (query.get("handle", [None])[0] or "").lstrip("@").strip()

        if not handle:
            self.send_json(400, {"ok": False, "error": "handle parametresi eksik", "tweets": []})
            return

        token = load_x_config()
        if not token:
            self.send_json(200, {
                "ok": False,
                "error": "config_missing",
                "message": (
                    "x-ayarlari.json bulunamadi veya bearer_token bos. "
                    "X Developer Portal'dan bir Bearer Token alip bu dosyaya ekle."
                ),
                "tweets": [],
            })
            return

        now = time.time()
        cached = X_CACHE.get(handle)
        if cached and (now - cached[0] < X_CACHE_TTL):
            self.send_json(200, {"ok": True, "tweets": cached[1], "cache": "HIT"})
            return

        try:
            user_id = X_USER_ID_CACHE.get(handle)
            if not user_id:
                user_id = self.x_api_get_user_id(handle, token)
                X_USER_ID_CACHE[handle] = user_id

            tweets = self.x_api_get_tweets(user_id, handle, token)
            X_CACHE[handle] = (now, tweets)
            self.send_json(200, {"ok": True, "tweets": tweets, "cache": "MISS"})

        except Exception as e:
            print(f"[x-feed hatası] {handle} -> {e}")
            self.send_json(200, {"ok": False, "error": "istek_hatasi", "message": str(e), "tweets": []})

    def x_api_request(self, path, token):
        req = urllib.request.Request(
            X_API_BASE + path,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def x_api_get_user_id(self, handle, token):
        data = self.x_api_request(f"/users/by/username/{urllib.parse.quote(handle)}", token)
        user = data.get("data")
        if not user or "id" not in user:
            raise Exception(f"kullanici bulunamadi: @{handle}")
        return user["id"]

    def x_api_get_tweets(self, user_id, handle, token):
        path = (
            f"/users/{user_id}/tweets"
            "?max_results=5&tweet.fields=created_at&exclude=retweets,replies"
        )
        data = self.x_api_request(path, token)
        results = []
        for tw in data.get("data", []):
            results.append({
                "id": tw.get("id"),
                "text": tw.get("text", ""),
                "created_at": tw.get("created_at", ""),
                "link": f"https://x.com/{handle}/status/{tw.get('id')}",
            })
        return results

    def handle_nitter_feed(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        handle = (query.get("handle", [None])[0] or "").lstrip("@").strip()

        if not handle:
            self.send_error(400, "handle parametresi eksik")
            return

        now = time.time()
        cached = NITTER_CACHE.get(handle)
        if cached and (now - cached[0] < NITTER_CACHE_TTL):
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached[1])
            return

        last_error = "bilinmeyen hata"
        for instance in NITTER_INSTANCES:
            url = f"{instance}/{urllib.parse.quote(handle)}/rss"
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        )
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = response.read()

                NITTER_CACHE[handle] = (now, data)
                self.send_response(200)
                self.send_header("Content-Type", "application/xml; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Cache", "MISS")
                self.send_header("X-Nitter-Instance", instance)
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                last_error = str(e)
                print(f"[nitter denemesi basarisiz] {instance} -> {e}")
                continue

        print(f"[nitter-feed] tum sunucular basarisiz oldu: @{handle} -> {last_error}")
        self.send_response(502)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"Nitter sunuculari erisilemedi: {last_error}".encode("utf-8"))

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Konsolu daha sade tutmak icin varsayilan loglamayi kisaltiyoruz
        print(f"{self.address_string()} - {format % args}")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # Aynı anda birden fazla istek (siteni yüklerken atılan ~80 besleme
    # isteği, ya da birden fazla ziyaretçi) birbirini bloklamasın diye
    # her isteği ayrı bir thread'de işliyoruz.
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with ThreadingHTTPServer(("", PORT), ProxyHandler) as httpd:
        print(f"Sunucu calisiyor: http://localhost:{PORT}/")
        print(f"Haber sayfasi:    http://localhost:{PORT}/haber-akisi.html")
        print("Durdurmak icin bu pencerede Ctrl+C'ye basabilirsin.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nSunucu durduruldu.")
            sys.exit(0)
