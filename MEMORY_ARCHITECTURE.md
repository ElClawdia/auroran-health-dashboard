# Clawdia Hybrid Memory Architecture

This project uses a **hybrid memory model**:

- **Markdown memory (`MEMORY.md`, `memory/YYYY-MM-DD.md`)** for durable, human-readable narrative memory
- **InfluxDB (`clawdia_memory` bucket)** for high-frequency episodic memory (what happened, when, and with what confidence)

---

## 1) Influx Bucket (created)

- **Bucket name:** `clawdia_memory`
- **Org:** `auroran`
- **Org ID:** `46df2bf27b8de167`
- **Retention:** infinite (`everySeconds = 0`)
- **Purpose:** machine-queried event history and pattern learning support

---

## 2) Data model (measurements)

### `events`
Atomic occurrences (messages, actions, observations).

**Tags**
- `source` (slack|whatsapp|calendar|email|sensor|system)
- `category` (communication|health|task|environment|ops)
- `actor` (user|assistant|external)
- `context` (dm|group|heartbeat|manual)

**Fields**
- `event_type` (string)
- `summary` (string)
- `importance` (float, 0-1)
- `confidence` (float, 0-1)
- `sentiment` (float, -1 to 1, optional)
- `tokens_in` (int, optional)
- `tokens_out` (int, optional)

### `patterns`
Derived regularities learned from events.

**Tags**
- `pattern_key` (e.g., `wtw->weather_helsinki`)
- `scope` (global|user|channel)
- `status` (candidate|stable|stale)

**Fields**
- `description` (string)
- `support_count` (int)
- `confidence` (float, 0-1)
- `precision` (float, 0-1)
- `recall` (float, 0-1)

### `recommendations`
Action proposals generated from current state + patterns.

**Tags**
- `domain` (health|schedule|communication|ops)
- `priority` (low|medium|high)
- `status` (proposed|accepted|rejected|executed)

**Fields**
- `title` (string)
- `reason` (string)
- `score` (float, 0-1)
- `risk` (float, 0-1)

### `feedback`
Outcome signal for closed-loop learning.

**Tags**
- `target_type` (event|pattern|recommendation)
- `target_id` (string)
- `source` (user|system|metric)

**Fields**
- `outcome` (string)
- `rating` (float, -1 to 1)
- `notes` (string)
- `confidence_delta` (float)

---

## 3) Usage policy (what goes where)

### Store in Markdown when:
- It is long-term preference, identity, decision, or high-level project direction
- Human readability/editability matters
- Information should survive schema changes

### Store in Influx when:
- It is time-series/episodic and useful for trend analysis
- It may be aggregated (hour/day/week)
- It contributes to confidence scoring, pattern mining, or anomaly detection

### Write both when:
- An event creates a durable decision
  - Example: Log event in `events`, then summarize final decision in `MEMORY.md`

---

## 4) Minimal write examples (line protocol)

```txt
events,source=slack,category=communication,actor=user,context=dm event_type="request",summary="Asked to create memory architecture",importance=0.9,confidence=1.0 1772516940000000000
patterns,pattern_key=wtw-weather,scope=user,status=stable description="wtw means weather request",support_count=12i,confidence=0.95,precision=0.93,recall=0.88 1772516940000000000
recommendations,domain=ops,priority=high,status=proposed title="Run morning summary",reason="Daily habit",score=0.86,risk=0.12 1772516940000000000
feedback,target_type=recommendation,target_id=run_morning_summary,source=user outcome="accepted",rating=1.0,notes="useful",confidence_delta=0.05 1772516940000000000
```

---

## 5) Query examples (Flux)

```flux
from(bucket: "clawdia_memory")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "events" and r.category == "communication")
  |> aggregateWindow(every: 1d, fn: count)

from(bucket: "clawdia_memory")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "patterns" and r._field == "confidence")
  |> group(columns: ["pattern_key"])
  |> last()
```

---

## 6) Guardrails

- Do **not** store secrets/tokens in Influx fields
- Keep PII minimized; prefer references or hashed IDs when possible
- Use confidence fields explicitly; avoid binary certainty
- Periodically distill Influx trends back into Markdown memory

---

## 7) Next iteration roadmap

1. Add a tiny `memory_writer.py` helper for validated writes
2. Add scheduled pattern compaction (`candidate -> stable -> stale`)
3. Build confidence calibration dashboard (Brier-like drift)
4. Add retention policy per measurement if volume increases

