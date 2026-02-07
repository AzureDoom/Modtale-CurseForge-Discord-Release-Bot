 
import os
import re
import json
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

import aiohttp
import discord
import signal
from urllib.parse import urljoin
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

CACHE_FILE = "cache.json"
MODTALE_BASE_URL = "https://api.modtale.net/"
_CF_FILE_ID_RE = re.compile(r"/files/(\d+)")


@dataclass(frozen=True)
class ModtaleProjectCfg:
    project_uuid: str
    api_token: str = ""


@dataclass(frozen=True)
class CurseforgeProjectCfg:
    project_id: str
    project_slug: str


@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int

    poll_seconds: int
    curseforge_poll_seconds: int

    modtale_projects: List[ModtaleProjectCfg]
    curseforge_projects: List[CurseforgeProjectCfg]


def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _parse_json_env_optional(name: str) -> Any:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Env var {name} must be valid JSON: {e}") from e


def load_config() -> Config:
    poll_seconds = int(os.getenv("POLL_SECONDS", "300"))
    cf_poll_seconds = int(os.getenv("CURSEFORGE_POLL_SECONDS", str(poll_seconds)))

    # Expect JSON arrays in .env
    modtale_raw = _parse_json_env_optional("MODTALE_PROJECTS_JSON")
    curseforge_raw = _parse_json_env_optional("CURSEFORGE_PROJECTS_JSON")

    if not isinstance(modtale_raw, list):
        raise RuntimeError("MODTALE_PROJECTS_JSON must be a JSON array")
    if not isinstance(curseforge_raw, list):
        raise RuntimeError("CURSEFORGE_PROJECTS_JSON must be a JSON array")

    modtale_projects: List[ModtaleProjectCfg] = []
    for i, p in enumerate(modtale_raw):
        if not isinstance(p, dict):
            raise RuntimeError(f"MODTALE_PROJECTS_JSON[{i}] must be an object")
        uuid = str(p.get("project_uuid") or p.get("uuid") or "").strip()
        if not uuid:
            raise RuntimeError(f"MODTALE_PROJECTS_JSON[{i}] missing project_uuid")
        api_token = str(p.get("api_token") or "").strip()
        modtale_projects.append(ModtaleProjectCfg(project_uuid=uuid, api_token=api_token))

    curseforge_projects: List[CurseforgeProjectCfg] = []
    for i, p in enumerate(curseforge_raw):
        if not isinstance(p, dict):
            raise RuntimeError(f"CURSEFORGE_PROJECTS_JSON[{i}] must be an object")
        pid = str(p.get("project_id") or "").strip()
        slug = str(p.get("project_slug") or "").strip()
        if not pid:
            raise RuntimeError(f"CURSEFORGE_PROJECTS_JSON[{i}] missing project_id")
        if not slug:
            raise RuntimeError(f"CURSEFORGE_PROJECTS_JSON[{i}] missing project_slug")
        curseforge_projects.append(CurseforgeProjectCfg(project_id=pid, project_slug=slug))

    return Config(
        discord_token=require_env("DISCORD_BOT_TOKEN"),
        channel_id=int(require_env("CHANNEL_ID")),
        poll_seconds=poll_seconds,
        curseforge_poll_seconds=cf_poll_seconds,
        modtale_projects=modtale_projects,
        curseforge_projects=curseforge_projects,
    )


class JsonCache:
    """
    Persistent cache:
    {
      "modtale_seen": {"<project_uuid>": ["v1","v2"]},
      "curseforge_seen": {"<project_id>": ["6075247","1234567"]}
    }
    """
    def __init__(self, path: str):
        self.path = path
        self.modtale_seen: Dict[str, Set[str]] = {}
        self.curseforge_seen: Dict[str, Set[str]] = {}

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.modtale_seen = {
                str(k): set(map(str, v or []))
                for k, v in (data.get("modtale_seen") or {}).items()
            }
            self.curseforge_seen = {
                str(k): set(map(str, v or []))
                for k, v in (data.get("curseforge_seen") or {}).items()
            }
        except Exception as e:
            print(f"[cache] Failed to load cache; starting fresh: {e}")
            self.modtale_seen = {}
            self.curseforge_seen = {}

    def save(self) -> None:
        data = {
            "modtale_seen": {k: sorted(v) for k, v in self.modtale_seen.items()},
            "curseforge_seen": {k: sorted(v) for k, v in self.curseforge_seen.items()},
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def get_modtale_seen(self, project_uuid: str) -> Set[str]:
        return self.modtale_seen.setdefault(project_uuid, set())

    def get_curseforge_seen(self, project_id: str) -> Set[str]:
        return self.curseforge_seen.setdefault(project_id, set())


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    headers: Optional[Dict[str, str]] = None,
) -> str:
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        return await resp.text()


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        return await resp.json()


