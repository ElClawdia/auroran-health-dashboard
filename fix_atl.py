                # Group by date and SUM daily load (Strava style!)
                if "date" in result.columns:
                    import pandas as pd
                    
                    def get_load(row):
                        ss = row.get('suffer_score', 0)
                        if pd.notna(ss) and ss and float(ss) > 0:
                            return float(ss)
                        dur = row.get('duration', 0) or 0
                        if dur:
                            return calculate_training_load(int(dur), row.get('avg_hr'), row.get('max_hr'))
                        return 0
                    
                    result['load'] = result.apply(get_load, axis=1)
                    daily_sums = result.groupby('date')['load'].sum().reset_index()
                    
                    for _, row in daily_sums.iterrows():
                        if row['date'] and row['load'] > 0:
                            daily_loads.append({"date": row['date'], "load": row['load']})
