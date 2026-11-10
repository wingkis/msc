#!/usr/bin/env python3
"""MSC CLI — Memory Sync Client command line tool.

Talks to the REST API, so it works from any host that can reach the server.
LAN mode (the default) needs no credentials at all.
"""

import json
import os
import sys
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.table import Table

__version__ = "2.4.7"

console = Console()
err_console = Console(stderr=True)

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "msc"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "credentials"

CATEGORIES = ("config", "project", "user", "knowledge", "reference")
PRIORITIES = ("low", "medium", "high", "critical")
LIFESPANS = ("temporary", "permanent")


class Client:
    """Thin wrapper that keeps auth, actor attribution and error handling in one place."""

    def __init__(self, base_url: str, api_key: str = "", agent: str = "", timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        headers = {"X-Client": f"msc-cli/{__version__}"}
        if agent:
            headers["X-Agent"] = agent
        if api_key:
            headers["X-API-Key" if api_key.startswith("api-") else "Authorization"] = (
                api_key if api_key.startswith("api-") else f"Bearer {api_key}")
        self._http = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def request(self, method: str, path: str, *, params=None, json_body=None, raw=False):
        try:
            resp = self._http.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise click.ClickException(f"Cannot reach {self.base_url}: {exc}")

        if raw:
            return resp
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise click.ClickException(f"{resp.status_code} {method} {path}: {detail}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return resp.text

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def close(self):
        self._http.close()


def _load_config_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Malformed config file {path}: {exc}")


def _resolve(ctx: click.Context) -> Client:
    """Precedence: flags > environment > config file > defaults."""
    opts = ctx.obj
    config_path = Path(opts["config"]) if opts["config"] else DEFAULT_CONFIG_FILE
    stored = _load_config_file(config_path)

    base_url = opts["base_url"] or os.environ.get("MSC_BASE_URL") or stored.get("base_url") or DEFAULT_BASE_URL
    api_key = opts["key"] or os.environ.get("MSC_API_KEY") or stored.get("api_key") or ""
    agent = opts["agent"] or os.environ.get("MSC_AGENT") or stored.get("agent") or ""

    ctx.obj["client"] = Client(base_url, api_key=api_key, agent=agent, timeout=opts["timeout"])
    ctx.obj["as_json"] = opts["json"]
    return ctx.obj["client"]


def _output(data, human=None) -> None:
    if human:
        human()
    else:
        console.print_json(json.dumps(data, ensure_ascii=False, default=str))


def _read_body(body: str, body_file: str, stdin_default: bool) -> str:
    """Body from --body, --body-file, or stdin — whichever was provided."""
    if body:
        return body
    if body_file:
        return Path(body_file).read_text(encoding="utf-8")
    if stdin_default and not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _split_csv(value: str) -> list:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@click.group()
@click.option("--base-url", default=None, help="API server URL (default $MSC_BASE_URL).")
@click.option("--key", default=None, help="API key or JWT (only needed when auth is enabled).")
@click.option("--agent", default=None, help="Attribution recorded in version history (X-Agent).")
@click.option("--config", default=None, help=f"Config file path (default {DEFAULT_CONFIG_FILE}).")
@click.option("--timeout", default=20.0, show_default=True, help="HTTP timeout in seconds.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.version_option(version=__version__, prog_name="msc-cli")
@click.pass_context
def cli(ctx, base_url, key, agent, config, timeout, as_json):
    """MSC — Memory Sync Client CLI.

    \b
    Common flows:
      msc list                     list memories
      msc add --name "note" --body "..."
      msc search "keyword"         full-text search
      msc sync status              DB vs filesystem
      msc api-help                 the server's own usage guide
    """
    ctx.ensure_object(dict)
    ctx.obj.update({"base_url": base_url, "key": key, "agent": agent,
                    "config": config, "timeout": timeout, "json": as_json})


@cli.result_callback()
@click.pass_context
def _cleanup(ctx, result, *args, **kwargs):
    client = ctx.obj.get("client")
    if client:
        client.close()


# ── Setup / discovery ────────────────────────────────────────────────

@cli.command()
@click.option("--base-url", default=None, help="API base URL.")
@click.option("--key", default="", help="API key; leave empty for LAN mode.")
@click.option("--agent", default="", help="Default X-Agent attribution.")
@click.option("--yes", is_flag=True, help="Accept defaults, no prompts.")
def init(base_url, key, agent, yes):
    """Write ~/.config/msc/credentials."""
    config_dir = DEFAULT_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "credentials"

    if yes:
        base_url = base_url or DEFAULT_BASE_URL
    else:
        base_url = click.prompt("API base URL", default=base_url or DEFAULT_BASE_URL)
        # LAN mode (the server default) needs no key at all.
        key = click.prompt("API key (empty = LAN mode, no token needed)", default=key, hide_input=True)
        agent = click.prompt("Agent name for attribution", default=agent or os.environ.get("USER", ""))

    config_file.write_text(json.dumps({"base_url": base_url, "api_key": key, "agent": agent}, indent=2))
    config_file.chmod(0o600)
    console.print(f"[green]Config saved to {config_file}[/green]")


@cli.command()
@click.pass_context
def status(ctx):
    """Server health, counts and sync state in one view."""
    client = _resolve(ctx)
    health = client.get("/api/v1/health")
    config = client.get("/api/v1/config")
    stats = client.get("/api/v1/stats")
    sync_state = client.get("/api/v1/sync/status")

    def human():
        mode = "auth required" if config.get("auth_enabled") else "LAN (no login)"
        console.print(f"\n[bold]MSC {health.get('version')}[/bold] — {client.base_url}  [{mode}]")
        console.print(f"  memories: {stats['total']}  "
                      f"categories: {', '.join(f'{k}={v}' for k, v in stats['by_category'].items())}")
        console.print(f"  files: {sync_state['files']} in {sync_state['memory_dir']}  "
                      f"only-in-db: {len(sync_state['only_in_db'])}  only-in-files: {len(sync_state['only_in_files'])}")
        if sync_state.get("last_sync"):
            last = sync_state["last_sync"]
            console.print(f"  last sync: {last['direction']} {last['status']} — {last['detail']}")

    _output({"health": health, "config": config, "stats": stats, "sync": sync_state}, human)


@cli.command("api-help")
@click.pass_context
def api_help(ctx):
    """Print the server's plain-text usage guide."""
    client = _resolve(ctx)
    resp = client.request("GET", "/api/v1/help", raw=True)
    click.echo(resp.text)


# ── Memories ─────────────────────────────────────────────────────────

@cli.command()
@click.option("--category", type=click.Choice(CATEGORIES), default=None)
@click.option("--priority", type=click.Choice(PRIORITIES), default=None)
@click.option("--tag", default=None, help="Comma-separated tag names.")
@click.option("--branch", default=None)
@click.option("--q", default=None, help="Substring match on name/description/body/slug.")
@click.option("--since", default=None, help="Only memories updated at/after this ISO timestamp.")
@click.option("--order", type=click.Choice(["updated", "created", "priority", "name"]), default="updated")
@click.option("--archived", is_flag=True, help="List soft-deleted memories instead of live ones.")
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def list(ctx, category, priority, tag, branch, q, since, order, archived, limit):
    """List memories with optional filters."""
    client = _resolve(ctx)
    params = {k: v for k, v in {
        "category": category, "priority": priority, "tag": tag, "branch": branch,
        "q": q, "since": since, "order": order, "limit": limit, "archived": archived or None,
    }.items() if v not in (None, "")}

    memories = client.get("/api/v1/memories", params=params)

    def human():
        table = Table(show_header=True)
        for column in ("Slug", "Name", "Category", "Priority", "Updated"):
            table.add_column(column)
        for m in memories:
            table.add_row(m["slug"], m["name"][:40], m["category"], m["priority"], m["updated_at"][:10])
        console.print(f"\n  {len(memories)} memories\n")
        console.print(table)

    _output(memories, human)


@cli.command()
@click.argument("ref")
@click.option("--raw", is_flag=True, help="Print the body only (pipe-friendly).")
@click.option("--full", is_flag=True, help="Do not truncate the body.")
@click.pass_context
def get(ctx, ref, raw, full):
    """Show one memory (slug, exact name, or a unique fragment)."""
    client = _resolve(ctx)
    memory = client.get(f"/api/v1/memories/{ref}")

    if raw:
        click.echo(memory["body"])
        return

    def human():
        console.print(f"\n[bold]{memory['name']}[/bold]  [dim]{memory['slug']}[/dim]")
        console.print(f"  {memory['category']} / {memory['priority']} / {memory['lifespan']}"
                      + (f" / branch={memory['branch']}" if memory.get("branch") else ""))
        if memory["tags"]:
            console.print(f"  tags: {', '.join(t['name'] for t in memory['tags'])}")
        if memory.get("description"):
            console.print(f"  [dim]{memory['description']}[/dim]")
        body = memory["body"] or ""
        console.print("")
        console.print(body if full or len(body) <= 1200 else body[:1200] + "\n...")

    _output(memory, human)


def _memory_payload(name, slug, description, body, tags, priority, category, lifespan, branch, related):
    payload = {}
    if name:
        payload["name"] = name
    if slug:
        payload["slug"] = slug
    if description is not None:
        payload["description"] = description
    if body is not None:
        payload["body"] = body
    if tags is not None:
        payload["tags"] = tags
    if priority:
        payload["priority"] = priority
    if category:
        payload["category"] = category
    if lifespan:
        payload["lifespan"] = lifespan
    if branch is not None:
        payload["branch"] = branch
    if related:
        payload["related_slugs"] = related
    return payload


@cli.command()
@click.option("--name", required=True, help="Memory name.")
@click.option("--slug", default=None, help="Explicit slug (otherwise derived from the name).")
@click.option("--body", default=None, help="Body text.")
@click.option("--body-file", default=None, help="Read the body from a file.")
@click.option("--description", default="", show_default=True)
@click.option("--tags", default="", help="Comma-separated tags.")
@click.option("--priority", type=click.Choice(PRIORITIES), default="medium")
@click.option("--category", type=click.Choice(CATEGORIES), default="reference")
@click.option("--lifespan", type=click.Choice(LIFESPANS), default="permanent")
@click.option("--branch", default="")
@click.option("--related", default="", help="Comma-separated slugs to link.")
@click.pass_context
def add(ctx, name, slug, body, body_file, description, tags, priority, category, lifespan, branch, related):
    """Create a new memory. Body may come from --body, --body-file or stdin."""
    client = _resolve(ctx)
    payload = _memory_payload(name, slug, description,
                              _read_body(body, body_file, stdin_default=True),
                              _split_csv(tags), priority, category, lifespan, branch,
                              _split_csv(related))
    payload.setdefault("name", name)

    created = client.post("/api/v1/memories", json_body=payload)

    def human():
        console.print(f"[green]Created:[/green] {created['name']} (slug: {created['slug']})")

    _output(created, human)


@cli.command()
@click.argument("ref")
@click.option("--name", default=None)
@click.option("--description", default=None)
@click.option("--body", default=None)
@click.option("--body-file", default=None)
@click.option("--tags", default=None, help="Comma-separated tags (replaces the current set).")
@click.option("--priority", type=click.Choice(PRIORITIES), default=None)
@click.option("--category", type=click.Choice(CATEGORIES), default=None)
@click.option("--lifespan", type=click.Choice(LIFESPANS), default=None)
@click.option("--branch", default=None)
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the new body from stdin.")
@click.pass_context
def edit(ctx, ref, name, description, body, body_file, tags, priority, category, lifespan, branch, from_stdin):
    """Edit an existing memory."""
    client = _resolve(ctx)
    body_text = _read_body(body, body_file, stdin_default=from_stdin)
    payload = _memory_payload(name, None, description, body_text or None,
                              _split_csv(tags) if tags is not None else None,
                              priority, category, lifespan, branch, None)

    if not payload:
        raise click.ClickException("No changes specified (try --body, --tags, --priority ...)")

    updated = client.put(f"/api/v1/memories/{ref}", json_body=payload)

    def human():
        console.print(f"[green]Updated:[/green] {updated['slug']} "
                      f"(priority={updated['priority']}, category={updated['category']})")

    _output(updated, human)


@cli.command()
@click.argument("ref")
@click.option("--hard", is_flag=True, help="Delete the row, its history and its file for good.")
@click.pass_context
def delete(ctx, ref, hard):
    """Archive a memory (soft delete) — undo with `msc restore`."""
    client = _resolve(ctx)
    result = client.delete(f"/api/v1/memories/{ref}", params={"hard": hard})

    def human():
        console.print(f"[green]{'Deleted' if hard else 'Archived'}:[/green] {ref}"
                      + ("" if hard else "  (undo: msc restore %s)" % ref))

    _output(result, human)


@cli.command()
@click.argument("ref")
@click.pass_context
def restore(ctx, ref):
    """Undo a soft delete."""
    client = _resolve(ctx)
    memory = client.post(f"/api/v1/memories/{ref}/restore")

    def human():
        console.print(f"[green]Restored:[/green] {memory['slug']}")

    _output(memory, human)


@cli.command()
@click.argument("file", type=click.File("r"))
@click.option("--no-upsert", is_flag=True, help="Fail instead of updating when the slug exists.")
@click.pass_context
def batch(ctx, file, no_upsert):
    """Create or upsert many memories from a JSON file."""
    client = _resolve(ctx)
    data = json.load(file)
    if no_upsert:
        data["upsert"] = False
    result = client.post("/api/v1/memories/batch", json_body=data)

    def human():
        console.print(f"[green]{result['message']}[/green]")
        for group in ("created", "updated"):
            if result[group]:
                console.print(f"  {group}: {', '.join(result[group])}")
        for item in result["failed"]:
            console.print(f"  [red]failed[/red] {item['slug']}: {item['error']}")

    _output(result, human)


# ── Search & history ─────────────────────────────────────────────────

@cli.command()
@click.argument("query")
@click.option("--tag", default=None, help="Comma-separated tags.")
@click.option("--category", default=None, help="Comma-separated categories.")
@click.option("--priority", default=None, help="Comma-separated priorities.")
@click.option("--include-archived", is_flag=True)
@click.option("--limit", default=20, show_default=True)
@click.pass_context
def search(ctx, query, tag, category, priority, include_archived, limit):
    """Full-text search (safe with any characters, including quotes and AND)."""
    client = _resolve(ctx)
    params = {k: v for k, v in {
        "q": query, "tags": tag, "categories": category, "priorities": priority,
        "include_archived": include_archived or None, "limit": limit,
    }.items() if v not in (None, "")}

    results = client.get("/api/v1/search", params=params)

    def human():
        if not results:
            console.print("[dim]No results found[/dim]")
            return
        console.print(f"\n[bold]Found {len(results)} results for '{query}'[/bold]")
        for r in results:
            console.print(f"  [cyan]{r['slug']}[/cyan]  {r['name']}  [dim]score={r['score']}[/dim]")
            console.print(f"    [{r['category']}]/[{r['priority']}]  {(r.get('description') or '')[:80]}")

    _output(results, human)


@cli.command()
@click.argument("ref")
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def versions(ctx, ref, limit):
    """Show a memory's edit history."""
    client = _resolve(ctx)
    history = client.get(f"/api/v1/memories/{ref}/versions", params={"limit": limit})

    def human():
        table = Table(show_header=True)
        for column in ("#", "When", "By", "Summary", "Body"):
            table.add_column(column)
        for v in history:
            body = (v["body"] or "").replace("\n", " ")
            table.add_row(str(v["version_num"]), v["created_at"][:19].replace("T", " "),
                          v.get("created_by") or "-", (v["change_summary"] or "")[:40], body[:40])
        console.print(table)

    _output(history, human)


@cli.command()
@click.argument("ref")
@click.argument("version_num", type=int)
@click.pass_context
def revert(ctx, ref, version_num):
    """Restore a memory to an earlier version (the revert is itself versioned)."""
    client = _resolve(ctx)
    result = client.post(f"/api/v1/memories/versions/{ref}/restore/{version_num}")

    def human():
        console.print(f"[green]{result['message']}[/green]")

    _output(result, human)


# ── Taxonomy & links ─────────────────────────────────────────────────

@cli.command()
@click.pass_context
def tags(ctx):
    """List tags with usage counts."""
    client = _resolve(ctx)
    data = client.get("/api/v1/tags")

    def human():
        table = Table(show_header=True)
        table.add_column("Tag")
        table.add_column("Memories", justify="right")
        for t in data:
            table.add_row(t["name"], str(t["count"]))
        console.print(table)

    _output(data, human)


@cli.command()
@click.option("--with-memories", is_flag=True, help="Also list the memories in each branch.")
@click.pass_context
def branches(ctx, with_memories):
    """List branches, including the ones only implied by memories."""
    client = _resolve(ctx)
    data = client.get("/api/v1/branches")

    def human():
        table = Table(show_header=True)
        for column in ("Branch", "Memories", "Registered"):
            table.add_column(column)
        for b in data:
            table.add_row(b["name"], str(b["memories"]), "yes" if b["registered"] else "no")
        console.print(table)
        if with_memories:
            for b in data:
                memories = client.get(f"/api/v1/branches/{b['name']}")
                console.print(f"\n[bold]{b['name']}[/bold]")
                for m in memories:
                    console.print(f"  [cyan]{m['slug']}[/cyan]  {m['name']}")

    _output(data, human)


@cli.command()
@click.argument("slug")
@click.argument("target")
@click.option("--type", "relation_type", default="related",
              type=click.Choice(["related", "depends_on", "supersedes", "seealso"]))
@click.pass_context
def relate(ctx, slug, target, relation_type):
    """Link two memories together."""
    client = _resolve(ctx)
    result = client.post(f"/api/v1/memories/{slug}/relate",
                         params={"target": target, "relation_type": relation_type})

    def human():
        console.print(f"[green]{result['message']}[/green]")

    _output(result, human)


@cli.command("related")
@click.argument("ref")
@click.pass_context
def related(ctx, ref):
    """Memories linked to, or sharing tags with, this one."""
    client = _resolve(ctx)
    data = client.get(f"/api/v1/search/recommend/{ref}")

    def human():
        if not data:
            console.print("[dim]No related memories[/dim]")
            return
        for r in data:
            console.print(f"  [cyan]{r['slug']}[/cyan]  ({r['relation_type']})  {r['name']}")

    _output(data, human)


# ── Sync & backups ───────────────────────────────────────────────────

@cli.group()
@click.pass_context
def sync(ctx):
    """Filesystem <-> database sync."""
    pass


@sync.command("push")
@click.pass_context
def sync_push(ctx):
    """Write every memory out as a markdown file."""
    client = _resolve(ctx)
    result = client.post("/api/v1/sync/push")

    def human():
        console.print(f"[green]{result['message']}[/green]")
        for error in result.get("errors", []):
            console.print(f"  [red]{error}[/red]")

    _output(result, human)


@sync.command("pull")
@click.pass_context
def sync_pull(ctx):
    """Import file edits (and new files) back into the database."""
    client = _resolve(ctx)
    result = client.post("/api/v1/sync/pull")

    def human():
        console.print(f"[green]{result['message']}[/green]")
        for group in ("created", "updated"):
            if result[group]:
                console.print(f"  {group}: {', '.join(result[group])}")

    _output(result, human)


@sync.command("status")
@click.pass_context
def sync_status(ctx):
    """Compare the database with the memory files on disk."""
    client = _resolve(ctx)
    data = client.get("/api/v1/sync/status")

    def human():
        console.print(f"\n  dir: {data['memory_dir']} ({'ok' if data['memory_dir_exists'] else 'MISSING'})")
        console.print(f"  db memories: {data['db_memories']}   files: {data['files']}")
        if data["only_in_db"]:
            console.print(f"  [yellow]only in db:[/yellow] {', '.join(data['only_in_db'])}")
        if data["only_in_files"]:
            console.print(f"  [yellow]only in files:[/yellow] {', '.join(data['only_in_files'])}")
        if data["search_index_unindexed"]:
            console.print(f"  [red]unindexed:[/red] {data['search_index_unindexed']} "
                          "(run: msc sync reindex)")
        if data.get("last_sync"):
            last = data["last_sync"]
            console.print(f"  last: {last['direction']} {last['status']} — {last['detail']}")

    _output(data, human)


@sync.command("reindex")
@click.pass_context
def sync_reindex(ctx):
    """Rebuild the search index and the JSON file index."""
    client = _resolve(ctx)
    result = client.post("/api/v1/sync/reindex")

    def human():
        console.print(f"[green]{result['message']}[/green]")

    _output(result, human)


@sync.command("logs")
@click.option("--limit", default=20, show_default=True)
@click.option("--direction", type=click.Choice(["push", "pull", "sync"]), default=None)
@click.pass_context
def sync_logs(ctx, limit, direction):
    """Recent sync activity."""
    client = _resolve(ctx)
    params = {"limit": limit}
    if direction:
        params["direction"] = direction
    data = client.get("/api/v1/sync/logs", params=params)

    def human():
        for entry in data:
            console.print(f"  {entry['created_at'][:19].replace('T', ' ')}  "
                          f"{entry['direction']:5}  {entry['status']:8}  {entry['detail']}")

    _output(data, human)


@cli.group(invoke_without_command=True)
@click.pass_context
def backup(ctx):
    """Backups: create, list, download, delete."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@backup.command("create")
@click.pass_context
def backup_create(ctx):
    """Create a tar.gz backup of the database and memory files."""
    client = _resolve(ctx)
    result = client.post("/api/v1/backup/create")

    def human():
        console.print(f"[green]Created[/green] {result['filename']} "
                      f"({result['size'] / 1024:.0f} KB)")

    _output(result, human)


@backup.command("list")
@click.pass_context
def backup_list(ctx):
    """List available backups."""
    client = _resolve(ctx)
    data = client.get("/api/v1/backup/list")

    def human():
        if not data:
            console.print("[dim]No backups yet[/dim]")
            return
        for b in data:
            console.print(f"  {b['filename']}  {b['size'] / 1024:.0f} KB  {b['created_at'][:10]}")

    _output(data, human)


@backup.command("download")
@click.argument("filename")
@click.option("--out", "out_path", default=None, help="Local path (default: the filename).")
@click.pass_context
def backup_download(ctx, filename, out_path):
    """Download a backup to this machine."""
    client = _resolve(ctx)
    resp = client.request("GET", f"/api/v1/backup/backups/{filename}/download", raw=True)
    if resp.status_code != 200:
        raise click.ClickException(f"Download failed: {resp.status_code}")

    destination = Path(out_path or filename)
    destination.write_bytes(resp.content)
    console.print(f"[green]Saved[/green] {destination} ({len(resp.content) / 1024:.0f} KB)")


@backup.command("delete")
@click.argument("filename")
@click.pass_context
def backup_delete(ctx, filename):
    """Delete a backup on the server."""
    client = _resolve(ctx)
    result = client.delete(f"/api/v1/backup/backups/{filename}")

    def human():
        console.print(f"[green]{result['message']}[/green]")

    _output(result, human)


if __name__ == "__main__":
    cli()
