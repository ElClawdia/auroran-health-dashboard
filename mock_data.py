#!/usr/bin/env python3
"""
Mock Data Generator for Health Dashboard
Generates realistic mock health data for testing the dashboard
"""

import random
from datetime import datetime, timedelta

def generate_mock_daily_data(days=30):
    """Generate mock daily health data"""
    data = []
    base_date = datetime.now()
    
    for i in range(days):
        date = base_date - timedelta(days=days-i-1)
        
        # Realistic HRV values (Tapio's current range)
        hrv = random.randint(35, 55)
        
        # Sleep varies but averages around 7.5 hours
        sleep = round(6.5 + random.random() * 2, 1)
        
        # Resting HR typically 55-65
        resting_hr = random.randint(54, 65)
        
        # Steps - higher on active days
        steps = random.randint(4000, 15000)
        
        # Recovery score based on metrics
        recovery = min(100, int(
            (hrv / 50 * 35) +
            (sleep / 8 * 30) +
            ((70 - resting_hr) / 20 * 20) +
            random.randint(5, 15)
        ))
        
        data.append({
            "date": date.strftime("%Y-%m-%d"),
            "sleep_hours": sleep,
            "hrv": hrv,
            "resting_hr": resting_hr,
            "steps": steps,
            "recovery_score": recovery,
            "training_load": round(random.uniform(0.6, 1.5), 2)
        })
    
    return data

def generate_mock_workouts(days=14):
    """Generate mock workout data"""
    workouts = []
    base_date = datetime.now()
    
    workout_types = [
        ("Running", 25, 45, 130, 165),
        ("Cycling", 40, 90, 115, 155),
        ("Strength", 30, 60, 90, 130),
        ("HIIT", 20, 35, 145, 175),
        ("Swimming", 30, 60, 120, 160),
        ("Rest", 0, 0, 60, 80)
    ]
    
    for i in range(days):
        # Skip some days for rest
        if random.random() < 0.3:
            continue
            
        date = base_date - timedelta(days=i)
        wtype, dur_min, dur_max, hr_min, hr_max = random.choice(workout_types)
        
        duration = random.randint(dur_min, dur_max) if dur_min > 0 else 0
        avg_hr = random.randint(hr_min, hr_max) if hr_min > 0 else 65
        max_hr = hr_max + random.randint(5, 15)
        
        feeling = random.choice(["great", "great", "good", "good", "okay", "bad"])
        
        workouts.append({
            "date": date.strftime("%Y-%m-%d"),
            "type": wtype,
            "duration": duration,
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "calories": int(duration * random.uniform(8, 12)),
            "intensity": random.randint(3, 9),
            "feeling": feeling
        })
    
    return workouts

def print_mock_data():
    """Print mock data in a readable format"""
    print("=" * 60)
    print("MOCK DAILY HEALTH DATA (Last 30 Days)")
    print("=" * 60)
    
    daily = generate_mock_daily_data(30)
    for d in daily[-7:]:  # Last 7 days
        print(f"{d['date']}: Sleep={d['sleep_hours']}h | HRV={d['hrv']}ms | RHR={d['resting_hr']}bpm | Recovery={d['recovery_score']}%")
    
    print("\n" + "=" * 60)
    print("MOCK WORKOUTS (Last 14 Days)")
    print("=" * 60)
    
    workouts = generate_mock_workouts(14)
    for w in workouts[:10]:
        emoji = "🔥" if w["feeling"] == "great" else "😊" if w["feeling"] == "good" else "😐"
        print(f"{w['date']}: {w['type']} - {w['duration']}min, {w['avg_hr']}bpm avg {emoji}")

if __name__ == "__main__":
    print_mock_data()
    
    # Also generate JSON for the dashboard
    import json
    
    daily = generate_mock_daily_data(30)
    workouts = generate_mock_workouts(14)
    
    print("\n" + "=" * 60)
    print("JSON OUTPUT (for API/dashboard)")
    print("=" * 60)
    print(json.dumps({
        "daily": daily,
        "workouts": workouts
    }, indent=2))
