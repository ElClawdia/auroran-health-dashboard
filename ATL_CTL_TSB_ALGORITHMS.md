# ATL, CTL, and TSB Training Metrics: Algorithms and Calculation Methods

## Overview

ATL (Acute Training Load), CTL (Chronic Training Load), and TSB (Training Stress Balance) are key metrics in the **Performance Management Chart (PMC)** model, popularized by Dr. Andrew Coggan and TrainingPeaks. These metrics help athletes and coaches monitor training load, fitness, and freshness over time.

---

## Core Concepts

### 1. Training Stress Score (TSS) - The Foundation

Before calculating ATL, CTL, or TSB, you need a **daily training stress value**. The most common metric is **TSS (Training Stress Score)**, but alternatives exist:

| Metric | Sport/Use | Formula |
|--------|-----------|---------|
| **TSS** | Cycling (power-based) | `TSS = (Duration × NP × IF) / (FTP × 3600) × 100` |
| **hrTSS** | Heart rate-based | Uses heart rate zones instead of power |
| **rTSS** | Running | Based on pace and duration |
| **sTSS** | Swimming | Based on pace and distance |
| **TRIMP** | General endurance | Training Impulse method |

#### TSS Formula Components:
- **Duration**: Workout duration in seconds
- **NP**: Normalized Power (weighted average power)
- **IF**: Intensity Factor = NP / FTP
- **FTP**: Functional Threshold Power (power at lactate threshold)

---

## 2. CTL (Chronic Training Load) - "Fitness"

CTL represents your **long-term training load** and is often interpreted as "fitness." It uses an **Exponentially Weighted Moving Average (EWMA)** with a typical time constant of **42 days**.

### Algorithm 1: Classic EWMA Formula

```
CTL_today = CTL_yesterday + (TSS_today - CTL_yesterday) / τ_CTL
```

Where:
- `τ_CTL` = Time constant (typically 42 days)

### Algorithm 2: Decay Factor Formula

```
CTL_today = CTL_yesterday × (1 - 1/τ_CTL) + TSS_today × (1/τ_CTL)
```

Or equivalently:

```
k = 1 - exp(-1/τ_CTL)
CTL_today = CTL_yesterday × (1 - k) + TSS_today × k
```

Where for τ = 42:
- `k ≈ 0.02353` (decay constant)
- `1 - k ≈ 0.97647` (retention factor)

### Algorithm 3: Summation Form (Rolling Window Approximation)

```python
def calculate_ctl_rolling(tss_values, window=42):
    """
    Simplified rolling average approach.
    Less accurate than EWMA but simpler to understand.
    """
    weights = [exp(-i/window) for i in range(len(tss_values))]
    weighted_sum = sum(tss * w for tss, w in zip(reversed(tss_values), weights))
    return weighted_sum / sum(weights)
```

### Python Implementation (EWMA):

```python
import numpy as np

def calculate_ctl(tss_series, time_constant=42):
    """
    Calculate CTL using exponentially weighted moving average.
    
    Args:
        tss_series: Array/list of daily TSS values
        time_constant: Decay constant (default 42 days)
    
    Returns:
        Array of CTL values
    """
    k = 1 - np.exp(-1 / time_constant)
    ctl = np.zeros(len(tss_series))
    
    for i in range(1, len(tss_series)):
        ctl[i] = ctl[i-1] * (1 - k) + tss_series[i] * k
    
    return ctl
```

---

## 3. ATL (Acute Training Load) - "Fatigue"

ATL represents your **short-term training load** and indicates accumulated fatigue. It uses the same EWMA approach but with a shorter time constant of typically **7 days**.

### Algorithm (Same as CTL but different τ):

```
ATL_today = ATL_yesterday + (TSS_today - ATL_yesterday) / τ_ATL
```

Where:
- `τ_ATL` = 7 days (typical)

### Python Implementation:

```python
def calculate_atl(tss_series, time_constant=7):
    """
    Calculate ATL using exponentially weighted moving average.
    
    Args:
        tss_series: Array/list of daily TSS values
        time_constant: Decay constant (default 7 days)
    
    Returns:
        Array of ATL values
    """
    k = 1 - np.exp(-1 / time_constant)
    atl = np.zeros(len(tss_series))
    
    for i in range(1, len(tss_series)):
        atl[i] = atl[i-1] * (1 - k) + tss_series[i] * k
    
    return atl
```

---

## 4. TSB (Training Stress Balance) - "Form"

TSB indicates your **readiness to perform**. It's simply the difference between CTL and ATL.

### Formula:

```
TSB = CTL - ATL
```

### Interpretation:

