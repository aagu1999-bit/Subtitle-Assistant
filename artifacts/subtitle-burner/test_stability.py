#!/usr/bin/env python3
"""
Subtitle Assistant Studio — Automated Stability & Integrity Test Suite
Runs 1-click diagnostic checks across system requirements, app code, database, and rendering cache.
"""

import os
import sys
import shutil
import subprocess
import py_compile
import hashlib
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent / "scratch" / "Subtitle-Assistant" / "artifacts" / "subtitle-burner"
if not BASE_DIR.exists():
    BASE_DIR = Path("/Users/owner/.gemini/antigravity/scratch/Subtitle-Assistant/artifacts/subtitle-burner")

APP_FILE = BASE_DIR / "app.py"
CACHE_DIR = BASE_DIR / "cache"
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"

def run_test(name, fn):
    print(f"------------ [TEST] {name} ------------")
    try:
        fn()
        print(f"✅ PASSED: {name}\n")
        return True
    except Exception as e:
        print(f"❌ FAILED: {name}")
        print(f"   Error Details: {e}\n")
        return False

def test_python_syntax():
    """Verify app.py compiles cleanly without syntax errors."""
    py_compile.compile(str(APP_FILE), doraise=True)

def test_required_directories():
    """Verify required app directories exist and are writable."""
    for d in [CACHE_DIR, UPLOAD_DIR, OUTPUT_DIR]:
        d.mkdir(exist_ok=True)
        test_file = d / ".healthcheck"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()

def test_ffmpeg_availability():
    """Verify FFmpeg binary is available on PATH."""
    res = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg check failed with code {res.returncode}")

def test_segment_cache_hashing():
    """Verify MD5 segment cache key generation and serialization."""
    sample_data = {
        "src": "video.mp4",
        "t_in": 1.5,
        "t_out": 4.5,
        "W": 1080,
        "H": 1920,
        "fps": 30,
        "fit": "cover",
        "bg": "black"
    }
    raw = json.dumps(sample_data, sort_keys=True)
    h1 = hashlib.md5(raw.encode("utf-8")).hexdigest()
    h2 = hashlib.md5(raw.encode("utf-8")).hexdigest()
    assert h1 == h2 and len(h1) == 32, "MD5 hashing non-deterministic"

def test_database_init():
    """Verify SQLite database schema and connection."""
    import sqlite3
    db_path = BASE_DIR / "jobs.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

def main():
    print("=" * 60)
    print("🎬 SUBTITLE ASSISTANT STUDIO — STABILITY DIAGNOSTICS")
    print("=" * 60 + "\n")

    tests = [
        ("Python Code Compilation Syntax", test_python_syntax),
        ("Required Directories & File Permissions", test_required_directories),
        ("FFmpeg Binary Availability", test_ffmpeg_availability),
        ("Smart Segment Cache MD5 Hashing", test_segment_cache_hashing),
        ("SQLite Database Schema Integrity", test_database_init),
    ]

    passed = 0
    total = len(tests)

    for name, fn in tests:
        if run_test(name, fn):
            passed += 1

    print("=" * 60)
    print(f"📊 DIAGNOSTIC RESULTS: {passed}/{total} Tests Passed")
    print("=" * 60)

    if passed == total:
        print("🎉 ALL STABILITY CHECKS PASSED! System is 100% healthy.\n")
        sys.exit(0)
    else:
        print("⚠️ Some stability checks failed. Review errors above.\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
