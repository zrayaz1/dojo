import json
import os
import textwrap
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from redis import asyncio as aioredis


REDIS_URL = os.environ.get("REDIS_URL", "redis://cache:6379/0")
JOB_PREFIX = os.environ.get("WORKSPACE_JOB_PREFIX", "dojo:docker_job:")
REFRESH_SECONDS = int(os.environ.get("WORKSPACE_JOB_REFRESH", "3"))
redis_client = aioredis.from_url(
    REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
)


app = FastAPI()


async def _load_job(job_id: str) -> Optional[dict]:
    payload = await redis_client.get(f"{JOB_PREFIX}{job_id}")
    if not payload:
        return None
    return json.loads(payload)


def _wait_markup(job: dict) -> str:
    challenge = job.get("challenge_name") or "workspace"
    dojo = job.get("dojo_name") or "dojo"
    message = f"Preparing {challenge} ({dojo})"
    if job.get("practice"):
        message += " in practice mode"
    refresh = max(REFRESH_SECONDS, 1)
    return textwrap.dedent(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta http-equiv="refresh" content="{refresh}">
            <title>Preparing workspace…</title>
            <style>
                body {{
                    font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
                    background-color: #050607;
                    color: #f2f4f8;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                }}
                .wrap {{
                    text-align: center;
                    max-width: 480px;
                    padding: 2rem;
                }}
                .spinner {{
                    width: 3rem;
                    height: 3rem;
                    border: 0.35rem solid rgba(255,255,255,.2);
                    border-top-color: #f29f05;
                    border-radius: 50%;
                    margin: 0 auto 1.5rem;
                    animation: spin 0.8s linear infinite;
                }}
                @keyframes spin {{
                    to {{ transform: rotate(360deg); }}
                }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <div class="spinner"></div>
                <h1>Hang tight…</h1>
                <p>{message}. This page refreshes automatically.</p>
            </div>
        </body>
        </html>
        """
    ).strip()


def _error_markup(job: dict) -> str:
    detail = job.get("error") or "Workspace failed to initialize."
    return textwrap.dedent(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <title>Workspace failed to start</title>
            <style>
                body {{
                    font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
                    background-color: #050607;
                    color: #f2f4f8;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                }}
                .wrap {{
                    text-align: center;
                    max-width: 480px;
                    padding: 2rem;
                }}
                h1 {{
                    color: #ff6673;
                }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <h1>Workspace failed to start</h1>
                <p>{detail}</p>
                <p>Please restart the challenge.</p>
            </div>
        </body>
        </html>
        """
    ).strip()


@app.get("/workspace/job/{job_id}/{token}")
async def handle_workspace_job(job_id: str, token: str):
    try:
        job = await _load_job(job_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to query workspace job") from exc
    if not job or job.get("token") != token:
        raise HTTPException(status_code=404, detail="Unknown workspace job")

    headers = {"Cache-Control": "no-store"}
    state = job.get("state", "pending")

    if state == "ready" and job.get("workspace_url"):
        return RedirectResponse(
            url=job["workspace_url"],
            status_code=302,
            headers=headers,
        )

    if state == "error":
        return HTMLResponse(
            content=_error_markup(job),
            status_code=502,
            headers=headers,
        )

    return HTMLResponse(
        content=_wait_markup(job),
        status_code=200,
        headers=headers,
    )

