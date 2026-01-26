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
import logging
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import paramiko
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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

            if dry_run:
                logger.info(f"[DRY RUN] Would download and process {zip_name}")
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

            # Upload to GCS
            upload_to_gcs(
                rtcm3_files,
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
        help="List files without downloading or uploading"
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
