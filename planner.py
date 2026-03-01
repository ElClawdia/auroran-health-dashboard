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
    
    def get_recommendation(self, health_data: Dict, allowed_sports: Optional[List[str]] = None,
                           max_workout_days: int = 6) -> Dict:
        """
        Generate personalized exercise recommendation for today.

        Constraints:
        - allowed_sports: list of selected sports
        - max_workout_days: cap for training days/week (3-7)
        """
        allowed = self._normalize_sports(allowed_sports)
        max_days = max(3, min(7, int(max_workout_days or 6)))

        recovery = self.calculate_recovery_score(health_data)

        # Determine recommendation based on recovery
        if recovery >= 85:
            rec = self._high_intensity(recovery, health_data, allowed)
        elif recovery >= 70:
            rec = self._moderate_intensity(recovery, health_data, allowed)
        elif recovery >= 50:
            rec = self._easy_intensity(recovery, health_data, allowed)
        else:
            rec = self._rest_day(recovery, health_data)

        rec["allowed_sports"] = allowed
        rec["max_workout_days"] = max_days
        rec["weekly_plan"] = self.generate_weekly_plan(health_data, allowed_sports=allowed, max_workout_days=max_days)
        return rec
    
    def _normalize_sports(self, allowed_sports: Optional[List[str]]) -> List[str]:
        supported = ["cycling", "run", "swim", "gym", "xc_skiing", "kayaking"]
        if not allowed_sports:
            return supported
        norm = []
        for s in allowed_sports:
            key = (s or "").strip().lower()
            if key in supported and key not in norm:
                norm.append(key)
        return norm or supported

    def _sport_pool(self, allowed_sports: List[str], level: str) -> List[str]:
        # Templates by intensity level
        templates = {
            "HIGH": {
                "run": ["Intervals", "Tempo Run", "Long Run"],
                "cycling": ["Threshold Ride", "VO2 Ride", "Long Ride"],
                "swim": ["Threshold Swim", "Speed Sets", "Endurance Swim"],
                "gym": ["Heavy Strength", "Power Session"],
                "xc_skiing": ["Intervals Ski", "Tempo Ski", "Long Ski"],
                "kayaking": ["Intervals Paddle", "Tempo Paddle", "Long Paddle"],
            },
            "MODERATE": {
                "run": ["Aerobic Run", "Steady Run"],
                "cycling": ["Endurance Ride", "Steady Ride"],
                "swim": ["Steady Swim", "Technique Swim"],
                "gym": ["Strength", "Mobility + Core"],
                "xc_skiing": ["Aerobic Ski", "Steady Ski"],
                "kayaking": ["Endurance Paddle", "Steady Paddle"],
            },
            "EASY": {
                "run": ["Easy Run", "Walk/Jog"],
                "cycling": ["Recovery Ride"],
                "swim": ["Easy Swim"],
                "gym": ["Mobility", "Light Strength"],
                "xc_skiing": ["Easy Ski"],
                "kayaking": ["Easy Paddle"],
            },
        }
        pool = []
        for s in allowed_sports:
            pool.extend(templates.get(level, {}).get(s, []))
        return pool

    def _suggest_workout_type(self, allowed_sports: Optional[List[str]] = None, level: str = "HIGH") -> str:
        """Suggest workout type constrained by selected sports."""
        allowed = self._normalize_sports(allowed_sports)
        pool = self._sport_pool(allowed, level)
        return pool[0] if pool else "Intervals"

    def _high_intensity(self, recovery: int, data: Dict, allowed_sports: Optional[List[str]] = None) -> Dict:
        """High intensity workout recommendation"""
        return {
            "recovery": recovery,
            "recommendation": "HIGH",
            "message": "🔥 Prime day for hard efforts! Your HRV is excellent and recovery is complete.",
            "workout": {
                "type": self._suggest_workout_type(allowed_sports, "HIGH"),
                "duration": 45,
                "zone": "3-4",
                "intensity": "High",
                "description": "Push the pace today - intervals or tempo work",
                "pace": "5:00-5:30 /km" if data.get("hrv", 40) > 45 else "5:15-5:45 /km"
            },
            "alternatives": [
                {"type": self._suggest_workout_type(allowed_sports, "MODERATE"), "duration": 60, "zone": "2-3", "description": "Steady state"},
                {"type": self._suggest_workout_type(allowed_sports, "HIGH"), "duration": 40, "zone": "4", "description": "Quality session"}
            ],
            "tips": [
                "Great HRV - your nervous system is recovered",
                "Perfect for VO2 max work",
                "Consider a race-pace effort"
            ]
        }
    
    def _moderate_intensity(self, recovery: int, data: Dict, allowed_sports: Optional[List[str]] = None) -> Dict:
        """Moderate intensity recommendation"""
        return {
            "recovery": recovery,
            "recommendation": "MODERATE",
            "message": "✅ Good to train today, but don't go too hard. Build the base.",
            "workout": {
                "type": self._suggest_workout_type(allowed_sports, "MODERATE"),
                "duration": 40,
                "zone": "2",
                "intensity": "Moderate",
                "description": "Comfortable conversational pace",
                "pace": "5:45-6:15 /km"
            },
            "alternatives": [
                {"type": self._suggest_workout_type(allowed_sports, "MODERATE"), "duration": 60, "zone": "2", "description": "Steady endurance"},
                {"type": self._suggest_workout_type(allowed_sports, "EASY"), "duration": 45, "zone": "N/A", "description": "Low stress complementary session"}
            ],
            "tips": [
                "Stay in Zone 2 for aerobic development",
                "Avoid sudden sprints",
                "Focus on form and cadence"
            ]
        }
    
    def _easy_intensity(self, recovery: int, data: Dict, allowed_sports: Optional[List[str]] = None) -> Dict:
        """Easy/light recommendation"""
        return {
            "recovery": recovery,
            "recommendation": "EASY",
            "message": "🟡 Recovery needed. Keep it light today - easy movement only.",
            "workout": {
                "type": self._suggest_workout_type(allowed_sports, "EASY"),
                "duration": 20,
                "zone": "1",
                "intensity": "Light",
                "description": "Very easy, conversational",
                "pace": "7:00+ /km or walking"
            },
            "alternatives": [
                {"type": self._suggest_workout_type(allowed_sports, "EASY"), "duration": 30, "zone": "N/A", "description": "Mobility and low-intensity work"},
                {"type": self._suggest_workout_type(allowed_sports, "EASY"), "duration": 30, "zone": "1", "description": "Easy recovery session"}
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
    
    # legacy _suggest_workout_type removed; see constrained version above
    
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
    
    def generate_weekly_plan(self, health_data: Dict,
                             allowed_sports: Optional[List[str]] = None,
                             max_workout_days: int = 6) -> List[Dict]:
        """Generate constrained weekly plan based on selected sports and max days."""
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        allowed = self._normalize_sports(allowed_sports)
        max_days = max(3, min(7, int(max_workout_days or 6)))

        recovery = self.calculate_recovery_score(health_data)
        if recovery >= 85:
            level = "HIGH"
            durations = [45, 40, 45, 40, 35, 75, 30]
        elif recovery >= 70:
            level = "MODERATE"
            durations = [40, 35, 40, 35, 30, 60, 25]
        elif recovery >= 50:
            level = "EASY"
            durations = [30, 25, 30, 25, 20, 45, 20]
        else:
            level = "EASY"
            durations = [20, 20, 20, 20, 20, 30, 20]

        pool = self._sport_pool(allowed, level)
        if not pool:
            pool = ["Easy Run"]

        # Build workout slots first; enforce max days and distribute rest days.
        plan = []
        workouts_used = 0
        pool_idx = 0

        # Keep one quality day pattern for run-only users on high/moderate weeks.
        run_only = len(allowed) == 1 and allowed[0] == "run"
        run_pattern = {
            "HIGH": ["Intervals", "Steady Run", "Long Run", "Tempo Run", "Steady Run", "Easy Run"],
            "MODERATE": ["Tempo Run", "Steady Run", "Long Run", "Steady Run", "Easy Run", "Easy Run"],
            "EASY": ["Easy Run", "Easy Run", "Walk/Jog", "Easy Run", "Walk/Jog", "Easy Run"],
        }

        for i, day in enumerate(days):
            remaining_days = len(days) - i
            remaining_workouts = max_days - workouts_used
            must_rest_now = remaining_workouts <= 0 or remaining_workouts < remaining_days - remaining_workouts

            if must_rest_now:
                plan.append({"day": day, "type": "Rest", "duration": 0})
                continue

            if run_only:
                types = run_pattern.get(level, run_pattern["MODERATE"])
                workout_type = types[min(workouts_used, len(types) - 1)]
            else:
                workout_type = pool[pool_idx % len(pool)]
                pool_idx += 1

            # Bias gym sessions to 1-2 days if gym is selected among multiple sports.
            if "gym" in allowed and len(allowed) > 1 and workouts_used in (1, 4):
                workout_type = "Strength"

            plan.append({"day": day, "type": workout_type, "duration": durations[i]})
            workouts_used += 1

        return plan


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
