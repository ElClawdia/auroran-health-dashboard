#!/usr/bin/env python3
"""
Formula Learning Module for ATL/CTL Calibration

This module implements a learning mechanism that adjusts the PMC formula
parameters based on manually entered reference values. Over time, the
formula will converge to match the user's expected values (e.g., from Strava).

The learning process:
1. User enters manual CTL/ATL values for specific dates
2. System stores these as "ground truth" reference points
3. Periodically, the system runs an optimization to find parameters
   that minimize the error between calculated and manual values
4. The optimized parameters are stored and used for future calculations
"""

import json
import math
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

# Default parameters (can be overridden by learned values)
DEFAULT_PARAMS = {
    "ctl_days": 60,
    "atl_days": 7,
    "load_scale_factor": 1.4,
}

# File to store learned parameters
PARAMS_FILE = Path(__file__).parent / "learned_params.json"


def load_params() -> Dict:
    """Load learned parameters from file, or return defaults"""
    if PARAMS_FILE.exists():
        try:
            with open(PARAMS_FILE) as f:
                params = json.load(f)
                # Merge with defaults to ensure all keys exist
                return {**DEFAULT_PARAMS, **params}
        except Exception:
            pass
    return DEFAULT_PARAMS.copy()


def save_params(params: Dict):
    """Save learned parameters to file"""
    with open(PARAMS_FILE, 'w') as f:
        json.dump(params, f, indent=2)


def calculate_pmc_with_params(
    daily_loads: Dict[str, float],
    ctl_days: float,
    atl_days: float,
    load_scale_factor: float,
    target_date: str
) -> Tuple[float, float, float]:
    """
    Calculate CTL, ATL, TSB for a specific date using given parameters.
    
    Args:
        daily_loads: Dict mapping date strings (YYYY-MM-DD) to load values
        ctl_days: Time constant for CTL (chronic training load)
        atl_days: Time constant for ATL (acute training load)
        load_scale_factor: Multiplier for raw load values
        target_date: Date to calculate values for
        
    Returns:
        Tuple of (CTL, ATL, TSB)
    """
    if not daily_loads:
        return 0.0, 0.0, 0.0
    
    # Calculate decay factors
    ctl_k = 1 - math.exp(-1 / ctl_days)
    atl_k = 1 - math.exp(-1 / atl_days)
    
    # Get sorted dates
    all_dates = sorted(daily_loads.keys())
    if not all_dates:
        return 0.0, 0.0, 0.0
    
    # Build full date range
    start_date = datetime.strptime(all_dates[0], "%Y-%m-%d")
    end_date = datetime.strptime(target_date, "%Y-%m-%d")
    
    if start_date > end_date:
        return 0.0, 0.0, 0.0
    
    # Initialize
    ctl = 0.0
    atl = 0.0
    
    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        load = daily_loads.get(date_str, 0.0) * load_scale_factor
        
        # EWMA update
        ctl = ctl + ctl_k * (load - ctl)
        atl = atl + atl_k * (load - atl)
        
        current += timedelta(days=1)
    
    tsb = ctl - atl
    return ctl, atl, tsb


def calculate_error(
    daily_loads: Dict[str, float],
    reference_points: List[Dict],
    ctl_days: float,
    atl_days: float,
    load_scale_factor: float
) -> float:
    """
    Calculate total squared error between calculated and reference values.
    
    Args:
        daily_loads: Dict mapping date strings to load values
        reference_points: List of dicts with 'date', 'ctl', 'atl' keys
        ctl_days, atl_days, load_scale_factor: Parameters to test
        
    Returns:
        Sum of squared errors
    """
    total_error = 0.0
    
    for ref in reference_points:
        date = ref.get("date")
        ref_ctl = ref.get("ctl")
        ref_atl = ref.get("atl")
        
        if not date:
            continue
        
        calc_ctl, calc_atl, _ = calculate_pmc_with_params(
            daily_loads, ctl_days, atl_days, load_scale_factor, date
        )
        
        if ref_ctl is not None:
            total_error += (calc_ctl - ref_ctl) ** 2
        if ref_atl is not None:
            total_error += (calc_atl - ref_atl) ** 2
    
    return total_error


