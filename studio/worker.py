"""3D studio at scale: Blender worker (several copies, number driven by KEDA).

Fetches a request from the queue when it is free (BLMOVE queue -> processing), renders it with a
fresh Blender (render.py), stores the files in Redis, then starts again. During the render, it beats
(studio:alive:<n>, every 3 s, expires in 10 s): if it disappears, the server puts the request back in the queue. When asked to stop
(SIGTERM, fewer workers), it finishes its current render, then stops.

Module s3d03: it publishes its metrics on port 9101 (render duration, wait, results: metrics.py).
Module s3d04: it marks each render after Blender, before storing it (marking.py); a marking error
puts the request in error: no unmarked file ever goes out.
Module s3d05: Blender reads the asset library (ASSETS folder, read-only); the assets used are
stored with the request, counted, and added to the marking.
Module s3d06: if the MCP server planned the scene, a fresh Blender cleans, marks and packs it
(package.py), then the worker uploads it to the other team's S3 storage, with the temporary
credentials prepared by the server. A storage failure does not fail the render.
"""
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import common as c
import marking
import metrics as m
import storage

BLENDER = os.environ.get("BLENDER", "/opt/blender/blender")
RENDER = Path(__file__).with_name("render.py")
PACKAGE = Path(__file__).with_name("package.py")
MAX_DURATION = int(os.environ.get("MAX_DURATION", "300"))
NAME = socket.gethostname()  # pod name
r = c.connection()
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())


def beat(number: int, finished: threading.Event) -> None:
    """Heartbeat: while the render lasts, a key that expires in 10 s, renewed every 3 s."""
    while not finished.wait(3):
        r.setex(c.alive(number), 10, NAME)


def render(number: int) -> None:
    d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(number)).items()}
    m.WAIT.observe(max(0.0, time.time() - float(d.get("submitted") or time.time())))  # time spent in the queue
    r.setex(c.alive(number), 10, NAME)
    r.hset(c.request(number), mapping={"status": "running", "started": time.time(), "worker": NAME})
    finished = threading.Event()
    threading.Thread(target=beat, args=(number, finished), daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "scene.py").write_text(d.get("code", ""), encoding="utf-8")
            # The model's code runs in this Blender, without the worker's environment variables.
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "ASSETS": str(c.ASSETS)}  # except the library
            scene = d.get("scene_planned") == "1"  # module s3d06: decided by the MCP server
            if scene:
                env["SCENE_BLEND"] = "1"
            with m.DURATION.time():  # timer around the render, successful or not
                output = subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(RENDER), "--", str(folder)],
                                        capture_output=True, text=True, timeout=MAX_DURATION, env=env).stdout
            result = folder / "result.json"
            if not result.exists():
                raise RuntimeError("\n".join(output.strip().splitlines()[-12:]) or "Blender produced nothing")
            info = json.loads(result.read_text(encoding="utf-8"))
            if info.get("error"):
                raise RuntimeError(info["error"])
            # Library assets used: the trace (only catalog names count).
            assets = c.asset_trace(info.get("assets", []))
            for a in assets:
                m.ASSETS_USED.labels(asset=a["name"]).inc()
            r.hset(c.request(number), mapping={"assets": ",".join(a["name"] for a in assets),
                                               "library": str(info.get("library") or "")})
            # Marking by our own code, after Blender: always in the files,
            # banner if requested. An error here: render in error, nothing goes out.
            i = marking.info(d, time.time(), assets, info.get("library"))
            visible = d.get("visible_marking") == "1"
            marking.mark_png(folder / "render.png", i, visible)
            marking.mark_glb(folder / "model.glb", i)
            r.setex(c.file(number, "png"), c.FILE_TTL, (folder / "render.png").read_bytes())
            r.setex(c.file(number, "glb"), c.FILE_TTL, (folder / "model.glb").read_bytes())
            if scene:
                upload_scene(number, d, i, folder, env)
        r.hset(c.request(number), mapping={"status": "done", "finished": time.time()})
        m.RENDERS.labels(result="done").inc()
    except subprocess.TimeoutExpired:
        r.hset(c.request(number), mapping={"status": "error", "finished": time.time(), "error": f"render interrupted after {MAX_DURATION} s"})
        m.RENDERS.labels(result="interrupted").inc()
    except Exception as e:  # the message goes back to the model, which can fix and resubmit
        r.hset(c.request(number), mapping={"status": "error", "finished": time.time(), "error": str(e)[-1500:]})
        m.RENDERS.labels(result="error").inc()
    finally:
        finished.set()
        r.lrem(c.PROCESSING, 1, number)
        r.lrem(c.LOAD, 1, number)  # request finished (successful or not): KEDA may scale down
        r.delete(c.alive(number))
        if d.get("user_id"):  # notifications (next series): a notifier will tell the user
            r.lpush(c.NOTIFICATIONS, number)


def upload_scene(number: int, d: dict, i: dict, folder: Path, env: dict) -> None:
    """Module s3d06: the Blender scene, cleaned and packed, uploaded to the other team's storage."""
    try:
        (folder / "marking.json").write_text(json.dumps(i, ensure_ascii=False), encoding="utf-8")
        # A fresh Blender, which opens the file without running its scripts (--disable-autoexec).
        subprocess.run([BLENDER, "-b", "--factory-startup", "--disable-autoexec", str(folder / "raw.blend"),
                        "--python", str(PACKAGE), "--", str(folder)], capture_output=True, timeout=120, env=env)
        if not (folder / "scene.blend").exists():
            raise RuntimeError("scene not packed")
        # Temporary credentials prepared by the MCP server: this one file, fifteen minutes.
        creds = None
        for _ in range(12):  # ready long ago, unless storage is unreachable
            creds = r.get(c.upload_s3(number))
            if creds:
                break
            time.sleep(1)
        if not creds:
            raise RuntimeError("no upload credentials")
        up = storage.upload(folder / "scene.blend", d, i, json.loads(creds))
        r.hset(c.request(number), mapping={"scene": up["key"], "scene_version": up["version"],
                                           "scene_bytes": up["bytes"], "scene_status": "uploaded"})
        m.SCENES.labels(result="uploaded").inc()
        m.SCENE_BYTES.inc(up["bytes"])
    except Exception as e:  # no storage, refused, timeout: the render stays successful
        result = "too_large" if str(e) == "too_large" else "unavailable"
        r.hset(c.request(number), mapping={"scene_status": f"{result}: {str(e)[:200]}"})
        m.SCENES.labels(result=result).inc()
    finally:
        r.delete(c.upload_s3(number))


def main() -> None:
    m.start_http_server(9101, registry=m.REGISTRY)  # metrics page, read by Prometheus
    print(f"{NAME} ready", flush=True)
    while not stop.is_set():  # stop requested (SIGTERM): finish the current render, then exit
        # Waits 2 s at most for a request, and moves it in one go from the queue to "processing".
        raw = r.blmove(c.QUEUE, c.PROCESSING, 2, "RIGHT", "LEFT")
        if raw is None:
            continue
        number = int(raw)
        print(f"{NAME}: request {number}", flush=True)
        render(number)
    print(f"{NAME} stopped", flush=True)


if __name__ == "__main__":
    main()
