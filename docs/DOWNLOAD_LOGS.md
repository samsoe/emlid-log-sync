# Downloading Logs for Analysis

The `download_logs.py` script allows you to pull RTCM3 log files from Google Cloud Storage for a specific date range and station.

## Use Case

Primary use: **Base health monitoring** - download logs to analyze base station performance, check for data gaps, or perform quality assessments.

## Setup

### 1. Create Configuration File

```bash
cp download_config.example.yaml download_config.yaml
```

Edit `download_config.yaml` with your GCS bucket and path settings.

### 2. Install Dependencies

```bash
# Ensure you have the google-cloud-storage library
pip install google-cloud-storage

# Or if using conda environment
conda activate emlid-log-sync
pip install google-cloud-storage
```

### 3. Authenticate with GCS

```bash
gcloud auth application-default login
```

## Usage

### Download logs for a single day

```bash
python src/download_logs.py --date 2026-01-20 --station top_house
```

### Download logs for a date range

```bash
python src/download_logs.py --start 2026-01-20 --end 2026-01-25 --station top_house
```

### List available logs without downloading

```bash
python src/download_logs.py --list --start 2026-01-20 --end 2026-01-25
```

### Specify custom output directory

```bash
python src/download_logs.py --date 2026-01-20 --output ./my_logs
```

## Examples

**Download a week of logs:**
```bash
python src/download_logs.py \
  --start 2026-01-20 \
  --end 2026-01-27 \
  --station top_house \
  --output ./week_logs
```

**Check what's available for January:**
```bash
python src/download_logs.py \
  --list \
  --start 2026-01-01 \
  --end 2026-01-31
```

## Output

Downloaded files are saved to `./downloaded_logs/` by default (or your specified output directory).

Files are named like: `TOP_HOUSE_B_base_20260120174253.RTCM3`
- Format: `{STATION}_base_{YYYYMMDDHHMMSS}.RTCM3`
- Size: ~24-28MB per day

## Configuration

The script reads GCS settings from `download_config.yaml`:
- **Bucket**: Your GCS bucket name
- **Path template**: Path to logs with `{station}` placeholder
- **Default station**: Station name to use if not specified on command line
- **Output directory**: Default location for downloaded files

All infrastructure-specific settings are in the config file (gitignored), not hardcoded.

## Troubleshooting

**Authentication errors:**
```bash
gcloud auth application-default login
```

**Missing google-cloud-storage:**
```bash
pip install google-cloud-storage
```

**No files found:**
- Verify the date range has data
- Check that the station name is correct
- Confirm files exist in GCS: `gcloud storage ls gs://<YOUR_BUCKET>/<YOUR_PREFIX>/`

## Next Steps

Once downloaded, RTCM3 files can be:
- Analyzed with GNSS processing tools (RTKLIB, etc.)
- Parsed to extract observation statistics
- Compared across dates to identify trends or issues
- Used for post-processed kinematic (PPK) corrections
