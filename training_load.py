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

# PMC constants
CTL_DAYS = 42  # Chronic Training Load period
ATL_DAYS = 7   # Acute Training Load period
# Exponential smoothing factor: α = 2/(N+1)
CTL_ALPHA = 2 / (CTL_DAYS + 1)
ATL_ALPHA = 2 / (ATL_DAYS + 1)


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
        hr_multiplier = 1.0
    
    load = duration_minutes * intensity * hr_multiplier
    
    return round(load, 1)


def calculate_ctl_atl_tsb(daily_loads: List[Dict]) -> Dict:
    """
    Calculate CTL, ATL, and TSB from daily training loads
    
    daily_loads: List of {date: "YYYY-MM-DD", load: float}
    
    Returns: {ctl, atl, tsb, status}
    """
    if not daily_loads:
        return {"ctl": 0, "atl": 0, "tsb": 0, "status": "No data"}
    
    # Sort by date
    sorted_loads = sorted(daily_loads, key=lambda x: x.get("date", ""))
    
    # Initialize with first load
    if sorted_loads:
        first_load = sorted_loads[0].get("load", 0)
        ctl = first_load
        atl = first_load
    else:
        ctl = atl = 0
    
    # Impulse-response model (EMA) - matches Strava's actual method!
    ATL_ALPHA = 2 / (7 + 1)  # 0.25 for 7-day
    CTL_ALPHA = 2 / (42 + 1)  # ~0.047 for 42-day
    
    all_loads = [day.get("load", 0) for day in sorted_loads]
    
    if not all_loads:
        return {"ctl": 0, "atl": 0, "tsb": 0, "status": "No data"}
    
    # Calculate EMA
    ctl = all_loads[0]
    atl = all_loads[0]
    for load in all_loads[1:]:
        ctl = CTL_ALPHA * load + (1 - CTL_ALPHA) * ctl
        atl = ATL_ALPHA * load + (1 - ATL_ALPHA) * atl
    
    tsb = ctl - atl
    
    # Determine status
    if tsb > 10:
        status = "Fresh 🏃"
    elif tsb > -10:
        status = "Balanced ⚖️"
    elif tsb > -30:
        status = "Fatigued 😴"
    else:
        status = "Overreaching ⚠️"
    
    return {
        "ctl": round(ctl, 1),
        "atl": round(atl, 1),
        "tsb": round(tsb, 1),
        "status": status
    }


def get_status_description(tsb: float) -> str:
    """Get human-readable description of TSB"""
    if tsb > 20:
        return "Peak form - Great for races! 🏆"
    elif tsb > 10:
        return "Fresh - Ready for high intensity 🏃"
    elif tsb > 0:
        return "Prepared - Good for training 💪"
    elif tsb > -10:
        return "Moderately tired - Easy training recommended ⚖️"
    elif tsb > -25:
        return "Fatigued - Rest or very easy sessions 😴"
    else:
        return "Overreaching - Take a rest day! ⚠️"


# Zone descriptions
def get_pmc_zones() -> Dict:
    """Get PMC zone descriptions"""
    return {
        "peak": {"tsb_min": 20, "color": "#22c55e", "desc": "Peak Form"},
        "fresh": {"tsb_min": 10, "color": "#84cc16", "desc": "Fresh"},
        "optimal": {"tsb_min": 0, "color": "#eab308", "desc": "Optimal"},
        "training": {"tsb_min": -10, "color": "#f97316", "desc": "Training"},
        "overreaching": {"tsb_min": float("-inf"), "color": "#ef4444", "desc": "Overreaching"}
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
