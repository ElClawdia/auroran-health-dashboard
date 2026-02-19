#!/usr/bin/env python3
"""
Debug script to test PMC calculations and find formula that matches Strava.
Run on server: python debug_pmc.py
"""

import math
import sys
from datetime import datetime, timedelta
from collections import defaultdict

# Add parent to path
sys.path.insert(0, '.')

from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
from influxdb_client import InfluxDBClient

# Target values from Strava
STRAVA_CTL = 45
STRAVA_ATL = 34
STRAVA_TSB = 11


def fetch_daily_loads(query_api, days=365):
    """Fetch daily loads from InfluxDB"""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{days}d)
      |> filter(fn: (r) => r._measurement == "workouts")
      |> filter(fn: (r) => r._field == "suffer_score")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    result = query_api.query_data_frame(query)
    
    if result.empty:
        print("No data from InfluxDB!")
        return {}
    
    by_date = defaultdict(float)
    for _, row in result.iterrows():
        date = row.get('date', '')
        if not date and '_time' in row:
            try:
                import pandas as pd
                date = pd.Timestamp(row['_time']).strftime('%Y-%m-%d')
            except:
                pass
        load = row.get('suffer_score', 0) or 0
        if date:
            by_date[date] += float(load)
    
    return dict(by_date)


def build_full_series(daily_loads, days=365):
    """Build continuous series with 0 for missing days"""
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days - 1)
    
    series = []
    cur = start_date
    while cur <= end_date:
        ds = cur.isoformat()
        series.append({"date": ds, "load": daily_loads.get(ds, 0.0)})
        cur += timedelta(days=1)
    return series


def calc_pmc_standard(series, ctl_days=42, atl_days=7, init_ctl=0, init_atl=0):
    """Standard EWMA: k = 1 - exp(-1/τ), init from params"""
    k_ctl = 1 - math.exp(-1 / ctl_days)
    k_atl = 1 - math.exp(-1 / atl_days)
    ctl, atl = init_ctl, init_atl
    
    for day in series:
        load = day["load"]
        ctl = ctl * (1 - k_ctl) + load * k_ctl
        atl = atl * (1 - k_atl) + load * k_atl
    
    return ctl, atl, ctl - atl


def calc_pmc_simple_ema(series, ctl_days=42, atl_days=7, init_ctl=0, init_atl=0):
    """Simple EMA: α = 2/(N+1)"""
    alpha_ctl = 2 / (ctl_days + 1)
    alpha_atl = 2 / (atl_days + 1)
    ctl, atl = init_ctl, init_atl
    
    for day in series:
        load = day["load"]
        ctl = alpha_ctl * load + (1 - alpha_ctl) * ctl
        atl = alpha_atl * load + (1 - alpha_atl) * atl
    
    return ctl, atl, ctl - atl


def calc_pmc_inverse(series, ctl_days=42, atl_days=7, init_ctl=0, init_atl=0):
    """Simple 1/τ formula"""
    ctl, atl = init_ctl, init_atl
    
    for day in series:
        load = day["load"]
        ctl = ctl + (load - ctl) / ctl_days
        atl = atl + (load - atl) / atl_days
    
    return ctl, atl, ctl - atl


def score(ctl, atl, tsb):
    """Score how close we are to Strava (lower is better)"""
    return abs(ctl - STRAVA_CTL) + abs(atl - STRAVA_ATL) + abs(tsb - STRAVA_TSB)


