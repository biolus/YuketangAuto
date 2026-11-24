# -*- coding: utf-8 -*-
"""长江雨课堂自动刷课 —— 图形界面

Tkinter 原生窗口（Python 自带，零额外依赖）：
  · 粘贴浏览器 Cookie（自动存 cookie.txt，下次自动带出）、填课程序号，点按钮后台启动
    yuketang.py 子进程；
  · 子进程输出逐行读进日志区实时滚动，窗口不卡死（子线程 + 队列）；
  · 自动识别「📊 任务点: x/y」行，解析成真正的进度条；
  · 运行日志同时落盘到 运行日志/，关窗口也不丢；
  · [停止运行] 直接结束子进程；关窗口自动收尾。

协议层完全复用 yuketang.py，本文件只做壳，不碰刷课逻辑。
"""
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

# --windowed 模式下 sys.stdout/stderr 可能为 None，防止 print 崩溃
# 必须显式指定 utf-8 + errors=ignore：默认 GBK 编码打印 emoji 会抛 UnicodeEncodeError
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="ignore")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="ignore")

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "cookie.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class _PipeOut:
    """打包模式下，把 yuketang.main() 的 print 输出转发到 GUI 队列。"""

    def __init__(self, q):
        self.q = q
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.q.put(("line", line.rstrip("\r\n")))

    def flush(self):
        if self._buf.strip():
            self.q.put(("line", self._buf.rstrip("\r\n")))
            self._buf = ""

# 输出解析（和 yuketang.py 的打印格式保持一致）
PROG_RE = re.compile(r"任务点:\s*(\d+)/(\d+)")
COURSE_RE = re.compile(r"当前课程：(.+?)\s*$")
WARE_RE = re.compile(r"进入课件：(.+?)\s*（")

FONT = ("Microsoft YaHei UI", 10)

MODES = [("all", "全部（视频 + 作业）"), ("video", "仅视频"), ("homework", "仅作业")]
SPEEDS = [
    ("sim", "仿真 1×（真实时钟，最稳最慢）"),
    ("5", "5×"),
    ("10", "10×（默认）"),
    ("20", "20×（激进，易触发限流）"),
]


