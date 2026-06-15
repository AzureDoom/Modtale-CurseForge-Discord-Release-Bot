import asyncio
import html
import json
import os
import re
import signal
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, urlencode, urljoin

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

CACHE_FILE = "cache.json"
MODTALE_BASE_URL = "https://api.modtale.net/"
MODIFOLD_BASE_URL = "https://api.modifold.com"
CURSEFORGE_API_BASE_URL = "https://api.curseforge.com"

VERSION_HEADER_RE = re.compile(r"(?mi)^(v?\d+(?:\.\d+)*(?:[-+._][A-Za-z0-9]+)?)\s*$")


@dataclass(frozen=True)
class ModtaleProjectCfg:
    project_uuid: str


@dataclass(frozen=True)
class CurseforgeProjectCfg:
    project_id: str
    project_slug: str


@dataclass(frozen=True)
class ModifoldProjectCfg:
    project_slug: str


@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int
    poll_seconds: int
    curseforge_poll_seconds: int
    modtale_api_token: str
    modtale_projects: List[ModtaleProjectCfg]
    curseforge_api_token: str
    curseforge_projects: List[CurseforgeProjectCfg]
    modifold_api_token: str
    modifold_projects: List[ModifoldProjectCfg]


def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def parse_json_env_optional(name: str) -> Any:
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
    modtale_api_token = str(os.getenv("MODTALE_API_TOKEN") or "").strip()
    curseforge_api_token = str(os.getenv("CURSEFORGE_API_TOKEN") or "").strip()
    modtale_raw = parse_json_env_optional("MODTALE_PROJECTS_JSON")
    curseforge_raw = parse_json_env_optional("CURSEFORGE_PROJECTS_JSON")

    if not isinstance(modtale_raw, list):
        raise RuntimeError("MODTALE_PROJECTS_JSON must be a JSON array")
    if not isinstance(curseforge_raw, list):
        raise RuntimeError("CURSEFORGE_PROJECTS_JSON must be a JSON array")

    modtale_projects: List[ModtaleProjectCfg] = []
    for i, item in enumerate(modtale_raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"MODTALE_PROJECTS_JSON[{i}] must be an object")
        project_uuid = str(item.get("project_uuid") or item.get("uuid") or "").strip()
        if not project_uuid:
            raise RuntimeError(f"MODTALE_PROJECTS_JSON[{i}] missing project_uuid")
        modtale_projects.append(ModtaleProjectCfg(project_uuid=project_uuid))

    curseforge_projects: List[CurseforgeProjectCfg] = []
    for i, item in enumerate(curseforge_raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"CURSEFORGE_PROJECTS_JSON[{i}] must be an object")
        project_id = str(item.get("project_id") or "").strip()
        project_slug = str(item.get("project_slug") or "").strip()
        if not project_id:
            raise RuntimeError(f"CURSEFORGE_PROJECTS_JSON[{i}] missing project_id")
        if not project_slug:
            raise RuntimeError(f"CURSEFORGE_PROJECTS_JSON[{i}] missing project_slug")
        curseforge_projects.append(
            CurseforgeProjectCfg(project_id=project_id, project_slug=project_slug)
        )

    modifold_api_token = str(os.getenv("MODIFOLD_API_TOKEN") or "").strip()
    modifold_raw = parse_json_env_optional("MODIFOLD_PROJECTS_JSON")
    if not isinstance(modifold_raw, list):
        raise RuntimeError("MODIFOLD_PROJECTS_JSON must be a JSON array")

    modifold_projects: List[ModifoldProjectCfg] = []
    for i, item in enumerate(modifold_raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"MODIFOLD_PROJECTS_JSON[{i}] must be an object")
        project_slug = str(item.get("project_slug") or item.get("slug") or "").strip()
        if not project_slug:
            raise RuntimeError(f"MODIFOLD_PROJECTS_JSON[{i}] missing project_slug")
        modifold_projects.append(ModifoldProjectCfg(project_slug=project_slug))

    return Config(
        discord_token=require_env("DISCORD_BOT_TOKEN"),
        channel_id=int(require_env("CHANNEL_ID")),
        poll_seconds=poll_seconds,
        curseforge_poll_seconds=cf_poll_seconds,
        modtale_api_token=modtale_api_token,
        modtale_projects=modtale_projects,
        curseforge_api_token=curseforge_api_token,
        curseforge_projects=curseforge_projects,
        modifold_api_token=modifold_api_token,
        modifold_projects=modifold_projects,
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
        self.modifold_seen: Dict[str, Set[str]] = {}

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
            self.modifold_seen = {
                str(k): set(map(str, v or []))
                for k, v in (data.get("modifold_seen") or {}).items()
            }
        except Exception as e:
            print(f"[cache] Failed to load cache; starting fresh: {e}")
            self.modtale_seen = {}
            self.curseforge_seen = {}
            self.modifold_seen = {}

    def save(self) -> None:
        data = {
            "modtale_seen": {k: sorted(v) for k, v in self.modtale_seen.items()},
            "curseforge_seen": {k: sorted(v) for k, v in self.curseforge_seen.items()},
            "modifold_seen": {k: sorted(v) for k, v in self.modifold_seen.items()},
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def get_modtale_seen(self, project_uuid: str) -> Set[str]:
        return self.modtale_seen.setdefault(project_uuid, set())

    def get_curseforge_seen(self, project_id: str) -> Set[str]:
        return self.curseforge_seen.setdefault(project_id, set())

    def get_modifold_seen(self, project_slug: str) -> Set[str]:
        return self.modifold_seen.setdefault(project_slug, set())


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    async with session.get(
        url,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


def make_absolute_url(base: str, maybe_relative: str) -> str:
    maybe_relative = (maybe_relative or "").strip()
    if not maybe_relative:
        return ""
    if maybe_relative.startswith(("http://", "https://")):
        return maybe_relative
    return urljoin(base.rstrip("/") + "/", maybe_relative.lstrip("/"))


def strip_html_tags(value: str) -> str:
    class HTMLToText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: List[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "br":
                self.parts.append("\n")
            elif tag in {"p", "div", "section", "article"}:
                if self.parts and not self.parts[-1].endswith("\n\n"):
                    self.parts.append("\n\n")
            elif tag == "li":
                self.parts.append("- ")
            elif tag in {"ul", "ol"}:
                if self.parts and not self.parts[-1].endswith("\n"):
                    self.parts.append("\n")
            elif tag in {"strong", "b"}:
                self.parts.append("**")
            elif tag in {"em", "i"}:
                self.parts.append("*")
            elif tag == "code":
                self.parts.append("`")
            elif tag == "pre":
                self.parts.append("\n```text\n")

        def handle_endtag(self, tag):
            if tag in {"p", "div", "section", "article"}:
                if not self.parts or not self.parts[-1].endswith("\n\n"):
                    self.parts.append("\n\n")
            elif tag in {"strong", "b"}:
                self.parts.append("**")
            elif tag in {"em", "i"}:
                self.parts.append("*")
            elif tag == "code":
                self.parts.append("`")
            elif tag == "pre":
                if not self.parts or not self.parts[-1].endswith("\n"):
                    self.parts.append("\n")
                self.parts.append("```\n")
            elif tag == "li":
                if not self.parts or not self.parts[-1].endswith("\n"):
                    self.parts.append("\n")

        def handle_data(self, data):
            if data:
                self.parts.append(data)

        def get_text(self) -> str:
            text = "".join(self.parts)
            text = html.unescape(text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()

    parser = HTMLToText()
    parser.feed(value or "")
    return parser.get_text()


def clean_discord_changelog_text(text: str) -> str:
    if not text:
        return ""
    text = strip_html_tags(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def convert_markdown_tables_to_codeblocks(text: str) -> str:
    lines = text.splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        if (
            i + 1 < len(lines)
            and "|" in lines[i]
            and "|" in lines[i + 1]
            and re.search(r":?-{3,}:?", lines[i + 1])
        ):
            table_lines = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            out.append("```")
            out.extend(table_lines)
            out.append("```")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def extract_latest_changelog_section(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return ""
    matches = list(VERSION_HEADER_RE.finditer(text))
    if not matches:
        return text
    start = matches[0].start()
    end = matches[1].start() if len(matches) > 1 else len(text)
    return text[start:end].strip()


def truncate_discord_markdown(text: str, limit: int = 1000) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    truncated = text[: limit - 3]
    if truncated.count("```") % 2 != 0:
        truncated = truncated.rsplit("```", 1)[0].rstrip()
    if truncated.count("**") % 2 != 0:
        truncated = truncated.rsplit("**", 1)[0].rstrip()
    if "\n" in truncated[-200:]:
        truncated = truncated.rsplit("\n", 1)[0].rstrip()
    elif " " in truncated[-200:]:
        truncated = truncated.rsplit(" ", 1)[0].rstrip()
    return truncated + "..."


def format_changelog_for_discord(raw_text: str, limit: int = 1000) -> str:
    if not raw_text:
        return ""
    text = clean_discord_changelog_text(raw_text)
    text = extract_latest_changelog_section(text)
    text = convert_markdown_tables_to_codeblocks(text)
    text = clean_discord_changelog_text(text)
    return truncate_discord_markdown(text, limit=limit)


def slugify(text: str) -> str:
    text = str(text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def make_url_view(*buttons: tuple[str, Optional[str]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, url in buttons:
        if url:
            view.add_item(discord.ui.Button(label=label, url=url))
    return view


def get_modtale_changelog(version: dict, project: dict) -> str:
    return (
        str(version.get("changelog") or "").strip()
        or str(version.get("releaseNotes") or "").strip()
        or str(version.get("body") or "").strip()
        or str(project.get("changelog") or "").strip()
    )


def modtale_download_url_endpoint(
    project_uuid: str,
    version_number: str,
    game_version: Optional[str] = None,
) -> str:
    project_id = quote(str(project_uuid), safe="")
    version = quote(str(version_number), safe="")
    url = (
        f"{MODTALE_BASE_URL.rstrip('/')}/api/v1/projects/"
        f"{project_id}/versions/{version}/download-url"
    )
    if game_version:
        url += "?" + urlencode({"gameVersion": game_version})
    return url


def get_modtale_game_version(version: dict) -> Optional[str]:
    game_versions = version.get("gameVersions") or version.get("game_versions") or []
    if isinstance(game_versions, list) and game_versions:
        return str(game_versions[0]).strip() or None
    game_version = str(version.get("gameVersion") or version.get("game_version") or "").strip()
    return game_version or None


async def fetch_modtale_download_url(
    session: aiohttp.ClientSession,
    api_token: str,
    project_uuid: str,
    version_number: str,
    game_version: Optional[str] = None,
) -> str:
    """Resolve the short-lived Modtale download URL for a version."""
    if not project_uuid or not version_number:
        return ""

    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_token:
        headers["X-MODTALE-KEY"] = api_token

    try:
        payload = await fetch_json(
            session,
            modtale_download_url_endpoint(project_uuid, version_number, game_version),
            headers=headers,
        )
    except aiohttp.ClientResponseError as e:
        print(
            f"[modtale:{project_uuid}] Download URL HTTP error "
            f"{e.status}: {e.message}"
        )
        return ""
    except Exception as e:
        print(f"[modtale:{project_uuid}] Download URL fetch failed: {e}")
        return ""

    if isinstance(payload, dict):
        raw = str(payload.get("downloadUrl") or payload.get("url") or "").strip()
        if not raw:
            return ""
        # The API returns a root-relative path like /download/{token}.
        # The actual endpoint is /api/v1/download/{token}, so we must prepend
        # the base URL plus /api/v1 rather than using make_absolute_url which
        # would produce https://api.modtale.net/download/{token} (404).
        if raw.startswith("/"):
            return f"{MODTALE_BASE_URL.rstrip('/')}/api/v1{raw}"
        return raw
    return ""


def modtale_project_page_url(project: dict, project_uuid: str) -> str:
    slug = (
        project.get("slug") or project.get("name") or project.get("title") or "project"
    )
    return f"https://modtale.net/mod/{slugify(slug)}-{project_uuid}"


def modtale_icon_url_from_project(project: dict) -> str:
    icon = (project.get("imageUrl") or "").strip()
    if not icon:
        gallery_images = project.get("galleryImages") or []
        if gallery_images:
            icon = str(gallery_images[0]).strip()
    return make_absolute_url(MODTALE_BASE_URL, icon)


def curseforge_file_page_url(project_slug: str, file_id: str) -> str:
    return f"https://www.curseforge.com/hytale/mods/{project_slug}/files/{file_id}"


def curseforge_file_download_url(project_slug: str, file_id: str) -> str:
    return f"https://www.curseforge.com/hytale/mods/{project_slug}/download/{file_id}"


def cfwidget_project_url(project_id: str) -> str:
    return f"https://api.cfwidget.com/{project_id}"


def curseforge_changelog_url(project_id: str, file_id: str) -> str:
    return (
        f"{CURSEFORGE_API_BASE_URL.rstrip('/')}"
        f"/v1/mods/{project_id}/files/{file_id}/changelog"
    )


async def fetch_curseforge_changelog(
    session: aiohttp.ClientSession,
    api_token: str,
    project_id: str,
    file_id: str,
) -> str:
    """
    Fetch the changelog (HTML) for a CurseForge file via the official API.

    Returns an empty string on any failure or when no api_token is provided,
    so the caller can fall back to the existing no-changelog behavior.
    """
    if not api_token or not project_id or not file_id:
        return ""
    try:
        payload = await fetch_json(
            session,
            curseforge_changelog_url(project_id, file_id),
            headers={
                "Accept": "application/json",
                "x-api-key": api_token,
            },
        )
    except aiohttp.ClientResponseError as e:
        print(
            f"[curseforge:{project_id}] Changelog HTTP error {e.status}: {e.message}"
        )
        return ""
    except Exception as e:
        print(f"[curseforge:{project_id}] Changelog fetch failed: {e}")
        return ""

    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, str):
            return data
    if isinstance(payload, str):
        return payload
    return ""


def parse_cfwidget_files(project_json: dict) -> List[dict]:
    files = project_json.get("files") or []
    out: List[dict] = []
    seen: Set[str] = set()
    for file_obj in files:
        file_id = file_obj.get("id")
        if file_id is None:
            continue
        file_id_str = str(file_id)
        if file_id_str in seen:
            continue
        seen.add(file_id_str)
        out.append(file_obj)
    return out


def pick_new_modtale_versions(
    project_json: Dict[str, Any], seen: Set[str]
) -> List[Dict[str, Any]]:
    versions = project_json.get("versions") or []
    new_items: List[Dict[str, Any]] = []
    for version in versions:
        version_id = str(version.get("id", "")).strip()
        if version_id and version_id not in seen:
            new_items.append(version)
    return new_items


def get_curseforge_author(project_json: dict) -> str:
    members = project_json.get("members") or []
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict):
                continue
            for key in ("username", "name", "title"):
                value = str(member.get(key) or "").strip()
                if value:
                    return value
    return "Unknown Author"


def normalize_thumbnail_url(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def build_release_embed_and_view(
    *,
    platform_name: str,
    project_title: str,
    version_label: str,
    author: str,
    changelog_text: str = "",
    thumbnail_url: Optional[str] = None,
    buttons: List[tuple[str, Optional[str]]],
) -> tuple[discord.Embed, discord.ui.View]:
    # Sets title, description, and color on the left side of the bots message.
    embed = discord.Embed(
        title=f"A new version of {project_title} is available",
        description=(
            f"**Version:** `{version_label}`\n\n"
            f"*A new version has been published on {platform_name}.*"
        ),
        color=discord.Color(0x0F172A),
    )
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    if changelog_text:
        embed.add_field(name="Changelog", value=changelog_text, inline=False)
    embed.set_footer(text=f"By {author}")
    view = make_url_view(*buttons)
    return embed, view


def build_modtale_embed_and_view(
    project_uuid: str,
    project: dict,
    version: dict,
    download_url: Optional[str] = None,
) -> tuple[discord.Embed, discord.ui.View]:
    project_title = str(project.get("title") or "Modtale Project")
    version_label = (
        str(version.get("versionNumber", "")).strip()
        or str(version.get("id", "")).strip()
    )
    author = str(project.get("author") or "Unknown Author")
    changelog_text = format_changelog_for_discord(
        get_modtale_changelog(version, project),
        limit=1000,
    )
    thumbnail_url = normalize_thumbnail_url(modtale_icon_url_from_project(project))
    return build_release_embed_and_view(
        platform_name="Modtale",
        project_title=project_title,
        version_label=version_label,
        author=author,
        changelog_text=changelog_text,
        thumbnail_url=thumbnail_url,
        buttons=[
            (
                "Download from Modtale",
                download_url if version_label else None,
            ),
            (
                "View File Page",
                modtale_project_page_url(project, project_uuid),
            ),
        ],
    )


def build_curseforge_embed_and_view(
    project_slug: str,
    project_json: dict,
    file_obj: dict,
    changelog_text: str = "",
) -> tuple[discord.Embed, discord.ui.View]:
    project_title = str(
        project_json.get("title") or project_json.get("name") or project_slug
    )
    version_label = str(
        file_obj.get("display")
        or file_obj.get("name")
        or file_obj.get("version")
        or file_obj.get("id")
        or "Unknown Version"
    )
    file_id = str(file_obj.get("id", "")).strip()
    author = get_curseforge_author(project_json)
    thumbnail_url = normalize_thumbnail_url(
        project_json.get("thumbnail")
        or project_json.get("logo")
        or (project_json.get("attachments") or {}).get("logo")
        or project_json.get("avatar")
    )
    return build_release_embed_and_view(
        platform_name="CurseForge",
        project_title=project_title,
        version_label=version_label,
        author=author,
        changelog_text=changelog_text,
        thumbnail_url=thumbnail_url,
        buttons=[
            (
                "Download from CurseForge",
                (
                    curseforge_file_download_url(project_slug, file_id)
                    if file_id
                    else None
                ),
            ),
            (
                "View File Page",
                curseforge_file_page_url(project_slug, file_id) if file_id else None,
            ),
        ],
    )


def modifold_project_page_url(slug: str) -> str:
    return f"https://modifold.com/mod/{slug}"


def modifold_download_url(slug: str, version_number: str) -> str:
    return f"{MODIFOLD_BASE_URL}/projects/{slug}/version/{version_number}"


def pick_new_modifold_versions(project_json: Dict[str, Any], seen: Set[str]) -> List[Dict[str, Any]]:
    versions = project_json.get("versions") or []
    new_items = []
    for version in versions:
        version_id = str(
            version.get("id")
            or version.get("_id")
            or version.get("version_number")
            or version.get("versionNumber")
            or ""
        ).strip()
        if version_id and version_id not in seen:
            new_items.append(version)
    return new_items


def get_modifold_author(project: dict) -> str:
    owner = project.get("owner")
    if isinstance(owner, dict):
        return str(owner.get("username") or owner.get("slug") or "Unknown Author")
    org = project.get("organization")
    if isinstance(org, dict):
        return str(org.get("name") or org.get("slug") or "Unknown Author")
    return "Unknown Author"


def build_modifold_embed_and_view(
    project_slug: str,
    project: dict,
    version: dict,
) -> tuple[discord.Embed, discord.ui.View]:
    project_title = str(project.get("title") or project_slug)
    version_label = str(
        version.get("version_number")
        or version.get("versionNumber")
        or version.get("name")
        or version.get("id")
        or "Unknown Version"
    ).strip()
    author = get_modifold_author(project)
    changelog_text = format_changelog_for_discord(
        str(version.get("changelog") or version.get("release_notes") or version.get("description") or ""),
        limit=1000,
    )
    file_url = normalize_thumbnail_url(version.get("file_url"))
    thumbnail_url = normalize_thumbnail_url(
        project.get("icon")
        or project.get("icon_url")
        or project.get("image")
        or project.get("imageUrl")
    )
    return build_release_embed_and_view(
        platform_name="Modifold",
        project_title=project_title,
        version_label=version_label,
        author=author,
        changelog_text=changelog_text,
        thumbnail_url=thumbnail_url,
        buttons=[
            ("Download from Modifold", file_url),
            ("View on Modifold", modifold_project_page_url(project_slug)),
        ],
    )


def log_release(
    source: str, project_name: str, version_name: str, identifier: str
) -> None:
    print(f"[release][{source}] {project_name} -> {version_name} ({identifier})")


intents = discord.Intents.default()
client = discord.Client(intents=intents)
cfg = load_config()
cache = JsonCache(CACHE_FILE)
cache.load()
http_session: Optional[aiohttp.ClientSession] = None


async def get_target_channel() -> discord.TextChannel:
    channel = client.get_channel(cfg.channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    fetched = await client.fetch_channel(cfg.channel_id)
    if not isinstance(fetched, discord.TextChannel):
        raise RuntimeError("CHANNEL_ID is not a text channel.")
    return fetched


@tasks.loop(seconds=60)
async def poll_curseforge() -> None:
    if http_session is None:
        return
    channel = await get_target_channel()
    for project_cfg in cfg.curseforge_projects:
        try:
            project_json = await fetch_json(
                http_session,
                cfwidget_project_url(project_cfg.project_id),
                headers={"Accept": "application/json"},
            )
            files = parse_cfwidget_files(project_json)
            if not files:
                continue
            seen = cache.get_curseforge_seen(project_cfg.project_id)
            new_files = [f for f in files if str(f.get("id")) not in seen]
            if not new_files:
                continue
            for file_obj in reversed(new_files):
                file_id = str(file_obj.get("id", "")).strip()
                file_display = (
                    file_obj.get("display")
                    or file_obj.get("name")
                    or file_obj.get("version")
                    or file_id
                )
                project_title = (
                    project_json.get("title")
                    or project_json.get("name")
                    or project_cfg.project_slug
                )

                changelog_text = ""
                if cfg.curseforge_api_token and file_id:
                    raw_changelog = await fetch_curseforge_changelog(
                        http_session,
                        cfg.curseforge_api_token,
                        project_cfg.project_id,
                        file_id,
                    )
                    changelog_text = format_changelog_for_discord(
                        raw_changelog,
                        limit=1000,
                    )

                embed, view = build_curseforge_embed_and_view(
                    project_cfg.project_slug,
                    project_json,
                    file_obj,
                    changelog_text=changelog_text,
                )
                await channel.send(embed=embed, view=view)
                if file_id:
                    seen.add(file_id)
                log_release("curseforge", project_title, str(file_display), file_id)
            cache.save()
        except aiohttp.ClientResponseError as e:
            print(
                f"[curseforge:{project_cfg.project_id}] HTTP error {e.status}: {e.message}"
            )
        except Exception as e:
            print(f"[curseforge:{project_cfg.project_id}] Error: {e}")


@poll_curseforge.before_loop
async def before_curseforge() -> None:
    await client.wait_until_ready()
    await asyncio.sleep(2)


@tasks.loop(seconds=60)
async def poll_modtale() -> None:
    if http_session is None:
        return
    channel = await get_target_channel()
    for project_cfg in cfg.modtale_projects:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if cfg.modtale_api_token:
            headers["X-MODTALE-KEY"] = cfg.modtale_api_token
        try:
            project = await fetch_json(
                http_session,
                f"{MODTALE_BASE_URL.rstrip('/')}/api/v1/projects/{project_cfg.project_uuid}",
                headers=headers,
            )
            seen = cache.get_modtale_seen(project_cfg.project_uuid)
            new_versions = pick_new_modtale_versions(project, seen)
            if not new_versions:
                continue
            for version in reversed(new_versions):
                version_id = str(version.get("id", "")).strip()
                version_number = (
                    str(version.get("versionNumber", "")).strip() or version_id
                )
                download_url = await fetch_modtale_download_url(
                    http_session,
                    cfg.modtale_api_token,
                    project_cfg.project_uuid,
                    version_number,
                    game_version=get_modtale_game_version(version),
                )
                embed, view = build_modtale_embed_and_view(
                    project_cfg.project_uuid,
                    project,
                    version,
                    download_url=download_url,
                )
                await channel.send(embed=embed, view=view)
                project_title = str(project.get("title") or project_cfg.project_uuid)
                if version_id:
                    seen.add(version_id)
                log_release("modtale", project_title, version_number, version_id)
            cache.save()
        except aiohttp.ClientResponseError as e:
            print(
                f"[modtale:{project_cfg.project_uuid}] HTTP error {e.status}: {e.message}"
            )
        except Exception as e:
            print(f"[modtale:{project_cfg.project_uuid}] Error: {e}")


@poll_modtale.before_loop
async def before_modtale() -> None:
    await client.wait_until_ready()
    await asyncio.sleep(2)


@tasks.loop(seconds=60)
async def poll_modifold() -> None:
    if http_session is None:
        return
    channel = await get_target_channel()
    for project_cfg in cfg.modifold_projects:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if cfg.modifold_api_token:
            headers["Authorization"] = f"Bearer {cfg.modifold_api_token}"
        try:
            project = await fetch_json(
                http_session,
                f"{MODIFOLD_BASE_URL}/projects/{project_cfg.project_slug}",
                headers=headers,
            )
            seen = cache.get_modifold_seen(project_cfg.project_slug)
            new_versions = pick_new_modifold_versions(project, seen)
            if not new_versions:
                continue
            for version in reversed(new_versions):
                embed, view = build_modifold_embed_and_view(
                    project_cfg.project_slug,
                    project,
                    version,
                )
                await channel.send(embed=embed, view=view)
                version_id = str(
                    version.get("id")
                    or version.get("_id")
                    or version.get("version_number")
                    or version.get("versionNumber")
                    or ""
                ).strip()
                version_number = str(
                    version.get("version_number")
                    or version.get("versionNumber")
                    or version_id
                )
                project_title = str(project.get("title") or project_cfg.project_slug)
                if version_id:
                    seen.add(version_id)
                log_release("modifold", project_title, version_number, version_id)
            cache.save()
        except aiohttp.ClientResponseError as e:
            print(f"[modifold:{project_cfg.project_slug}] HTTP error {e.status}: {e.message}")
        except Exception as e:
            print(f"[modifold:{project_cfg.project_slug}] Error: {e}")


@poll_modifold.before_loop
async def before_modifold() -> None:
    await client.wait_until_ready()
    await asyncio.sleep(2)


@client.event
async def on_ready() -> None:
    poll_curseforge.change_interval(seconds=cfg.curseforge_poll_seconds)
    poll_modtale.change_interval(seconds=cfg.poll_seconds)
    poll_modifold.change_interval(seconds=cfg.poll_seconds)
    if cfg.modtale_projects and not poll_modtale.is_running():
        poll_modtale.start()
        print(f"Modtale projects: {len(cfg.modtale_projects)}")
    if cfg.curseforge_projects and not poll_curseforge.is_running():
        poll_curseforge.start()
        print(f"CurseForge projects: {len(cfg.curseforge_projects)}")
        if cfg.curseforge_api_token:
            print("CurseForge API token detected: changelogs will be fetched.")
        else:
            print(
                "No CurseForge API token configured: "
                "changelogs will be omitted from CurseForge embeds."
            )
    if cfg.modifold_projects and not poll_modifold.is_running():
        poll_modifold.start()
        print(f"Modifold projects: {len(cfg.modifold_projects)}")
    print(f"Logged in as {client.user}")
    print("Successfully finished startup!")


async def shutdown() -> None:
    global http_session
    try:
        if poll_modtale.is_running():
            poll_modtale.cancel()
        if poll_curseforge.is_running():
            poll_curseforge.cancel()
        if poll_modifold.is_running():
            poll_modifold.cancel()
        if http_session is not None:
            await http_session.close()
            http_session = None
    finally:
        await client.close()


async def run_bot() -> None:
    global http_session
    if http_session is None:
        http_session = aiohttp.ClientSession()
    try:
        await client.start(cfg.discord_token)
    finally:
        if http_session is not None:
            await http_session.close()
            http_session = None


def main() -> None:
    async def runner() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: stop_event.set())
        bot_task = asyncio.create_task(run_bot())
        await stop_event.wait()
        await shutdown()
        await bot_task

    asyncio.run(runner())


if __name__ == "__main__":
    main()