#!/usr/bin/env python3
"""RTCM3 Daily Base Station Health Report.

Parses RTCM3 binary logs from Emlid GNSS base stations and produces
a per-epoch CSV plus a terminal summary covering satellite tracking,
signal quality, carrier phase integrity, observation completeness,
and base position stability.

Supports both legacy observation messages (1001-1004, 1009-1012)
and modern MSM7 messages (1077, 1087, 1097, 1127).

Usage:
    python src/unpack_log.py <file.RTCM3> [--output path.csv] [--summary-only] [--detail]
"""

import argparse
import csv
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pyrtcm import RTCMReader, ERR_LOG

# Message type -> constellation name
CONSTELLATION_MAP = {
    # MSM7
    "1077": "GPS",
    "1087": "GLONASS",
    "1097": "Galileo",
    "1127": "BeiDou",
    # Legacy GPS
    "1001": "GPS",
    "1002": "GPS",
    "1003": "GPS",
    "1004": "GPS",
    # Legacy GLONASS
    "1009": "GLONASS",
    "1010": "GLONASS",
    "1011": "GLONASS",
    "1012": "GLONASS",
}

# Which message types are MSM7
MSM7_TYPES = {"1077", "1087", "1097", "1127"}

# Legacy GPS (L1 only vs L1/L2)
LEGACY_GPS_TYPES = {"1001", "1002", "1003", "1004"}
LEGACY_GPS_L2_TYPES = {"1003", "1004"}  # have L2 data
LEGACY_GLONASS_TYPES = {"1009", "1010", "1011", "1012"}
LEGACY_GLONASS_L2_TYPES = {"1011", "1012"}

# Known RTCM3 message type descriptions
MESSAGE_DESCRIPTIONS = {
    "1001": "GPS L1 Obs",
    "1002": "GPS L1 Obs Extended",
    "1003": "GPS L1/L2 Obs",
    "1004": "GPS L1/L2 Obs Extended",
    "1005": "Ref Station ARP",
    "1006": "Ref Station ARP + Height",
    "1007": "Antenna Descriptor",
    "1008": "Antenna Descriptor + Serial",
    "1009": "GLONASS L1 Obs",
    "1010": "GLONASS L1 Obs Extended",
    "1011": "GLONASS L1/L2 Obs",
    "1012": "GLONASS L1/L2 Obs Extended",
    "1013": "System Parameters",
    "1033": "Receiver/Antenna Descriptors",
    "1077": "GPS MSM7",
    "1087": "GLONASS MSM7",
    "1097": "Galileo MSM7",
    "1107": "SBAS MSM7",
    "1117": "QZSS MSM7",
    "1127": "BeiDou MSM7",
    "1230": "GLONASS Code-Phase Biases",
}

# GPS-UTC leap seconds (18 as of 2017, still current through 2026)
GPS_LEAP_SECONDS_MS = 18_000

# GLONASS time offset from UTC (Moscow time = UTC+3)
GLONASS_UTC_OFFSET_MS = 3 * 3600 * 1000  # 10,800,000 ms

# Lock time indicator threshold for legacy messages (encoded 0-127 scale).
# A drop from a high value to 0 indicates loss of lock / cycle slip.
LEGACY_LOCK_SLIP_THRESHOLD = 2

# Low SNR threshold (dB-Hz)
LOW_SNR_THRESHOLD = 35

# Milliseconds per day/week
MS_PER_DAY = 86_400_000
MS_PER_WEEK = 7 * MS_PER_DAY


@dataclass
class CellObs:
    """Single satellite-signal observation within an epoch."""
    prn: str
    signal: str
    cn0: float
    lock_time: float


@dataclass
class EpochData:
    """All observations for one epoch."""
    epoch_gws: int  # GPS week seconds (used as epoch key)
    observations: list  # list[CellObs]
    cycle_slips: int = 0


@dataclass
class ParseResult:
    """Collected data from a single pass through the RTCM3 file."""
    epochs: dict = field(default_factory=dict)           # gws -> EpochData
    positions: list = field(default_factory=list)         # list of dicts
    message_counts: dict = field(default_factory=dict)    # msg type -> count
    total_messages: int = 0


