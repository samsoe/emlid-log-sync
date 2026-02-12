#!/usr/bin/env python3
"""
Sync RTCM3 correction logs from Emlid GNSS base station to Google Cloud Storage.

Workflow:
1. Connect to Emlid device via SFTP (requires WARP VPN)
2. Download ZIP archives
3. Extract .RTCM3 files
4. Upload to GCS
5. Clean up local files
"""

import argparse
import fnmatch
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import yaml

from unpack_log import (
    parse_rtcm3,
    parse_filename_timestamp,
    _gps_day_of_week,
    gws_to_timestamp,
    ecef_to_geodetic,
    _detect_position_change,
    detect_data_gaps,
    _sat_counts_for_epoch,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _format_gnss_timestamp(dt):
    """Format datetime as YYYY:MM:DD:HH:MM:SS."""
    if dt is None:
        return None
    return dt.strftime("%Y:%m:%d:%H:%M:%S")


def build_status_json(parse_result, filepath):
    """Build lightweight status report dict from parsed RTCM3 data.

    Returns a dict with: file, generated_utc, time_span, position,
    satellites, outages.
    """
    file_date = parse_filename_timestamp(filepath)
    gps_day = _gps_day_of_week(file_date) if file_date else 0
    epoch_keys = sorted(parse_result.epochs.keys())

    # Time span
    if epoch_keys:
        start_ts = gws_to_timestamp(epoch_keys[0], file_date, gps_day)
        end_ts = gws_to_timestamp(epoch_keys[-1], file_date, gps_day)
        time_span = {
            "start": _format_gnss_timestamp(start_ts),
            "end": _format_gnss_timestamp(end_ts),
            "duration_sec": epoch_keys[-1] - epoch_keys[0],
        }
    else:
        time_span = {"start": None, "end": None, "duration_sec": 0}

    # Position
    position = None
    if parse_result.positions:
        pos_info = _detect_position_change(parse_result.positions)

        def _ecef_to_pos_dict(p):
            x, y, z = p["ecef_x"], p["ecef_y"], p["ecef_z"]
            if x is None or y is None or z is None:
                return None
            lat, lon, height = ecef_to_geodetic(x, y, z)
            return {
                "lat_deg": round(lat, 8),
                "lon_deg": round(lon, 8),
                "height_hae_m": round(height, 2),
            }

        init = _ecef_to_pos_dict(parse_result.positions[0])
        final = _ecef_to_pos_dict(parse_result.positions[-1])
        if init:
            position = {
                "status": "STABLE" if pos_info["stable"] else "MOVED",
                "spread_m": round(pos_info["spread_m"], 4),
                "position_init": init,
                "position_final": final,
            }

    # Satellites (min/max total across all epochs)
    if epoch_keys:
        totals = [
            sum(_sat_counts_for_epoch(parse_result.epochs[k]).values())
            for k in epoch_keys
        ]
        satellites = {"min": min(totals), "max": max(totals)}
    else:
        satellites = {"min": 0, "max": 0}

    # Outages
    gaps = detect_data_gaps(epoch_keys)
    outages = []
    for gap in gaps:
        start_t = gws_to_timestamp(gap["start_gws"], file_date, gps_day)
        end_t = gws_to_timestamp(gap["end_gws"], file_date, gps_day)
        outages.append({
            "start": _format_gnss_timestamp(start_t),
            "end": _format_gnss_timestamp(end_t),
        })

    return {
        "file": os.path.basename(filepath),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_span": time_span,
        "position": position,
        "satellites": satellites,
        "outages": outages,
    }


def load_config(config_path: str) -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def connect_sftp(config: dict) -> paramiko.SFTPClient:
    """Establish SFTP connection to Emlid device."""
    emlid = config["emlid"]
    logger.info(f"Connecting to {emlid['host']}:{emlid['port']}...")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=emlid["host"],
        port=emlid["port"],
        username=emlid["username"],
        password=emlid["password"],
        timeout=30
    )

    sftp = ssh.open_sftp()
    logger.info("SFTP connection established")
    return sftp, ssh


