import os
import signal
import subprocess
import threading
from typing import Dict

from flask import Flask, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit


app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False)


TWITCH_URL = os.getenv("TWITCH_URL", "rtmp://lax.contribute.live-video.net/app/")
TWITCH_STREAM_KEY = os.getenv("TWITCH_STREAM_KEY", "live_730458392_hziVKdS2Za41727VIAJdA4xRxg3Frl")


def get_twitch_settings(url: str):
    if not url:
        return []
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-c:a",
        "aac",
        "-strict",
        "-2",
        "-ar",
        "44100",
        "-b:a",
        "64k",
        "-y",
        "-use_wallclock_as_timestamps",
        "1",
        "-async",
        "1",
        "-bufsize",
        "1000",
        "-f",
        "flv",
        url,
    ]


ffmpeg_processes_by_sid: Dict[str, subprocess.Popen] = {}
ffmpeg_stderr_threads_by_sid: Dict[str, threading.Thread] = {}


@app.route("/")
def index():
    return "Streaming server up"


@socketio.on("connect")
def handle_connect():
    sid = request.sid
    twitch_url = TWITCH_URL
    twitch_key = TWITCH_STREAM_KEY

    if not twitch_url or not twitch_key:
        emit("error", {"message": "Server configuration error: Twitch keys missing."})
        return False

    if twitch_url.endswith("/"):
        destination = f"{twitch_url}{twitch_key}"
    else:
        destination = f"{twitch_url}/{twitch_key}"

    ffmpeg_args = [
        "-f",
        "webm",
        "-i",
        "-",
        "-v",
        "error",
    ] + get_twitch_settings(destination)

    try:
        proc = subprocess.Popen(
            ["ffmpeg"] + ffmpeg_args,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ffmpeg_processes_by_sid[sid] = proc

        def log_stderr(process: subprocess.Popen, current_sid: str):
            for line in iter(process.stderr.readline, b""):
                try:
                    app.logger.debug("[%s] %s", current_sid, line.decode().rstrip())
                except Exception:
                    pass

        t = threading.Thread(target=log_stderr, args=(proc, sid), daemon=True)
        t.start()
        ffmpeg_stderr_threads_by_sid[sid] = t
    except FileNotFoundError:
        emit("error", {"message": "FFmpeg not found on server."})
        return False
    except Exception as e:  # noqa: BLE001
        emit("error", {"message": f"Failed to start streaming process: {e}"})
        return False


@socketio.on("message")
def handle_message(data):
    sid = request.sid
    proc = ffmpeg_processes_by_sid.get(sid)
    if not proc or not proc.stdin:
        emit("error", {"message": "Streaming process not available."})
        return
    try:
        if isinstance(data, (bytes, bytearray)):
            payload = data
        else:
            try:
                payload = bytes(data)
            except Exception:
                return
        if proc.poll() is None:
            proc.stdin.write(payload)
        else:
            cleanup(sid)
    except BrokenPipeError:
        cleanup(sid)
    except Exception:
        cleanup(sid)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    cleanup(sid)


def cleanup(sid: str):
    proc = ffmpeg_processes_by_sid.pop(sid, None)
    ffmpeg_stderr_threads_by_sid.pop(sid, None)
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            if proc.poll() is None:
                proc.kill()
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.getenv("WS_PORT", "3100"))
    socketio.run(app, port=port, debug=True, allow_unsafe_werkzeug=True)


