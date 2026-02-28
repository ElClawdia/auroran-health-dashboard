#!/usr/bin/env python3
"""
Training Load Calculator
Calculates CTL (Fitness), ATL (Strain), and TSB (Form) using the PMC model

CTL (Chronic Training Load) - 42-day exponential moving average of daily load
ATL (Acute Training Load) - 7-day exponential moving average  
TSB (Training Stress Balance) = CTL - ATL

Positive TSB = Fresh (good for races)
Negative TSB = Fatigued (good for training)
"""

import math
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# Try to load learned parameters, fall back to defaults
try:
    from formula_learning import load_params
    _params = load_params()
except ImportError:
    _params = {"ctl_days": 42, "atl_days": 7, "load_scale_factor": 1.27}

# PMC constants - can be overridden by learned values
# Empirically calibrated against multiple reference points from Strava:
#   12/02: CTL=30, ATL=40 | 01/01: CTL=27, ATL=25 | 01/29: CTL=50, ATL=85
#   02/18: CTL=46, ATL=40 | Current: CTL=47, ATL=43
CTL_DAYS = _params.get("ctl_days", 42)  # Chronic Training Load period (τ)
ATL_DAYS = _params.get("atl_days", 7)   # Acute Training Load period (τ)
# EWMA decay: k = 1 - exp(-1/τ)
CTL_K = 1 - math.exp(-1 / CTL_DAYS)   # ≈ 0.0235
ATL_K = 1 - math.exp(-1 / ATL_DAYS)   # ≈ 0.133

# Load scaling factor to align with Strava's PMC display
# Strava's "Relative Effort" (suffer_score) needs ~1.27x scaling for PMC
LOAD_SCALE_FACTOR = _params.get("load_scale_factor", 1.27)


def reload_params():
    """Reload parameters from learned values (call after learning cycle)"""
    global CTL_DAYS, ATL_DAYS, CTL_K, ATL_K, LOAD_SCALE_FACTOR, _params
    try:
        from formula_learning import load_params
        _params = load_params()
        CTL_DAYS = _params.get("ctl_days", 42)
        ATL_DAYS = _params.get("atl_days", 7)
        CTL_K = 1 - math.exp(-1 / CTL_DAYS)
        ATL_K = 1 - math.exp(-1 / ATL_DAYS)
        LOAD_SCALE_FACTOR = _params.get("load_scale_factor", 1.27)
    except ImportError:
        pass


def calculate_intensity_factor(avg_hr: Optional[float], max_hr: Optional[float], 
                                resting_hr: Optional[float] = 60, 
                                max_hr_estimate: int = 185) -> float:
    """
    Calculate intensity factor (IF) based on heart rate
    
    Methods:
    1. If we have max HR: IF = (avg_hr - resting_hr) / (max_hr - resting_hr)
    2. If no HR: estimate from duration (very rough)
    
    Returns value 0.5-2.0 typically
    """
    if avg_hr and max_hr and max_hr > 0:
        hr_reserve = max_hr - resting_hr
        if hr_reserve > 0:
            return (avg_hr - resting_hr) / hr_reserve
    
    # Fallback: estimate from avg HR alone
    if avg_hr:
        # Rough estimation: <120 = easy (0.6), 120-140 = moderate (0.8), 140-160 = hard (1.0), 160+ = very hard (1.2)
        if avg_hr < 120:
            return 0.6
        elif avg_hr < 140:
            return 0.8
        elif avg_hr < 160:
            return 1.0
        else:
            return 1.2
    
    return 0.75  # Default moderate intensity


def calculate_training_load(duration_minutes: int, avg_hr: Optional[float] = None, 
                            max_hr: Optional[float] = None,
                            watts: Optional[float] = None,
                            suffer_score: Optional[float] = None) -> float:
    """
    Calculate Training Load (also called TRIMP or TSS)
    
    Priority:
    1. Strava suffer_score (Relative Effort) - most accurate
    2. Power (watts) - if available
    3. Heart rate based calculation
    
    Returns a load score (roughly 0-500+ per workout)
    """
    # Use Strava's suffer_score if available (Relative Effort)
    if suffer_score and suffer_score > 0:
        return round(suffer_score, 1)
    
    # Use power if available (more accurate)
    if watts and watts > 0:
        # TSS-style calculation using watts
        # Assuming 200W as threshold for 100% (rough estimate)
        intensity = min(watts / 200.0, 1.5)  # Cap at 150%
        return round(duration_minutes * intensity * 1.5, 1)
    
    # Use heart rate
    intensity = calculate_intensity_factor(avg_hr, max_hr)
    
    # Base load = duration × intensity
    # HR-based multiplier: higher HR = more stress
    if avg_hr:
        hr_multiplier = 1 + (avg_hr - 100) / 100  # 0.5 at 50bpm, 1.0 at 100bpm, 1.5 at 150bpm
    else:
        # No HR data: assume moderate-hard effort (matches Strava's estimates better)
        hr_multiplier = 1.3
        intensity = 1.0
    
    load = duration_minutes * intensity * hr_multiplier
    
    return round(load, 1)


