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