| TSB Value | State | Interpretation |
|-----------|-------|----------------|
| **> +25** | Very Fresh | Possibly undertrained, good for A-races |
| **+10 to +25** | Fresh | Ready for competition |
| **0 to +10** | Neutral | Normal training state |
| **-10 to 0** | Slightly Fatigued | Absorbing training load |
| **-20 to -10** | Fatigued | Heavy training block |
| **< -30** | Very Fatigued | Risk of overtraining |

### Python Implementation:

```python
def calculate_tsb(tss_series, ctl_time_constant=42, atl_time_constant=7):
    """
    Calculate TSB (Training Stress Balance).
    
    Args:
        tss_series: Array/list of daily TSS values
        ctl_time_constant: CTL decay constant (default 42)
        atl_time_constant: ATL decay constant (default 7)
    
    Returns:
        Tuple of (CTL, ATL, TSB) arrays
    """
    ctl = calculate_ctl(tss_series, ctl_time_constant)
    atl = calculate_atl(tss_series, atl_time_constant)
    tsb = ctl - atl
    
    return ctl, atl, tsb
```

---

## 5. Alternative Algorithms and Variations

### 5.1 Banister Impulse-Response Model (Original)

The original model by Banister (1975) uses a **fitness-fatigue** approach:

```
Performance(t) = p₀ + k₁ × Fitness(t) - k₂ × Fatigue(t)
```

Where:
- `p₀` = baseline performance
- `k₁`, `k₂` = scaling factors
- `Fitness` and `Fatigue` = convolutions of training impulse with decay functions

### 5.2 TRIMP (Training Impulse)

An alternative to TSS, especially when power data isn't available:

```
TRIMP = Duration (min) × HR_ratio × 0.64 × e^(1.92 × HR_ratio)
```

Where:
```
HR_ratio = (HR_exercise - HR_rest) / (HR_max - HR_rest)
```

### 5.3 Lucia's TRIMP (Zone-Based)

```
TRIMP_lucia = (Time_Zone1 × 1) + (Time_Zone2 × 2) + (Time_Zone3 × 3)
```

### 5.4 Edwards' TRIMP

```
TRIMP_edwards = Σ (Duration_in_zone × Zone_coefficient)

Zone coefficients:
- Zone 1 (50-60% HRmax): 1
- Zone 2 (60-70% HRmax): 2
- Zone 3 (70-80% HRmax): 3
- Zone 4 (80-90% HRmax): 4
- Zone 5 (90-100% HRmax): 5
```

### 5.5 Coggan's Chronic/Acute Model Variations

Some platforms use different time constants:

| Platform | CTL (τ) | ATL (τ) |
|----------|---------|---------|
| TrainingPeaks (default) | 42 days | 7 days |
| Golden Cheetah | 42 days | 7 days |
| Intervals.icu | Configurable | Configurable |
| Custom models | 28-56 days | 5-14 days |

---

## 6. Complete Implementation Example