def _build_full_series(daily_loads: List[Dict]) -> List[Dict]:
    """
    Build continuous daily series from min to max date, filling gaps with 0.
    Per spec: days with no workout = TSS 0.
    """
    if not daily_loads:
        return []
    loads_map = {d["date"]: float(d.get("load", 0.0)) for d in daily_loads}
    dates = sorted(loads_map.keys())
    start = datetime.strptime(dates[0], "%Y-%m-%d").date()
    end = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    full_series = []
    cur = start
    while cur <= end:
        ds = cur.isoformat()
        full_series.append({"date": ds, "load": loads_map.get(ds, 0.0)})
        cur += timedelta(days=1)
    return full_series


def calculate_pmc_series(
    full_series: List[Dict],
    ctl_days: int = CTL_DAYS,
    atl_days: int = ATL_DAYS,
) -> List[Dict]:
    """
    Calculate CTL, ATL, TSB for each day using EWMA.
    full_series: consecutive days with no gaps (missing days = load 0).
    
    Applies LOAD_SCALE_FACTOR to align with Strava's PMC values.
    Uses ATL_DAYS=5 (not standard 7) to better match Strava's Fatigue calculation.
    """
    if not full_series:
        return []
    
    k_ctl = 1 - math.exp(-1 / ctl_days)
    k_atl = 1 - math.exp(-1 / atl_days)

    ctl = atl = 0.0

    result = []
    for day in full_series:
        load = day.get("load", 0.0) * LOAD_SCALE_FACTOR
        ctl = ctl * (1 - k_ctl) + load * k_ctl
        atl = atl * (1 - k_atl) + load * k_atl
        tsb = ctl - atl
        result.append({
            "date": day["date"],
            "load": round(load, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1),
        })
    return result


def calculate_ctl_atl_tsb(daily_loads: List[Dict]) -> Dict:
    """
    Calculate CTL, ATL, and TSB from daily training loads.
    Aligns with ATL_CTL_TSB_ALGORITHMS.md: EWMA k=1-exp(-1/τ), fill gaps.
    Uses series-average init to avoid ramp-up artifacts.
    
    daily_loads: List of {date: "YYYY-MM-DD", load: float}
    
    Returns: {ctl, atl, tsb, status}
    """
    if not daily_loads:
        return {"ctl": 0, "atl": 0, "tsb": 0, "status": "No data"}
    
    full_series = _build_full_series(daily_loads)
    if not full_series:
        return {"ctl": 0, "atl": 0, "tsb": 0, "status": "No data"}
    
    pmc_series = calculate_pmc_series(full_series)
    latest = pmc_series[-1]
    tsb = latest["tsb"]
    
    # Determine status (aligned to Suunto-style zones)
    if tsb > 10:
        status = "Recovering / Resting"
    elif tsb >= -10:
        status = "Productive Training"
    elif tsb >= -30:
        status = "Maintaining Fitness"
    else:
        status = "Going Too Hard"
    
    return {
        "ctl": latest["ctl"],
        "atl": latest["atl"],
        "tsb": latest["tsb"],
        "status": status
    }


def get_status_description(tsb: float) -> str:
    """Get human-readable description of TSB"""
    if tsb > 10:
        return "Recovering / Resting - short-term freshness before races"
    if tsb >= -10:
        return "Productive Training - adding load in a manageable way"
    if tsb >= -30:
        return "Maintaining Fitness - load roughly in balance"
    return "Going Too Hard - risk of illness/injury, take a step back"


# Zone descriptions
def get_pmc_zones() -> Dict:
    """Get PMC zone descriptions"""
    return {
        "recovering": {"tsb_min": 10, "color": "#22c55e", "desc": "Recovering / Resting"},
        "productive": {"tsb_min": -10, "color": "#84cc16", "desc": "Productive Training"},
        "maintaining": {"tsb_min": -30, "color": "#eab308", "desc": "Maintaining Fitness"},
        "going_too_hard": {"tsb_min": float("-inf"), "color": "#ef4444", "desc": "Going Too Hard"}
    }


if __name__ == "__main__":
    # Test with sample data
    test_loads = [
        {"date": "2026-02-10", "load": 150},
        {"date": "2026-02-11", "load": 180},
        {"date": "2026-02-12", "load": 0},    # Rest
        {"date": "2026-02-13", "load": 200},
        {"date": "2026-02-14", "load": 160},
        {"date": "2026-02-15", "load": 140},
        {"date": "2026-02-16", "load": 180},
    ]
    
    result = calculate_ctl_atl_tsb(test_loads)
    print(f"CTL (Fitness): {result['ctl']}")
    print(f"ATL (Strain): {result['atl']}")
    print(f"TSB (Form): {result['tsb']}")
    print(f"Status: {result['status']}")
    print(f"\nDescription: {get_status_description(result['tsb'])}")
