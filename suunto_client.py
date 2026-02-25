#!/usr/bin/env python3
"""
Suunto API Client
Integrates with apizone.suunto.com to fetch health data
"""

import os
import requests
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional

class SuuntoClient:
    """Client for Suunto API integration"""
    
    BASE_URL = "https://apizone.suunto.com/v1"
    
    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id or os.getenv('SUUNTO_CLIENT_ID', '')
        self.client_secret = client_secret or os.getenv('SUUNTO_CLIENT_SECRET', '')
        self.access_token = None
        self.token_expiry = None
    
    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)
    
    def _get_token(self) -> Optional[str]:
        """Get OAuth access token"""
        if not self.is_configured:
            return None
        
        # Check if we have a valid token
        if self.access_token and self.token_expiry:
            if datetime.now() < self.token_expiry:
                return self.access_token
        
        # Get new token
        try:
            response = requests.post(
                f"{self.BASE_URL}/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self.token_expiry = datetime.now() + timedelta(seconds=expires_in - 60)
                return self.access_token
        except Exception as e:
            print(f"Token fetch error: {e}")
        
        return None
    
    def _request(self, endpoint: str, params: dict = None) -> dict:
        """Make authenticated API request"""
        token = self._get_token()
        if not token:
            return {"error": "Not authenticated"}
        
        try:
            response = requests.get(
                f"{self.BASE_URL}{endpoint}",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"API error: {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}
    
    def get_daily_summaries(self, days: int = 7) -> List[Dict]:
        """
        Get daily summaries (dailies)
        Includes: steps, calories, HR, HRV, sleep
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        result = self._request(
            "/dailies",
            params={
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d")
            }
        )
        
        if "error" in result:
            return []
        
        dailies = []
        for day in result.get("data", []):
            dailies.append({
                "date": day.get("date"),
                "steps": day.get("steps", 0),
                "calories": day.get("calories", 0),
                "distance_meters": day.get("distance", 0),
                "sleep_hours": day.get("sleepDuration", 0) / 3600,  # Convert seconds
                "hrv": day.get("hrv", {}).get("average", 0),
                "resting_hr": day.get("hr", {}).get("resting", 0),
                "max_hr": day.get("hr", {}).get("maximum", 0),
                "min_hr": day.get("hr", {}).get("minimum", 0)
            })
        
        return dailies
    
    def get_exercises(self, days: int = 30) -> List[Dict]:
        """Get exercise/sport sessions"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        result = self._request(
            "/exercises",
            params={
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "limit": 100
            }
        )
        
        if "error" in result:
            return []
        
        exercises = []
        for ex in result.get("data", []):
            exercises.append({
                "id": ex.get("id"),
                "date": ex.get("startTime", "")[:10],
                "start_time": ex.get("startTime"),
                "duration_seconds": ex.get("duration", 0),
                "type": ex.get("sport", {}).get("name", "Unknown"),
                "avg_hr": ex.get("heartRate", {}).get("average", 0),
                "max_hr": ex.get("heartRate", {}).get("maximum", 0),
                "calories": ex.get("calories", 0),
                "distance": ex.get("distance", 0),
                "elevation_gain": ex.get("elevation", {}).get("ascent", 0)
            })
        
        return exercises
    
    def get_sleep_data(self, days: int = 7) -> List[Dict]:
        """Get detailed sleep analysis"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        result = self._request(
            "/sleeps",
            params={
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d")
            }
        )
        
        if "error" in result:
            return []
        
        sleeps = []
        for sleep in result.get("data", []):
            sleeps.append({
                "date": sleep.get("dateOfSleep", ""),
                "duration_hours": sleep.get("duration", 0) / 3600,
                "deep_sleep_hours": sleep.get("deepSleepDuration", 0) / 3600,
                "rem_sleep_hours": sleep.get("remSleepDuration", 0) / 3600,
                "hrv": sleep.get("hrv", {}).get("average", 0),
                "quality": sleep.get("quality", 0)
            })
        
        return sleeps
    
    def get_recovery(self, date: str = None) -> Dict:
        """Get daily recovery recommendation"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        
        result = self._request(f"/recovery/{date}")
        
        if "error" in result:
            return {}
        
        return {
            "date": date,
            "score": result.get("score", 0),
            "status": result.get("status", "unknown"),
            "description": result.get("description", ""),
            "recommendations": result.get("recommendations", [])
        }


# Demo mode - generates mock data for testing
class MockSuuntoClient(SuuntoClient):
    """Mock client for demo/testing"""
    
    def __init__(self):
        super().__init__("", "")
    
    def get_daily_summaries(self, days: int = 7) -> List[Dict]:
        dailies = []
        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            # Generate realistic mock data with some variance
            base_hrv = 35 + (i % 5) * 2  # Oscillate around 40
            dailies.append({
                "date": date.strftime("%Y-%m-%d"),
                "steps": 5000 + i * 1000 + (i % 3) * 2000,
                "calories": 1800 + i * 100,
                "distance_meters": 4000 + i * 800,
                "sleep_hours": 6.5 + (i % 4) * 0.5,
                "hrv": base_hrv + (i % 7),
                "resting_hr": 62 - (i % 4),
                "max_hr": 175,
                "min_hr": 52
            })
        return dailies
    
    def get_exercises(self, days: int = 30) -> List[Dict]:
        exercises = []
        sports = ["Running", "Cycling", "Strength", "Swimming", "HIIT"]
        for i in range(min(days, 10)):
            date = datetime.now() - timedelta(days=i*3)
            exercises.append({
                "id": f"ex_{i}",
                "date": date.strftime("%Y-%m-%d"),
                "start_time": date.isoformat(),
                "duration_seconds": 1800 + i * 600,
                "type": sports[i % len(sports)],
                "avg_hr": 130 + i * 5,
                "max_hr": 165 + i * 3,
                "calories": 250 + i * 50,
                "distance": 3000 + i * 500,
                "elevation_gain": 50 + i * 20
            })
        return exercises


if __name__ == "__main__":
    # Test with mock data
    client = MockSuuntoClient()
    print("Mock daily summaries:")
    for day in client.get_daily_summaries(7):
        print(f"  {day}")
