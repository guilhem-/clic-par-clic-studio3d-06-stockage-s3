"""3D studio at scale: Prometheus metrics, shared by the MCP server (api.py) and the workers (worker.py).

The server publishes them on /metrics (port 8000), each worker on port 9101. Prometheus discovers
the pods by their annotations (k8s-scale.yaml) and comes to read them every five seconds.
"""
import time

from prometheus_client import CollectorRegistry, Counter, Histogram, disable_created_metrics
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, start_http_server  # noqa: F401 (for api.py and worker.py)
from prometheus_client.core import GaugeMetricFamily

import common as c

disable_created_metrics()  # no "_created" series: the page stays readable
REGISTRY = CollectorRegistry()

# A single label: the tool called. Never the user's address (one series per address, and personal data).
REQUESTS = Counter("studio_requests", "Calls received, per MCP tool", ["tool"], registry=REGISTRY)  # published: studio_requests_total
for _tool in ("submit_render", "render_status", "my_renders", "list_assets"):
    REQUESTS.labels(tool=_tool)  # series created at zero: one curve per tool from the start

# Buckets in seconds: a render takes from twenty seconds (alone) to over a minute (four in parallel).
BUCKETS = (10, 20, 30, 45, 60, 90, 120, 180, 300)
DURATION = Histogram("studio_render_duration_seconds", "Duration of a Blender render", buckets=BUCKETS, registry=REGISTRY)
WAIT = Histogram("studio_wait_seconds", "Wait in the queue before the render", buckets=BUCKETS, registry=REGISTRY)
RENDERS = Counter("studio_renders", "Finished renders, per result: done, error, interrupted", ["result"], registry=REGISTRY)
for _result in ("done", "error", "interrupted"):
    RENDERS.labels(result=_result)
# Module s3d05: assets used by the renders. Bounded label: catalog names only.
ASSETS_USED = Counter("studio_assets_used", "Library assets used by the renders", ["asset"], registry=REGISTRY)
# Module s3d06: Blender scenes uploaded to the other team's storage (bounded labels).
SCENES = Counter("studio_scenes", "Blender scenes, by result: uploaded, unavailable, too_large", ["result"], registry=REGISTRY)
for _result in ("uploaded", "unavailable", "too_large"):
    SCENES.labels(result=_result)
SCENE_BYTES = Counter("studio_scene_bytes", "Bytes of the uploaded scenes", registry=REGISTRY)
SCENE_LINKS = Counter("studio_scene_links", "Scene links followed, by result: redirected, expired, unavailable", ["result"], registry=REGISTRY)
for _result in ("redirected", "expired", "unavailable"):
    SCENE_LINKS.labels(result=_result)
CREDENTIALS = Counter("studio_temporary_credentials", "Temporary credentials obtained from storage, by use: upload, read", ["use"], registry=REGISTRY)
for _use in ("upload", "read"):
    CREDENTIALS.labels(use=_use)


def hour_key(t: float | None = None) -> str:
    """Redis key of the distinct users of the hour (HyperLogLog, expires in two hours)."""
    return "studio:users:" + time.strftime("%Y%m%d%H", time.gmtime(t))


class QueueCollector:
    """Read on every Prometheus scrape: fresh values that no server copy has to maintain."""

    def __init__(self, r):
        self.r = r

    def collect(self):
        yield GaugeMetricFamily("studio_queue_waiting", "Requests waiting in the queue", value=self.r.llen(c.QUEUE))
        yield GaugeMetricFamily("studio_renders_running", "Requests taken by a worker", value=self.r.llen(c.PROCESSING))
        yield GaugeMetricFamily("studio_users_hour", "Distinct users in the current hour", value=self.r.pfcount(hour_key()))
