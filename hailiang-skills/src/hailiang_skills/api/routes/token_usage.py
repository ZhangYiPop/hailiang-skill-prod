from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy import text


def build_token_usage_router(engine) -> APIRouter:
    """Protected, bounded token aggregation kept off the request hot path."""
    router = APIRouter(tags=["operations"])

    def authorize(token: str | None, authorization: str | None) -> None:
        expected = os.getenv("HAILIANG_SECURITY_ADMIN_TOKEN", "")
        bearer = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
        if not expected or not (token or bearer):
            raise HTTPException(status_code=403, detail="security administrator access required")
        if not hmac.compare_digest(token or bearer, expected):
            raise HTTPException(status_code=403, detail="security administrator access required")

    @router.get("/token-usage")
    def token_usage(
        start: datetime = Query(description="Inclusive UTC start time"),
        end: datetime = Query(description="Exclusive UTC end time"),
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorize(x_security_admin_token, authorization)
        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(status_code=400, detail="start and end must include timezone")
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start:
            raise HTTPException(status_code=400, detail="end must be after start")
        if end - start > timedelta(days=31):
            raise HTTPException(status_code=400, detail="query range cannot exceed 31 days")
        if engine is None:
            raise HTTPException(status_code=503, detail="token usage requires PostgreSQL storage")

        sql = text("""
            SELECT DATE_TRUNC('day', created_at AT TIME ZONE 'UTC') AS day,
                   COUNT(*) FILTER (WHERE payload->>'input_tokens' ~ '^[0-9]+$') AS request_count,
                   COALESCE(SUM(CASE WHEN payload->>'input_tokens' ~ '^[0-9]+$'
                                    THEN (payload->>'input_tokens')::bigint ELSE 0 END), 0) AS input_tokens,
                   COALESCE(SUM(CASE WHEN payload->>'output_tokens' ~ '^[0-9]+$'
                                    THEN (payload->>'output_tokens')::bigint ELSE 0 END), 0) AS output_tokens,
                   COALESCE(SUM(CASE WHEN payload->>'total_tokens' ~ '^[0-9]+$'
                                    THEN (payload->>'total_tokens')::bigint ELSE 0 END), 0) AS total_tokens
            FROM advisor_session_events
            WHERE created_at >= :start AND created_at < :end
              AND (payload ? 'input_tokens' OR payload ? 'output_tokens' OR payload ? 'total_tokens')
            GROUP BY 1 ORDER BY 1
        """)
        # Bound the read so an unexpectedly large event table cannot hold a
        # pooled connection (or compete with chat transactions) indefinitely.
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            rows = connection.execute(sql, {"start": start, "end": end}).mappings().all()
        daily = [
            {"day": row["day"].date().isoformat(), "request_count": int(row["request_count"] or 0),
             "input_tokens": int(row["input_tokens"] or 0), "output_tokens": int(row["output_tokens"] or 0),
             "total_tokens": int(row["total_tokens"] or 0)}
            for row in rows
        ]
        return {"start": start.isoformat(), "end": end.isoformat(), "daily": daily,
                "request_count": sum(x["request_count"] for x in daily),
                "input_tokens": sum(x["input_tokens"] for x in daily),
                "output_tokens": sum(x["output_tokens"] for x in daily),
                "total_tokens": sum(x["total_tokens"] for x in daily)}

    return router