```python
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

class PerformanceManagement:
    """
    Complete Performance Management Chart (PMC) calculator.
    """
    
    def __init__(self, ctl_days=42, atl_days=7):
        self.ctl_days = ctl_days
        self.atl_days = atl_days
        self.ctl_decay = 1 - np.exp(-1 / ctl_days)
        self.atl_decay = 1 - np.exp(-1 / atl_days)
    
    def calculate_tss(self, duration_sec, normalized_power, ftp):
        """Calculate Training Stress Score for cycling."""
        intensity_factor = normalized_power / ftp
        tss = (duration_sec * normalized_power * intensity_factor) / (ftp * 3600) * 100
        return tss
    
    def calculate_hrtss(self, duration_min, avg_hr, hr_rest, hr_max, lthr):
        """Calculate heart rate-based TSS."""
        hr_ratio = (avg_hr - hr_rest) / (hr_max - hr_rest)
        trimp = duration_min * hr_ratio * 0.64 * np.exp(1.92 * hr_ratio)
        
        # Normalize to hour at threshold
        lthr_ratio = (lthr - hr_rest) / (hr_max - hr_rest)
        trimp_threshold = 60 * lthr_ratio * 0.64 * np.exp(1.92 * lthr_ratio)
        
        hrtss = (trimp / trimp_threshold) * 100
        return hrtss
    
    def calculate_metrics(self, tss_series):
        """
        Calculate CTL, ATL, and TSB from a series of TSS values.
        
        Args:
            tss_series: pandas Series with DatetimeIndex and TSS values
        
        Returns:
            DataFrame with CTL, ATL, TSB columns
        """
        # Ensure we have a complete date range
        date_range = pd.date_range(
            start=tss_series.index.min(),
            end=tss_series.index.max(),
            freq='D'
        )
        
        # Reindex and fill missing days with 0
        tss_daily = tss_series.reindex(date_range, fill_value=0)
        
        # Initialize arrays
        n = len(tss_daily)
        ctl = np.zeros(n)
        atl = np.zeros(n)
        
        # Calculate using EWMA
        for i in range(1, n):
            ctl[i] = ctl[i-1] * (1 - self.ctl_decay) + tss_daily.iloc[i] * self.ctl_decay
            atl[i] = atl[i-1] * (1 - self.atl_decay) + tss_daily.iloc[i] * self.atl_decay
        
        # Calculate TSB
        tsb = ctl - atl
        
        # Create result DataFrame
        result = pd.DataFrame({
            'date': date_range,
            'tss': tss_daily.values,
            'ctl': ctl,
            'atl': atl,
            'tsb': tsb
        })
        result.set_index('date', inplace=True)
        
        return result
    
    def predict_future_tsb(self, current_ctl, current_atl, planned_tss, days=7):
        """
        Predict future TSB based on planned training.
        
        Args:
            current_ctl: Current CTL value
            current_atl: Current ATL value
            planned_tss: List of planned daily TSS values
            days: Number of days to predict
        
        Returns:
            List of predicted (CTL, ATL, TSB) tuples
        """
        predictions = []
        ctl, atl = current_ctl, current_atl
        
        for i in range(days):
            tss = planned_tss[i] if i < len(planned_tss) else 0
            ctl = ctl * (1 - self.ctl_decay) + tss * self.ctl_decay
            atl = atl * (1 - self.atl_decay) + tss * self.atl_decay
            tsb = ctl - atl
            predictions.append((ctl, atl, tsb))
        
        return predictions


# Example usage
if __name__ == "__main__":
    # Create sample data
    dates = pd.date_range(start='2024-01-01', periods=90, freq='D')
    
    # Simulate training pattern (higher on weekdays, rest on weekends)
    np.random.seed(42)
    tss_values = []
    for i, date in enumerate(dates):
        if date.weekday() < 5:  # Weekdays
            tss = np.random.normal(80, 20)
        else:  # Weekends
            tss = np.random.normal(30, 10) if date.weekday() == 5 else 0
        tss_values.append(max(0, tss))
    
    tss_series = pd.Series(tss_values, index=dates)
    
    # Calculate metrics
    pmc = PerformanceManagement()
    results = pmc.calculate_metrics(tss_series)
    
    print("Last 7 days:")
    print(results.tail(7).round(1))
```

---

## 7. Key Considerations and Best Practices

### 7.1 Initial Values
- CTL and ATL typically start at 0 for new athletes
- For existing athletes, estimate initial CTL from recent training history
- Some systems use a "ramp up" period to stabilize calculations

### 7.2 Missing Data
- Days with no workout should be treated as TSS = 0
- This still affects CTL/ATL through the decay function

### 7.3 Time Constant Selection
| Athlete Type | Recommended CTL τ | Recommended ATL τ |
|--------------|-------------------|-------------------|
| Elite/Professional | 42-50 days | 7 days |
| Amateur/Recreational | 42 days | 7 days |
| Masters (older) | 35-42 days | 7-10 days |
| Juniors | 42 days | 5-7 days |

### 7.4 Limitations
1. **Individual Variation**: Response to training varies significantly between athletes
2. **Training Type**: Doesn't account for workout type (intervals vs. endurance)
3. **Non-Training Stress**: Ignores life stress, sleep, nutrition
4. **Oversimplification**: Reduces complex physiology to single numbers

---

## 8. Mathematical Foundation

The EWMA approach is based on the **first-order linear system** response:

```
dL/dt = (S(t) - L(t)) / τ
```

Where:
- `L(t)` = Training load at time t
- `S(t)` = Stress input (TSS) at time t
- `τ` = Time constant

This differential equation has the solution:

```
L(t) = L₀ × e^(-t/τ) + ∫₀ᵗ S(u) × (1/τ) × e^(-(t-u)/τ) du
```

The discrete approximation gives us our EWMA formula.

---

## References

1. Coggan, A. R., & Allen, H. (2010). *Training and Racing with a Power Meter*
2. Banister, E. W. (1991). Modeling elite athletic performance
3. Foster, C. (1998). Monitoring training in athletes with reference to overtraining syndrome
4. TrainingPeaks Documentation: https://www.trainingpeaks.com/learn/articles/the-science-of-the-performance-manager/
5. Golden Cheetah Wiki: https://github.com/GoldenCheetah/GoldenCheetah/wiki

---

## Summary

| Metric | Purpose | Time Constant | Formula |
|--------|---------|---------------|---------|
| **CTL** | Fitness indicator | 42 days | EWMA of daily TSS |
| **ATL** | Fatigue indicator | 7 days | EWMA of daily TSS |
| **TSB** | Form/readiness | N/A | CTL - ATL |

The PMC model provides a simple yet powerful framework for monitoring training load and predicting performance readiness. While it has limitations, it remains one of the most widely used tools in endurance sports coaching and self-coaching.