def ecef_to_geodetic(x, y, z):
    """Convert ECEF coordinates to geodetic (lat, lon, height) using Bowring's method."""
    a = 6378137.0
    f = 1 / 298.257223563
    b = a * (1 - f)
    e2 = 2 * f - f * f
    ep2 = (a * a - b * b) / (b * b)

    lon = math.atan2(y, x)
    p = math.sqrt(x * x + y * y)

    theta = math.atan2(z * a, p * b)
    lat = math.atan2(
        z + ep2 * b * math.sin(theta) ** 3,
        p - e2 * a * math.cos(theta) ** 3,
    )

    for _ in range(10):
        sin_lat = math.sin(lat)
        n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
        lat_new = math.atan2(z + e2 * n * sin_lat, p)
        if abs(lat_new - lat) < 1e-12:
            break
        lat = lat_new

    sin_lat = math.sin(lat)
    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    height = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-10 else abs(z) - b

    return math.degrees(lat), math.degrees(lon), height


def parse_filename_timestamp(filepath):
    """Extract datetime from *_YYYYMMDDHHMMSS.RTCM3 filename pattern."""
    basename = os.path.basename(filepath)
    match = re.search(r"(\d{14})\.RTCM3$", basename, re.IGNORECASE)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    return None


def _gps_day_of_week(dt):
    """Return GPS day-of-week (0=Sunday) for a given datetime."""
    return (dt.weekday() + 1) % 7


def _gps_epoch_to_gws(epoch_ms):
    """Convert GPS DF004 (ms into GPS week) to GPS week seconds (integer)."""
    return int(epoch_ms / 1000)


def _glonass_epoch_to_gws(epoch_ms, gps_day, glo_day_count):
    """Convert GLONASS DF034 to GPS week seconds.

    glo_day_count tracks how many GLONASS day wraps have occurred
    (incremented externally when DF034 drops from >43200s to <43200s).
    """
    utc_ms = epoch_ms - GLONASS_UTC_OFFSET_MS
    if utc_ms < 0:
        utc_ms += MS_PER_DAY
    utc_sod = utc_ms / 1000
    gps_ws = (gps_day + glo_day_count) * 86400 + utc_sod + GPS_LEAP_SECONDS_MS / 1000
    return int(gps_ws)


def gws_to_timestamp(gws, file_date, gps_day):
    """Convert GPS week seconds to a datetime using the file date."""
    if file_date is None:
        return None
    utc_seconds_from_day_start = gws - gps_day * 86400 - GPS_LEAP_SECONDS_MS / 1000
    # file_date is the start date; seconds may extend past midnight into next day(s)
    base = file_date.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return base + timedelta(seconds=utc_seconds_from_day_start)
    except (ValueError, OverflowError):
        return None


def parse_rtcm3(filepath):
    """Single-pass parse of an RTCM3 file. Returns ParseResult."""
    result = ParseResult()
    lock_history = {}  # (prn, signal) -> previous lock time indicator
    file_date = parse_filename_timestamp(filepath)
    gps_day = _gps_day_of_week(file_date) if file_date else 0

    # Track GLONASS day wraps (DF034 resets at midnight Moscow time = 21:00 UTC)
    glo_day_count = 0
    last_glo_utc_sod = None

    with open(filepath, "rb") as f:
        reader = RTCMReader(f, quitonerror=ERR_LOG, labelmsm=1)
        for _raw, msg in reader:
            if msg is None:
                continue

            identity = msg.identity
            result.message_counts[identity] = result.message_counts.get(identity, 0) + 1
            result.total_messages += 1

            if identity in MSM7_TYPES:
                _process_msm7(msg, identity, gps_day, result, lock_history)
            elif identity in LEGACY_GPS_TYPES:
                _process_legacy_gps(msg, identity, gps_day, result, lock_history)
            elif identity in LEGACY_GLONASS_TYPES:
                # Detect GLONASS day wrap
                df034 = getattr(msg, "DF034", None)
                if df034 is not None:
                    utc_ms = df034 - GLONASS_UTC_OFFSET_MS
                    if utc_ms < 0:
                        utc_ms += MS_PER_DAY
                    utc_sod = utc_ms / 1000
                    if last_glo_utc_sod is not None and last_glo_utc_sod > 43200 and utc_sod < 43200:
                        glo_day_count += 1
                    last_glo_utc_sod = utc_sod
                _process_legacy_glonass(msg, identity, gps_day, glo_day_count, result, lock_history)
            elif identity in ("1005", "1006"):
                _process_position(msg, identity, result)

    return result


