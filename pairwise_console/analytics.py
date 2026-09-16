from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from .db import Database


SHANGHAI = timezone(timedelta(hours=8))


def dashboard(db: Database) -> Dict[str, Any]:
    task_counts = db.all("SELECT status,COUNT(*) count FROM tasks GROUP BY status")
    pair_counts = db.all("SELECT status,COUNT(*) count FROM pairs GROUP BY status")
    verdicts = db.all("SELECT verdict,COUNT(*) count FROM gsb_reviews WHERE status='confirmed' GROUP BY verdict")
    types = db.all(
        """SELECT t.task_type,COUNT(*) count FROM pairs p JOIN tasks t ON t.id=p.task_id
           WHERE p.status='completed' GROUP BY t.task_type"""
    )
    artifact = db.all("SELECT status,COUNT(*) count FROM artifact_checks GROUP BY status")
    recording = db.all("SELECT status,COUNT(*) count FROM recordings GROUP BY status")
    recent = db.all(
        """SELECT substr(datetime(completed_at,'+8 hours'),1,13) hour,COUNT(*) count
           FROM pairs WHERE completed_at IS NOT NULL AND completed_at >= datetime('now','-24 hours')
           GROUP BY hour ORDER BY hour"""
    )
    completed_total = db.one("SELECT COUNT(*) count FROM pairs WHERE status='completed'") or {"count": 0}
    active = db.one("SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running','review')") or {"count": 0}
    confirmed = db.one("SELECT COUNT(*) count FROM gsb_reviews WHERE status='confirmed'") or {"count": 0}
    peak = max(recent, key=lambda x: x["count"], default={"hour": "", "count": 0})
    active_hours = sum(1 for row in recent if row["count"])
    return {
        "generatedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "summary": {
            "completedPairs": completed_total["count"],
            "activePairs": active["count"],
            "confirmedGsb": confirmed["count"],
            "peakHour": peak["hour"],
            "peakHourCount": peak["count"],
            "activeHours24h": active_hours,
            "hourlyAverage24h": round(sum(x["count"] for x in recent) / 24.0, 2),
        },
        "taskStatus": task_counts,
        "pairStatus": pair_counts,
        "verdicts": verdicts,
        "taskTypes": types,
        "artifactChecks": artifact,
        "recordings": recording,
        "trend24h": recent,
    }