def make_absolute_url(base: str, maybe_relative: str) -> str:
    maybe_relative = (maybe_relative or "").strip()
    if not maybe_relative:
        return ""
    if maybe_relative.startswith("http://") or maybe_relative.startswith("https://"):
        return maybe_relative
    return urljoin(base.rstrip("/") + "/", maybe_relative.lstrip("/"))


def modtale_download_url(project_uuid: str, version_number: str) -> str:
    return f"{MODTALE_BASE_URL.rstrip('/')}/api/v1/projects/{project_uuid}/versions/{version_number}/download"

def modtale_icon_url_from_project(project: dict) -> str:
    icon = (project.get("imageUrl") or "").strip()
    if not icon:
        imgs = project.get("galleryImages") or []
        if imgs:
            icon = str(imgs[0]).strip()
    return make_absolute_url(MODTALE_BASE_URL, icon)

def build_modtale_embed_and_view(project_uuid: str, project: dict, version: dict):
    title = project.get("title", "Modtale Project")
    version_number = str(version.get("versionNumber", "")).strip() or str(version.get("id", "")).strip()
    author = project.get("author", "Unknown Author")

    # Sets Modtale title, description, and color on the left side of the bots message. 
    embed = discord.Embed(
        title=f"A new version of {title} is available",
        description=f"**Version:** `{version_number}`\n\n*A new version has been published on Modtale.*",
        color=discord.Color(0x0F172A),
    )

    icon_url = modtale_icon_url_from_project(project)
    if icon_url:
        embed.set_thumbnail(url=icon_url)

    embed.set_footer(text=f"By {author}")

    view = discord.ui.View(timeout=None)
    if version_number:
        dl = modtale_download_url(project_uuid, version_number)
        view.add_item(discord.ui.Button(label="Download from Modtale", url=dl))

    return embed, view


def pick_new_modtale_versions(project_json: Dict[str, Any], seen: Set[str]) -> List[Dict[str, Any]]:
    versions = project_json.get("versions") or []
    new_items: List[Dict[str, Any]] = []
    for v in versions:
        vid = str(v.get("id", "")).strip()
        if not vid:
            continue
        if vid not in seen:
            new_items.append(v)
    return new_items


def curseforge_modern_file_page_url(project_slug: str, file_id: str) -> str:
    return f"https://www.curseforge.com/hytale/mods/{project_slug}/download/{file_id}"


def curseforge_modern_file_download_url(project_slug: str, file_id: str) -> str:
    return f"https://www.curseforge.com/hytale/mods/{project_slug}/files/{file_id}/download"


def cfwidget_project_url(project_id: str) -> str:
    return f"https://api.cfwidget.com/{project_id}"


def parse_cfwidget_files(project_json: dict) -> list[dict]:
    files = project_json.get("files") or []
    out: list[dict] = []
    seen: set[str] = set()

    for f in files:
        fid = f.get("id")
        if fid is None:
            continue
        fid_s = str(fid)
        if fid_s in seen:
            continue
        seen.add(fid_s)
        out.append(f)

    return out


def build_curseforge_embed_and_view(project_slug: str, project_json: dict, file_obj: dict) -> tuple[discord.Embed, discord.ui.View]:
    project_title = (
        project_json.get("title")
        or project_json.get("name")
        or project_slug
    )

    file_display = (
        file_obj.get("displayName")
        or file_obj.get("name")
        or file_obj.get("fileName")
        or str(file_obj.get("id"))
    )

    author = (
        project_json.get("author")
        or project_json.get("owner")
        or project_json.get("username")
        or "Unknown"
    )

    file_id = str(file_obj.get("id", "")).strip()

    file_page = curseforge_modern_file_page_url(project_slug, file_id)
    file_dl = curseforge_modern_file_download_url(project_slug, file_id)

    # Sets CurseForge title, description, and color on the left side of the bots message. 
    embed = discord.Embed(
        title=f"A new version of {project_title} is available",
        description=f"**Version:** `{file_display}`\n\n*A new file has been published on CurseForge.*",
        color=discord.Color(0x0F172A),
    )

    thumb = (
        project_json.get("thumbnail")
        or project_json.get("logo")
        or (project_json.get("attachments") or {}).get("logo")
        or project_json.get("avatar")
    )
    if isinstance(thumb, str) and thumb.startswith("http"):
        embed.set_thumbnail(url=thumb)

    embed.set_footer(text=f"By {author}")

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Download from CurseForge", url=file_page))
    # If you want the direct download link too, uncomment:
    # view.add_item(discord.ui.Button(label="Direct download", url=file_dl))

    return embed, view


