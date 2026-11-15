"""Run with python3 -m server, from the repository root."""
import argparse
import os
from pathlib import Path
import signal

from .backends import backend_summary, create_backend
from .config import ConfigError, load_config
from .launcher import DEFAULT_PORT, bind_server, existing_service, open_page
from .service import Application, core


def main(argv=None, *, allow_force_exit=False):
    parser = argparse.ArgumentParser(description="本机论文阅读 Agent 服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间。")
    existing = existing_service(args.port)
    if existing is not None:
        url = f"http://127.0.0.1:{args.port}"
        print(f"论文阅读服务已在运行：{url}", flush=True)
        print(f"当前模型后端：{existing['backend']}", flush=True)
        print("如需停止，请关闭原来的服务窗口，或在原窗口按 Ctrl+C。", flush=True)
        if args.open:
            open_page(url)
        return 0

    home = Path(os.environ.get("PAPERREAD_HOME", "~/.paperread")).expanduser().resolve()
    stop_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        stop_signals.append(signal.SIGHUP)
    previous = {number: signal.getsignal(number) for number in stop_signals}

    def stop(signum, frame):
        raise KeyboardInterrupt

    for number in stop_signals:
        signal.signal(number, stop)
    app = server = None
    finished = True
    status = 0
    try:
        config = load_config(home)
        info = backend_summary(config)
        info["backend"] = info.pop("selected")
        with core.file_lock(home / ".server.lock", timeout=0.2):
            try:
                app = Application(core.Store(home), create_backend(config), backend_info=info, config=config)
                server = bind_server(app, args.port)
                url = f"http://127.0.0.1:{server.server_port}"
                print(f"论文阅读服务已启动：{url}", flush=True)
                print(f"当前模型后端：{config.backend}", flush=True)
                print("关掉这个窗口，或按 Ctrl+C，即可停止服务。", flush=True)
                if args.open:
                    open_page(url)
                server.serve_forever(poll_interval=0.2)
            finally:
                # Terminal closure may deliver several signals. Finish the
                # owned-process cleanup before honoring another stop request.
                for number in stop_signals:
                    signal.signal(number, signal.SIG_IGN)
                if server is not None:
                    server.server_close()
                if app is not None:
                    app.close(wait=False)
                    finished = app.wait_closed(timeout=3)
    except KeyboardInterrupt:
        pass
    except ConfigError as error:
        print(f"配置错误：{error}", flush=True)
        status = 1
    except (core.PaperError, OSError):
        print("无法启动服务。请检查同一数据目录是否已有服务运行，以及目录是否可写。", flush=True)
        status = 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    if not finished and allow_force_exit:
        # urllib may still be waiting for response headers in a worker. The
        # application has stopped writes and killed its owned CLI groups; the
        # lock was released above. Only the standalone entry point may bypass
        # ThreadPoolExecutor's interpreter-exit wait, never an imported fixture.
        os._exit(status)
    return status


if __name__ == "__main__":
    raise SystemExit(main(allow_force_exit=True))