def list_remote_zips(sftp: paramiko.SFTPClient, remote_path: str, pattern: str) -> list[str]:
    """List ZIP files on remote device matching pattern."""
    files = sftp.listdir(remote_path)
    zips = [f for f in files if fnmatch.fnmatch(f, pattern)]
    logger.info(f"Found {len(zips)} ZIP files on device")
    return sorted(zips)


def list_gcs_files(bucket_name: str, prefix: str) -> set[str]:
    """List existing files in GCS bucket."""
    gcs_path = f"gs://{bucket_name}/{prefix}"
    cmd = ["gcloud", "storage", "ls", gcs_path]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.warning(f"Could not list GCS files: {result.stderr}")
        return set()

    # Extract just filenames from full paths
    files = set()
    for line in result.stdout.strip().split("\n"):
        if line:
            filename = line.split("/")[-1]
            if filename:
                files.add(filename)

    logger.info(f"Found {len(files)} existing files in GCS")
    return files


def predict_rtcm3_name(zip_name: str) -> str:
    """Predict RTCM3 filename from ZIP name.

    Pattern: TOP_HOUSE_B_20260107174243.zip -> TOP_HOUSE_B_base_20260107174243.RTCM3
    """
    base_name = zip_name.replace(".zip", "")
    # Insert 'base_' after the last underscore before the timestamp
    parts = base_name.rsplit("_", 1)
    if len(parts) == 2:
        return f"{parts[0]}_base_{parts[1]}.RTCM3"
    return f"{base_name}_base.RTCM3"


def download_zip(sftp: paramiko.SFTPClient, remote_path: str, filename: str, local_dir: Path) -> Path:
    """Download a ZIP file from remote device."""
    remote_file = f"{remote_path}/{filename}"
    local_file = local_dir / filename

    logger.info(f"Downloading {filename}...")
    sftp.get(remote_file, str(local_file))

    size_mb = local_file.stat().st_size / (1024 * 1024)
    logger.info(f"Downloaded {filename} ({size_mb:.1f} MB)")
    return local_file


def extract_rtcm3_files(zip_path: Path, extract_dir: Path) -> list[Path]:
    """Extract .RTCM3 files from ZIP archive."""
    rtcm3_files = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.upper().endswith(".RTCM3"):
                zf.extract(name, extract_dir)
                extracted = extract_dir / name
                rtcm3_files.append(extracted)
                logger.info(f"Extracted: {name}")

    logger.info(f"Extracted {len(rtcm3_files)} RTCM3 files")
    return rtcm3_files


