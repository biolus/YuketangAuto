# -*- coding: utf-8 -*-
"""把刷课脚本的输出实时转显到窗口，同时落盘一份完整日志。

用法（在 bat 里）:
    python yuketang.py ... 2>&1 | python tee_log.py

左边进程的 stdout/stderr 逐块读到、立刻写窗口 + 写文件，
长挂机时窗口照常实时滚动，日志里留全量，出问题拿日志就能排障。
日志存到脚本同目录的 运行日志/ 下，文件名 run_时间戳.log。
"""
import os
import sys
import time


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base, "运行日志")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = base
    path = os.path.join(log_dir, "run_%s.log" % time.strftime("%Y%m%d_%H%M%S"))

    out = sys.stdout.buffer
    header = "===== 开始 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")
    header = header.encode("utf-8")

    with open(path, "wb") as f:
        f.write(header)
        f.flush()
        while True:
            try:
                chunk = os.read(0, 8192)  # read 而非 read(n)：有多少读多少，不攒批
            except OSError:
                break
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            f.write(chunk)
            f.flush()
        tail = (
            "\n===== 运行结束 %s | 完整日志: %s =====\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), path)
        ).encode("utf-8")
        f.write(tail)
    out.write(tail)
    out.flush()


if __name__ == "__main__":
    main()
