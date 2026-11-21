"""SQLite cache for window-level measurements and full-shot encodes."""
import sqlite3

from search_common import SEARCH_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS window_jobs (
    job_id        TEXT PRIMARY KEY,
    config_id     TEXT NOT NULL,
    shot_id       TEXT NOT NULL,
    window_idx    INTEGER NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    crf           INTEGER NOT NULL,
    status        TEXT NOT NULL,
    ffmpeg_rc     INTEGER,
    file_bytes    INTEGER,
    ssim_avg      REAL,
    ssim_min      REAL,
    frame_ssims   TEXT,
    frames        INTEGER,
    encode_secs   REAL,
    output_path   TEXT,
    error_tail    TEXT,
    UNIQUE(config_id, shot_id, window_idx)
);
CREATE TABLE IF NOT EXISTS full_encodes (
    job_id        TEXT PRIMARY KEY,
    config_id     TEXT NOT NULL,
    shot_id       TEXT NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    crf           INTEGER NOT NULL,
    status        TEXT NOT NULL,
    file_bytes    INTEGER,
    ssim_avg      REAL,
    ssim_min      REAL,
    frame_ssims   TEXT,
    frames        INTEGER,
    encode_secs   REAL,
    output_path   TEXT,
    error_tail    TEXT,
    UNIQUE(config_id, shot_id)
);
"""


def connect():
    SEARCH_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SEARCH_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
