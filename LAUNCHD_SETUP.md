# Launchd Setup - Daily Automation

This guide configures macOS to run the Emlid log sync script automatically every day at 2:00 AM.

## Installation

### 1. Customize the Plist File

Open `com.mpgranch.emlid-log-sync.plist` and replace the following placeholders:

- **REPLACE_WITH_REPO_PATH**: Full path to your cloned repo
  - Example: `/Users/yourname/Projects/emlid-log-sync`
  - Find it: `cd ~/Projects/emlid-log-sync && pwd`

- **REPLACE_WITH_HOME_DIR**: Your home directory path
  - Example: `/Users/yourname`
  - Find it: `echo $HOME`

**Quick command to get values:**
```bash
cd ~/Projects/emlid-log-sync  # or wherever you cloned it
echo "Repo path: $(pwd)"
echo "Home dir: $HOME"
```

### 2. Install the Launchd Job

Copy the plist to your LaunchAgents directory:

```bash
cp com.mpgranch.emlid-log-sync.plist ~/Library/LaunchAgents/
```

### 3. Load the Job

```bash
launchctl load ~/Library/LaunchAgents/com.mpgranch.emlid-log-sync.plist
```

### 4. Verify It's Loaded

```bash
launchctl list | grep emlid-log-sync
```

You should see output like:
```
-       0       com.mpgranch.emlid-log-sync
```

## Testing

### Test Run Immediately

To test the job without waiting until 2 AM:

```bash
launchctl start com.mpgranch.emlid-log-sync
```

Check the logs:
```bash
tail -f ~/Library/Logs/emlid-log-sync.log
```

### View Recent Runs

```bash
# Standard output (successful runs)
tail -n 50 ~/Library/Logs/emlid-log-sync.log

# Error output (if something goes wrong)
tail -n 50 ~/Library/Logs/emlid-log-sync.error.log
```

## Schedule Customization

The default schedule is **daily at 2:00 AM**. To change it, edit the plist file:

### Run multiple times per day

Replace the `StartCalendarInterval` section:

```xml
<!-- Run every 6 hours -->
<key>StartCalendarInterval</key>
<array>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <dict>
        <key>Hour</key>
        <integer>14</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <dict>
        <key>Hour</key>
        <integer>20</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
</array>
```

### Run weekly

```xml
<!-- Run every Sunday at 3:00 AM -->
<key>StartCalendarInterval</key>
<dict>
    <key>Weekday</key>
    <integer>0</integer>  <!-- 0=Sunday, 1=Monday, etc. -->
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>0</integer>
</dict>
```

After changing the schedule, reload:
```bash
launchctl unload ~/Library/LaunchAgents/com.mpgranch.emlid-log-sync.plist
launchctl load ~/Library/LaunchAgents/com.mpgranch.emlid-log-sync.plist
```

## Troubleshooting

### Job not running

1. **Check if loaded:**
   ```bash
   launchctl list | grep emlid-log-sync
   ```

2. **Check for errors:**
   ```bash
   cat ~/Library/Logs/emlid-log-sync.error.log
   ```

3. **Validate plist syntax:**
   ```bash
   plutil -lint ~/Library/LaunchAgents/com.mpgranch.emlid-log-sync.plist
   ```

### Conda environment not activating

If conda isn't found, update the plist to use the full conda path:

```xml
<string>
    eval "$(/opt/homebrew/Caskroom/miniconda/base/bin/conda shell.bash hook)";
    conda activate emlid-log-sync;
    cd /full/path/to/emlid-log-sync;
    python src/sync_emlid_logs.py
</string>
```

Find your conda path:
```bash
which conda
```

### GCS authentication issues

The script uses Application Default Credentials. Ensure they're set up:

```bash
gcloud auth application-default login
```

The credentials are stored at: `~/.config/gcloud/application_default_credentials.json`

Make sure this file exists and the account has access to the GCS bucket.

## Uninstalling

To stop and remove the automated job:

```bash
# Unload the job
launchctl unload ~/Library/LaunchAgents/com.mpgranch.emlid-log-sync.plist

# Remove the plist file
rm ~/Library/LaunchAgents/com.mpgranch.emlid-log-sync.plist
```

The script and repo remain untouched - only the automation is removed.

## Monitoring

### Set up email notifications (optional)

macOS launchd doesn't natively support email notifications. For alerts, consider:

1. **Check logs regularly:**
   ```bash
   tail -f ~/Library/Logs/emlid-log-sync.log
   ```

2. **Add a notification script** - modify the plist to run a wrapper script that sends emails on failure

3. **Use a monitoring service** - integrate with an external monitoring tool

### Log rotation

Logs can grow over time. To prevent disk issues, set up log rotation:

```bash
# Create a weekly cleanup job
echo "0 3 * * 0 rm -f ~/Library/Logs/emlid-log-sync.log.* && mv ~/Library/Logs/emlid-log-sync.log ~/Library/Logs/emlid-log-sync.log.\$(date +\%Y\%m\%d)" | crontab -
```
