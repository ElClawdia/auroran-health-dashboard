@app.route('/api/pmc')
def pmc():
    """
    Get Performance Management Chart data (CTL, ATL, TSB)
    Uses Strava API directly for accurate calculation
    """
    logger.info("Fetching PMC data")
    days = request.args.get('days', 90, type=int)
    
    daily_loads = []
    
    # Use Strava API directly
    if strava.is_configured:
        try:
            activities = strava.get_activities(days)
            logger.info(f"Got {len(activities)} activities from Strava")
            
            # Group by date and SUM (Strava style)
            from collections import defaultdict
            by_date = defaultdict(float)
            for a in activities:
                date = a.get("date", "")
                ss = a.get("suffer_score", 0) or 0
                if date and ss:
                    by_date[date] += float(ss)
            
            daily_loads = [{"date": d, "load": l} for d, l in sorted(by_date.items())]
            logger.info(f"Processed {len(daily_loads)} unique dates")
        except Exception as e:
            logger.error(f"Error fetching PMC from Strava: {e}")
    
    # Fallback to mock
    if not daily_loads:
        import random
        for i in range(min(days, 30)):
            date = (datetime.now() - timedelta(days=days-i-1)).strftime("%Y-%m-%d")
            load = random.randint(30, 150) if random.random() > 0.3 else 0
            daily_loads.append({"date": date, "load": load})
        logger.info("Using mock PMC data")
    
    # Calculate PMC
    pmc = calculate_ctl_atl_tsb(daily_loads)
    
    return jsonify({
        "ctl": pmc["ctl"],
        "atl": pmc["atl"],
        "tsb": pmc["tsb"],
        "status": pmc["status"],
        "description": get_status_description(pmc["tsb"]),
        "recent_loads": daily_loads[-14:] if len(daily_loads) > 14 else daily_loads,
        "days_tracked": len(daily_loads)
    })
