#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 学习站 · 桌面版

做的事只有两件：
1. 起一个只监听本机的静态服务（解决 file:// 下不能 fetch 同目录文件的问题）
2. 用系统自带的 WebView2 开一个窗口指向它

没有 Electron 那 200MB 的 Chromium，用的就是 Windows 预装的 WebView2。
"""

import http.server
import os
import socket
import socketserver
import sys
import threading
import traceback

TITLE = "AI 学习站"
WIDTH, HEIGHT = 1280, 880


def resource_dir():
    """打包后静态资源在 _MEIPASS；源码运行时就是脚本所在目录"""
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """
    数据目录：exe 所在的目录。
    out/ 和 cache/ 必须留在外面——用户会不断往里面加新论文，
    打进 exe 就没法更新了。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DATA_PREFIXES = ("paper-reader/out/", "paper-reader/cache/")


class Handler(http.server.SimpleHTTPRequestHandler):
    """静态资源走 exe 内部，论文数据走 exe 同级目录"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=resource_dir(), **kwargs)

    def translate_path(self, path):
        clean = path.split("?")[0].split("#")[0].lstrip("/")
        if clean.startswith(DATA_PREFIXES):
            return os.path.join(data_dir(), clean.replace("/", os.sep))
        return super().translate_path(path)

    def log_message(self, fmt, *args):
        # 静默：控制台窗口留给错误信息
        pass


def pick_port():
    """随机空闲端口，避免和其他程序抢 8765"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(port):
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def main():
    try:
        import webview
    except ImportError:
        print("缺少 pywebview。安装：python -m pip install pywebview")
        input("按回车退出…")
        return 1

    port = pick_port()
    server = serve(port)
    url = "http://127.0.0.1:%d/index.html" % port

    print("%s 已启动" % TITLE)
    print("  本地地址：%s" % url)
    print("  数据目录：%s" % data_dir())
    print("  关掉窗口即退出")

    try:
        webview.create_window(
            TITLE,
            url,
            width=WIDTH,
            height=HEIGHT,
            min_size=(900, 600),
        )
        webview.start()
    except Exception:
        print("\n窗口启动失败：")
        traceback.print_exc()
        print("\n如果提示缺少 WebView2，去微软官网装 WebView2 Runtime。")
        input("按回车退出…")
        return 1
    finally:
        server.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
