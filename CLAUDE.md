# Emlid Log Sync

Local utility for syncing RTCM3 correction logs from Emlid GNSS base stations to Google Cloud Storage.

## Project Context

- **Parent repo:** `survey_utility` (MPG Ranch aerial survey tools)
- **Related ClickUp:** https://app.clickup.com/t/86ab818my
- **Created:** 2025-01-26

## Workflow

```
[WARP VPN connected]
    ↓
SSH/SFTP to Emlid device (10.0.106.161)
    ↓
Download ZIP archives (~500MB each, naming: TOP_HOUSE_B_YYYYMMDDHHMMSS.zip)
    ↓
Unzip locally
    ↓
Extract .RTCM3 files only
    ↓
Upload to GCS: gs://mpg-aerial-survey/surveys/gps_network/base/top_house/logs/
    ↓
Delete local ZIP and extracted files after successful upload
```

## Key Decisions (2025-01-26)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Architecture | Local script + WARP VPN (Option A) | Simplest path; user already has WARP access |
| Target files | .RTCM3 only | Only correction data needed in GCS |
| Cleanup | Delete after upload | Save disk space |
| Fresh directory | Yes | Clean slate vs. refactoring rtcm3-upload-automation |

## Emlid Device

- **IP:** 10.0.106.161 (internal network, requires WARP VPN)
- **User:** reach
- **Password:** emlidreach (default — consider changing)
- **Log path:** /home/reach/logs/

## Dependencies

- `paramiko` — SSH/SFTP
- `google-cloud-storage` — GCS uploads
- `pyyaml` — config parsing

## Tasks

1. [ ] Create conda environment (environment.yml, requirements.txt)
2. [ ] Write sync_emlid_logs.py (core workflow script)
3. [ ] Create config.example.yaml template
4. [ ] Add README.md
5. [ ] Test end-to-end with WARP connected

## File Structure (planned)

```
emlid-log-sync/
├── CLAUDE.md              # this file
├── README.md
├── environment.yml
├── requirements.txt
├── config.example.yaml    # template (committed)
├── config.yaml            # actual config (gitignored)
├── .gitignore
└── src/
    └── sync_emlid_logs.py
```

## GCS Credentials

Uses application default credentials. Ensure `gcloud auth application-default login` is run before first use.
