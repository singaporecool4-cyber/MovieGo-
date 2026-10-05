#!/usr/bin/env python3
"""
MovieGo - Production TUI for Termux
Powered by Textual, HTTPX, and SQLite3.
Targeting MovieBox API architecture.
"""

import asyncio
import datetime
import hashlib
import hmac
import base64
import random
import json
import sqlite3
import time
from urllib.parse import urlparse, parse_qsl, urlencode
from typing import Dict, List, Optional, Any

import httpx
from bs4 import BeautifulSoup
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

# ==========================================
# PART 1: Config, Crypto Security & Cache
# ==========================================

class MovieBoxCrypto:
    """Exact HMAC-MD5 signing and Auth translation from MovieBox-TUI Rust source."""
    # Source Secret Key for generating signatures
    SECRET_BYTES = b"\xef\xa8\x91\x97\x4e\xec\xd3\x14\x8d\xf6\x3a\xa6\x11\x60\x2d\xef\xd1\x01\x25\x9b\xa5\x21\x02\x2c\x57\xae\x05\x66\xbd\x8e"

    @staticmethod
    def generate_x_client_token(ts_ms: int) -> str:
        ts_str = str(ts_ms)
        reversed_ts = ts_str[::-1]
        hash_val = hashlib.md5(reversed_ts.encode()).hexdigest()
        return f"{ts_str},{hash_val}"

    @staticmethod
    def generate_signature(method: str, url: str, body_str: str, ts_ms: int) -> str:
        parsed = urlparse(url)
        path = parsed.path
        
        # Sort query strings alphabetically as required by the Rust parser
        if parsed.query:
            queries = parse_qsl(parsed.query)
            queries.sort(key=lambda x: x[0])
            query_string = urlencode(queries, safe="&=")
            canonical_url = f"{path}?{query_string}"
        else:
            canonical_url = path

        body_hash = ""
        body_length = ""
        if body_str:
            body_bytes = body_str.encode('utf-8')[:102400]
            body_hash = hashlib.md5(body_bytes).hexdigest()
            body_length = str(len(body_str.encode('utf-8')))

        canonical = f"{method.upper()}\napplication/json\napplication/json\n{body_length}\n{ts_ms}\n{body_hash}\n{canonical_url}"
        h = hmac.new(MovieBoxCrypto.SECRET_BYTES, canonical.encode('utf-8'), hashlib.md5)
        sig_b64 = base64.b64encode(h.digest()).decode('utf-8')
        return f"{ts_ms}|2|{sig_b64}"

    @staticmethod
    def get_client_info() -> str:
        """Spoofed Android Client Metrics"""
        return json.dumps({
            "package_name": "com.community.oneroom",
            "version_name": "4.0.01.0813.03",
            "version_code": 50020117,
            "os": "android",
            "os_version": "13",
            "install_ch": "ps",
            "device_id": hashlib.md5(str(time.time()).encode()).hexdigest(),
            "brand": "Redmi",
            "model": "23078RKD5C",
            "system_language": "en",
            "net": "NETWORK_WIFI",
            "timezone": "Asia/Kolkata",
            "sp_code": "40401",
            "X-Play-Mode": "2"
        })

    @staticmethod
    def random_spoofed_ip() -> str:
        prefixes = ["103.241", "49.36", "117.195", "106.198", "122.162", "157.32", "182.70", "103.58", "27.60", "59.90"]
        prefix = random.choice(prefixes)
        return f"{prefix}.{random.randint(1,254)}.{random.randint(1,254)}"


