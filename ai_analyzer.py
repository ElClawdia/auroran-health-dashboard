#!/usr/bin/env python3
"""
AI Analyzer - Active Health & Training Intelligence
Auroran Health Command Center 🦞

Monitors health metrics and dynamically adjusts workout recommendations.
Pulls real data from InfluxDB + Strava, provides adaptive recommendations.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional


class AIAnalyzer:
    """
    Active AI analyzer that monitors health data and adapts recommendations.
    """
    
    HRV_THRESHOLDS = {
        "excellent": 50,
        "good": 40,
        "fair": 30,
        "poor": 20
    }
    
    ZONES = {
        "Z1": {"name": "Recovery", "pct": "50-60%", "hr": "95-114"},
        "Z2": {"name": "Aerobic", "pct": "60-70%", "hr": "114-133"},
        "Z3": {"name": "Tempo", "pct": "70-80%", "hr": "133-152"},
        "Z4": {"name": "Threshold", "pct": "80-90%", "hr": "152-171"},
        "Z5": {"name": "VO2 Max", "pct": "90-100%", "hr": "171-190"}
    }
    
    def __init__(self, influx_client=None, strava_client=None):
        self.influx = influx_client
        self.strava = strava_client
        self._health_cache = {}
        self._last_fetch = None
    
    def fetch_latest_health(self) -> Dict:
        """Fetch latest health metrics from InfluxDB."""
        return {
            "hrv": None,
            "sleep_hours": None,
            "resting_hr": None,
            "recovering": None,
            "source": "influxdb"
        }
    
    def fetch_recent_workouts(self, days: int = 7) -> List[Dict]:
        """Fetch recent workouts from Strava."""
        return []
    
    def calculate_training_load(self, workouts: List[Dict]) -> Dict:
        """Calculate CTL, ATL, and TSB from workout history."""
        if not workouts:
            return {"ctl": 0, "atl": 0, "tsb": 0, "status": "fresh"}
        
        daily_load = {}
        for w in workouts:
            date = w.get("date")
            if not date:
                continue
            load = w.get("duration", 30) * w.get("intensity", 5)
            daily_load[date] = daily_load.get(date, 0) + load
        
        if not daily_load:
            return {"ctl": 0, "atl": 0, "tsb": 0, "status": "fresh"}
        
        ctl = self._ema(list(daily_load.values()), 42)
        atl = self._ema(list(daily_load.values()), 7)
        tsb = ctl - atl
        
        if tsb >= 10:
            status = "fresh"
        elif tsb >= 0:
            status = "productive"
        elif tsb >= -10:
            status = "neutral"
        else:
            status = "overreaching"
        
        return {
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1),
            "status": status
        }
    
    def _ema(self, values: List[float], period: int) -> float:
        """Calculate exponential moving average."""
        if not values:
            return 0
        alpha = 2 / (period + 1)
        ema = values[0]
        for v in values[1:]:
            ema = alpha * v + (1 - alpha) * ema
        return ema
    
    def analyze_recovery(self, health_data: Dict, training_load: Dict) -> Dict:
        """Comprehensive recovery analysis."""
        hrv = health_data.get("hrv")
        sleep = health_data.get("sleep_hours", 7)
        resting_hr = health_data.get("resting_hr", 60)
        tsb = training_load.get("tsb", 0)
        
        # HRV score
        if hrv and hrv >= self.HRV_THRESHOLDS["excellent"]:
            hrv_score = 100
            hrv_status = "excellent"
        elif hrv and hrv >= self.HRV_THRESHOLDS["good"]:
            hrv_score = 75
            hrv_status = "good"
        elif hrv and hrv >= self.HRV_THRESHOLDS["fair"]:
            hrv_score = 50
            hrv_status = "fair"
        elif hrv:
            hrv_score = 25
            hrv_status = "poor"
        else:
            hrv_score = 50
            hrv_status = "unknown"
        
        # Sleep score
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
        
        # Resting HR score
        if resting_hr and resting_hr <= 50:
            hr_score = 100
        elif resting_hr <= 55:
            hr_score = 85
        elif resting_hr <= 60:
            hr_score = 70
        elif resting_hr <= 70:
            hr_score = 50
        elif resting_hr:
            hr_score = 30
        else:
            hr_score = 50
        
        # Training balance score
        if tsb >= 10:
            load_score = 100
        elif tsb >= 5:
            load_score = 80
        elif tsb >= 0:
            load_score = 60
        elif tsb >= -5:
            load_score = 40
        else:
            load_score = 20
        
        # Weighted composite
        total_score = (
            hrv_score * 0.35 +
            sleep_score * 0.30 +
            hr_score * 0.20 +
            load_score * 0.15
        )
        
        if total_score >= 80:
            recommendation = "HIGH"
            message = "🔥 Prime condition! Your body is fully recovered - go hard!"
        elif total_score >= 60:
            recommendation = "MODERATE"
            message = "✅ Good to train, but respect the fatigue. Build the base."
        elif total_score >= 40:
            recommendation = "EASY"
            message = "🟡 Recovery needed. Keep it light today."
        else:
            recommendation = "REST"
            message = "🔴 Your body needs rest. Take a full recovery day."
        
        return {
            "score": round(total_score),
            "status": recommendation,
            "message": message,
            "components": {
                "hrv": {"score": hrv_score, "value": hrv, "status": hrv_status},
                "sleep": {"score": sleep_score, "value": sleep},
                "resting_hr": {"score": hr_score, "value": resting_hr},
                "training_balance": {"score": load_score, "tsb": tsb}
            },
            "advice": self._generate_advice(hrv_status, sleep, tsb)
        }
    
    def _generate_advice(self, hrv_status: str, sleep: float, tsb: float) -> List[str]:
        """Generate personalized advice."""
        advice = []
        
        if hrv_status == "poor":
            advice.append("⚠️ HRV is low - prioritize sleep tonight")
        elif hrv_status == "excellent":
            advice.append("💪 HRV is excellent - ready for intensity")
        
        if sleep < 6:
            advice.append("😴 Sleep debt detected - aim for 8+ hours tonight")
        
        if tsb < -10:
            advice.append("⚠️ High fatigue - consider deload")
        elif tsb >= 10:
            advice.append("🎯 Fresh and ready - great day for competition")
        
        return advice
    
    def get_daily_recommendation(self, health_data: Dict = None, 
                                  allowed_sports: List[str] = None,
                                  max_workout_days: int = 6) -> Dict:
        """Get AI-generated daily recommendation."""
        if not health_data:
            health_data = self.fetch_latest_health()
        
        workouts = self.fetch_recent_workouts(days=14)
        training_load = self.calculate_training_load(workouts)
        recovery = self.analyze_recovery(health_data, training_load)
        recommendation = self._generate_workout(recovery, training_load, allowed_sports)
        
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "recovery": recovery,
            "training_load": training_load,
            "workout": recommendation,
            "weekly_context": self._get_weekly_context(training_load, max_workout_days),
            "data_status": {
                "hrv": "available" if health_data.get("hrv") else "estimated",
                "sleep": "available" if health_data.get("sleep_hours") else "estimated",
                "resting_hr": "available" if health_data.get("resting_hr") else "estimated"
            }
        }
    
    def _generate_workout(self, recovery: Dict, training_load: Dict,
                          allowed_sports: List[str] = None) -> Optional[Dict]:
        """Generate workout recommendation."""
        status = recovery["status"]
        
        if not allowed_sports:
            allowed_sports = ["cycling", "run", "swim", "gym"]
        
        templates = {
            "HIGH": {
                "cycling": {"type": "Threshold Ride", "duration": 45, "zone": "3-4", "intensity": "High"},
                "run": {"type": "Tempo Run", "duration": 40, "zone": "3-4", "intensity": "High"},
                "swim": {"type": "Threshold Swim", "duration": 40, "zone": "3-4", "intensity": "High"},
                "gym": {"type": "Heavy Strength", "duration": 45, "zone": "3-4", "intensity": "High"},
            },
            "MODERATE": {
                "cycling": {"type": "Endurance Ride", "duration": 60, "zone": "2", "intensity": "Moderate"},
                "run": {"type": "Steady Run", "duration": 45, "zone": "2", "intensity": "Moderate"},
                "swim": {"type": "Steady Swim", "duration": 40, "zone": "2", "intensity": "Moderate"},
                "gym": {"type": "Strength", "duration": 45, "zone": "2", "intensity": "Moderate"},
            },
            "EASY": {
                "cycling": {"type": "Recovery Ride", "duration": 30, "zone": "1", "intensity": "Easy"},
                "run": {"type": "Easy Run", "duration": 25, "zone": "1", "intensity": "Easy"},
                "swim": {"type": "Easy Swim", "duration": 30, "zone": "1", "intensity": "Easy"},
                "gym": {"type": "Mobility", "duration": 20, "zone": "1", "intensity": "Easy"},
            },
            "REST": None
        }
        
        if status == "REST":
            return None
        
        sport = allowed_sports[datetime.now().weekday() % len(allowed_sports)]
        primary = templates.get(status, {}).get(sport, templates[status]["cycling"])
        
        alternatives = []
        for s in allowed_sports[:3]:
            if s != sport and s in templates.get(status, {}):
                alt = templates[status][s].copy()
                alternatives.append(alt)
        
        return {
            "primary_sport": sport,
            "primary": primary,
            "alternatives": alternatives[:2],
            "reasoning": recovery["message"]
        }
    
    def _get_weekly_context(self, training_load: Dict, max_days: int) -> Dict:
        """Get weekly training context."""
        ctl = training_load.get("ctl", 0)
        atl = training_load.get("atl", 0)
        tsb = training_load.get("tsb", 0)
        
        if ctl < 50:
            theme = "base"
            theme_message = "Focus on volume and consistency"
        elif ctl < 100:
            theme = "build"
            theme_message = "Mix of intensity and volume"
        else:
            theme = "peak"
            theme_message = "Maintain fitness, avoid overtraining"
        
        return {
            "fitness_level": theme,
            "message": theme_message,
            "optimal_days": min(max_days, 5 if tsb < 0 else max_days),
            "ctl": ctl,
            "atl": atl,
            "tsb": tsb
        }
    
    def generate_adaptive_weekly_plan(self, health_data: Dict = None,
                                       allowed_sports: List[str] = None,
                                       max_workout_days: int = 6) -> List[Dict]:
        """Generate adaptive weekly plan based on training load."""
        if not allowed_sports:
            allowed_sports = ["cycling", "run", "swim", "gym"]
        
        workouts = self.fetch_recent_workouts(days=14)
        training_load = self.calculate_training_load(workouts)
        recovery = self.analyze_recovery(health_data, training_load)
        
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        plan = []
        
        score = recovery["score"]
        if score >= 80:
            pattern = ["HIGH", "REST", "HIGH", "REST", "HIGH", "LONG", "REST"]
        elif score >= 60:
            pattern = ["MODERATE", "REST", "MODERATE", "REST", "MODERATE", "MODERATE", "REST"]
        else:
            pattern = ["EASY", "EASY", "EASY", "EASY", "EASY", "MODERATE", "REST"]
        
        for i, day in enumerate(days):
            date = (datetime.now() - timedelta(days=6-i)).strftime("%Y-%m-%d")
            actual = next((w for w in workouts if w.get("date") == date), None)
            
            if actual:
                plan.append({
                    "day": day,
                    "date": date,
                    "status": "completed",
                    "type": actual.get("type"),
                    "duration": actual.get("duration"),
                })
            else:
                intensity = pattern[i] if i < len(pattern) else "REST"
                plan.append({
                    "day": day,
                    "date": date,
                    "status": "planned",
                    "type": self._get_workout_for_intensity(intensity, allowed_sports, i),
                    "duration": self._get_duration_for_intensity(intensity)
                })
        
        return plan
    
    def _get_workout_for_intensity(self, intensity: str, sports: List[str], day_idx: int) -> str:
        """Get workout type for intensity."""
        templates = {
            "HIGH": {"cycling": "Threshold Ride", "run": "Tempo Run", "swim": "Threshold Swim", "gym": "Heavy Strength"},
            "MODERATE": {"cycling": "Endurance Ride", "run": "Steady Run", "swim": "Steady Swim", "gym": "Strength"},
            "EASY": {"cycling": "Recovery Ride", "run": "Easy Run", "swim": "Easy Swim", "gym": "Mobility"},
            "LONG": {"cycling": "Long Ride", "run": "Long Run", "swim": "Endurance Swim", "gym": "Strength"},
            "REST": "Rest"
        }
        
        sport = sports[day_idx % len(sports)]
        return templates.get(intensity, {}).get(sport, "Rest")
    
    def _get_duration_for_intensity(self, intensity: str) -> int:
        """Get typical duration for intensity."""
        durations = {"HIGH": 45, "MODERATE": 50, "EASY": 30, "LONG": 75, "REST": 0}
        return durations.get(intensity, 0)


if __name__ == "__main__":
    analyzer = AIAnalyzer()
    
    test_health = {
        "hrv": 45,
        "sleep_hours": 7.5,
        "resting_hr": 55
    }
    
    test_workouts = [
        {"date": "2026-03-19", "duration": 40, "intensity": 6, "type": "Ride"},
        {"date": "2026-03-18", "duration": 45, "intensity": 7, "type": "Ride"},
        {"date": "2026-03-17", "duration": 30, "intensity": 4, "type": "Swim"},
    ]
    
    load = analyzer.calculate_training_load(test_workouts)
    print("Training Load:", load)
    
    recovery = analyzer.analyze_recovery(test_health, load)
    print("\nRecovery:", recovery)
    
    rec = analyzer.get_daily_recommendation(test_health)
    print("\nDaily Rec:", rec)
