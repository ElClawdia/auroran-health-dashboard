#!/usr/bin/env python3
"""
Strava API Client
Integrates with Strava to fetch workout data
"""

import os
import requests
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional

class StravaClient:
    """Client for Strava API integration"""
    
    BASE_URL = "https://www.strava.com/api/v3"
    TOKEN_URL = "https://www.strava.com/oauth/token"
    
    def __init__(self, access_token: str = "", client_id: str = "", client_secret: str = "", refresh_token: str = ""):
        self.access_token = access_token or os.getenv('STRAVA_ACCESS_TOKEN', '')
        self.client_id = client_id or os.getenv('STRAVA_CLIENT_ID', '')
        self.client_secret = client_secret or os.getenv('STRAVA_CLIENT_SECRET', '')
        self.refresh_token = refresh_token or os.getenv('STRAVA_REFRESH_TOKEN', '')
    
    @property
    def is_configured(self) -> bool:
        return bool(self.access_token)
    
    def refresh_access_token(self) -> bool:
        """Refresh the access token using refresh_token"""
        if not all([self.client_id, self.client_secret, self.refresh_token]):
            print("ERROR: Missing client_id, client_secret, or refresh_token for token refresh")
            return False
        
        try:
            response = requests.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get("access_token", "")
                self.refresh_token = data.get("refresh_token", "")
                print(f"✓ Strava token refreshed successfully")
                return True
            else:
                print(f"ERROR: Token refresh failed: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            print(f"ERROR: Token refresh exception: {e}")
            return False
    
    def _request(self, endpoint: str, params: dict = None) -> dict:
        """Make authenticated API request"""
        if not self.is_configured:
            return {"error": "Not authenticated"}
        
        try:
            response = requests.get(
                f"{self.BASE_URL}{endpoint}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json"
                }
            )
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                # Token expired - try to refresh
                if self.refresh_access_token():
                    # Retry with new token
                    response = requests.get(
                        f"{self.BASE_URL}{endpoint}",
                        params=params,
                        headers={
                            "Authorization": f"Bearer {self.access_token}",
                            "Content-Type": "application/json"
                        }
                    )
                    if response.status_code == 200:
                        return response.json()
                return {"error": "Invalid access token"}
            else:
                return {"error": f"API error: {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}
    
    def get_activities(self, days: int = 30) -> List[Dict]:
        """
        Get recent activities/workouts
        """
        result = self._request(
            "/athlete/activities",
            {"before": int(datetime.now().timestamp()), "after": int((datetime.now() - timedelta(days=days)).timestamp()), "per_page": 100}
        )
        
        if "error" in result:
            return []
        
        activities = []
        for activity in result:
            # Extract time from start_date_local (e.g., "2026-02-15T15:25:30Z" -> "15:25")
            start_time = activity.get("start_date_local", "")
            time_str = start_time[11:16] if len(start_time) > 11 else ""
            
            activities.append({
                "id": activity.get("id"),
                "date": activity.get("start_date", "")[:10],
                "time": time_str,
                "type": activity.get("type", "Unknown"),
                "name": activity.get("name", ""),
                "duration": int(activity.get("moving_time", 0) / 60),  # Convert to minutes
                "distance": activity.get("distance", 0),  # meters
                "avg_hr": activity.get("average_heartrate"),
                "max_hr": activity.get("max_heartrate"),
                "calories": activity.get("calories"),
                "elevation_gain": activity.get("total_elevation_gain"),
                "suffer_score": activity.get("suffer_score", 0),
                "relative_effort": activity.get("suffer_score", 0),  # Strava's Relative Effort
                "feeling": self._guess_feeling(activity)
            })
        
        return activities
    
    def _guess_feeling(self, activity: Dict) -> str:
        """Guess feeling based on heart rate data"""
        avg_hr = activity.get("average_heartrate")
        max_hr = activity.get("max_heartrate")
        
        if not avg_hr:
            return "good"
        
        # Simple heuristic based on HR
        if avg_hr < 130:
            return "great"
        elif avg_hr < 150:
            return "good"
        else:
            return "okay"
    
    def get_athlete(self) -> Dict:
        """Get athlete profile"""
        return self._request("/athlete")
    
    def sync_to_influxdb(self, write_api, bucket: str, org: str, days: int = 30) -> int:
        """Fetch activities and write to InfluxDB"""
        if not self.is_configured:
            return 0
        
        activities = self.get_activities(days)
        
        if write_api and activities:
            from influxdb_client import Point
            for activity in activities:
                point = Point("workouts")\
                    .tag("type", activity.get("type", "Unknown"))\
                    .tag("date", activity.get("date", ""))\
                    .field("duration_minutes", float(activity.get("duration", 0)))\
                    .field("distance_meters", float(activity.get("distance", 0)))\
                    .field("avg_hr", float(activity.get("avg_hr", 0)) if activity.get("avg_hr") else 0.0)\
                    .field("max_hr", float(activity.get("max_hr", 0)) if activity.get("max_hr") else 0.0)\
                    .field("calories", activity.get("calories", 0))\
                    .field("elevation_gain", float(activity.get("elevation_gain", 0)))\
                    .field("feeling", activity.get("feeling", "good"))
                
                write_api.write(bucket=bucket, org=org, record=point)
        
        return len(activities)


# Mock client for demo
class MockStravaClient(StravaClient):
    """Mock Strava client for demo"""
    
    def get_activities(self, days: int = 30) -> List[Dict]:
        import random
        activities = []
        sports = ["Run", "Ride", "Swim", "Workout", "Hike"]
        
        for i in range(min(days, 10)):
            if random.random() < 0.4:  # Skip some days
                continue
            date = datetime.now() - timedelta(days=i)
            sport = random.choice(sports)
            duration = random.randint(20, 90)
            avg_hr = random.randint(120, 165) if sport != "Swim" else random.randint(110, 150)
            
            activities.append({
                "id": f"strava_{i}",
                "date": date.strftime("%Y-%m-%d"),
                "type": sport,
                "name": f"{sport} {date.strftime('%b %d')}",
                "duration": duration,
                "distance": random.randint(2000, 15000) if sport in ["Run", "Ride"] else 0,
                "avg_hr": avg_hr,
                "max_hr": avg_hr + random.randint(15, 30),
                "calories": int(duration * random.uniform(8, 12)),
                "elevation_gain": random.randint(50, 300) if sport == "Run" else random.randint(100, 500),
                "feeling": random.choice(["great", "great", "good", "okay"])
            })
        
        return activities


if __name__ == "__main__":
    # Test with mock
    client = MockStravaClient()
    for a in client.get_activities(7):
        print(f"{a['date']}: {a['type']} - {a['duration']}min, {a.get('avg_hr', 'N/A')}bpm")
