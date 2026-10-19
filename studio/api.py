"""3D studio at scale: stateless MCP server, several copies behind the "studio" service.

Same tools as in module s3d01 (submit_render, render_status, my_renders): for Open WebUI, nothing
changes. No copy keeps any state in memory: requests go to Redis, and the workers (worker.py)
fetch them there. Each copy also watches for requests whose worker disappeared, and puts them
back in the queue.

Module s3d03: each tool counts its calls, and the metrics are published on /metrics (metrics.py).
Module s3d04: each request gets a unique id and a visible-marking choice (visible_marking
parameter, organization policy); the worker marks the render (marking.py).
Module s3d05: asset library (3D objects, HDRI lighting) mounted read-only; the list_assets
tool lets the model discover it, the /a/ route publishes the thumbnails.
Module s3d06: the Blender scene of each render is uploaded to another team's S3 storage.
The server keeps the long-term keys: it prepares the temporary upload credentials when a
worker takes the request, and answers the scene link with a redirect to a presigned link.
"""
import asyncio
import hmac
import json
import time
import uuid

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

import common as c
import metrics as m
import storage

r = c.connection()
m.REGISTRY.register(m.QueueCollector(r))  # queue, renders running, users: read in Redis on every Prometheus scrape
# Stateless: each call stands on its own, any copy can answer it.
mcp = FastMCP("studio", host="0.0.0.0", port=8000, stateless_http=True)


def _headers(ctx: Context):
    req = ctx.request_context.request
    return req.headers if req is not None else {}


def _user(ctx: Context) -> str:
    """The user's address (header filled in by Open WebUI), after checking the studio token."""
    headers = _headers(ctx)
    if headers.get("authorization", "") != f"Bearer {c.TOKEN}":
        raise PermissionError("invalid studio token")
    email = headers.get("x-user-email", "").strip().lower()
    if not email:
        raise PermissionError("unknown user: X-User-Email header missing")
    # Distinct users of the hour: counted in Redis, never the address as a label.
    hour = m.hour_key()
    r.pfadd(hour, email)
    r.expire(hour, 7200)
    return email


def _position(number: int) -> int | None:
    """1 = next in line; None if the request is no longer waiting."""
    waiting = [int(x) for x in r.lrange(c.QUEUE, 0, -1)]  # left = most recent
    if number not in waiting:
        return None
    return len(waiting) - waiting.index(number)


# ---------------------------------------------------------------- MCP tools

@mcp.tool()
def submit_render(title: str, code: str, ctx: Context,
                  visible_marking: bool | None = None) -> dict:
    """Queues the render of a Blender scene and answers immediately.

    code: Python script (bpy) that builds the scene in an empty scene: objects, materials,
    light, camera. Do not set the render engine or save: the studio does it
    (Cycles, 1280 x 720, PNG + GLB 3D model). Decor objects (floor, table, background) must have
    a name starting with "Decor": they are rendered but not exported in the 3D model.
    visible_marking: true for a visible banner on the image (source and date),
    only if the user asks for a visible notice; otherwise, leave it out.
    The studio always marks the file invisibly (metadata).
    The studio has a library of real objects and HDRI lighting: search with list_assets,
    then use load_asset("name") and environment("name") in the code, never a file path.
    Returns the request number and its position in the queue."""
    m.REQUESTS.labels(tool="submit_render").inc()
    user = _user(ctx)
    uid = _headers(ctx).get("x-user-id", "").strip()  # notifications (next series)
    # Banner: enforced by the organization, otherwise the user's choice,
    # otherwise the default policy.
    choice = visible_marking if visible_marking is not None else c.MARKING_DEFAULT
    visible = c.MARKING_ENFORCED or choice
    number = r.incr("studio:counter")  # atomic: a unique number, even with several copies
    r.hset(c.request(number), mapping={
        "number": number, "title": title[:80], "code": code, "user": user,
        "user_id": uid, "status": "queued", "submitted": time.time(),
        # The number is for humans, the id for traceability.
        "id": str(uuid.uuid4()),
        "visible_marking": "1" if visible else "0",
        "scene_planned": "1" if c.STORAGE else "0",  # module s3d06: the Blender scene will be uploaded
    })
    r.lpush(c.QUEUE, number)  # the queue: workers take from the right
    r.lpush(c.LOAD, number)   # the load, read by KEDA: one entry per request waiting or running
    follow_up = "The user will be notified in Open WebUI." if uid else "Ask for its status with render_status."
    return {"number": number, "title": title, "position": _position(number), "visible_marking": visible,
            "message": f"Request recorded. {follow_up}"}


