#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地启动阅读器：起一个只监听本机的静态服务，然后自动打开浏览器。

为什么需要这个：
直接双击 reader.html 打开时，file:// 协议下浏览器禁止网页读取同目录的
paper.json（安全限制），所以「一键打开已解析论文」用不了。

走 http 之后：
- 快捷入口正常
- 搜索页的跨域请求更稳
- Service Worker 也能注册（PWA 可安装）

关掉这个窗口就停止服务。
"""

import http.server
import os
import socketserver
import threading
import webbrowser

PORT = 8765
HOST = "127.0.0.1"


def main():
    # 以脚本所在目录为站点根，这样 out/ 和 reader.html 的相对路径才对
    base_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base_dir)

    handler = http.server.SimpleHTTPRequestHandler

    # 端口被占用就往后找，避免第二次启动失败
    port = PORT
    httpd = None
    for candidate in range(PORT, PORT + 20):
        try:
            httpd = socketserver.TCPServer((HOST, candidate), handler)
            port = candidate
            break
        except OSError:
            continue

    if httpd is None:
        print("端口 %d-%d 全被占用，关掉别的服务再试。" % (PORT, PORT + 19))
        return 1

    url = "http://%s:%d/reader.html" % (HOST, port)
    print("论文精读助手")
    print("-" * 46)
    print("  阅读器    %s" % url)
    print("  搜索页    http://%s:%d/search.html" % (HOST, port))
    print("  学习站    http://%s:%d/../index.html" % (HOST, port))
    print()
    print("服务运行中，关掉这个窗口即停止。")

    # 等服务起来再开浏览器，否则可能抢跑导致 502
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
