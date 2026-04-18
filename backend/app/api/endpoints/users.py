import datetime
from collections import Counter
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.rate_limit import limiter
from app.core.security import get_current_user_id
from app.db.supabase import get_supabase

router = APIRouter()


@router.get("/me/activity")
@limiter.limit("60/minute")
async def my_activity(
    request: Request,
    days: int = Query(default=365, ge=1, le=730),
    user_id: str = Depends(get_current_user_id),
):
    """GitHub-style activity: count of cards answered per day over the last N days.

    Returns a dense array of {date, count} from `since` to today (UTC), so the
    frontend can render a contiguous heatmap without filling gaps itself.
    """
    today = datetime.datetime.utcnow().date()
    since = today - datetime.timedelta(days=days - 1)

    supabase = get_supabase()
    rows = (
        supabase.table("user_reviews")
        .select("created_at")
        .eq("user_id", user_id)
        .gte("created_at", since.isoformat())
        .execute()
        .data
        or []
    )

    per_day: Counter = Counter()
    for r in rows:
        raw = r.get("created_at")
        if not raw:
            continue
        try:
            d = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        per_day[d.isoformat()] += 1

    out = []
    cursor = since
    while cursor <= today:
        key = cursor.isoformat()
        out.append({"date": key, "count": per_day.get(key, 0)})
        cursor += datetime.timedelta(days=1)

    total = sum(per_day.values())
    active_days = sum(1 for v in per_day.values() if v > 0)
    return {
        "from": since.isoformat(),
        "to": today.isoformat(),
        "total": total,
        "active_days": active_days,
        "days": out,
    }