@mcp.tool()
def render_status(number: int, ctx: Context) -> dict:
    """Gives the status of one of the user's requests: queued (with its position), running,
    done (with the image in Markdown and the 3D artifact to copy as is, and the Blender scene
    link, scene_blend_markdown, to copy as is if present) or error."""
    m.REQUESTS.labels(tool="render_status").inc()
    user = _user(ctx)
    d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(number)).items()}
    if not d or d.get("user") != user:
        return {"number": number, "error": "no request with this number for this user"}
    status = {"number": number, "title": d["title"], "status": d["status"],
              "id": d.get("id", ""), "visible_marking": d.get("visible_marking") == "1"}
    if d["status"] == "queued":
        status["position"] = _position(number)
    elif d["status"] == "running":
        status["running_seconds"] = round(time.time() - float(d["started"]))
        status["worker"] = d.get("worker", "")
    elif d["status"] == "done":
        status["duration_seconds"] = round(float(d["finished"]) - float(d["started"]))
        status["worker"] = d.get("worker", "")
        if d.get("assets"):  # library assets used: name, source, license
            status["assets"] = c.asset_trace(d["assets"].split(","))
        status["image_markdown"] = f'![{d["title"]}]({c.link(number, "png")})'
        status["model_3d"] = c.artifact(number, d["title"])
        if d.get("scene"):  # module s3d06: the Blender scene, through the studio's stable link
            status["scene_blend_markdown"] = f"[{c.SCENE_LABEL}]({c.link(number, 'blend')})"
            status["scene_available_until"] = time.strftime("%Y-%m-%d", time.gmtime(float(d["finished"]) + c.SCENE_TTL))
        elif d.get("scene_planned") == "1":
            status["scene_blend"] = "unavailable"
    else:
        status["error"] = d.get("error", "")
    return status


@mcp.tool()
def my_renders(ctx: Context) -> list[dict]:
    """Lists the signed-in user's requests (status, worker, position), most recent first."""
    m.REQUESTS.labels(tool="my_renders").inc()
    user = _user(ctx)
    last = int(r.get("studio:counter") or 0)
    results = []
    for n in range(last, 0, -1):
        d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(n)).items()}
        if d.get("user") == user:
            line = {"number": n, "title": d["title"], "status": d["status"], "visible_marking": d.get("visible_marking") == "1"}
            if d["status"] == "queued":
                line["position"] = _position(n)
            if d.get("worker"):
                line["worker"] = d["worker"]
            results.append(line)
    return results


@mcp.tool()
def list_assets(ctx: Context, search: str = "", kind: str = "") -> list[dict]:
    """Searches the studio's asset library: real 3D objects (kind "model") and
    HDRI lighting (kind "environment"). Call it before building a piece of furniture, an object or
    lighting: if it exists, use it. search: one word (chair, lamp, studio…), empty for everything.
    In the scene code: load_asset("name", position=(x, y, z), rotation_z=…) for an object
    (placed on the floor at this position, dimensions in meters: width, depth, height; front
    facing -y: rotation_z=math.pi to face +y), environment("name") for the lighting.
    To show the library to the user, copy thumbnail_markdown."""
    m.REQUESTS.labels(tool="list_assets").inc()
    _user(ctx)  # same check as the other tools
    word = search.strip().lower()
    results = []
    for family, sheets in (("model", c.catalog()["models"]), ("environment", c.catalog()["environments"])):
        if kind and not family.startswith(kind.strip().lower()[:5]):
            continue
        for name, f in sheets.items():
            # Search in the names, in three languages, and in the tags.
            text = " ".join([name, *f["name"].values(), *f.get("tags", [])]).lower()
            if word and word not in text:
                continue
            label = f["name"].get(c.LANGUAGE, f["name"]["EN"])
            results.append({
                "name": name,                                # the name to write in the code
                "kind": family,
                "label": label,
                "dimensions_m": f.get("dimensions_m"),       # to place objects without guessing
                "thumbnail_markdown": f"![{label}]({c.PUBLIC}/a/{name}.jpg)",
                "example": f'load_asset("{name}")' if family == "model" else f'environment("{name}")',
                "source": "Poly Haven, CC0" if f["source"].get("polyhaven") else f["source"].get("license", ""),
            })
    return results


# ---------------------------------------------------------------- Files (signed links) and health

