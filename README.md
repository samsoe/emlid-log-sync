# Emlid Log Sync

Local utility for syncing RTCM3 correction logs from Emlid GNSS base stations to Google Cloud Storage.

## Prerequisites

- Network access to Emlid device (10.0.106.161) - either on internal network or via WARP VPN
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
4. Extracts `.RTCM3` files only
5. Uploads to GCS (skips files that already exist)
6. Deletes local ZIP and extracted files after successful upload

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

Files are uploaded to:

```
gs://mpg-aerial-survey/surveys/gps_network/base/top_house/logs/
```
