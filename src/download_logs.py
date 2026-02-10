#!/usr/bin/env python3
"""
Download RTCM3 logs from GCS for a given date range and station.

Usage:
    python download_logs.py --start 2026-01-20 --end 2026-01-25 --station top_house
    python download_logs.py --date 2026-01-20 --station top_house  # single day
    python download_logs.py --list --start 2026-01-20 --end 2026-01-25  # list only
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from google.cloud import storage


def load_config(config_path: str = "download_config.yaml") -> dict:
    """Load configuration from YAML file."""
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Copy download_config.example.yaml to download_config.yaml and update values")
        sys.exit(1)

    with open(config_file, "r") as f:
        return yaml.safe_load(f)


def parse_date(date_str: str) -> datetime:
    """Parse date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Use YYYY-MM-DD")


def generate_date_range(start_date: datetime, end_date: datetime) -> list[datetime]:
    """Generate list of dates between start and end (inclusive)."""
    dates = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def list_logs(
    bucket_name: str,
    prefix: str,
    start_date: datetime,
    end_date: datetime
) -> list[storage.Blob]:
    """List RTCM3 files in GCS matching the date range."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    # List all blobs with the prefix
    blobs = bucket.list_blobs(prefix=prefix)

    # Filter by date range
    # Files are named like: TOP_HOUSE_B_base_20260120174253.RTCM3
    matching_blobs = []
    for blob in blobs:
        if not blob.name.endswith(".RTCM3"):
            continue

        # Extract date from filename (format: *_YYYYMMDDHHMMSS.RTCM3)
        try:
            filename = blob.name.split("/")[-1]
            # Get the timestamp part before .RTCM3
            timestamp_str = filename.split("_")[-1].replace(".RTCM3", "")
            file_date = datetime.strptime(timestamp_str[:8], "%Y%m%d")

            if start_date <= file_date <= end_date:
                matching_blobs.append(blob)
        except (ValueError, IndexError):
            # Skip files that don't match expected naming pattern
            continue

    return sorted(matching_blobs, key=lambda b: b.name)


def download_blob(blob: storage.Blob, output_dir: Path) -> None:
    """Download a single blob to the output directory."""
    filename = blob.name.split("/")[-1]
    output_path = output_dir / filename

    print(f"Downloading: {filename} ({blob.size / (1024*1024):.1f} MB)")
    blob.download_to_filename(str(output_path))
    print(f"  → Saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Download RTCM3 logs from GCS by date range and station"
    )
    parser.add_argument(
        "--start",
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end",
        help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--date",
        help="Single date (YYYY-MM-DD) - shortcut for --start and --end on same day"
    )
    parser.add_argument(
        "--station",
        help="Station name (uses config default if not specified)"
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output directory for downloads (uses config default if not specified)"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List matching files without downloading"
    )
    parser.add_argument(
        "--config",
        "-c",
        default="download_config.yaml",
        help="Path to config file (default: download_config.yaml)"
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)

    # Get values from config with command-line overrides
    station = args.station or config.get("default_station", "station")
    output_dir_str = args.output or config.get("output_dir", "./downloaded_logs")
    gcs_bucket = config["gcs"]["bucket"]
    gcs_prefix_template = config["gcs"]["prefix_template"]

    # Parse dates
    if args.date:
        start_date = end_date = parse_date(args.date)
    elif args.start and args.end:
        start_date = parse_date(args.start)
        end_date = parse_date(args.end)
    elif args.start:
        start_date = end_date = parse_date(args.start)
    else:
        print("Error: Must specify --date or --start (and optionally --end)")
        sys.exit(1)

    # Validate date range
    if start_date > end_date:
        print("Error: Start date must be before or equal to end date")
        sys.exit(1)

    # Build GCS prefix
    prefix = gcs_prefix_template.format(station=station)

    print(f"Searching for logs:")
    print(f"  Station: {station}")
    print(f"  Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"  GCS location: gs://{gcs_bucket}/{prefix}")
    print()

    # List matching files
    try:
        blobs = list_logs(gcs_bucket, prefix, start_date, end_date)
    except Exception as e:
        print(f"Error listing files: {e}")
        sys.exit(1)

    if not blobs:
        print("No matching files found.")
        return

    print(f"Found {len(blobs)} file(s):")
    total_size = 0
    for blob in blobs:
        size_mb = blob.size / (1024 * 1024)
        total_size += size_mb
        print(f"  - {blob.name.split('/')[-1]} ({size_mb:.1f} MB)")

    print(f"\nTotal size: {total_size:.1f} MB")

    if args.list:
        print("\n(List-only mode - no files downloaded)")
        return

    # Download files
    output_dir = Path(output_dir_str)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nDownloading to: {output_dir.absolute()}")
    print()

    for i, blob in enumerate(blobs, 1):
        print(f"[{i}/{len(blobs)}]", end=" ")
        try:
            download_blob(blob, output_dir)
        except Exception as e:
            print(f"  ✗ Error downloading {blob.name}: {e}")
            continue

    print(f"\n✓ Downloaded {len(blobs)} file(s) to {output_dir.absolute()}")


if __name__ == "__main__":
    main()