@mcp.custom_route("/r/{number}/{name}", methods=["GET"])
async def serve(request: Request) -> Response:
    try:
        number = int(request.path_params["number"])
        sig, extension = request.path_params["name"].rsplit(".", 1)
    except ValueError:
        return Response(status_code=404)
    if extension not in ("png", "glb", "blend") or not hmac.compare_digest(sig, c.signature(number, extension)):
        return Response(status_code=404)
    if extension == "blend":  # module s3d06: redirect to storage, sixty-second link
        return await _scene(number)
    content = r.get(c.file(number, extension))
    if content is None:
        return Response(status_code=404)
    return Response(content, media_type="image/png" if extension == "png" else "model/gltf-binary",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "private, max-age=3600"})


async def _scene(number: int) -> Response:
    d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(number)).items()}
    if not d.get("scene") or time.time() > float(d.get("finished") or 0) + c.SCENE_TTL:
        m.SCENE_LINKS.labels(result="expired").inc()
        return Response(status_code=404)
    try:
        url = await asyncio.to_thread(storage.presigned_link, d)  # read credentials, then signature
    except Exception as e:  # storage unreachable: the rest of the studio keeps going
        print(f"scene link {number}: {e}")
        m.SCENE_LINKS.labels(result="unavailable").inc()
        return Response(status_code=503)
    m.SCENE_LINKS.labels(result="redirected").inc()
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/a/{file}", methods=["GET"])
async def thumbnail(request: Request) -> Response:
    """Library thumbnails: public, unsigned (royalty-free assets, no user data)."""
    name, _, extension = request.path_params["file"].rpartition(".")
    cat = c.catalog()
    sheet = cat["models"].get(name) or cat["environments"].get(name)  # name looked up, never concatenated as is
    if extension != "jpg" or sheet is None:
        return Response(status_code=404)
    return FileResponse(c.ASSETS / sheet["thumbnail"], media_type="image/jpeg",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request) -> Response:
    """Metrics in the Prometheus format: internal to the cluster (no Ingress publishes /metrics)."""
    return Response(m.generate_latest(m.REGISTRY), media_type=m.CONTENT_TYPE_LATEST)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse({"ok": bool(r.ping()), "queued": r.llen(c.QUEUE), "processing": r.llen(c.PROCESSING), "load": r.llen(c.LOAD)})


# Routes of the next series (tracking widget, Pipe function): a separate file, not in this repository.
try:
    import integrations
    integrations.connect(mcp, r, _position)
except ImportError:
    pass


# ---------------------------------------------------------------- Orphan requests

async def _watch() -> None:
    """Puts back in the queue the requests whose worker no longer beats (pod deleted, hard stop)."""
    suspects: set[int] = set()  # requests without a heartbeat in the previous round
    while True:
        await asyncio.sleep(3)
        try:
            for raw in r.lrange(c.PROCESSING, 0, -1):
                n = int(raw)
                if r.exists(c.alive(n)):  # the worker beats: all good
                    suspects.discard(n)
                    continue
                if n not in suspects:      # first round without a heartbeat: wait for the next one
                    suspects.add(n)
                    continue
                suspects.discard(n)
                if r.lrem(c.PROCESSING, 1, raw):  # only one server copy wins
                    r.hset(c.request(n), mapping={"status": "queued", "worker": ""})
                    r.rpush(c.QUEUE, n)  # on the right: it goes first again
        except Exception as e:  # Redis briefly unavailable: try again
            print(f"watch: {e}")


# ---------------------------------------------------------------- Upload credentials (module s3d06)

async def _prepare_uploads() -> None:
    """As soon as a worker takes a request, upload credentials for it alone (fifteen minutes).
    The worker never sees the long-term key: it stays in the MCP server."""
    while True:
        await asyncio.sleep(2)
        try:
            running = r.lrange(c.PROCESSING, 0, -1)
        except Exception:  # Redis briefly unavailable: try again
            continue
        for raw in running:
            n = int(raw)
            try:
                # A single server copy handles it (SET NX), once per request.
                if r.hget(c.request(n), "scene_planned") != b"1" or not r.set(f"studio:upload-prepared:{n}", 1, nx=True, ex=c.CREDENTIALS_TTL):
                    continue
                d = {c.text(k): c.text(v) for k, v in r.hgetall(c.request(n)).items()}
                creds = await asyncio.to_thread(storage.prepare_upload, d)
                r.setex(c.upload_s3(n), c.CREDENTIALS_TTL, json.dumps(creds))
            except Exception as e:  # storage unreachable: try again in the next round
                r.delete(f"studio:upload-prepared:{n}")
                print(f"upload credentials {n}: {e}")


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    startup = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(application):
        tasks = [asyncio.create_task(_watch())]
        if c.STORAGE:
            tasks.append(asyncio.create_task(_prepare_uploads()))
        async with startup(application):
            yield
        for task in tasks:
            task.cancel()

    app.router.lifespan_context = lifespan
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