def _get_or_create_epoch(result, gws):
    """Get existing epoch or create new one for given GPS week second."""
    if gws not in result.epochs:
        result.epochs[gws] = EpochData(epoch_gws=gws, observations=[])
    return result.epochs[gws]


def _check_lock_slip(prn, signal, lock_time, lock_history):
    """Check for cycle slip via lock time indicator reset. Returns True if slip detected."""
    key = (prn, signal)
    slip = False
    if key in lock_history:
        prev_lock = lock_history[key]
        if prev_lock > 10 and lock_time <= LEGACY_LOCK_SLIP_THRESHOLD:
            slip = True
    lock_history[key] = lock_time
    return slip


def _process_msm7(msg, identity, gps_day, result, lock_history):
    """Extract observations from an MSM7 message."""
    constellation = CONSTELLATION_MAP[identity]
    epoch_ms = getattr(msg, "DF004", None)
    if epoch_ms is None:
        return

    gws = _gps_epoch_to_gws(epoch_ms)
    ncell = getattr(msg, "NCell", 0)
    if ncell == 0:
        return

    epoch = _get_or_create_epoch(result, gws)
    prefix = {"GPS": "G", "GLONASS": "R", "Galileo": "E", "BeiDou": "C"}.get(
        constellation, "?"
    )

    for i in range(ncell):
        idx = f"{i + 1:02d}"
        prn_raw = getattr(msg, f"CELLPRN_{idx}", None)
        sig = getattr(msg, f"CELLSIG_{idx}", "")
        cn0 = getattr(msg, f"DF408_{idx}", None)
        lock_time = getattr(msg, f"DF407_{idx}", None)

        if prn_raw is None or cn0 is None or cn0 <= 0:
            continue

        prn = f"{prefix}{prn_raw}" if isinstance(prn_raw, int) else str(prn_raw)

        if lock_time is not None and _check_lock_slip(prn, sig, lock_time, lock_history):
            epoch.cycle_slips += 1

        epoch.observations.append(CellObs(prn=prn, signal=sig, cn0=cn0, lock_time=lock_time or 0))


def _process_legacy_gps(msg, identity, gps_day, result, lock_history):
    """Extract observations from legacy GPS messages (1001-1004)."""
    epoch_ms = getattr(msg, "DF004", None)
    if epoch_ms is None:
        return

    gws = _gps_epoch_to_gws(epoch_ms)
    nsat = getattr(msg, "DF006", 0)
    if nsat == 0:
        return

    epoch = _get_or_create_epoch(result, gws)
    has_l2 = identity in LEGACY_GPS_L2_TYPES

    for i in range(nsat):
        idx = f"{i + 1:02d}"
        prn_num = getattr(msg, f"DF009_{idx}", None)
        if prn_num is None:
            continue
        prn = f"G{prn_num:02d}"

        # L1 observation
        l1_cn0 = getattr(msg, f"DF015_{idx}", None)
        l1_lock = getattr(msg, f"DF013_{idx}", None)
        if l1_cn0 is not None and l1_cn0 > 0:
            if l1_lock is not None and _check_lock_slip(prn, "L1", l1_lock, lock_history):
                epoch.cycle_slips += 1
            epoch.observations.append(CellObs(prn=prn, signal="L1", cn0=l1_cn0, lock_time=l1_lock or 0))

        # L2 observation (1003/1004 only)
        if has_l2:
            l2_cn0 = getattr(msg, f"DF020_{idx}", None)
            l2_lock = getattr(msg, f"DF019_{idx}", None)
            if l2_cn0 is not None and l2_cn0 > 0:
                if l2_lock is not None and _check_lock_slip(prn, "L2", l2_lock, lock_history):
                    epoch.cycle_slips += 1
                epoch.observations.append(CellObs(prn=prn, signal="L2", cn0=l2_cn0, lock_time=l2_lock or 0))


