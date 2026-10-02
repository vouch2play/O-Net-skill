# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""
O*NET Database Update Tool

Check for new O*NET database releases and download updated Excel files.
Tracks the current version in references/.version.

Usage:
    uv run scripts/onet_update.py              # Check + download if newer
    uv run scripts/onet_update.py --check      # Check only, don't download
    uv run scripts/onet_update.py --force       # Re-download current version
    uv run scripts/onet_update.py --version     # Show current local version
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"
VERSION_FILE = REFERENCES_DIR / ".version"

RSS_URL = "https://www.onetcenter.org/rss/whatsnew.xml"
DATABASE_PAGE_URL = "https://www.onetcenter.org/database.html"
DOWNLOAD_BASE = "https://www.onetcenter.org/dl_files/database"

# Current O*NET Excel files (31.0+). Used if the zip download is unavailable.
# 30.3 renamed/split several files and dropped Tools Used, Work Values, and
# UNSPSC Reference. See https://www.onetcenter.org/dictionary/current/excel/
DATABASE_FILES = [
    "Abilities to Work Activities.xlsx",
    "Abilities to Work Context.xlsx",
    "Abilities.xlsx",
    "Career Interest Type Keywords.xlsx",
    "Career Interest Types.xlsx",
    "Content Model Reference.xlsx",
    "Education Categories.xlsx",
    "Education.xlsx",
    "Emerging Tasks.xlsx",
    "Essential Skills to Work Activities.xlsx",
    "Essential Skills to Work Context.xlsx",
    "Essential Skills.xlsx",
    "GWAs to IWAs to DWAs.xlsx",
    "GWAs to IWAs.xlsx",
    "Interests Illustrative Activities.xlsx",
    "Interests Illustrative Occupations.xlsx",
    "Job Titles.xlsx",
    "Job Zone Reference.xlsx",
    "Job Zones.xlsx",
    "Knowledge.xlsx",
    "Level Scale Anchors.xlsx",
    "Occupation Data.xlsx",
    "Occupation Level Metadata.xlsx",
    "Related Occupations.xlsx",
    "Sample of Reported Titles.xlsx",
    "Scales Reference.xlsx",
    "Software Skills.xlsx",
    "Specific Interest Areas to Career Interest Types.xlsx",
    "Specific Interest Areas.xlsx",
    "Survey Booklet Locations.xlsx",
    "Task Categories.xlsx",
    "Task Ratings.xlsx",
    "Task Statements.xlsx",
    "Tasks to DWAs.xlsx",
    "Training and Experience Categories.xlsx",
    "Training and Experience.xlsx",
    "Transferable Skills to Work Activities.xlsx",
    "Transferable Skills to Work Context.xlsx",
    "Transferable Skills.xlsx",
    "Work Activities.xlsx",
    "Work Context Categories.xlsx",
    "Work Context.xlsx",
    "Work Styles to Work Activities.xlsx",
    "Work Styles to Work Context.xlsx",
    "Work Styles.xlsx",
]

# Last published in 30.2; no longer included in current releases.
LEGACY_FILES = [
    "Tools Used.xlsx",
    "Work Values.xlsx",
]
LEGACY_VERSION = "30.2"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "onet-skill-updater/1.0"})


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def _version_to_path_segment(version: str) -> str:
    """Convert '30.2' to 'db_30_2'."""
    return "db_" + version.replace(".", "_")


def get_local_version() -> str | None:
    """Read the locally stored version from references/.version."""
    if VERSION_FILE.exists():
        text = VERSION_FILE.read_text().strip()
        return text if text else None
    return None


def _set_local_version(version: str) -> None:
    """Write the version to references/.version."""
    VERSION_FILE.write_text(version + "\n")


