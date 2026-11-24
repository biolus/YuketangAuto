# -*- coding: utf-8 -*-
"""长江雨课堂 —— 浏览器半自动登录抓 Cookie

流程：点一下 → 自动弹出浏览器登录页 → 你正常登录（扫码/账密都行）
      → 工具检测到 sessionid 出现 → 自动收割全部 Cookie 存 cookie.txt → 自动关浏览器。

技术路线：Selenium 驱动系统自带 Edge（Windows 10/11 免装浏览器）；
driver 由 Selenium Manager 自动下载（selenium >= 4.6 自带，无需手动管理）。

用法：
  GUI      ：「🌐 浏览器登录抓 Cookie」按钮
  命令行   ：python browser_login.py
  代码调用 ：from browser_login import grab_cookie_via_browser

依赖：selenium（requests 项目本体不需要它，只有本功能需要；
      首次使用会自动 pip 安装，走清华镜像）。
"""
import os
import subprocess
import sys
import time

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "cookie.txt")

LOGIN_URL = "https://changjiang.yuketang.cn/v2/web/index"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
TIMEOUT = 600  # 10 分钟没登录成功就放弃

BROWSER_PRIORITY = ("edge", "chrome")


def _log(msg, on_status=None):
    try:
        print(msg, flush=True)
    except (UnicodeEncodeError, ValueError, OSError):
        # 打包成 windowed exe 时 stdout 可能是 GBK/devnull，emoji 无法编码；
        # print 失败绝不能影响主流程（GUI 的 on_status 回调才是真正的日志出口）
        pass
    if on_status:
        on_status(msg)


def ensure_selenium(on_status=None):
    """selenium 没装就自动装（清华镜像）。返回是否可用。"""
    try:
        import selenium  # noqa: F401
        return True
    except ImportError:
        pass
    _log("📦 首次使用：正在安装 selenium（约 10 MB，走清华镜像）...", on_status)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-i", MIRROR, "selenium"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        _log("❌ selenium 安装失败：%s" % e, on_status)
        return False
    try:
        import selenium  # noqa: F401
        return True
    except ImportError:
        _log("❌ selenium 装完仍无法导入，请手动执行: pip install selenium", on_status)
        return False


def _make_driver(on_status=None):
    """按 Edge → Chrome 顺序尝试。Selenium Manager 会自动下载匹配的驱动。"""
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException

    # 稳定性参数：避免 Edge/Chrome 启动时的弹窗、扩展、首次运行向导等干扰
    # 这些干扰会让浏览器进程在 Selenium 控制初期就崩掉（表现为
    # "❌ 浏览器被关闭，抓取中断"——driver 还在但 Edge 进程已死）
    stable_args = [
        "--no-first-run",             # 不弹首次运行向导
        "--no-default-browser-check", # 不弹"设为默认浏览器"
        "--disable-extensions",       # 禁用已装扩展（避免扩展弹窗卡住）
        "--disable-popup-blocking",   # 不拦截弹窗（登录流程可能需要弹窗）
        "--disable-features=Translate,InterestCohort",
        "--disable-blink-features=AutomationControlled",  # 不显示"被自动化控制"提示
    ]

    for kind in BROWSER_PRIORITY:
        try:
            if kind == "edge":
                from selenium.webdriver.edge.options import Options
            else:
                from selenium.webdriver.chrome.options import Options
            opts = Options()
            opts.add_argument("--start-maximized")
            for a in stable_args:
                opts.add_argument(a)
            # 用独立用户数据目录，避免和用户已开的浏览器抢占 profile 锁
            import tempfile, uuid
            tmp_profile = os.path.join(tempfile.gettempdir(),
                                       "ykt_sel_%s" % uuid.uuid4().hex[:8])
            opts.add_argument("--user-data-dir=%s" % tmp_profile)
            driver = (webdriver.Edge(options=opts) if kind == "edge"
                      else webdriver.Chrome(options=opts))
            _log("🪟 已打开 %s 浏览器" % ("Edge" if kind == "edge" else "Chrome"),
                 on_status)
            return driver
        except WebDriverException as e:
            _log("⚠️ %s 启动失败（%s），尝试下一个..." % (kind, str(e)[:80]), on_status)
        except ImportError as e:
            _log("⚠️ %s 驱动不可用：%s" % (kind, e), on_status)
    return None


def _cookie_string(cookies):
    return "; ".join("%s=%s" % (c["name"], c["value"]) for c in cookies)


def _has_session(cookies):
    return any(c["name"] == "sessionid" and c.get("value") for c in cookies)