def _process_legacy_glonass(msg, identity, gps_day, glo_day_count, result, lock_history):
    """Extract observations from legacy GLONASS messages (1009-1012)."""
    epoch_ms = getattr(msg, "DF034", None)
    if epoch_ms is None:
        return

    gws = _glonass_epoch_to_gws(epoch_ms, gps_day, glo_day_count)
    nsat = getattr(msg, "DF035", 0)
    if nsat == 0:
        return

    epoch = _get_or_create_epoch(result, gws)
    has_l2 = identity in LEGACY_GLONASS_L2_TYPES

    for i in range(nsat):
        idx = f"{i + 1:02d}"
        slot_num = getattr(msg, f"DF038_{idx}", None)
        if slot_num is None:
            continue
        prn = f"R{slot_num:02d}"

        # L1 observation
        l1_cn0 = getattr(msg, f"DF045_{idx}", None)
        l1_lock = getattr(msg, f"DF043_{idx}", None)
        if l1_cn0 is not None and l1_cn0 > 0:
            if l1_lock is not None and _check_lock_slip(prn, "L1", l1_lock, lock_history):
                epoch.cycle_slips += 1
            epoch.observations.append(CellObs(prn=prn, signal="L1", cn0=l1_cn0, lock_time=l1_lock or 0))

        # L2 observation (1011/1012 only)
        if has_l2:
            l2_cn0 = getattr(msg, f"DF050_{idx}", None)
            l2_lock = getattr(msg, f"DF049_{idx}", None)
            if l2_cn0 is not None and l2_cn0 > 0:
                if l2_lock is not None and _check_lock_slip(prn, "L2", l2_lock, lock_history):
                    epoch.cycle_slips += 1
                epoch.observations.append(CellObs(prn=prn, signal="L2", cn0=l2_cn0, lock_time=l2_lock or 0))


def _process_position(msg, identity, result):
    """Extract position from a 1005/1006 message."""
    pos = {
        "station_id": getattr(msg, "DF003", None),
        "ecef_x": getattr(msg, "DF025", None),
        "ecef_y": getattr(msg, "DF026", None),
        "ecef_z": getattr(msg, "DF027", None),
        "antenna_height": getattr(msg, "DF028", None) if identity == "1006" else None,
    }
    result.positions.append(pos)


def detect_data_gaps(epoch_keys):
    """Find gaps where consecutive epoch interval exceeds 2 seconds.

    Returns list of {start_gws, end_gws, duration_sec}.
    """
    if len(epoch_keys) < 2:
        return []

    gaps = []
    for i in range(1, len(epoch_keys)):
        diff = epoch_keys[i] - epoch_keys[i - 1]
        if diff > 2:
            gaps.append({
                "start_gws": epoch_keys[i - 1],
                "end_gws": epoch_keys[i],
                "duration_sec": diff,
            })

    return gaps


def _sat_counts_for_epoch(epoch):
    """Return dict of constellation -> unique satellite count."""
    counts = {"GPS": set(), "GLONASS": set(), "Galileo": set(), "BeiDou": set()}
    prefix_map = {"G": "GPS", "R": "GLONASS", "E": "Galileo", "C": "BeiDou"}
    for obs in epoch.observations:
        if obs.prn and len(obs.prn) >= 2:
            const = prefix_map.get(obs.prn[0])
            if const:
                counts[const].add(obs.prn)
    return {k: len(v) for k, v in counts.items()}


def build_epoch_rows(parse_result, file_date, gps_day):
    """Convert epoch data to CSV row dicts, sorted by epoch time."""
    rows = []
    for gws in sorted(parse_result.epochs):
        epoch = parse_result.epochs[gws]
        sat_counts = _sat_counts_for_epoch(epoch)
        total_sats = sum(sat_counts.values())

        cn0_values = [obs.cn0 for obs in epoch.observations if obs.cn0 > 0]
        mean_snr = sum(cn0_values) / len(cn0_values) if cn0_values else 0.0
        min_snr = min(cn0_values) if cn0_values else 0.0
        low_snr_count = sum(1 for v in cn0_values if v < LOW_SNR_THRESHOLD)

        ts = gws_to_timestamp(gws, file_date, gps_day)
        timestamp_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""

        rows.append({
            "epoch_time_gws": gws,
            "timestamp": timestamp_str,
            "gps_sats": sat_counts.get("GPS", 0),
            "glonass_sats": sat_counts.get("GLONASS", 0),
            "galileo_sats": sat_counts.get("Galileo", 0),
            "beidou_sats": sat_counts.get("BeiDou", 0),
            "total_sats": total_sats,
            "mean_snr": round(mean_snr, 1),
            "min_snr": round(min_snr, 1),
            "low_snr_count": low_snr_count,
            "cycle_slips": epoch.cycle_slips,
        })

    return rows


def write_csv(rows, output_path):
    """Write epoch rows to CSV."""
    if not rows:
        return
    fieldnames = [
        "epoch_time_gws", "timestamp",
        "gps_sats", "glonass_sats", "galileo_sats", "beidou_sats", "total_sats",
        "mean_snr", "min_snr", "low_snr_count", "cycle_slips",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)



