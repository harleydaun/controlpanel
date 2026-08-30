"""FastAPI app: REST API + static web UI, wraps the fan controller."""
import asyncio
import contextlib
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import ConfigStore, MODES
from controller import Controller
from history import History
from ipmi import IpmiError, make_ipmi

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

store = ConfigStore()
history = History()
ipmi = make_ipmi()
controller = Controller(store, ipmi, history)


@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(controller.run())
    yield
    await controller.shutdown()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="R730xd Fan Control", lifespan=lifespan)


# --------------------------------------------------------------------- status
@app.get("/api/status")
def get_status():
    return controller.status()


@app.get("/api/history")
def get_history(seconds: int = 3600, points: int = 400):
    seconds = max(60, min(seconds, 90 * 86400))
    points = max(10, min(points, 2000))
    return history.query(seconds, points)


@app.get("/api/events")
def get_events(limit: int = 200):
    return history.events(max(1, min(limit, 1000)))


# --------------------------------------------------------------------- config
@app.get("/api/config")
def get_config():
    return store.get()


@app.put("/api/config")
def put_config(patch: dict):
    try:
        cfg = store.update(patch)
    except ValueError as e:
        raise HTTPException(400, str(e))
    controller.wake.set()
    return cfg


class ModeBody(BaseModel):
    mode: str
    manual_percent: int | None = None


@app.post("/api/mode")
def set_mode(body: ModeBody):
    if body.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")
    patch = {"mode": body.mode}
    if body.manual_percent is not None:
        patch["manual_percent"] = body.manual_percent
    try:
        cfg = store.update(patch)
    except ValueError as e:
        raise HTTPException(400, str(e))
    controller.wake.set()
    return cfg


# ------------------------------------------------------------------- profiles
class ProfileBody(BaseModel):
    name: str


@app.post("/api/profiles")
def save_profile(body: ProfileBody):
    cfg = store.get()
    profiles = cfg["profiles"]
    profiles[body.name] = {"curve": cfg["curve"], "smoothing": cfg["smoothing"]}
    try:
        return store.update({"profiles": profiles})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/profiles/{name}/apply")
def apply_profile(name: str):
    cfg = store.get()
    prof = cfg["profiles"].get(name)
    if not prof:
        raise HTTPException(404, "no such profile")
    out = store.update({"curve": prof["curve"], "smoothing": prof["smoothing"]})
    controller.wake.set()
    return out


@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    cfg = store.get()
    if name not in cfg["profiles"]:
        raise HTTPException(404, "no such profile")
    del cfg["profiles"][name]
    return store.replace(cfg)


# ---------------------------------------------------------- third-party cards
class ThirdPartyBody(BaseModel):
    disabled: bool


@app.post("/api/thirdparty")
async def set_third_party(body: ThirdPartyBody):
    try:
        await ipmi.set_third_party_response(body.disabled)
    except IpmiError as e:
        raise HTTPException(502, str(e))
    controller.third_party_disabled = await ipmi.get_third_party_response()
    controller.log("info",
                   "Third-party PCIe cooling response "
                   + ("disabled" if body.disabled else "enabled"))
    return {"disabled": controller.third_party_disabled}


@app.post("/api/test")
async def test_connection():
    try:
        await ipmi.check()
        return {"ok": True}
    except IpmiError as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------- static
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
