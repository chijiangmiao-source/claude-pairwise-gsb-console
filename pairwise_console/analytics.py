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
           GROUP BY t.task_type ORDER BY count DESC,t.task_type"""
    )
    categories = db.all(
        """SELECT t.project_category,COUNT(*) count FROM pairs p JOIN tasks t ON t.id=p.task_id
           GROUP BY t.project_category ORDER BY count DESC,t.project_category"""
    )
    artifact = db.all("SELECT status,COUNT(*) count FROM artifact_checks GROUP BY status")
    recording = db.all("SELECT status,COUNT(*) count FROM recordings GROUP BY status")
    recent = db.all(
        """SELECT substr(datetime(completed_at,'+8 hours'),1,13) hour,COUNT(*) count
           FROM pairs WHERE completed_at IS NOT NULL AND completed_at >= datetime('now','-24 hours')
           GROUP BY hour ORDER BY hour"""
    )
    recent_pairs = db.all(
        """SELECT p.id pair_id,p.chain_id project_number,p.status,p.stage,p.created_at,p.updated_at,p.completed_at,
                  t.title,t.task_type,t.difficulty,t.project_category,g.verdict,g.status gsb_status,
                  (SELECT COUNT(DISTINCT c.arm) FROM artifact_checks c WHERE c.pair_id=p.id AND c.status='passed') checks_passed,
                  (SELECT COUNT(DISTINCT r.arm) FROM recordings r WHERE r.pair_id=p.id AND r.status='passed' AND r.commit_match=1) recordings_passed
             FROM pairs p JOIN tasks t ON t.id=p.task_id
             LEFT JOIN gsb_reviews g ON g.pair_id=p.id
            ORDER BY COALESCE(p.completed_at,p.updated_at) DESC,p.id LIMIT 60"""
    )
    total = db.one("SELECT COUNT(*) count FROM pairs") or {"count": 0}
    completed_total = db.one("SELECT COUNT(*) count FROM pairs WHERE status='completed'") or {"count": 0}
    active = db.one("SELECT COUNT(*) count FROM pairs WHERE status IN ('queued','running','review')") or {"count": 0}
    confirmed = db.one("SELECT COUNT(*) count FROM gsb_reviews WHERE status='confirmed'") or {"count": 0}
    peak = max(recent, key=lambda x: x["count"], default={"hour": "", "count": 0})
    active_hours = sum(1 for row in recent if row["count"])
    return {
        "generatedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "summary": {
            "totalPairs": total["count"],
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
        "projectCategories": categories,
        "recentPairs": recent_pairs,
        "artifactChecks": artifact,
        "recordings": recording,
        "trend24h": recent,
    }
