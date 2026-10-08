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
    print("Type to talk to TARS (Ctrl+C to quit). Replies are what TARS would say aloud.")
    loop = asyncio.get_running_loop()
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


def cmd_backup(args, config) -> None:
    from .memory import Vault

    target = Vault(config.vault).backup(config.backups, config.privacy.backup_days)
    print(f"Backed up the vault to {target}")


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
    sub.add_parser("backup", help="copy the memory vault to the backup folder").set_defaults(fn=cmd_backup)

    args = parser.parse_args(argv)
    config = load_config(args.config)
    args.fn(args, config)


if __name__ == "__main__":
    main()