def main():
    print("=" * 60)
    print("PMC Debug - Finding formula to match Strava")
    print("=" * 60)
    print(f"\nTarget (Strava): CTL={STRAVA_CTL}, ATL={STRAVA_ATL}, TSB={STRAVA_TSB}")
    
    # Connect to InfluxDB
    print(f"\nConnecting to InfluxDB at {INFLUXDB_URL}...")
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    query_api = client.query_api()
    
    # Fetch data
    print("Fetching daily loads...")
    daily_loads = fetch_daily_loads(query_api, days=365)
    print(f"Found {len(daily_loads)} days with workouts")
    
    # Build series
    series = build_full_series(daily_loads, days=365)
    print(f"Built {len(series)} day series")
    
    # Stats
    loads = [d["load"] for d in series]
    total = sum(loads)
    avg = total / len(loads)
    last_42_avg = sum(loads[-42:]) / 42
    last_7_avg = sum(loads[-7:]) / 7
    nonzero_days = sum(1 for l in loads if l > 0)
    
    print(f"\nData stats:")
    print(f"  Total load: {total:.1f}")
    print(f"  Avg daily load: {avg:.1f}")
    print(f"  Last 42 days avg: {last_42_avg:.1f}")
    print(f"  Last 7 days avg: {last_7_avg:.1f}")
    print(f"  Days with workouts: {nonzero_days}/{len(series)}")
    
    # Try different formulas
    print("\n" + "=" * 60)
    print("Testing different formulas:")
    print("=" * 60)
    
    results = []
    
    # 1. Standard EWMA, init=0
    ctl, atl, tsb = calc_pmc_standard(series, init_ctl=0, init_atl=0)
    results.append(("Standard EWMA, init=0", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 2. Standard EWMA, init=avg
    ctl, atl, tsb = calc_pmc_standard(series, init_ctl=avg, init_atl=avg)
    results.append(("Standard EWMA, init=avg", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 3. Standard EWMA, init=last_42_avg
    ctl, atl, tsb = calc_pmc_standard(series, init_ctl=last_42_avg, init_atl=last_42_avg)
    results.append(("Standard EWMA, init=last42", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 4. Simple EMA (α=2/(N+1)), init=0
    ctl, atl, tsb = calc_pmc_simple_ema(series, init_ctl=0, init_atl=0)
    results.append(("Simple EMA α=2/(N+1), init=0", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 5. Simple EMA, init=avg
    ctl, atl, tsb = calc_pmc_simple_ema(series, init_ctl=avg, init_atl=avg)
    results.append(("Simple EMA α=2/(N+1), init=avg", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 6. 1/τ formula, init=0
    ctl, atl, tsb = calc_pmc_inverse(series, init_ctl=0, init_atl=0)
    results.append(("1/τ formula, init=0", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 7. 1/τ formula, init=avg
    ctl, atl, tsb = calc_pmc_inverse(series, init_ctl=avg, init_atl=avg)
    results.append(("1/τ formula, init=avg", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 8. Try with load scaling (1.25x)
    scaled_series = [{"date": d["date"], "load": d["load"] * 1.25} for d in series]
    ctl, atl, tsb = calc_pmc_standard(scaled_series, init_ctl=0, init_atl=0)
    results.append(("Standard EWMA, load*1.25", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 9. Try with different time constants
    ctl, atl, tsb = calc_pmc_standard(series, ctl_days=35, atl_days=7, init_ctl=0, init_atl=0)
    results.append(("Standard EWMA, τ=35/7", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # 10. Try τ=28
    ctl, atl, tsb = calc_pmc_standard(series, ctl_days=28, atl_days=7, init_ctl=0, init_atl=0)
    results.append(("Standard EWMA, τ=28/7", ctl, atl, tsb, score(ctl, atl, tsb)))
    
    # Sort by score
    results.sort(key=lambda x: x[4])
    
    print(f"\n{'Formula':<35} {'CTL':>8} {'ATL':>8} {'TSB':>8} {'Score':>8}")
    print("-" * 70)
    for name, ctl, atl, tsb, sc in results:
        print(f"{name:<35} {ctl:>8.1f} {atl:>8.1f} {tsb:>8.1f} {sc:>8.1f}")
    
    print("\n" + "=" * 60)
    print(f"BEST: {results[0][0]}")
    print(f"  CTL={results[0][1]:.1f} (target {STRAVA_CTL})")
    print(f"  ATL={results[0][2]:.1f} (target {STRAVA_ATL})")
    print(f"  TSB={results[0][3]:.1f} (target {STRAVA_TSB})")
    print("=" * 60)
    
    # Show last 7 days of loads
    print("\nLast 7 days of loads:")
    for day in series[-7:]:
        print(f"  {day['date']}: {day['load']:.1f}")
    
    client.close()


if __name__ == "__main__":
    main()