intents = discord.Intents.default()
client = discord.Client(intents=intents)

cfg = load_config()
cache = JsonCache(CACHE_FILE)
cache.load()

http_session: Optional[aiohttp.ClientSession] = None


async def get_target_channel() -> discord.TextChannel:
    ch = client.get_channel(cfg.channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    fetched = await client.fetch_channel(cfg.channel_id)
    if not isinstance(fetched, discord.TextChannel):
        raise RuntimeError("CHANNEL_ID is not a text channel.")
    return fetched


@tasks.loop(seconds=60)
async def poll_curseforge():
    if http_session is None:
        return

    channel = await get_target_channel()

    for p in cfg.curseforge_projects:
        url = cfwidget_project_url(p.project_id)
        headers = {"Accept": "application/json"}

        try:
            project_json = await fetch_json(http_session, url, headers=headers)
            files = parse_cfwidget_files(project_json)
            if not files:
                continue

            seen = cache.get_curseforge_seen(p.project_id)
            new_files = [f for f in files if str(f.get("id")) not in seen]
            if not new_files:
                continue

            # Post oldest-first so Discord reads nicely
            for f in reversed(new_files):
                fid = str(f.get("id"))
                embed, view = build_curseforge_embed_and_view(p.project_slug, project_json, f)
                await channel.send(embed=embed, view=view)
                seen.add(fid)

            cache.save()

        except aiohttp.ClientResponseError as e:
            print(f"[curseforge:{p.project_id}] HTTP error {e.status}: {e.message}")
        except Exception as e:
            print(f"[curseforge:{p.project_id}] Error: {e}")


@poll_curseforge.before_loop
async def before_curseforge():
    await client.wait_until_ready()
    await asyncio.sleep(2)


@tasks.loop(seconds=60)
async def poll_modtale():
    if http_session is None:
        return

    channel = await get_target_channel()

    for p in cfg.modtale_projects:
        url = f"{MODTALE_BASE_URL.rstrip('/')}/api/v1/projects/{p.project_uuid}"
        headers: Dict[str, str] = {"Accept": "application/json"}
        if p.api_token:
            headers["X-MODTALE-KEY"] = p.api_token

        try:
            project = await fetch_json(http_session, url, headers=headers)

            seen = cache.get_modtale_seen(p.project_uuid)
            new_versions = pick_new_modtale_versions(project, seen)
            if not new_versions:
                continue

            for v in new_versions:
                embed, view = build_modtale_embed_and_view(p.project_uuid, project, v)
                await channel.send(embed=embed, view=view)

                vid = str(v.get("id", "")).strip()
                if vid:
                    seen.add(vid)

            cache.save()

        except aiohttp.ClientResponseError as e:
            print(f"[modtale:{p.project_uuid}] HTTP error {e.status}: {e.message}")
        except Exception as e:
            print(f"[modtale:{p.project_uuid}] Error: {e}")


@poll_modtale.before_loop
async def before_modtale():
    await client.wait_until_ready()
    await asyncio.sleep(2)


@client.event
async def on_ready():
    global http_session

    if http_session is None:
        http_session = aiohttp.ClientSession()

    poll_curseforge.change_interval(seconds=cfg.curseforge_poll_seconds)
    poll_modtale.change_interval(seconds=cfg.poll_seconds)

    if cfg.modtale_projects and not poll_modtale.is_running():
        poll_modtale.start()
        print(f"Modtale projects: {len(cfg.modtale_projects)}")

    if cfg.curseforge_projects and not poll_curseforge.is_running():
        poll_curseforge.start()
        print(f"CurseForge projects: {len(cfg.curseforge_projects)}")

    print(f"Logged in as {client.user}")
    print("Successfully finished startup!")


async def shutdown():
    global http_session
    try:
        if http_session is not None:
            await http_session.close()
            http_session = None
    finally:
        await client.close()

async def run_bot():
    global http_session
    http_session = aiohttp.ClientSession()

    try:
        await client.start(cfg.discord_token)
    finally:
        if http_session is not None:
            await http_session.close()
            http_session = None

def main():
    async def runner():
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: stop_event.set())

        bot_task = asyncio.create_task(run_bot())

        await stop_event.wait()

        await client.close()

        await bot_task

    asyncio.run(runner())


if __name__ == "__main__":
    main()