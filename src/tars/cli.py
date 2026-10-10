"""Command line entry point: `tars voice`, `tars chat`, `tars status`, and setup commands."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import shutil
import statistics
import sys
import time
from pathlib import Path

from . import secrets
from .config import load_config

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config.example.toml"


def cmd_init(args, config) -> None:
    target = config.home / "config.toml"
    if target.exists():
        print(f"{target} already exists.")
        return
    config.home.mkdir(parents=True, exist_ok=True)
    if EXAMPLE_CONFIG.exists():
        shutil.copy(EXAMPLE_CONFIG, target)
    else:  # installed without the repo checkout
        target.write_text('user_name = ""\n\n[location]\nhome_address = ""\ntimezone = ""\n', encoding="utf-8")
    print(f"Wrote {target}. Edit it, then add keys with `tars set-key`.")


def cmd_set_key(args, config) -> None:
    if args.name not in secrets.ALL_KEYS:
        sys.exit(f"Unknown key. Choose one of: {', '.join(secrets.ALL_KEYS)}")
    value = getpass.getpass(f"{args.name}: ").strip()
    secrets.set_secret(args.name, value)
    print("Saved to the system credential store.")


def cmd_google_auth(args, config) -> None:
    from .integrations.google_auth import run_consent_flow

    run_consent_flow(Path(args.client_secrets))
    print("Google connected.")


async def _chat(config) -> None:
    from .app import build_app

    async def announce(text: str) -> None:
        print(f"\n[TARS] {text}\n> ", end="", flush=True)

    app = build_app(config, announce)
    _mood_task = asyncio.create_task(app.mood.run())  # discretion and stress checks; kept referenced
    print("Type to talk to TARS (Ctrl+C to quit). Replies are what TARS would say aloud.")
    loop = asyncio.get_running_loop()
    _serve_ui(app, loop)
    while True:
        try:
            line = await loop.run_in_executor(None, input, "> ")
        except (EOFError, KeyboardInterrupt):
            return
        if not line.strip():
            continue

        async def on_text(delta: str) -> None:
            print(delta, end="", flush=True)

        print("TARS: ", end="", flush=True)
        result = await app.agent.handle(line, on_text)
        models = ",".join(m.split("-")[1] for m in result.models) or "-"
        print(f"\n   ({result.first_text_ms} ms to first word, {models}, ${result.usd:.4f})")


def cmd_chat(args, config) -> None:
    asyncio.run(_chat(config))


def cmd_voice(args, config) -> None:
    from .voice.run import run_voice

    asyncio.run(run_voice(config))


def cmd_status(args, config) -> None:
    from .app import build_app, status_lines

    async def noop(text: str) -> None:
        pass

    app = build_app(config, noop)
    print("\n".join(status_lines(app)))


async def _probe(config, n: int) -> None:
    """Phase 1 test: Claude Haiku 5.5's real time to first token, as TARS calls it."""
    from anthropic import AsyncAnthropic

    from .persona import SYSTEM_PROMPT

    client = AsyncAnthropic(api_key=secrets.get_secret(secrets.ANTHROPIC_API_KEY))
    prompts = ["What's 15% of 80?", "How many ounces in a pound?", "Say good morning.",
               "What's the capital of Australia?", "Convert 10 km to miles."]
    firsts, totals = [], []
    for i in range(n):
        start = time.monotonic()
        first = None
        async with client.messages.stream(
            model=config.brain.fast_model,
            max_tokens=256,
            thinking={"type": "disabled"},
            output_config={"effort": config.brain.fast_effort},
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompts[i % len(prompts)]}],
        ) as stream:
            async for text in stream.text_stream:
                if first is None and text.strip():
                    first = time.monotonic() - start
            await stream.get_final_message()
        totals.append(time.monotonic() - start)
        firsts.append(first or totals[-1])
        print(f"{i + 1:>2}. first text {firsts[-1] * 1000:6.0f} ms, done {totals[-1] * 1000:6.0f} ms")
    print(f"median first text {statistics.median(firsts) * 1000:.0f} ms (budget assumes 400-700 ms)")


def cmd_probe(args, config) -> None:
    asyncio.run(_probe(config, args.n))


def cmd_check(args, config) -> None:
    from .check import run_checks

    print("Checking each account with a read-only call (nothing is changed or sent)...")
    results = asyncio.run(run_checks(config))
    print("\n".join(r.line() for r in results))
    failed = [r.name for r in results if r.ok is False]
    if failed:
        sys.exit(f"\n{len(failed)} to fix before the test script: {', '.join(failed)}.")
    print("\nReady for the Gate 1 test script (docs/TEST_SCRIPT.md).")


def cmd_devices(args, config) -> None:
    from .check import audio_devices

    try:
        lines = audio_devices()
    except ImportError:
        sys.exit("Audio needs the voice extras: uv sync --locked --extra voice")
    print("Index  Kind   Name")
    print("\n".join(lines) or "No audio devices found.")
    print("\nTo pick the headset, set voice.input_device_index and voice.output_device_index in config.toml.")


