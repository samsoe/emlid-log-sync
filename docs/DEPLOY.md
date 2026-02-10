# Deployment Guide - Internal Network Mac

This guide covers setting up automated daily syncing on a Mac with network access to the Emlid device.

## Prerequisites

- Mac with network access to the Emlid device (VPN if remote, or direct if on local network)
- Python 3.11+
- `gcloud` CLI installed and authenticated
- Git installed

## Setup Steps

### 1. Clone the Repository

```bash
cd ~/Projects  # or wherever you keep code
git clone https://github.com/samsoe/emlid-log-sync.git
cd emlid-log-sync
```

### 2. Install Dependencies

**Option A: Using conda (recommended)**

```bash
conda env create -f environment.yml
conda activate emlid-log-sync
```

**Option B: Using pip + venv**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure GCS Authentication

```bash
gcloud auth application-default login
```

Follow the browser prompts to authenticate with your Google account that has access to your GCS bucket.

### 4. Create Configuration File

```bash
cp config.example.yaml config.yaml
```

**Edit `config.yaml`:**
- Update the Emlid device IP address
- Update the Emlid password (change from default!)
- Verify GCS bucket and prefix paths match your setup

### 5. Test the Script

**Dry run (no downloads, just lists files):**

```bash
conda activate emlid-log-sync  # or: source .venv/bin/activate
python src/sync_emlid_logs.py --dry-run
```

**Process one file:**

```bash
python src/sync_emlid_logs.py --limit 1 -v
```

**Full sync:**

```bash
python src/sync_emlid_logs.py
```

### 6. Set Up Daily Automation

See [LAUNCHD_SETUP.md](./LAUNCHD_SETUP.md) for instructions on configuring the daily automated run.

## Troubleshooting

### Can't connect to Emlid device

- Verify you're on the correct network (or VPN connected if remote)
- Test: `ping <EMLID_DEVICE_IP>`
- Test SSH: `ssh reach@<EMLID_DEVICE_IP>` (use your device's password)

### GCS upload fails

- Check authentication: `gcloud auth application-default print-access-token`
- Verify bucket access: `gcloud storage ls gs://<YOUR_BUCKET>/<YOUR_PREFIX>/`

### Python dependencies issues

If using conda:
```bash
conda env remove -n emlid-log-sync
conda env create -f environment.yml
```

## Maintenance

### Update the script

```bash
cd ~/Projects/emlid-log-sync
git pull origin main
conda activate emlid-log-sync  # or activate venv
pip install -r requirements.txt  # in case dependencies changed
```

### Check sync history

```bash
# View logs if running via launchd
tail -f ~/Library/Logs/emlid-log-sync.log

# Or check GCS directly
gcloud storage ls gs://<YOUR_BUCKET>/<YOUR_PREFIX>/
```
