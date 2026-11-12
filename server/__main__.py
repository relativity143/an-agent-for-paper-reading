"""Run with python3 -m server, from the repository root."""
import argparse
import os
from pathlib import Path
import signal
import webbrowser

from .backends import backend_summary, create_backend
from .config import ConfigError, load_config
from .http import create_server
from .service import Application, core


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机论文阅读 Agent 服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间。")
    home = Path(os.environ.get("PAPERREAD_HOME", "~/.paperread")).expanduser().resolve()
    try:
        config = load_config(home)
        info = backend_summary(config)
        info["backend"] = info.pop("selected")
        app = Application(core.Store(home), create_backend(config), backend_info=info)
        try:
            with core.file_lock(home / ".server.lock", timeout=0.2):
                server = create_server(app, port=args.port)
                url = f"http://127.0.0.1:{server.server_port}"
                print(f"论文阅读服务已启动：{url}（{config.backend}）", flush=True)
                print("按 Ctrl+C 停止。模型生成任务会在本轮结束或超时后退出。", flush=True)
                if args.open:
                    webbrowser.open(url)
                previous = signal.getsignal(signal.SIGTERM)
                def stop(signum, frame):
                    raise KeyboardInterrupt
                signal.signal(signal.SIGTERM, stop)
                try:
                    server.serve_forever(poll_interval=0.2)
                except KeyboardInterrupt:
                    pass
                finally:
                    signal.signal(signal.SIGTERM, previous)
                    server.server_close()
        finally:
            app.close()
    except ConfigError as error:
        print(f"配置错误：{error}")
        return 1
    except (core.PaperError, OSError):
        print("无法启动服务。请检查端口是否被占用、同一数据目录是否已有服务运行，以及目录是否可写。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