def grab_cookie_via_browser(timeout=TIMEOUT, on_status=None):
    """弹出浏览器等用户登录，抓到登录态 Cookie 返回字符串；超时/失败返回 None。

    on_status: 可选回调 f(msg)，GUI 用它把进度转发到日志区。
    """
    if not ensure_selenium(on_status):
        return None
    driver = _make_driver(on_status)
    if driver is None:
        _log("❌ Edge 和 Chrome 都启动不了（selenium 驱动下载失败或浏览器缺失）", on_status)
        return None

    try:
        _log("🌐 正在打开长江雨课堂登录页...", on_status)
        try:
            driver.get(LOGIN_URL)
        except Exception as e:
            # driver.get 抛异常（多半是 msedgedriver 与系统 Edge 版本不匹配，
            # 或 Selenium Manager 下载驱动受阻），不要让异常直接跳到 finally 关浏览器
            _log("❌ 打开登录页失败：%s" % str(e)[:160], on_status)
            _log("💡 排查：1) 重试一次（Selenium Manager 可能下次下载成功）；"
                 "2) 确认系统 Edge 版本（当前 %s）与 msedgedriver 匹配；"
                 "3) 实在不行改用 F12 手动复制 Cookie 粘贴到 GUI。" % _edge_version(),
                 on_status)
            return None
        _log("请在浏览器里登录（微信扫码 / 账号密码均可）。", on_status)
        _log("⏳ 登录成功后我会自动抓取 Cookie，最长等 %d 分钟，期间别关浏览器窗口。"
             % (timeout // 60), on_status)

        deadline = time.time() + timeout
        last_note = time.time()
        last_session = None  # 记住上次验证过的 sessionid，避免重复打 user_info
        while time.time() < deadline:
            try:
                cookies = driver.get_cookies()
            except Exception:
                # 用户手快先关了浏览器
                _log("❌ 浏览器被关闭，抓取中断", on_status)
                return None
            if _has_session(cookies):
                # 初筛通过：有 sessionid。但雨课堂未登录态也可能下发匿名 sessionid，
                # 必须调 user_info 二次验证（能换回 user_id 才算真登录态）
                cookie_str = _cookie_string(cookies)
                if cookie_str != last_session:
                    last_session = cookie_str
                    _log("🔍 检测到 sessionid，正在调 user_info 验证是否为登录态...",
                         on_status)
                    ok, _name = verify_cookie(cookie_str, on_status=on_status)
                    if ok:
                        # 登录刚完成，部分 cookie 可能还在写入，稳 2 秒再收一次
                        time.sleep(2)
                        try:
                            cookies = driver.get_cookies()
                        except Exception:
                            _log("❌ 收割时浏览器被关闭", on_status)
                            return cookie_str  # 至少手里这份是验证过的
                        cookie_str = _cookie_string(cookies)
                        _log("✅ 登录验证通过，正在收割 Cookie...", on_status)
                        return cookie_str
                    # 验证未通过 = 匿名 sessionid，继续等用户完成登录
                    _log("⏳ 当前 sessionid 仍是未登录态，继续等登录完成...",
                         on_status)
            if time.time() - last_note > 30:
                _log("⏳ 还在等待登录...（剩余 %d 分钟）"
                     % max(0, int(deadline - time.time()) // 60), on_status)
                last_note = time.time()
            time.sleep(1.5)
        _log("❌ 等待超时（%d 分钟）未检测到登录，Cookie 没抓到" % (timeout // 60),
             on_status)
        return None
    finally:
        try:
            driver.quit()  # 抓完自动关掉，不留后台进程
        except Exception:
            pass


def _edge_version():
    """读系统 Edge 版本号，排障时打印出来对照 msedgedriver 是否匹配。"""
    import subprocess as _sp
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        try:
            out = _sp.check_output([path, "--version"], stderr=_sp.STDOUT,
                                   timeout=8).decode(errors="replace").strip()
            return out
        except Exception:
            continue
    return "未知"


def save_cookie(cookie_str):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie_str.strip())


def verify_cookie(cookie_str, on_status=None):
    """抓完立刻验证一遍：调 user_info 确认 Cookie 真的能用。返回 (ok, user_name)"""
    import requests
    import re
    try:
        resp = requests.get(
            "https://changjiang.yuketang.cn/v/course_meta/user_info",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                "Cookie": cookie_str,
            }, timeout=30)
        js = resp.json()
        name = (js.get("data") or {}).get("name") or ""
        uid = re.search(r'"user_id":\s*(\d+)', resp.text)
        if uid:
            _log("✅ Cookie 验证通过：%s（ID %s）" % (name or "未知姓名", uid.group(1)),
                 on_status)
            return True, name
        _log("⚠️ 验证异常：user_info 没返回 user_id，Cookie 可能不完整", on_status)
        return False, name
    except Exception as e:
        _log("⚠️ 验证请求失败：%s" % e, on_status)
        return False, ""


def main():
    _log("=" * 46)
    _log("长江雨课堂 · 浏览器半自动登录抓 Cookie")
    _log("=" * 46)
    cookie = grab_cookie_via_browser()
    if not cookie:
        sys.exit(1)
    save_cookie(cookie)
    _log("💾 Cookie 已保存到 cookie.txt（GUI / 刷课脚本会自动使用）")
    ok, _ = verify_cookie(cookie)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