def _detect_position_change(positions):
    """Detect whether the base station position changed during the session.

    Returns dict with keys: stable (bool), unique_count, spread_m,
    num_reports, and optionally jump_index/jump_distance for MOVED cases.
    """
    coords = [
        (p["ecef_x"], p["ecef_y"], p["ecef_z"])
        for p in positions
        if p["ecef_x"] is not None and p["ecef_y"] is not None and p["ecef_z"] is not None
    ]
    if not coords:
        return {"stable": True, "unique_count": 0, "spread_m": 0.0, "num_reports": 0}

    # Deduplicate by rounding ECEF to 0.0001 m
    seen = set()
    unique = []
    for c in coords:
        rounded = (round(c[0], 4), round(c[1], 4), round(c[2], 4))
        if rounded not in seen:
            seen.add(rounded)
            unique.append(rounded)

    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    zs = [c[2] for c in coords]
    spread = math.sqrt(
        (max(xs) - min(xs)) ** 2
        + (max(ys) - min(ys)) ** 2
        + (max(zs) - min(zs)) ** 2
    )

    result = {
        "stable": len(unique) == 1,
        "unique_count": len(unique),
        "spread_m": spread,
        "num_reports": len(coords),
    }

    # Find where the first jump occurred
    if len(unique) > 1:
        first = (round(coords[0][0], 4), round(coords[0][1], 4), round(coords[0][2], 4))
        for i, c in enumerate(coords[1:], 1):
            rounded = (round(c[0], 4), round(c[1], 4), round(c[2], 4))
            if rounded != first:
                jump_dist = math.sqrt(
                    (c[0] - coords[0][0]) ** 2
                    + (c[1] - coords[0][1]) ** 2
                    + (c[2] - coords[0][2]) ** 2
                )
                result["jump_index"] = i
                result["jump_distance_m"] = jump_dist
                break

    return result


