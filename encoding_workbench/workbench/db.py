"""SQLite storage for encoding jobs and measured rate-distortion points."""
import sqlite3

from paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    segment_id    TEXT NOT NULL,
    codec         TEXT NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    quality_label TEXT NOT NULL,
    crf           INTEGER,
    bitrate_cap   INTEGER,
    preset        TEXT,
    extra         TEXT,
    expect_fail   INTEGER NOT NULL DEFAULT 0,
    fail_reason   TEXT,
    status        TEXT NOT NULL,            -- ok | failed
    ffmpeg_rc     INTEGER,
    file_bytes    INTEGER,
    stream_bytes  INTEGER,
    bitrate_meas  INTEGER,
    ssim_avg      REAL,
    ssim_min      REAL,
    psnr_avg      REAL,
    frames        INTEGER,
    encode_secs   REAL,
    output_path   TEXT,
    log_path      TEXT,
    error_tail    TEXT
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