def cmd_memory(args, config) -> None:
    from .memory import Vault
    from .memory_map import MemoryMap

    vault = Vault(config.vault)
    vault.ensure()
    memory_map = MemoryMap(vault)
    url = memory_map.open() if not args.no_browser else memory_map.start()
    print(f"Memory map: {url}\nIt updates as TARS learns things. Press Ctrl+C to close it.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        memory_map.stop()


def _serve_ui(app, loop) -> None:
    """Let the local pages act through this process: buttons become typed turns, printed here."""
    async def typed(text: str) -> None:
        async def on_text(delta: str) -> None:
            print(delta, end="", flush=True)

        print(f"\n[from the window] {text}\nTARS: ", end="", flush=True)
        await app.agent.handle(text, on_text)
        print("\n> ", end="", flush=True)

    app.ui.ctx.loop = loop
    app.ui.ctx.submit = lambda text: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(typed(text)))


def _wait_forever(stop) -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop()


def cmd_ui(args, config) -> None:
    import threading

    from .app import build_app

    async def announce(text: str) -> None:
        print(f"[TARS] {text}")

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="tars-loop").start()
    app = build_app(config, announce)
    _serve_ui(app, loop)
    url = app.ui.open(args.page) if not args.no_browser else (app.ui.start() and app.ui.url(args.page))
    print(f"TARS {args.page}: {url}\nPress Ctrl+C to close.")
    _wait_forever(app.ui.stop)


def cmd_setup(args, config) -> None:
    from .memory import Vault
    from .ui.server import AppServer, UiContext, ensure_config

    path = args.config or config.home / "config.toml"
    ensure_config(config, path)  # a fresh install starts from the example, with the TARS voice
    vault = Vault(config.vault)
    vault.ensure()
    server = AppServer(UiContext(config, vault, config_path=path))
    url = server.open("setup") if not args.no_browser else (server.start() and server.url("setup"))
    print(f"Setup: {url}\nPress Ctrl+C when you're done.")
    _wait_forever(server.stop)


def cmd_backup(args, config) -> None:
    from .backup import BackupError, backup

    try:
        target = backup(config, args.config or config.home / "config.toml")
    except BackupError as e:
        raise SystemExit(f"Backup failed: {e}") from None
    print(f"Backed up the memory vault, settings and history to {target} (checked against the originals).")


def cmd_restore(args, config) -> None:
    from .backup import SAFETY, BackupError, available, restore

    found = available(config)
    if not args.which:
        if not found:
            raise SystemExit(f"No backups yet in {config.backups}. `tars backup` makes one.")
        print("Backups, newest first:")
        for path in found:
            note = "  (how things were before the last restore)" if path.name == SAFETY else ""
            print(f"  {path.name}{note}")
        print("Restore one with `tars restore <date>`, e.g. `tars restore latest`.")
        return
    source = found[0] if args.which == "latest" and found else config.backups / args.which
    print(f"This puts back the memory vault, settings and history from {source.name}.\n"
          "Quit TARS first (tray menu, Quit) and close Obsidian. Your current state is kept as 'before-restore'.")
    if not args.yes and input("Restore it? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing changed.")
        return
    try:
        restore(config, args.config or config.home / "config.toml", source)
    except BackupError as e:
        raise SystemExit(str(e)) from None
    undo = "" if source.name == SAFETY else " To undo it: `tars restore before-restore`."
    print(f"Restored {source.name}. Start TARS again.{undo}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tars", description="TARS everyday voice assistant")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="write a starter config.toml").set_defaults(fn=cmd_init)
    p = sub.add_parser("set-key", help="store an API key in the credential store")
    p.add_argument("name")
    p.set_defaults(fn=cmd_set_key)
    p = sub.add_parser("google-auth", help="connect Gmail and Google Calendar")
    p.add_argument("client_secrets", help="OAuth client JSON downloaded from Google Cloud")
    p.set_defaults(fn=cmd_google_auth)
    sub.add_parser("voice", help="run TARS by voice (push-to-talk)").set_defaults(fn=cmd_voice)
    sub.add_parser("chat", help="talk to TARS by typing").set_defaults(fn=cmd_chat)
    sub.add_parser("status", help="what's connected, response times and spend").set_defaults(fn=cmd_status)
    p = sub.add_parser("probe", help="measure Claude's time to first word")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(fn=cmd_probe)
    sub.add_parser("check", help="test every account and setting with read-only calls").set_defaults(fn=cmd_check)
    sub.add_parser("devices", help="list audio devices, to pick the headset").set_defaults(fn=cmd_devices)
    p = sub.add_parser("setup", help="the setup wizard: you, keys, Google, headset, start at login")
    p.add_argument("--no-browser", action="store_true", help="print the address instead of opening it")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("ui", help="open a TARS window: status, history, settings, memory or overlay")
    p.add_argument("page", nargs="?", default="status", choices=["status", "history", "settings", "memory", "overlay"])
    p.add_argument("--no-browser", action="store_true", help="print the address instead of opening it")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("memory", help="browse everything TARS remembers, as a map")
    p.add_argument("--no-browser", action="store_true", help="print the address instead of opening it")
    p.set_defaults(fn=cmd_memory)
    sub.add_parser("backup", help="copy the memory vault, settings and history to the backup folder") \
        .set_defaults(fn=cmd_backup)
    p = sub.add_parser("restore", help="list backups, or put one back: tars restore <date|latest>")
    p.add_argument("which", nargs="?", help="a backup's date, 'latest' or 'before-restore'")
    p.add_argument("--yes", action="store_true", help="don't ask first")
    p.set_defaults(fn=cmd_restore)

    args = parser.parse_args(argv)
    config = load_config(args.config)
    from . import logs

    logs.start(config.data_dir, f"tars-{args.command}", config.privacy.transcript_days)
    args.fn(args, config)


if __name__ == "__main__":
    main()