def print_compact_summary(parse_result, filepath, output_path=None):
    """Print a compact (~20 line) terminal health summary."""
    file_date = parse_filename_timestamp(filepath)
    gps_day = _gps_day_of_week(file_date) if file_date else 0
    basename = os.path.basename(filepath)
    epoch_keys = sorted(parse_result.epochs.keys())
    epochs = parse_result.epochs
    num_epochs = len(epochs)

    print(f"\n=== Base Station Health Report ===")
    print(f"File: {basename}")

    # Time span
    if file_date and len(epoch_keys) >= 2:
        start_ts = gws_to_timestamp(epoch_keys[0], file_date, gps_day)
        end_ts = gws_to_timestamp(epoch_keys[-1], file_date, gps_day)
        if start_ts and end_ts:
            duration = end_ts - start_ts
            hours, remainder = divmod(int(duration.total_seconds()), 3600)
            minutes, secs = divmod(remainder, 60)
            end_fmt = end_ts.strftime('%H:%M:%S')
            if end_ts.date() != start_ts.date():
                end_fmt = end_ts.strftime('%Y-%m-%d %H:%M:%S')
            print(
                f"Date: {start_ts.strftime('%Y-%m-%d')}  "
                f"{start_ts.strftime('%H:%M:%S')} — {end_fmt} "
                f"({hours}:{minutes:02d}:{secs:02d})"
            )
    elif file_date:
        print(f"Date: {file_date.strftime('%Y-%m-%d')}")

    # --- Position ---
    if parse_result.positions:
        pos = parse_result.positions[0]
        station_id = pos.get("station_id", "?")
        print(f"\nPosition (Station {station_id}):")

        x, y, z = pos["ecef_x"], pos["ecef_y"], pos["ecef_z"]
        if x is not None and y is not None and z is not None:
            lat, lon, height = ecef_to_geodetic(x, y, z)
            lat_dir = "N" if lat >= 0 else "S"
            lon_dir = "E" if lon >= 0 else "W"
            print(f"  {abs(lat):.8f}{chr(176)}{lat_dir}  {abs(lon):.8f}{chr(176)}{lon_dir}  {height:.2f}m (HAE)")

        if pos.get("antenna_height") is not None:
            print(f"  Antenna height: {pos['antenna_height']} m")

        pos_info = _detect_position_change(parse_result.positions)
        if pos_info["num_reports"] > 0:
            if pos_info["stable"]:
                print(
                    f"  STABLE — {pos_info['spread_m']:.4f}m spread "
                    f"across {pos_info['num_reports']:,} reports"
                )
            else:
                jump_dist = pos_info.get("jump_distance_m", pos_info["spread_m"])
                jump_idx = pos_info.get("jump_index", "?")
                print(
                    f"  MOVED — {jump_dist:.4f}m jump at report ~{jump_idx}, "
                    f"{pos_info['unique_count']} unique positions"
                )

    if num_epochs == 0:
        print("\nNo observation epochs found.")
        return

    # --- Tracking ---
    constellations = ["GPS", "GLONASS", "Galileo", "BeiDou"]
    con_short = {"GPS": "GPS", "GLONASS": "GLO", "Galileo": "GAL", "BeiDou": "BDS"}

    per_epoch_counts = {k: _sat_counts_for_epoch(epochs[k]) for k in epoch_keys}
    totals_per_epoch = [
        sum(per_epoch_counts[k].get(c, 0) for c in constellations)
        for k in epoch_keys
    ]
    mean_total = sum(totals_per_epoch) / len(totals_per_epoch) if totals_per_epoch else 0
    min_total = min(totals_per_epoch) if totals_per_epoch else 0

    # Per-constellation means for active constellations
    con_parts = []
    for c in constellations:
        vals = [per_epoch_counts[k].get(c, 0) for k in epoch_keys]
        cmean = sum(vals) / len(vals) if vals else 0
        if cmean >= 0.5:
            con_parts.append(f"{con_short[c]}: {cmean:.0f}")

    print(f"\nTracking:  {mean_total:.0f} mean sats ({min_total} min)  —  {', '.join(con_parts) if con_parts else 'none'}")

    # --- Signal ---
    all_cn0 = []
    for k in epoch_keys:
        for obs in epochs[k].observations:
            if obs.cn0 > 0:
                all_cn0.append(obs.cn0)

    if all_cn0:
        mean_snr = sum(all_cn0) / len(all_cn0)
        min_snr = min(all_cn0)
        print(f"Signal:    {mean_snr:.1f} dB-Hz mean, {min_snr:.1f} min")

    # --- Phase ---
    total_slips = sum(epochs[k].cycle_slips for k in epoch_keys)
    all_prns = set()
    for k in epoch_keys:
        for obs in epochs[k].observations:
            all_prns.add(obs.prn)
    print(f"Phase:     {total_slips:,} cycle slips across {len(all_prns)} satellites")

    # --- Data completeness ---
    if len(epoch_keys) >= 2:
        expected = epoch_keys[-1] - epoch_keys[0] + 1
    else:
        expected = num_epochs

    missing = max(0, expected - num_epochs)
    completeness = ((expected - missing) / expected * 100) if expected > 0 else 100.0
    gaps = detect_data_gaps(epoch_keys)
    print(f"Data:      {completeness:.1f}% complete ({missing:,} missing, {len(gaps)} gaps)")

    if output_path:
        print(f"\nOutput: {output_path}")
    print()