def detect_latest_version_rss() -> str | None:
    """Detect latest O*NET database version from the What's New RSS feed."""
    try:
        resp = SESSION.get(RSS_URL, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  Warning: RSS feed unavailable ({exc})", file=sys.stderr)
        return None

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return None

    # Look through items for a database release announcement.
    for item in root.findall("./channel/item"):
        title_el = item.find("title")
        desc_el = item.find("description")
        text = ""
        if title_el is not None and title_el.text:
            text += title_el.text
        if desc_el is not None and desc_el.text:
            text += " " + desc_el.text

        match = re.search(r"O\*?NET\s+(\d+\.\d+)\s+Database", text)
        if match:
            return match.group(1)

    return None


def detect_latest_version_page() -> str | None:
    """Detect latest version by scraping the database page header."""
    try:
        resp = SESSION.get(DATABASE_PAGE_URL, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  Warning: Database page unavailable ({exc})", file=sys.stderr)
        return None

    match = re.search(r"O\*?NET\s+(\d+\.\d+)\s+Database", resp.text)
    if match:
        return match.group(1)
    return None


def detect_latest_version() -> str | None:
    """Detect the latest available O*NET database version.

    Tries RSS first (lightweight), falls back to page scraping.
    """
    print("Checking for latest O*NET database version...")

    version = detect_latest_version_rss()
    if version:
        print(f"  Found version {version} via RSS feed")
        return version

    version = detect_latest_version_page()
    if version:
        print(f"  Found version {version} via database page")
        return version

    print("  Error: Could not detect latest version from any source", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download_url(version: str, filename: str) -> str:
    """Build the download URL for a specific file and version."""
    segment = _version_to_path_segment(version)
    encoded = quote(filename)
    return f"{DOWNLOAD_BASE}/{segment}_excel/{encoded}"


def _cleanup_temp_files() -> None:
    for tmp in REFERENCES_DIR.glob("*.tmp"):
        try:
            tmp.unlink()
        except OSError:
            pass


def _atomic_replace(tmp: Path, dest: Path) -> None:
    """Replace dest with tmp. os.replace overwrites on Windows; Path.rename does not."""
    tmp.replace(dest)


def download_file(version: str, filename: str) -> bool:
    """Download a single file. Returns True on success."""
    url = _download_url(version, filename)
    dest = REFERENCES_DIR / filename

    try:
        resp = SESSION.get(url, timeout=60, stream=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  FAILED: {filename} ({exc})", file=sys.stderr)
        return False

    tmp = dest.with_suffix(".tmp")
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        _atomic_replace(tmp, dest)
        return True
    except OSError as exc:
        print(f"  FAILED to write {filename}: {exc}", file=sys.stderr)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False


def _is_appendix(filename: str) -> bool:
    return filename.lower().startswith("appendix")


def download_excel_zip(version: str) -> tuple[list[str], list[str]] | None:
    """Download the Excel zip for a version and extract database xlsx files.

    Returns (extracted, failed) filenames, or None if the zip is unavailable.
    """
    segment = _version_to_path_segment(version)
    url = f"{DOWNLOAD_BASE}/{segment}_excel.zip"
    zip_tmp = REFERENCES_DIR / f"{segment}_excel.zip.tmp"

    print(f"  Downloading {segment}_excel.zip...", end="", flush=True)
    try:
        resp = SESSION.get(url, timeout=180, stream=True)
        resp.raise_for_status()
        with open(zip_tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=262144):
                f.write(chunk)
    except requests.RequestException as exc:
        print(f" FAILED ({exc})", file=sys.stderr)
        if zip_tmp.exists():
            zip_tmp.unlink(missing_ok=True)
        return None

    extracted: list[str] = []
    failed: list[str] = []
    try:
        with ZipFile(zip_tmp) as zf:
            members = [
                info
                for info in zf.infolist()
                if Path(info.filename).name.lower().endswith(".xlsx")
                and not _is_appendix(Path(info.filename).name)
            ]
            total = len(members)
            print(f" OK ({total} Excel files)")
            for i, info in enumerate(members, 1):
                name = Path(info.filename).name
                pct = i * 100 // total if total else 100
                print(f"  [{pct:3d}%] Extracting {name}...", end="", flush=True)
                dest = REFERENCES_DIR / name
                tmp = dest.with_suffix(".tmp")
                try:
                    with zf.open(info) as src, open(tmp, "wb") as out:
                        shutil.copyfileobj(src, out)
                    _atomic_replace(tmp, dest)
                    print(" OK")
                    extracted.append(name)
                except OSError as exc:
                    print(f" FAILED ({exc})", file=sys.stderr)
                    if tmp.exists():
                        tmp.unlink(missing_ok=True)
                    if dest.exists() and dest.stat().st_size > 0:
                        print(f"    keeping existing {name}")
                        extracted.append(name)
                    else:
                        failed.append(name)
        return extracted, failed
    except Exception as exc:
        print(f" FAILED to extract ({exc})", file=sys.stderr)
        return None
    finally:
        if zip_tmp.exists():
            zip_tmp.unlink(missing_ok=True)


def download_excel_files(version: str, filenames: list[str]) -> tuple[list[str], list[str]]:
    """Download individual Excel files. Returns (succeeded, failed)."""
    succeeded: list[str] = []
    failed: list[str] = []
    total = len(filenames)
    for i, filename in enumerate(filenames, 1):
        pct = i * 100 // total
        print(f"  [{pct:3d}%] Downloading {filename}...", end="", flush=True)
        if download_file(version, filename):
            print(" OK")
            succeeded.append(filename)
        else:
            print(" FAILED")
            failed.append(filename)
        if i < total:
            time.sleep(0.2)
    return succeeded, failed


def download_all(version: str) -> tuple[int, int]:
    """Download all database files for a given version.

    Prefers the official Excel zip (always matches the current file names),
    then falls back to per-file downloads. Also pulls last-published copies of
    files that O*NET dropped after 30.2.

    Returns (success_count, failure_count).
    """
    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_temp_files()

    zip_result = download_excel_zip(version)
    if zip_result is None:
        print("  Zip download unavailable; falling back to individual files.")
        extracted, failed = download_excel_files(version, DATABASE_FILES)
    else:
        extracted, failed = zip_result

    keep = set(extracted)

    print("\nDownloading optional legacy files last published in 30.2...")
    for filename in LEGACY_FILES:
        dest = REFERENCES_DIR / filename
        print(f"  Downloading {filename} (from {LEGACY_VERSION})...", end="", flush=True)
        if download_file(LEGACY_VERSION, filename):
            print(" OK")
            keep.add(filename)
        elif dest.exists() and dest.stat().st_size > 0:
            print(" FAILED; keeping existing copy")
            keep.add(filename)
        else:
            print(" FAILED (optional, continuing)")

    for path in REFERENCES_DIR.glob("*.xlsx"):
        if path.name not in keep:
            print(f"  Removing obsolete file: {path.name}")
            path.unlink()

    required = REFERENCES_DIR / "Occupation Data.xlsx"
    if not required.exists():
        print("  Error: Occupation Data.xlsx is missing after download.", file=sys.stderr)
        return len(keep), max(len(failed), 1)

    return len(keep), len(failed)


# ---------------------------------------------------------------------------
# Database rebuild
# ---------------------------------------------------------------------------

BUILD_SCRIPT = Path(__file__).resolve().parent / "onet_build_db.py"


def _rebuild_database() -> None:
    """Rebuild the SQLite database from downloaded Excel files."""
    if not BUILD_SCRIPT.exists():
        print(
            f"  Warning: Build script not found at {BUILD_SCRIPT}. "
            f"Run manually: uv run scripts/onet_build_db.py",
            file=sys.stderr,
        )
        return

    print("\nRebuilding SQLite database...")
    result = subprocess.run(
        ["uv", "run", str(BUILD_SCRIPT)],
        cwd=str(BUILD_SCRIPT.parent.parent),
    )
    if result.returncode != 0:
        print(
            "  Warning: Database rebuild failed. "
            "Run manually: uv run scripts/onet_build_db.py",
            file=sys.stderr,
        )
    else:
        print("Database rebuild complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check for and download O*NET database updates.",
        epilog="Examples:\n"
        "  uv run scripts/onet_update.py              # Update to latest\n"
        "  uv run scripts/onet_update.py --check       # Check only\n"
        "  uv run scripts/onet_update.py --force        # Re-download\n"
        "  uv run scripts/onet_update.py --version      # Show local version\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for updates without downloading",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if already at latest version",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        dest="show_version",
        help="Show current local database version and exit",
    )
    parser.add_argument(
        "--set-version",
        metavar="VER",
        help="Manually set the local version (e.g., after a manual download)",
    )

    args = parser.parse_args()

    # --- Show version ---
    if args.show_version:
        local = get_local_version()
        if local:
            print(f"O*NET database version: {local}")
        else:
            print("No version file found. Run an update first.")
        sys.exit(0)

    # --- Set version manually ---
    if args.set_version:
        if not re.match(r"^\d+\.\d+$", args.set_version):
            print(f"Error: Invalid version format '{args.set_version}'. Expected X.Y (e.g., 30.2)",
                  file=sys.stderr)
            sys.exit(1)
        _set_local_version(args.set_version)
        print(f"Local version set to {args.set_version}")
        sys.exit(0)

    # --- Check / Update ---
    local = get_local_version()
    if local:
        print(f"Current local version: {local}")
    else:
        print("No local version found (first run)")

    latest = detect_latest_version()
    if not latest:
        sys.exit(1)

    if local == latest and not args.force:
        print(f"\nAlready up to date (version {latest}). Use --force to re-download.")
        sys.exit(0)

    if local and local != latest:
        print(f"\nNew version available: {latest} (current: {local})")
    elif args.force:
        print(f"\nForce re-downloading version {latest}...")

    if args.check:
        print("Run without --check to download.")
        sys.exit(0)

    # --- Download ---
    print(f"\nDownloading O*NET {latest} database...")
    print()

    success, failed = download_all(latest)
    print()

    if failed == 0:
        _set_local_version(latest)
        print(f"Update complete. Version: {latest}")
        print(f"  {success} files downloaded to {REFERENCES_DIR}/")
        _rebuild_database()
    else:
        print(f"Update finished with errors: {success} OK, {failed} failed.", file=sys.stderr)
        print("Version file NOT updated due to failures. Re-run to retry.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