def load_cfg():
    try:
        import json
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    try:
        import json
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.q = queue.Queue()
        self.running = False
        self.log_fp = None
        self.cfg = load_cfg()

        root.title("长江雨课堂 自动刷课")
        root.geometry("920x640")
        root.minsize(740, 520)

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        # ---------- Cookie 区 ----------
        rowc = ttk.Frame(main)
        rowc.pack(fill="x")
        ttk.Label(rowc, text="Cookie（浏览器 F12 复制整行，粘贴后自动保存）",
                  font=FONT).pack(anchor="w")
        self.txt_cookie = scrolledtext.ScrolledText(
            rowc, font=("Consolas", 9), height=3, wrap="char")
        self.txt_cookie.pack(fill="x", pady=(4, 0))
        self._load_cookie()

        hint = ("获取方法：登录 changjiang.yuketang.cn → F12 → 网络 → 点任意请求 → 标头 → "
                "请求标头里 cookie 整行复制。失效就重新复制粘贴一次。")
        rowh = ttk.Frame(main)
        rowh.pack(fill="x", pady=(2, 6))
        ttk.Label(rowh, text=hint, font=("Microsoft YaHei UI", 9),
                  foreground="#777777").pack(side="left")
        self.btn_browser = ttk.Button(
            rowh, text="🌐 浏览器登录抓 Cookie", command=self.browser_login, width=22)
        self.btn_browser.pack(side="right")

        # ---------- 参数区 ----------
        row1 = ttk.Frame(main)
        row1.pack(fill="x")
        ttk.Label(row1, text="课程序号", font=FONT).pack(side="left")
        self.e_idx = ttk.Entry(row1, width=8, font=FONT)
        self.e_idx.pack(side="left", padx=(6, 18))

        ttk.Label(row1, text="内容", font=FONT).pack(side="left")
        self.cb_mode = ttk.Combobox(
            row1, state="readonly", width=18, font=FONT,
            values=[label for _, label in MODES])
        self.cb_mode.pack(side="left", padx=(6, 18))
        mode_now = str(self.cfg.get("mode", "all"))
        ids = [mid for mid, _ in MODES]
        self.cb_mode.current(ids.index(mode_now) if mode_now in ids else 0)

        # ---------- 倍速区 ----------
        rows = ttk.Frame(main)
        rows.pack(fill="x", pady=(6, 2))
        ttk.Label(rows, text="看课倍速", font=FONT).pack(side="left")
        self.cb_speed = ttk.Combobox(
            rows, state="readonly", width=30, font=FONT,
            values=[label for _, label in SPEEDS])
        self.cb_speed.pack(side="left", padx=6)
        speed_now = str(self.cfg.get("video_speed", "10"))
        sids = [sid for sid, _ in SPEEDS]
        self.cb_speed.current(sids.index(speed_now) if speed_now in sids else 2)
        ttk.Label(rows, text="限流自动等待，进度不涨自动降速", font=FONT,
                  foreground="#777777").pack(side="left", padx=(8, 0))
        self.cb_speed.bind("<<ComboboxSelected>>", self._save_settings)
        self.cb_mode.bind("<<ComboboxSelected>>", self._save_settings)

        # ---------- 按钮区 ----------
        row2 = ttk.Frame(main)
        row2.pack(fill="x", pady=(8, 6))
        self.btn_test = ttk.Button(row2, text="测试连接", command=self.run_test, width=14)
        self.btn_test.pack(side="left")
        self.btn_ls = ttk.Button(row2, text="查看课程列表", command=self.run_ls, width=16)
        self.btn_ls.pack(side="left", padx=10)
        self.btn_run = ttk.Button(row2, text="开始刷课", command=self.run_main, width=16)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(row2, text="停止运行", command=self.stop,
                                   width=14, state="disabled")
        self.btn_stop.pack(side="left", padx=10)

        # ---------- 进度区 ----------
        row3 = ttk.Frame(main)
        row3.pack(fill="x", pady=(8, 2))
        self.bar = ttk.Progressbar(row3, maximum=100, value=0)
        self.bar.pack(side="left", fill="x", expand=True)
        self.lbl_pct = ttk.Label(row3, text="0%", font=FONT, width=6)
        self.lbl_pct.pack(side="left", padx=(8, 0))

        self.lbl_status = ttk.Label(
            main, text="状态：待粘贴 Cookie（粘贴后自动保存）",
            font=FONT, foreground="#555555")
        self.lbl_status.pack(fill="x", pady=(4, 8))

        # ---------- 日志区 ----------
        self.log = scrolledtext.ScrolledText(
            main, font=FONT, wrap="word", state="disabled", height=18)
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground="#c0392b")
        self.log.tag_configure("ok", foreground="#1e8449")
        self.log.tag_configure("warn", foreground="#b8860b")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll()

    # ---------- Cookie ----------
    def _load_cookie(self):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                self.txt_cookie.insert("1.0", f.read().strip())
        except OSError:
            pass

    def _flush_cookie(self):
        """输入框内容写回 cookie.txt，子进程从那里读。返回是否非空。"""
        cookie = self.txt_cookie.get("1.0", "end").strip()
        if cookie:
            try:
                with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                    f.write(cookie)
            except OSError:
                pass
            return True
        return os.path.exists(COOKIE_FILE)

    def _save_settings(self, _event=None):
        self.cfg["mode"] = MODES[self.cb_mode.current()][0]
        self.cfg["video_speed"] = SPEEDS[self.cb_speed.current()][0]
        save_cfg(self.cfg)

    # ---------- 浏览器半自动登录 ----------
    def browser_login(self):
        """点按钮 → 弹浏览器 → 用户登录 → 自动抓 Cookie 回填。
        selenium/浏览器操作全在子线程，进度经 queue 转发到日志区。"""
        if getattr(self, "_browser_busy", False):
            return
        self._browser_busy = True
        self.btn_browser["state"] = "disabled"
        self._append("\n🌐 浏览器登录抓 Cookie 已启动...\n", "warn")
        threading.Thread(target=self._browser_login_work, daemon=True).start()

    def _browser_login_work(self):
        import browser_login as bl

        def status(msg):
            self.q.put(("line", msg))

        try:
            cookie = bl.grab_cookie_via_browser(
                timeout=bl.TIMEOUT, on_status=status)
        except Exception as e:
            cookie = None
            status("❌ 浏览器登录模块异常：%s: %s" % (type(e).__name__, e))
        self.q.put(("cookie_grabbed", cookie))

    def _on_cookie_grabbed(self, cookie):
        self._browser_busy = False
        self.btn_browser["state"] = "normal"
        if not cookie:
            self.lbl_status["text"] = "状态：Cookie 抓取未完成（看上方日志）"
            return
        # 注意：browser_login.grab_cookie_via_browser 内部已经做过 user_info 二次验证，
        # 到这里的 cookie 理论上已验证通过。但仍兜底再验一次，避免边界情况把好 Cookie 冲掉。
        self.lbl_status["text"] = "状态：抓到 Cookie，正在确认后保存（不通过会保留原 Cookie）"
        self._append("🔍 抓到 Cookie，先验证再决定是否覆盖原 cookie.txt...\n", "warn")
        threading.Thread(target=self._verify_and_save, args=(cookie,),
                         daemon=True).start()

    def _verify_and_save(self, cookie):
        """二次验证通过才覆盖 cookie.txt；不通过保留旧 Cookie，避免误冲。"""
        import browser_login as bl
        ok, name = bl.verify_cookie(
            cookie, on_status=lambda m: self.q.put(("line", m)))
        if ok:
            self.q.put(("cookie_apply", cookie))
        else:
            self.q.put(("cookie_reject", None))

    def _apply_cookie(self, cookie):
        """主线程里执行覆盖 + 回填（Tkinter 控件必须在主线程操作）。"""
        self.txt_cookie.configure(state="normal")
        self.txt_cookie.delete("1.0", "end")
        self.txt_cookie.insert("1.0", cookie)
        import browser_login as bl
        bl.save_cookie(cookie)
        self._append("💾 Cookie 已回填并保存到 cookie.txt（原 Cookie 已被替换）\n", "ok")
        self._append("✅ 验证通过，可以开刷了\n", "ok")
        self.lbl_status["text"] = "状态：Cookie 已就绪，可以开刷了"

    def _reject_cookie(self):
        """验证未通过：保留旧 Cookie，提示用户。"""
        self._append(
            "❌ 抓到的 Cookie 验证未通过，已保留原 cookie.txt（避免把好的冲掉）\n", "err")
        self._append(
            "💡 建议：1) 在弹出的浏览器里完整登录后再点一次抓 Cookie；"
            "2) 或改用 F12 手动复制 Cookie 粘贴到上方输入框。\n", "warn")
        self.lbl_status["text"] = "状态：抓到的 Cookie 无效，已保留原 Cookie"

    def _verify_cookie(self, cookie):
        import browser_login as bl
        ok, _name = bl.verify_cookie(
            cookie, on_status=lambda m: self.q.put(("line", m)))
        self.q.put(("line", "✅ 验证通过，可以开刷了" if ok else "⚠️ 验证未通过，建议手动「测试连接」复查"))

    # ---------- 动作 ----------
    def run_test(self):
        if not self._flush_cookie():
            messagebox.showwarning("缺少 Cookie", "请先粘贴浏览器 Cookie")
            return
        self._start(["test"])

    def run_ls(self):
        if not self._flush_cookie():
            messagebox.showwarning("缺少 Cookie", "请先粘贴浏览器 Cookie")
            return
        self._start(["ls"])

    def run_main(self):
        if not self._flush_cookie():
            messagebox.showwarning("缺少 Cookie", "请先粘贴浏览器 Cookie")
            return
        idx = self.e_idx.get().strip()
        if not (idx.isdigit() or idx in ("all", "0")):
            messagebox.showwarning(
                "课程序号有误",
                "课程序号必须是数字（或 all）。\n先点「查看课程列表」，"
                "每行行首的序号就是要填的数字。"
            )
            return
        mode = MODES[self.cb_mode.current()][0]
        speed = SPEEDS[self.cb_speed.current()][0]
        self._save_settings()
        self._start([idx, mode, "--speed", speed])

    def stop(self):
        if getattr(sys, "frozen", False):
            self._append("⚠️ 打包版不支持中途停止，请等待当前任务完成或关闭窗口重开\n", "warn")
            return
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self._append("⚠️ 已发送停止指令，正在结束当前任务...\n", "warn")

    # ---------- 子进程 ----------
    def _start(self, args):
        if self.running:
            return
        self._append("\n" + "=" * 46 + "\n")
        shown = [a for a in args if not a.startswith(("csrftoken", "sessionid"))]
        self._append(
            "===== 开始 %s | python yuketang.py %s\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(shown)),
            "warn",
        )
        # 每次运行单独一份日志，和控制台版的 tee_log 保持同一目录
        log_dir = os.path.join(BASE_DIR, "运行日志")
        try:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "gui_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
            self.log_fp = open(path, "wb")
        except OSError:
            self.log_fp = None
        self.bar["value"] = 0
        self.lbl_pct["text"] = "0%"
        self.lbl_status["text"] = "状态：启动中..."
        self.running = True
        self.btn_test["state"] = "disabled"
        self.btn_ls["state"] = "disabled"
        self.btn_run["state"] = "disabled"
        self.btn_stop["state"] = "normal"
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()

    def _worker(self, args):
        code = None
        try:
            if getattr(sys, "frozen", False):
                # 打包模式：import 调用 yuketang.main()，stdout 转发到队列
                import yuketang
                old_argv = sys.argv
                old_stdout = sys.stdout
                sys.argv = ["yuketang.py"] + args
                sys.stdout = _PipeOut(self.q)
                try:
                    yuketang.main()
                    code = 0
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                finally:
                    sys.argv = old_argv
                    sys.stdout = old_stdout
                self.q.put(("done", code))
            else:
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                self.proc = subprocess.Popen(
                    [sys.executable, "yuketang.py"] + args,
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                for raw in iter(self.proc.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    self.q.put(("line", line))
                code = self.proc.wait()
                self.q.put(("done", code))
        except Exception as e:  # 子进程起不来等意外
            self.q.put(("error", "%s: %s" % (type(e).__name__, e)))

    # ---------- 输出消费 ----------
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "line":
                    self._on_line(data)
                elif kind == "cookie_grabbed":
                    self._on_cookie_grabbed(data)
                elif kind == "cookie_apply":
                    self._apply_cookie(data)
                elif kind == "cookie_reject":
                    self._reject_cookie()
                elif kind == "done":
                    self._finish("运行结束（退出码 %s）" % data)
                elif kind == "error":
                    self._finish("启动失败：%s" % data, err=True)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _on_line(self, line):
        self._append(line + "\n", tag=self._pick_tag(line))
        self._parse(line)
        if self.log_fp:
            try:
                self.log_fp.write((line + "\n").encode("utf-8"))
                self.log_fp.flush()
            except OSError:
                pass

    @staticmethod
    def _pick_tag(line):
        if "❌" in line or "Cookie" in line and "失效" in line or "Traceback" in line:
            return "err"
        if "✅" in line:
            return "ok"
        if "⚠️" in line:
            return "warn"
        return None

    def _parse(self, line):
        m = PROG_RE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            self.bar.configure(maximum=max(total, 1), value=min(done, total))
            self.lbl_pct["text"] = "%d%%" % (done * 100 // total if total else 0)
            return
        m = COURSE_RE.search(line)
        if m:
            self.lbl_status["text"] = "当前课程：%s" % m.group(1)
            return
        m = WARE_RE.search(line)
        if m:
            self.lbl_status["text"] = "当前课件：%s" % m.group(1)

    def _append(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _finish(self, msg, err=False):
        self.running = False
        if self.log_fp:
            try:
                tail = ("\n===== %s | %s =====\n" % (msg, time.strftime("%H:%M:%S"))).encode("utf-8")
                self.log_fp.write(tail)
                self.log_fp.close()
            except OSError:
                pass
            self.log_fp = None
        self._append("\n💡 %s\n" % msg, "err" if err else "ok")
        self.lbl_status["text"] = "状态：" + msg
        self.btn_test["state"] = "normal"
        self.btn_ls["state"] = "normal"
        self.btn_run["state"] = "normal"
        self.btn_stop["state"] = "disabled"

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(
                "仍在运行", "任务还在后台跑，确定要退出吗？（退出会中断刷课）"
            ):
                return
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.root.destroy()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # 高分屏不模糊
    except Exception:
        pass
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