def upload_to_gcs(files: list[Path], bucket_name: str, prefix: str) -> int:
    """Upload files to Google Cloud Storage using gcloud CLI."""
    uploaded = 0

    for file_path in files:
        gcs_path = f"gs://{bucket_name}/{prefix}{file_path.name}"

        # Check if file already exists
        check_cmd = ["gcloud", "storage", "ls", gcs_path]
        result = subprocess.run(check_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            logger.info(f"Skipping {file_path.name} (already exists in GCS)")
            continue

        logger.info(f"Uploading {file_path.name} to {gcs_path}")
        upload_cmd = ["gcloud", "storage", "cp", str(file_path), gcs_path]
        result = subprocess.run(upload_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"Upload failed: {result.stderr}")
            raise RuntimeError(f"Failed to upload {file_path.name}")

        uploaded += 1

    logger.info(f"Uploaded {uploaded} files to GCS")
    return uploaded


def cleanup_local(temp_dir: Path) -> None:
    """Remove temporary directory and contents."""
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
        logger.info(f"Cleaned up {temp_dir}")


def sync_logs(config: dict, dry_run: bool = False, limit: int = 0) -> None:
    """Main sync workflow."""
    temp_dir = Path(config["local"]["temp_dir"]).resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)

    sftp = None
    ssh = None

    try:
        # List existing files in GCS to avoid re-downloading
        existing_files = list_gcs_files(config["gcs"]["bucket"], config["gcs"]["prefix"])

        sftp, ssh = connect_sftp(config)

        remote_path = config["emlid"]["log_path"]
        pattern = config["options"]["file_pattern"]
        zip_files = list_remote_zips(sftp, remote_path, pattern)

        if not zip_files:
            logger.info("No ZIP files found to process")
            return

        if limit > 0:
            zip_files = zip_files[:limit]
            logger.info(f"Limited to {limit} file(s)")

        for zip_name in zip_files:
            logger.info(f"\n{'='*50}\nProcessing: {zip_name}\n{'='*50}")

            # Check if already synced by predicting RTCM3 filename
            predicted_name = predict_rtcm3_name(zip_name)
            if predicted_name in existing_files:
                logger.info(f"Skipping {zip_name} (already synced: {predicted_name})")
                continue

            # Download ZIP
            zip_path = download_zip(sftp, remote_path, zip_name, temp_dir)

            # Extract RTCM3 files
            extract_dir = temp_dir / zip_name.replace(".zip", "")
            extract_dir.mkdir(exist_ok=True)
            rtcm3_files = extract_rtcm3_files(zip_path, extract_dir)

            if not rtcm3_files:
                logger.warning(f"No RTCM3 files found in {zip_name}")
                continue

            # Generate status JSON for each RTCM3 file
            for rtcm3_file in rtcm3_files:
                try:
                    logger.info(f"Generating status report for {rtcm3_file.name}...")
                    pr = parse_rtcm3(str(rtcm3_file))
                    status = build_status_json(pr, str(rtcm3_file))
                    json_path = rtcm3_file.parent / (rtcm3_file.stem + ".status.json")
                    with open(json_path, "w") as jf:
                        json.dump(status, jf, indent=2)
                    logger.info(f"Wrote {json_path}")
                except Exception as e:
                    logger.warning(f"Status report failed for {rtcm3_file.name}: {e}")

            if dry_run:
                dry_run_dir = Path("dry_run")
                dry_run_dir.mkdir(exist_ok=True)
                for f in extract_dir.iterdir():
                    shutil.copy2(f, dry_run_dir / f.name)
                logger.info(f"[DRY RUN] Output in {dry_run_dir.resolve()}")
                continue

            # Upload RTCM3 + status JSON to GCS
            json_files = list(extract_dir.glob("*.status.json"))
            upload_to_gcs(
                rtcm3_files + json_files,
                config["gcs"]["bucket"],
                config["gcs"]["prefix"]
            )

            # Cleanup after successful upload
            if config["options"]["delete_after_upload"]:
                zip_path.unlink()
                shutil.rmtree(extract_dir)
                logger.info(f"Deleted local files for {zip_name}")

    finally:
        if sftp:
            sftp.close()
        if ssh:
            ssh.close()
        logger.info("SFTP connection closed")

        # Final cleanup of temp dir if empty
        if temp_dir.exists() and not any(temp_dir.iterdir()):
            temp_dir.rmdir()


def main():
    parser = argparse.ArgumentParser(
        description="Sync RTCM3 logs from Emlid device to GCS"
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pull from base and generate status JSON locally, skip GCS upload"
    )
    parser.add_argument(
        "--status",
        metavar="RTCM3_FILE",
        help="Generate .status.json for a local RTCM3 file (no sync)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=0,
        help="Limit number of files to process (0 = all)"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Local status JSON generation — no config or network needed
    if args.status:
        rtcm3_path = Path(args.status)
        if not rtcm3_path.is_file():
            logger.error(f"File not found: {rtcm3_path}")
            sys.exit(1)
        logger.info(f"Parsing {rtcm3_path.name}...")
        pr = parse_rtcm3(str(rtcm3_path))
        status = build_status_json(pr, str(rtcm3_path))
        json_path = rtcm3_path.parent / (rtcm3_path.stem + ".status.json")
        with open(json_path, "w") as f:
            json.dump(status, f, indent=2)
        logger.info(f"Wrote {json_path}")
        return

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        logger.error("Copy config.example.yaml to config.yaml and update values")
        sys.exit(1)

    config = load_config(str(config_path))

    logger.info("Starting Emlid log sync...")
    sync_logs(config, dry_run=args.dry_run, limit=args.limit)
    logger.info("Sync complete!")


if __name__ == "__main__":
    main()