def optimize_parameters(
    daily_loads: Dict[str, float],
    reference_points: List[Dict],
    current_params: Dict = None
) -> Dict:
    """
    Find optimal parameters that minimize error against reference points.
    
    Uses a simple grid search followed by local refinement.
    
    Args:
        daily_loads: Dict mapping date strings to load values
        reference_points: List of manual reference points
        current_params: Starting parameters (optional)
        
    Returns:
        Dict with optimized parameters
    """
    if not reference_points or len(reference_points) < 2:
        # Need at least 2 reference points for meaningful optimization
        return current_params or DEFAULT_PARAMS.copy()
    
    if current_params is None:
        current_params = DEFAULT_PARAMS.copy()
    
    best_params = current_params.copy()
    best_error = calculate_error(
        daily_loads, reference_points,
        best_params["ctl_days"],
        best_params["atl_days"],
        best_params["load_scale_factor"]
    )
    
    # Grid search around current values
    ctl_range = [best_params["ctl_days"] + d for d in range(-5, 6, 1)]
    atl_range = [best_params["atl_days"] + d for d in range(-2, 3, 1)]
    scale_range = [best_params["load_scale_factor"] + s * 0.05 for s in range(-4, 5)]
    
    # Constrain to reasonable ranges
    ctl_range = [c for c in ctl_range if 30 <= c <= 50]
    atl_range = [a for a in atl_range if 4 <= a <= 10]
    scale_range = [s for s in scale_range if 0.8 <= s <= 1.8]
    
    for ctl_days in ctl_range:
        for atl_days in atl_range:
            for scale in scale_range:
                error = calculate_error(
                    daily_loads, reference_points,
                    ctl_days, atl_days, scale
                )
                
                if error < best_error:
                    best_error = error
                    best_params = {
                        "ctl_days": ctl_days,
                        "atl_days": atl_days,
                        "load_scale_factor": round(scale, 3)
                    }
    
    return best_params


def get_reference_points_from_influx(query_api, bucket: str) -> List[Dict]:
    """
    Fetch manual CTL/ATL values from InfluxDB.
    
    Returns list of dicts with 'date', 'ctl', 'atl' keys.
    """
    reference_points = []
    
    try:
        query = f'''
        from(bucket: "{bucket}")
          |> range(start: -365d)
          |> filter(fn: (r) => r._measurement == "manual_values")
          |> filter(fn: (r) => r._field == "ctl" or r._field == "atl")
          |> filter(fn: (r) => r.deleted != "true")
          |> pivot(rowKey: ["_time", "date"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query(query)
        
        for table in result:
            for record in table.records:
                date = record.values.get("date")
                ctl = record.values.get("ctl")
                atl = record.values.get("atl")
                
                if date and (ctl is not None or atl is not None):
                    reference_points.append({
                        "date": date,
                        "ctl": float(ctl) if ctl is not None else None,
                        "atl": float(atl) if atl is not None else None
                    })
    except Exception as e:
        print(f"Error fetching reference points: {e}")
    
    return reference_points


def run_learning_cycle(query_api, bucket: str, daily_loads: Dict[str, float]) -> Dict:
    """
    Run a learning cycle: fetch reference points, optimize, save.
    
    Args:
        query_api: InfluxDB query API
        bucket: InfluxDB bucket name
        daily_loads: Dict mapping date strings to load values
        
    Returns:
        Updated parameters
    """
    current_params = load_params()
    reference_points = get_reference_points_from_influx(query_api, bucket)
    
    if len(reference_points) >= 2:
        new_params = optimize_parameters(daily_loads, reference_points, current_params)
        
        # Only save if parameters changed significantly
        if (abs(new_params["ctl_days"] - current_params["ctl_days"]) >= 1 or
            abs(new_params["atl_days"] - current_params["atl_days"]) >= 1 or
            abs(new_params["load_scale_factor"] - current_params["load_scale_factor"]) >= 0.02):
            
            new_params["last_updated"] = datetime.now().isoformat()
            new_params["reference_count"] = len(reference_points)
            save_params(new_params)
            return new_params
    
    return current_params
