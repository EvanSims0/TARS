import subprocess
import sys
import textwrap


def _run(tmp_path, body: str) -> str:
    script = textwrap.dedent(f"""
        from pathlib import Path
        from tars import logs
        logs.start(Path({str(tmp_path)!r}), "tars-chat")
    """) + textwrap.dedent(body)
    subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    files = list((tmp_path / "logs").glob("tars-chat-*.log"))
    assert files, "no log file was written"
    return files[0].read_text(encoding="utf-8")


def test_a_crash_is_written_to_the_log_with_its_traceback(tmp_path):
    text = _run(tmp_path, """
        def load(secret_key="sk-ant-should-not-appear"):
            raise RuntimeError("the settings file is broken")
        load()
    """)
    assert "tars-chat crashed" in text and "RuntimeError: the settings file is broken" in text
    assert "line" in text and "sk-ant-should-not-appear" not in text  # no variable values in tracebacks


def test_a_crash_in_another_thread_or_task_is_logged(tmp_path):
    text = _run(tmp_path, """
        import asyncio, gc, threading
        t = threading.Thread(target=lambda: 1 / 0, name="follow")
        t.start(); t.join()

        async def main():
            async def boom():
                raise ValueError("lost task")
            task = asyncio.ensure_future(boom())
            await asyncio.sleep(0)
            del task
            gc.collect()
        asyncio.run(main())
        gc.collect()
    """)
    assert "follow crashed" in text and "ZeroDivisionError" in text
    assert "Task exception was never retrieved" in text
