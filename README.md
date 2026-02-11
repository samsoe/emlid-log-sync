# Emlid Log Sync

Local utility for syncing RTCM3 correction logs from Emlid GNSS base stations to Google Cloud Storage.

## Prerequisites

- Network access to your Emlid device
- Google Cloud SDK (`gcloud`) installed and authenticated
- Python 3.11+

## Setup

1. **Create conda environment:**

   ```bash
   conda env create -f environment.yml
   conda activate emlid-log-sync
   ```

   Or with pip:

   ```bash
   pip install -r requirements.txt
   ```

2. **Configure GCS credentials:**

   ```bash
   gcloud auth application-default login
   ```

3. **Create config file:**

   ```bash
   cp config.example.yaml config.yaml
   ```

   Edit `config.yaml` with your specific settings (especially the Emlid password if changed from default).

## Usage

**Basic sync:**

```bash
python src/sync_emlid_logs.py
```

**Dry run (list files without downloading):**

```bash
python src/sync_emlid_logs.py --dry-run
```

**Custom config file:**

```bash
python src/sync_emlid_logs.py -c /path/to/config.yaml
```

**Verbose output:**

```bash
python src/sync_emlid_logs.py -v
```

**Limit number of files (for testing):**

```bash
python src/sync_emlid_logs.py --limit 1
```

## Workflow

1. Connects to Emlid device via SFTP
2. Lists ZIP archives in the device's log directory
3. Downloads each ZIP to a local temp directory
4. Extracts `.RTCM3` files
5. Parses each RTCM3 and generates a `.status.json` health summary alongside it
6. Uploads both files to GCS (skips files that already exist)
7. Deletes local ZIP and extracted files after successful upload

### Status JSON

Each synced RTCM3 file gets a companion `.status.json` with position, satellite counts, and data outages:

```
gs://your-bucket/your-prefix/
  ├── TOP_HOUSE_B_base_20260120174253.RTCM3
  ├── TOP_HOUSE_B_base_20260120174253.status.json
  ├── TOP_HOUSE_B_base_20260121174254.RTCM3
  ├── TOP_HOUSE_B_base_20260121174254.status.json
  └── ...
```

Example `.status.json`:

```json
{
  "file": "TOP_HOUSE_B_base_20260120174253.RTCM3",
  "generated_utc": "2026-02-10T15:30:00Z",
  "time_span": {
    "start": "2026:01:20:17:42:54",
    "end": "2026:01:21:17:42:54",
    "duration_sec": 86400
  },
  "position": {
    "status": "STABLE",
    "spread_m": 0.0,
    "position_init": {
      "lat_deg": 46.76284659,
      "lon_deg": -114.09685335,
      "height_hae_m": 1109.94
    },
    "position_final": {
      "lat_deg": 46.76284659,
      "lon_deg": -114.09685335,
      "height_hae_m": 1109.94
    }
  },
  "satellites": {
    "min": 14,
    "max": 20
  },
  "outages": [
    { "start": "2026:01:21:05:19:36", "end": "2026:01:21:05:20:02" },
    { "start": "2026:01:21:10:33:25", "end": "2026:01:21:10:33:51" }
  ]
}
```

You can also generate a status JSON for a local file without running a full sync:

```bash
python src/sync_emlid_logs.py --status path/to/file.RTCM3
```

## Configuration

See `config.example.yaml` for all available options:

| Section | Key | Description |
|---------|-----|-------------|
| `emlid.host` | IP address of Emlid device |
| `emlid.log_path` | Remote path to log files |
| `gcs.bucket` | GCS bucket name |
| `gcs.prefix` | Path prefix within bucket |
| `options.delete_after_upload` | Remove local files after upload |
| `options.file_pattern` | Glob pattern for ZIP files |

## GCS Destination

Files are uploaded to the bucket and prefix specified in your `config.yaml`.

## Downloading Logs for Analysis

To download RTCM3 files from GCS for base health monitoring or analysis:

```bash
python src/download_logs.py --date 2026-01-20 --station top_house
```

See **[docs/DOWNLOAD_LOGS.md](./docs/DOWNLOAD_LOGS.md)** for full usage guide.

## Production Deployment

For setting up automated daily syncing on a Mac inside the network, see:
- **[docs/DEPLOY.md](./docs/DEPLOY.md)** - Complete setup guide for work machine
- **[docs/LAUNCHD_SETUP.md](./docs/LAUNCHD_SETUP.md)** - Daily automation with launchd