def print_detail_summary(parse_result, filepath, output_path=None):
    """Print the full verbose terminal health report (--detail mode)."""
    file_date = parse_filename_timestamp(filepath)
    gps_day = _gps_day_of_week(file_date) if file_date else 0
    basename = os.path.basename(filepath)
    epoch_keys = sorted(parse_result.epochs.keys())
    epochs = parse_result.epochs
    num_epochs = len(epochs)

    print(f"\n{'=' * 50}")
    print("  RTCM3 Base Station Health Report")
    print(f"{'=' * 50}")
    print(f"  File: {basename}")
    if file_date:
        print(f"  Date: {file_date.strftime('%Y-%m-%d')}")
    print()

    # --- Message Inventory ---
    print("--- Message Inventory ---")
    print(f"  Total messages: {parse_result.total_messages:,}")
    for msg_type in sorted(parse_result.message_counts, key=lambda x: int(x)):
        count = parse_result.message_counts[msg_type]
        desc = MESSAGE_DESCRIPTIONS.get(msg_type, "")
        label = f"  {msg_type}"
        if desc:
            label += f" ({desc})"
        print(f"{label + ':':<42}{count:>8,}")
    print()

    if num_epochs == 0:
        print("  No observation epochs found.")
        return

    # Gather per-epoch sat counts
    per_epoch_counts = {}
    for k in epoch_keys:
        per_epoch_counts[k] = _sat_counts_for_epoch(epochs[k])

    constellations = ["GPS", "GLONASS", "Galileo", "BeiDou"]
    con_short = ["GPS", "GLO", "GAL", "BDS"]

    # Only show constellations that have data
    active = [i for i, c in enumerate(constellations)
              if any(per_epoch_counts[k].get(c, 0) > 0 for k in epoch_keys)]

    # --- Satellite Tracking ---
    print("--- Satellite Tracking ---")
    header = "                 "
    for i in active:
        header += f" {con_short[i]:>6}"
    header += "  Total"
    print(header)

    for stat_name, stat_fn in [("Mean sats:", lambda v: sum(v) / len(v) if v else 0),
                                ("Min sats:", lambda v: min(v) if v else 0),
                                ("Max sats:", lambda v: max(v) if v else 0)]:
        line = f"  {stat_name:<15}"
        for i in active:
            c = constellations[i]
            vals = [per_epoch_counts[k].get(c, 0) for k in epoch_keys]
            val = stat_fn(vals)
            if "Mean" in stat_name:
                line += f" {val:>6.0f}"
            else:
                line += f" {val:>6}"
        totals_per_epoch = [
            sum(per_epoch_counts[k].get(c, 0) for c in constellations)
            for k in epoch_keys
        ]
        total_val = stat_fn(totals_per_epoch)
        if "Mean" in stat_name:
            line += f"  {total_val:>5.0f}"
        else:
            line += f"  {total_val:>5}"
        print(line)

    low_coverage = sum(
        1 for k in epoch_keys
        if sum(per_epoch_counts[k].get(c, 0) for c in constellations) < 5
    )
    print(f"  Low coverage (<5 sats) periods: {low_coverage}")
    print()

    # --- Signal Quality ---
    print("--- Signal Quality ---")
    all_cn0 = []
    sat_cn0 = {}
    for k in epoch_keys:
        for obs in epochs[k].observations:
            if obs.cn0 > 0:
                all_cn0.append(obs.cn0)
                sat_cn0.setdefault(obs.prn, []).append(obs.cn0)

    if all_cn0:
        mean_snr = sum(all_cn0) / len(all_cn0)
        min_snr = min(all_cn0)
        low_epochs = sum(
            1 for k in epoch_keys
            if any(obs.cn0 < LOW_SNR_THRESHOLD for obs in epochs[k].observations if obs.cn0 > 0)
        )
        print(f"  Mean SNR:     {mean_snr:.1f} dB-Hz")
        print(f"  Min SNR:      {min_snr:.1f} dB-Hz")
        print(f"  Signals < {LOW_SNR_THRESHOLD} dB-Hz:  {low_epochs:,} / {num_epochs:,} epochs")

        low_sats = []
        for prn, cn0_list in sorted(sat_cn0.items()):
            if sum(cn0_list) / len(cn0_list) < LOW_SNR_THRESHOLD:
                low_sats.append(prn)
        if low_sats:
            print(f"  Persistently low SNR satellites: {', '.join(low_sats)}")
    print()

    # --- Carrier Phase Health ---
    print("--- Carrier Phase Health ---")
    total_slips = sum(epochs[k].cycle_slips for k in epoch_keys)
    print(f"  Total cycle slips: {total_slips}")
    _print_slip_details(parse_result, epoch_keys)
    print()

    # --- Observation Completeness ---
    print("--- Observation Completeness ---")
    if len(epoch_keys) >= 2:
        expected = epoch_keys[-1] - epoch_keys[0] + 1
    else:
        expected = num_epochs

    missing = max(0, expected - num_epochs)
    pct = (missing / expected * 100) if expected > 0 else 0
    print(f"  Expected epochs (1 Hz): {expected:,}")
    print(f"  Actual epochs:          {num_epochs:,}")
    print(f"  Missing:                {missing:,} ({pct:.1f}%)")

    gaps = detect_data_gaps(epoch_keys)
    print(f"  Data gaps: {len(gaps)}")
    for gap in gaps:
        start_t = gws_to_timestamp(gap["start_gws"], file_date, gps_day)
        end_t = gws_to_timestamp(gap["end_gws"], file_date, gps_day)
        start_str = start_t.strftime("%H:%M:%S") if start_t else str(gap["start_gws"])
        end_str = end_t.strftime("%H:%M:%S") if end_t else str(gap["end_gws"])
        print(f"    {start_str} - {end_str} ({gap['duration_sec']}s)")
    print()

    # --- Base Position ---
    print("--- Base Position ---")
    if parse_result.positions:
        pos = parse_result.positions[0]
        print(f"  Station ID: {pos['station_id']}")

        x, y, z = pos["ecef_x"], pos["ecef_y"], pos["ecef_z"]
        if x is not None and y is not None and z is not None:
            print(f"  ECEF X: {x:,.4f} m")
            print(f"  ECEF Y: {y:,.4f} m")
            print(f"  ECEF Z: {z:,.4f} m")

            lat, lon, height = ecef_to_geodetic(x, y, z)
            print(f"  Latitude:  {lat:.8f} deg")
            print(f"  Longitude: {lon:.8f} deg")
            print(f"  Height:    {height:.4f} m")

        if pos.get("antenna_height") is not None:
            print(f"  Antenna height: {pos['antenna_height']} m")

        if len(parse_result.positions) > 1:
            xs = [p["ecef_x"] for p in parse_result.positions if p["ecef_x"] is not None]
            ys = [p["ecef_y"] for p in parse_result.positions if p["ecef_y"] is not None]
            zs = [p["ecef_z"] for p in parse_result.positions if p["ecef_z"] is not None]
            if xs and ys and zs:
                spread = math.sqrt(
                    (max(xs) - min(xs)) ** 2
                    + (max(ys) - min(ys)) ** 2
                    + (max(zs) - min(zs)) ** 2
                )
                label = "stable" if spread < 0.01 else "variable"
                print(f"  Position spread: {spread:.4f} m ({label})")
    else:
        print("  No position messages found.")
    print()

    if output_path:
        print(f"Output: {output_path}")
        print()


