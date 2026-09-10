"""
Tiger V19 — Daily Research Cleanup Module
==========================================

Runs every morning at 09:00 IST before market open. Performs:

  1. Downloads fresh daily Scrip Master from Angel One.
  2. Purges yesterday's stale log files.
  3. Cleans temporary research/junk files.
  4. Reports disk usage summary.

Keeps the AWS disk 100% clean.  All operations are wrapped in
try/except so the main trading loop is never affected.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, date, timedelta
from pathlib import Path

logger = logging.getLogger("tiger_brain.daily_cleanup")

# Default paths — relative to repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
TMP_DIR = REPO_ROOT / "tmp"
RESEARCH_DIR = REPO_ROOT / "research"

# Retention policy
LOG_RETENTION_DAYS = 3          # keep last 3 days of logs
RESEARCH_RETENTION_DAYS = 2     # keep last 2 days of research data
TMP_MAX_AGE_HOURS = 6           # delete tmp files older than 6 hours


def cleanup_old_logs(retention_days: int = LOG_RETENTION_DAYS) -> dict:
    """Delete log directories older than retention_days.

    Log structure: logs/YYYY-MM-DD/app.log
    Removes entire day-directories older than the cutoff.
    """
    deleted = 0
    freed_mb = 0.0

    if not LOG_DIR.exists():
        return {"deleted": 0, "freed_mb": 0.0, "error": "log dir not found"}

    cutoff = date.today() - timedelta(days=retention_days)

    for day_dir in LOG_DIR.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            dir_date = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dir_date < cutoff:
            size_mb = sum(
                f.stat().st_size for f in day_dir.rglob("*") if f.is_file()
            ) / (1024 * 1024)
            try:
                shutil.rmtree(day_dir)
                deleted += 1
                freed_mb += size_mb
                logger.info(
                    "Cleanup: deleted log dir %s (%.1f MB)", day_dir.name, size_mb)
            except Exception as exc:
                logger.warning("Cleanup: failed to delete %s: %s", day_dir, exc)

    return {"deleted": deleted, "freed_mb": round(freed_mb, 1)}


def cleanup_tmp_files(max_age_hours: int = TMP_MAX_AGE_HOURS) -> dict:
    """Delete temporary files older than max_age_hours."""
    deleted = 0
    freed_mb = 0.0

    for tmp_path in (TMP_DIR, RESEARCH_DIR):
        if not tmp_path.exists():
            continue
        cutoff_time = time.time() - (max_age_hours * 3600)
        for f in tmp_path.rglob("*"):
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime < cutoff_time:
                    size_mb = f.stat().st_size / (1024 * 1024)
                    f.unlink()
                    deleted += 1
                    freed_mb += size_mb
            except Exception as exc:
                logger.debug("Cleanup: skip %s: %s", f, exc)

    if deleted > 0:
        logger.info(
            "Cleanup: deleted %d temp files (%.1f MB freed)", deleted, freed_mb)

    return {"deleted": deleted, "freed_mb": round(freed_mb, 1)}


def cleanup_research_data(retention_days: int = RESEARCH_RETENTION_DAYS) -> dict:
    """Delete stale research data older than retention_days."""
    deleted = 0
    freed_mb = 0.0

    if not RESEARCH_DIR.exists():
        return {"deleted": 0, "freed_mb": 0.0}

    cutoff = date.today() - timedelta(days=retention_days)

    for item in RESEARCH_DIR.iterdir():
        try:
            mtime = datetime.fromtimestamp(item.stat().st_mtime).date()
            if mtime < cutoff:
                if item.is_file():
                    size_mb = item.stat().st_size / (1024 * 1024)
                    item.unlink()
                    deleted += 1
                    freed_mb += size_mb
                elif item.is_dir():
                    size_mb = sum(
                        f.stat().st_size for f in item.rglob("*") if f.is_file()
                    ) / (1024 * 1024)
                    shutil.rmtree(item)
                    deleted += 1
                    freed_mb += size_mb
        except Exception as exc:
            logger.debug("Cleanup: skip research %s: %s", item, exc)

    if deleted > 0:
        logger.info(
            "Cleanup: deleted %d research items (%.1f MB freed)",
            deleted, freed_mb)

    return {"deleted": deleted, "freed_mb": round(freed_mb, 1)}


def disk_usage_summary() -> dict:
    """Return disk usage summary for the repo root."""
    try:
        total, used, free = shutil.disk_usage(REPO_ROOT)
        return {
            "total_gb": round(total / (1024**3), 1),
            "used_gb": round(used / (1024**3), 1),
            "free_gb": round(free / (1024**3), 1),
            "free_pct": round(free / total * 100, 1) if total > 0 else 0.0,
        }
    except Exception as exc:
        logger.warning("Disk usage check failed: %s", exc)
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "free_pct": 0.0}


def run_daily_cleanup(broker=None) -> dict:
    """Execute the full daily cleanup sequence.

    Args:
        broker: AngelBroker instance. If provided, triggers a fresh
            Scrip Master download via data.loader.

    Returns:
        Summary dict with all cleanup results + disk usage.
    """
    logger.info("=" * 60)
    logger.info("DAILY RESEARCH CLEANUP — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 60)

    results = {}

    # 1. Refresh Scrip Master
    if broker is not None:
        try:
            from data.loader import load_angel_instrument_master
            count = load_angel_instrument_master(broker, force_refresh=True)
            results["scrip_master_instruments"] = count
            logger.info("Scrip Master refreshed: %d instruments", count)
        except Exception as exc:
            logger.error("Scrip Master refresh failed: %s", exc)
            results["scrip_master_error"] = str(exc)

    # 2. Purge old logs
    results["logs"] = cleanup_old_logs()

    # 3. Clean temp files
    results["tmp"] = cleanup_tmp_files()

    # 4. Clean research data
    results["research"] = cleanup_research_data()

    # 5. Disk usage
    results["disk"] = disk_usage_summary()

    total_freed = (
        results["logs"]["freed_mb"]
        + results["tmp"]["freed_mb"]
        + results["research"]["freed_mb"]
    )
    results["total_freed_mb"] = round(total_freed, 1)

    logger.info(
        "Cleanup complete: %.1f MB freed | Disk: %.1f/%.1f GB free (%.1f%%)",
        total_freed,
        results["disk"]["free_gb"],
        results["disk"]["total_gb"],
        results["disk"]["free_pct"],
    )
    logger.info("=" * 60)

    return results


if __name__ == "__main__":
    result = run_daily_cleanup()
    for k, v in result.items():
        print(f"  {k}: {v}")
