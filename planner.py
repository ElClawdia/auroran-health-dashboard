#!/usr/bin/env python3
"""
Exercise Planner & Recommendation Engine
Auroran Health Command Center 🦞

Generates personalized exercise recommendations based on:
- HRV trends (key recovery indicator)
- Sleep quality and duration
- Resting heart rate
- Training load history
- Recent workout intensity
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional
import math


class ExercisePlanner:
    """AI-powered exercise recommendation engine"""
    
    # Training zones based on HR
    ZONES = {
        "Z1": {"name": "Recovery", "pct": "50-60%", "hr": "95-114"},
        "Z2": {"name": "Aerobic", "pct": "60-70%", "hr": "114-133"},
        "Z3": {"name": "Tempo", "pct": "70-80%", "hr": "133-152"},
        "Z4": {"name": "Threshold", "pct": "80-90%", "hr": "152-171"},
        "Z5": {"name": "VO2 Max", "pct": "90-100%", "hr": "171-190"}
    }
    
    # Recovery thresholds
    RECOVERY_THRESHOLDS = {
        "HIGH": 80,
        "MODERATE": 60,
        "EASY": 40,
        "REST": 0
    }
    
    def __init__(self):
        self.history_days = 30
    
    def calculate_recovery_score(self, health_data: Dict) -> int:
        """
        Calculate recovery score (0-100) based on multiple factors:
        - HRV percentile (35%)
        - Sleep quality (30%)
        - Resting HR vs baseline (20%)
        - Days since intense workout (15%)
        """
        score = 0
        
        # HRV contribution (higher = better recovery)
        hrv = health_data.get("hrv", 40)
        if hrv >= 55:
            hrv_score = 100
        elif hrv >= 45:
            hrv_score = 80
        elif hrv >= 35:
            hrv_score = 60
        elif hrv >= 25:
            hrv_score = 40
        else:
            hrv_score = 20
        score += hrv_score * 0.35
        
        # Sleep contribution
        sleep = health_data.get("sleep_hours", 7)
        if sleep >= 8:
            sleep_score = 100
        elif sleep >= 7:
            sleep_score = 85
        elif sleep >= 6:
            sleep_score = 60
        elif sleep >= 5:
            sleep_score = 40
        else:
            sleep_score = 20
        score += sleep_score * 0.30
        
        # Resting HR contribution (lower = better)
        resting_hr = health_data.get("resting_hr", 60)
        if resting_hr <= 50:
            hr_score = 100
        elif resting_hr <= 55:
            hr_score = 85
        elif resting_hr <= 60:
            hr_score = 70
        elif resting_hr <= 70:
            hr_score = 50
        else:
            hr_score = 30
        score += hr_score * 0.20
        
        # Training load contribution
        training_load = health_data.get("training_load", 1.0)
        if training_load <= 0.8:
            load_score = 100
        elif training_load <= 1.0:
            load_score = 80
        elif training_load <= 1.2:
            load_score = 60
        elif training_load <= 1.5:
            load_score = 40
        else:
            load_score = 20
        score += load_score * 0.15
        
        return min(100, max(0, int(score)))
    
    def get_recommendation(self, health_data: Dict) -> Dict:
        """
        Generate personalized exercise recommendation for today
        """
        recovery = self.calculate_recovery_score(health_data)
        
        # Determine recommendation based on recovery
        if recovery >= 85:
            return self._high_intensity(recovery, health_data)
        elif recovery >= 70:
            return self._moderate_intensity(recovery, health_data)
        elif recovery >= 50:
            return self._easy_intensity(recovery, health_data)
        else:
            return self._rest_day(recovery, health_data)
    
    def _high_intensity(self, recovery: int, data: Dict) -> Dict:
        """High intensity workout recommendation"""
        return {
            "recovery": recovery,
            "recommendation": "HIGH",
            "message": "🔥 Prime day for hard efforts! Your HRV is excellent and recovery is complete.",
            "workout": {
                "type": self._suggest_workout_type(),
                "duration": 45,
                "zone": "3-4",
                "intensity": "High",
                "description": "Push the pace today - intervals or tempo work",
                "pace": "5:00-5:30 /km" if data.get("hrv", 40) > 45 else "5:15-5:45 /km"
            },
            "alternatives": [
                {"type": "Long Run", "duration": 60, "zone": "2-3", "description": "Steady state"},
                {"type": "Hill Repeats", "duration": 40, "zone": "4", "description": "6x4min hills"}
            ],
            "tips": [
                "Great HRV - your nervous system is recovered",
                "Perfect for VO2 max work",
                "Consider a race-pace effort"
            ]
        }
    
    def _moderate_intensity(self, recovery: int, data: Dict) -> Dict:
        """Moderate intensity recommendation"""
        return {
            "recovery": recovery,
            "recommendation": "MODERATE",
            "message": "✅ Good to train today, but don't go too hard. Build the base.",
            "workout": {
                "type": "Aerobic Run",
                "duration": 40,
                "zone": "2",
                "intensity": "Moderate",
                "description": "Comfortable conversational pace",
                "pace": "5:45-6:15 /km"
            },
            "alternatives": [
                {"type": "Cycling", "duration": 60, "zone": "2", "description": "Steady endurance"},
                {"type": "Strength", "duration": 45, "zone": "N/A", "description": "Full body"}
            ],
            "tips": [
                "Stay in Zone 2 for aerobic development",
                "Avoid sudden sprints",
                "Focus on form and cadence"
            ]
        }
    
    def _easy_intensity(self, recovery: int, data: Dict) -> Dict:
        """Easy/light recommendation"""
        return {
            "recovery": recovery,
            "recommendation": "EASY",
            "message": "🟡 Recovery needed. Keep it light today - easy movement only.",
            "workout": {
                "type": "Easy Walk/Jog",
                "duration": 20,
                "zone": "1",
                "intensity": "Light",
                "description": "Very easy, conversational",
                "pace": "7:00+ /km or walking"
            },
            "alternatives": [
                {"type": "Yoga", "duration": 30, "zone": "N/A", "description": "Mobility and stretching"},
                {"type": "Swimming", "duration": 30, "zone": "1", "description": "Easy laps"}
            ],
            "tips": [
                "Your body needs recovery",
                "Focus on sleep tonight",
                "Consider active release/massage"
            ]
        }
    
    def _rest_day(self, recovery: int, data: Dict) -> Dict:
        """Rest day recommendation"""
        return {
            "recovery": recovery,
            "recommendation": "REST",
            "message": "🔴 Full rest day. Your body needs recovery - no exercise.",
            "workout": None,
            "alternatives": [
                {"type": "Mobility", "duration": 15, "zone": "N/A", "description": "Gentle stretching"},
                {"type": "Sauna", "duration": 20, "zone": "N/A", "description": "Relaxation"}
            ],
            "tips": [
                "Prioritize sleep tonight",
                "Stay hydrated",
                "Good nutrition will help recovery",
                "HRV indicates accumulated fatigue"
            ]
        }
    
    def _suggest_workout_type(self) -> str:
        """Suggest workout type based on weekly structure"""
        # In a full implementation, check day of week and training plan
        return "Intervals"
    
    def calculate_training_load(self, workouts: List[Dict]) -> float:
        """
        Calculate acute:chronic workload ratio (ACWR)
        - Acute: last 7 days
        - Chronic: last 28 days
        """
        if not workouts:
            return 0
        
        now = datetime.now()
        acute = 0
        chronic = 0
        
        for w in workouts:
            if "date" not in w:
                continue
            try:
                w_date = datetime.strptime(w["date"], "%Y-%m-%d")
                days_ago = (now - w_date).days
                
                # RPE-based load
                duration = w.get("duration", 30)  # minutes
                rpe = w.get("intensity", 5)  # 1-10 scale
                load = duration * rpe
                
                if days_ago < 7:
                    acute += load
                if days_ago < 28:
                    chronic += load
            except:
                continue
        
        if chronic == 0:
            return 0
        
        return acute / (chronic / 4)  # Normalized to weekly
    
    def predict_race_time(self, recent_5k: float, hrv: int) -> Dict:
        """
        Predict marathon time based on recent 5K and current HRV
        Uses Riegel's formula with HRV adjustment
        """
        if not recent_5k or recent_5k <= 0:
            return {}
        
        # Riegel's formula: T2 = T1 * (D2/D1)^1.06
        # Marathon = 42.195km, 5K = 5km
        base_time = recent_5k * (42.195 / 5) ** 1.06
        
        # HRV adjustment (higher = better recovery = faster)
        hrv_factor = 1.0
        if hrv > 50:
            hrv_factor = 0.95  # 5% faster
        elif hrv > 40:
            hrv_factor = 1.0
        elif hrv > 30:
            hrv_factor = 1.08  # 8% slower
        
        predicted = base_time * hrv_factor
        
        hours = int(predicted // 60)
        minutes = int(predicted % 60)
        
        return {
            "predicted_marathon": f"{hours}:{minutes:02d}:00",
            "predicted_hm": f"{hours//2}:{minutes//2:02d}:00",
            "confidence": "high" if hrv > 45 else "medium",
            "notes": "Based on current HRV and recent 5K"
        }
    
    def get_periodization_phase(self, week_number: int = None) -> str:
        """
        Determine current training phase
        Options: Recovery, Base, Build, Peak, Deload
        """
        if week_number is None:
            # Use current week of year
            week_number = datetime.now().isocalendar()[1]
        
        # Simple periodization based on week mod 4
        phases = ["Recovery", "Base", "Build", "Peak"]
        return phases[week_number % 4]
    
    def generate_weekly_plan(self, health_data: Dict) -> List[Dict]:
        """Generate a full weekly training plan"""
        recovery = self.calculate_recovery_score(health_data)
        phase = self.get_periodization_phase()
        
        if phase == "Recovery":
            return [
                {"day": "Mon", "type": "Rest", "intensity": 0},
                {"day": "Tue", "type": "Easy Run", "duration": 30, "zone": "1-2"},
                {"day": "Wed", "type": "Rest", "intensity": 0},
                {"day": "Thu", "type": "Easy Run", "duration": 30, "zone": "1-2"},
                {"day": "Fri", "type": "Rest", "intensity": 0},
                {"day": "Sat", "type": "Walk", "duration": 45, "zone": "1"},
                {"day": "Sun", "type": "Rest", "intensity": 0},
            ]
        elif phase == "Base":
            return [
                {"day": "Mon", "type": "Easy Run", "duration": 40, "zone": "2"},
                {"day": "Tue", "type": "Strength", "duration": 45, "zone": "N/A"},
                {"day": "Wed", "type": "Rest", "intensity": 0},
                {"day": "Thu", "type": "Tempo Run", "duration": 35, "zone": "3"},
                {"day": "Fri", "type": "Rest", "intensity": 0},
                {"day": "Sat", "type": "Long Run", "duration": 60, "zone": "2"},
                {"day": "Sun", "type": "Rest", "intensity": 0},
            ]
        elif phase == "Build":
            return [
                {"day": "Mon", "type": "Intervals", "duration": 40, "zone": "4"},
                {"day": "Tue", "type": "Easy Run", "duration": 35, "zone": "2"},
                {"day": "Wed", "type": "Strength", "duration": 45, "zone": "N/A"},
                {"day": "Thu", "type": "Tempo", "duration": 40, "zone": "3"},
                {"day": "Fri", "type": "Rest", "intensity": 0},
                {"day": "Sat", "type": "Long Run", "duration": 75, "zone": "2-3"},
                {"day": "Sun", "type": "Rest", "intensity": 0},
            ]
        else:  # Peak
            return [
                {"day": "Mon", "type": "Intervals", "duration": 45, "zone": "4-5"},
                {"day": "Tue", "type": "Medium Run", "duration": 45, "zone": "2-3"},
                {"day": "Wed", "type": "Rest", "intensity": 0},
                {"day": "Thu", "type": "Race Pace", "duration": 50, "zone": "4"},
                {"day": "Fri", "type": "Easy Run", "duration": 30, "zone": "1-2"},
                {"day": "Sat", "type": "Race", "duration": 90, "zone": "3-4"},
                {"day": "Sun", "type": "Rest", "intensity": 0},
            ]


if __name__ == "__main__":
    # Test the planner
    planner = ExercisePlanner()
    
    test_data = {
        "hrv": 42,
        "sleep_hours": 7.5,
        "resting_hr": 58,
        "training_load": 1.1
    }
    
    recovery = planner.calculate_recovery_score(test_data)
    print(f"Recovery score: {recovery}")
    
    rec = planner.get_recommendation(test_data)
    print(f"\nToday's recommendation:")
    print(f"  {rec['message']}")
    print(f"  Workout: {rec['workout']}")
    
    print(f"\nWeekly plan:")
    for day in planner.generate_weekly_plan(test_data):
        print(f"  {day}")