def _print_slip_details(parse_result, epoch_keys):
    """Print per-satellite cycle slip breakdown."""
    # Re-derive per-satellite slips by replaying lock history
    lock_hist = {}
    slip_by_sat = {}

    for k in epoch_keys:
        epoch = parse_result.epochs[k]
        for obs in epoch.observations:
            key = (obs.prn, obs.signal)
            if key in lock_hist:
                prev_lock = lock_hist[key]
                if prev_lock > 10 and obs.lock_time <= LEGACY_LOCK_SLIP_THRESHOLD:
                    slip_by_sat[obs.prn] = slip_by_sat.get(obs.prn, 0) + 1
            lock_hist[key] = obs.lock_time

    if slip_by_sat:
        sorted_slips = sorted(slip_by_sat.items(), key=lambda x: -x[1])
        top = sorted_slips[:10]
        parts = [f"{prn} ({count})" for prn, count in top]
        line = f"  Affected satellites: {', '.join(parts)}"
        if len(sorted_slips) > 10:
            line += f" (and {len(sorted_slips) - 10} more)"
        print(line)
    else:
        print("  Affected satellites: none")


def main():
    parser = argparse.ArgumentParser(
        description="RTCM3 Base Station Health Report",
    )
    parser.add_argument("file", help="Path to RTCM3 file")
    parser.add_argument(
        "-o", "--output", default=None,
        help="CSV output path (default: reports/<input>_summary.csv)",
    )
    parser.add_argument(
        "--summary-only", action="store_true",
        help="Print summary only, skip CSV",
    )
    parser.add_argument(
        "--detail", action="store_true",
        help="Print full detailed report instead of compact summary",
    )
    args = parser.parse_args()

    filepath = args.file
    if not os.path.isfile(filepath):
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    # Default output to reports/ directory next to src/
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    if args.output:
        output_path = args.output
    else:
        base, _ext = os.path.splitext(os.path.basename(filepath))
        os.makedirs(reports_dir, exist_ok=True)
        output_path = os.path.join(reports_dir, f"{base}_summary.csv")

    file_date = parse_filename_timestamp(filepath)
    gps_day = _gps_day_of_week(file_date) if file_date else 0

    print(f"Parsing {os.path.basename(filepath)} ...")
    parse_result = parse_rtcm3(filepath)

    if not args.summary_only:
        rows = build_epoch_rows(parse_result, file_date, gps_day)
        write_csv(rows, output_path)
        print(f"Wrote {len(rows):,} rows to {output_path}")
    else:
        output_path = None

    if args.detail:
        print_detail_summary(parse_result, filepath, output_path)
    else:
        print_compact_summary(parse_result, filepath, output_path)


if __name__ == "__main__":
    main()