class CacheManager:
    """Relational Cache Layer via SQLite3"""
    def __init__(self, db_path: str = "moviego_cache.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS movies (
                    movie_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL UNIQUE,
                    description TEXT,
                    release_year TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stream_links (
                    link_id TEXT PRIMARY KEY,
                    movie_id TEXT NOT NULL,
                    resolution TEXT,
                    stream_url TEXT NOT NULL,
                    expires_at TIMESTAMP,
                    FOREIGN KEY (movie_id) REFERENCES movies (movie_id) ON DELETE CASCADE
                )
            ''')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_movies_title ON movies (title)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_stream_movie ON stream_links (movie_id)")
            conn.commit()

    def clean_expired(self):
        """Purge old cached entries"""
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM stream_links WHERE expires_at < CURRENT_TIMESTAMP")
                conn.commit()
        except Exception:
            pass

    def save_movie(self, movie_id: str, title: str, source_url: str, desc: str = "", year: str = ""):
        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO movies (movie_id, title, source_url, description, release_year)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    release_year=excluded.release_year
            ''', (movie_id, title, source_url, desc, year))
            conn.commit()

    def save_stream(self, link_id: str, movie_id: str, resolution: str, stream_url: str):
        expires = (datetime.datetime.utcnow() + datetime.timedelta(hours=6)).isoformat()
        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO stream_links (link_id, movie_id, resolution, stream_url, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(link_id) DO UPDATE SET
                    stream_url=excluded.stream_url,
                    expires_at=excluded.expires_at
            ''', (link_id, movie_id, resolution, stream_url, expires))
            conn.commit()

    def get_all_movies(self) -> List[sqlite3.Row]:
        with self._get_connection() as conn:
            return conn.execute("SELECT * FROM movies ORDER BY created_at DESC LIMIT 50").fetchall()

    def get_streams(self, movie_id: str) -> List[sqlite3.Row]:
        with self._get_connection() as conn:
            return conn.execute("SELECT * FROM stream_links WHERE movie_id = ?", (movie_id,)).fetchall()
# ==========================================
# PART 2: Asynchronous API Client & Extraction
# ==========================================

class MovieBoxSession:
    """Handles JWT Token parsing and expiration from session.rs"""
    def __init__(self, token: str):
        self.token = token
        self.expires_at = self._parse_exp(token)
        self.created_at = int(time.time())

    def _parse_exp(self, token: str) -> Optional[int]:
        try:
            parts = token.split('.')
            if len(parts) > 1:
                payload = parts[1]
                pad_len = (4 - (len(payload) % 4)) % 4
                padded = payload + ("=" * pad_len)
                decoded = base64.urlsafe_b64decode(padded)
                val = json.loads(decoded)
                return int(val.get("exp", 0))
        except Exception:
            pass
        return None

    def is_valid(self) -> bool:
        if not self.token: return False
        now = int(time.time())
        if self.expires_at:
            return now + 60 < self.expires_at
        return now < self.created_at + (7 * 24 * 3600)


class MovieBoxClient:
    """Core HTTP Client translated from client.rs"""
    HOST_POOL = [
        "https://api6.aoneroom.com",
        "https://api5.aoneroom.com",
        "https://api4.aoneroom.com",
        "https://api4sg.aoneroom.com",
        "https://api3.aoneroom.com",
        "https://api6sg.aoneroom.com",
        "https://api.inmoviebox.com",
    ]

    def __init__(self):
        self.active_idx = 0
        self.session: Optional[MovieBoxSession] = None
        self.client_info = MovieBoxCrypto.get_client_info()
        self.spoofed_ip = MovieBoxCrypto.random_spoofed_ip()
        self.user_agent = "com.community.oneroom/50020117 (Linux; U; Android 13; en_US; Redmi; Build/TQ2A.230405.003; Cronet/135.0.7012.3)"
        self.semaphore = asyncio.Semaphore(3)

    def _get_base_url(self) -> str:
        return self.HOST_POOL[self.active_idx]

    def _rotate_host(self):
        self.active_idx = (self.active_idx + 1) % len(self.HOST_POOL)

    async def ensure_session(self) -> str:
        if self.session and self.session.is_valid():
            return self.session.token
            
        path = "/wefeed-mobile-bff/user-api/visitor-login"
        body_str = "{}"
        ts_ms = int(time.time() * 1000)
        url = self._get_base_url() + path
        
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "x-client-token": MovieBoxCrypto.generate_x_client_token(ts_ms),
            "x-tr-signature": MovieBoxCrypto.generate_signature("POST", url, body_str, ts_ms),
            "x-client-info": self.client_info,
            "x-client-status": "0",
            "x-forwarded-for": self.spoofed_ip,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, data=body_str, headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", {}) if "data" in resp.json() else resp.json()
                token = data.get("token")
                if token:
                    self.session = MovieBoxSession(token)
                    return token
        raise Exception("Failed to initialize visitor session")

    async def request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        async with self.semaphore:
            token = await self.ensure_session()
            body_str = json.dumps(body, separators=(',', ':')) if body else ""
            
            for _ in range(len(self.HOST_POOL)):
                base_url = self._get_base_url()
                full_url = base_url + path
                ts_ms = int(time.time() * 1000)
                
                headers = {
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "x-client-token": MovieBoxCrypto.generate_x_client_token(ts_ms),
                    "x-tr-signature": MovieBoxCrypto.generate_signature(method, full_url, body_str, ts_ms),
                    "x-client-info": self.client_info,
                    "x-client-status": "0",
                    "x-forwarded-for": self.spoofed_ip,
                }
                
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        if method == "POST":
                            resp = await client.post(full_url, data=body_str, headers=headers)
                        else:
                            resp = await client.get(full_url, headers=headers)
                            
                        if resp.status_code in [403, 406, 407, 429, 500, 502, 503, 504]:
                            self._rotate_host()
                            continue
                            
                        if resp.status_code == 200:
                            data = resp.json()
                            return data.get("data", data)
                except Exception:
                    self._rotate_host()
                    continue
            
            raise Exception("All MovieBox API hosts exhausted or unreachable.")

    # API Endpoints mapped from mod.rs & client.rs
    async def search(self, query: str, page: int = 1) -> List[dict]:
        payload = {"keyword": query, "page": page, "perPage": 15, "subjectType": 0}
        data = await self.request("POST", "/wefeed-mobile-bff/subject-api/search/v2", payload)
        return MovieBoxParser.parse_search(data)

    async def get_details(self, subject_id: str) -> dict:
        path = f"/wefeed-mobile-bff/subject-api/get?subjectId={subject_id}"
        return await self.request("GET", path)

    async def get_play_info(self, subject_id: str, season: int = 0, episode: int = 0) -> List[dict]:
        if season == 0 and episode == 0:
            path = f"/wefeed-mobile-bff/subject-api/play-info/v2?subjectId={subject_id}"
        else:
            path = f"/wefeed-mobile-bff/subject-api/play-info/v2?subjectId={subject_id}&se={season}&ep={episode}"
        
        data = await self.request("GET", path)
        return MovieBoxParser.parse_play_info(data)


class MovieBoxParser:
    """Translates adapt.rs JSON parsing to Python dictionaries"""
    @staticmethod
    def parse_search(payload: dict) -> List[dict]:
        items = []
        results = payload.get("results", [])
        if not results and "list" in payload:
            results = payload.get("list", [])
            
        subjects = results[0].get("subjects", []) if results and "subjects" in results[0] else results
        
        for s in subjects:
            id_str = str(s.get("subjectId", s.get("id", "")))
            if not id_str: continue
            
            items.append({
                "id": id_str,
                "title": s.get("title", s.get("name", "Unknown")),
                "type": "Series" if s.get("subjectType", s.get("stype")) == 2 else "Movie",
                "year": s.get("releaseDate", s.get("year", ""))[:4],
                "poster": s.get("cover", {}).get("url", "")
            })
        return items

    @staticmethod
    def parse_play_info(payload: dict) -> List[dict]:
        streams = payload.get("streams", [])
        releases = []
        for s in streams:
            quality = "1080p"
            resolutions = s.get("resolutions", "")
            if resolutions:
                res_list = [int(r) for r in resolutions.split(",") if r.isdigit()]
                if res_list:
                    quality = f"{max(res_list)}p"
            
            releases.append({
                "id": str(s.get("id", "")),
                "quality": quality,
                "codec": s.get("codecName", s.get("codec", "MP4")),
                "url": s.get("url", ""),
                "signCookie": s.get("signCookie", "")
            })
        return releases
        # ==========================================
# PART 3: The Textual User Interface (TUI)
# ==========================================

class MovieGoApp(App):
    """Main Terminal Interface for MovieGo"""
    
    CSS = """
    Screen {
        background: #121212;
        color: #e0e0e0;
    }
    #search_box {
        margin: 1 2;
        border: tall #00bcd4;
        background: #1e1e1e;
    }
    #results_list {
        margin: 0 2;
        border: round #4caf50;
        height: 1fr;
        background: #121212;
    }
    ListItem {
        padding: 1;
        border-bottom: solid #333333;
    }
    ListItem:focus {
        background: #2a2a2a;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "clear_cache", "Clear Cache")
    ]

    def __init__(self):
        super().__init__()
        self.client = MovieBoxClient()
        self.cache = CacheManager()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, name="MovieGo (Termux)")
        yield Input(placeholder="Search movies/series on MovieBox... (Press Enter)", id="search_box")
        yield ListView(id="results_list")
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted):
        query = event.value.strip()
        if not query:
            return
            
        list_view = self.query_one("#results_list", ListView)
        await list_view.clear()
        list_view.append(ListItem(Label(f"[bold yellow]Searching MovieBox for '{query}'...[/bold yellow]")))
        
        try:
            # MovieBox API को कॉल करना (Part 2 का इस्तेमाल करके)
            results = await self.client.search(query)
            await list_view.clear()
            
            if not results:
                list_view.append(ListItem(Label("[bold red]No results found or API blocked![/bold red]")))
                return
            
            # रिज़ल्ट्स को UI में दिखाना और SQLite कैशे में सेव करना
            for item in results:
                title = item.get("title", "Unknown")
                year = item.get("year", "N/A")
                m_type = item.get("type", "Movie")
                movie_id = item.get("id", "")
                
                icon = "📺" if m_type == "Series" else "🎬"
                
                list_view.append(
                    ListItem(
                        Label(f"{icon} [bold white]{title}[/bold white] ({year}) - {m_type}")
                    )
                )
                
                # बैकग्राउंड में SQLite में मूवी का डेटा सेव करें (Part 1 का इस्तेमाल करके)
                if movie_id:
                    self.cache.save_movie(
                        movie_id=movie_id,
                        title=title,
                        source_url=f"moviebox://{movie_id}",
                        year=year
                    )
                    
        except Exception as e:
            await list_view.clear()
            list_view.append(ListItem(Label(f"[bold red]API Error:[/bold red] {str(e)}")))

    def action_clear_cache(self):
        self.cache.clean_expired()
        self.notify("Cache cleaned successfully!")
      # ==========================================
# PART 4: Execution & Main Runner
# ==========================================

def main():
    """Entry point for MovieGo TUI"""
    try:
        app = MovieGoApp()
        app.run()
    except KeyboardInterrupt:
        print("\nExiting MovieGo...")
    except Exception as e:
        print(f"\nCritical Error: {str(e)}")

if __name__ == "__main__":
    main()
                
