# -*- coding: utf-8 -*-
"""长江雨课堂自动刷课工具（YuketangAuto）—— 核心协议脚本

协议路线（无浏览器 / 无 Selenium，纯 requests）：
  登录态    ：浏览器复制 Cookie（csrftoken + sessionid 为关键字段）
  用户信息  ：GET /v/course_meta/user_info
  课程列表  ：GET /v2/api/web/courses/list?identity=2
  课件列表  ：GET /v2/api/web/logs/learn/{classroom_id}?actype=-1&page=N&offset=20&sort=-1
  课件视频  ：GET /v2/api/web/cards/detlist/{cards_id}?classroom_id=...
  视频进度  ：GET /video-log/get_video_watch_progress/...
  视频心跳  ：POST /video-log/heartbeat/   ← 刷课核心：批量提交 heart_data，cp 可加速推进
  作业（mooc-api 通道，答案随题目直接下发，无需 LLM）：
    课程列表  GET /mooc-api/v1/lms/user/user-courses/
    章节树    GET /mooc-api/v1/lms/learn/course/chapter?cid=...&sign=...
    叶子信息  GET /mooc-api/v1/lms/learn/leaf_info/{cid}/{leaf_id}/
    题目列表  GET /mooc-api/v1/lms/exercise/get_exercise_list/{leaf_type_id}/
    提交答案  POST /mooc-api/v1/lms/exercise/problem_apply/

用法：
  python yuketang.py setcookie         # 交互式粘贴浏览器 Cookie，存入 cookie.txt
  python yuketang.py test              # 测试 Cookie 是否有效（顺带打印用户信息）
  python yuketang.py ls                # 列出全部课程
  python yuketang.py 1                 # 刷序号 1 的课程（视频 + 作业）
  python yuketang.py 1 视频            # 仅刷视频
  python yuketang.py 1 作业            # 仅刷作业
  python yuketang.py all               # 全部课程依次刷
  可选参数：--speed sim|5|10|20   倍速（sim=真实时钟仿真，最稳）
            --cookie "..."       直接传 Cookie（一般不用，走 cookie.txt 即可）

协议来源：zk chen 2022 抓包实战帖 / 52pojie 2023 逆向帖 / CSDN 2024-2025 长江雨课堂帖，
均经过长江雨课堂真实账号验证。接口若改版，先看 docs/HANDOFF.md 排障章节再动手。
"""
import json
import math
import os
import random
import re
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "cookie.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

BASE = "https://changjiang.yuketang.cn"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

LEAF_TYPE = {  # mooc-api 章节树里的叶子类型
    "video": 0,
    "exercise": 1,
    "recommend": 3,
    "discussion": 4,
    "exam": 5,
    "homework": 6,
}

SPEED_CHOICES = ("sim", "5", "10", "20")


class CookieExpiredError(Exception):
    """Cookie 失效（被重定向到登录页 / 401 / 响应非 JSON）"""


class RateLimitedError(Exception):
    """服务端显式限流：Expected available in X second"""


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def log(msg):
    try:
        print(msg, flush=True)
    except (UnicodeEncodeError, ValueError, OSError):
        # 打包成 windowed exe 时 stdout 可能是 GBK/devnull，emoji 无法编码
        pass


def rand36():
    """复刻雨课堂前端 pg 字段的随机段：
    Math.floor(1048576 * (1 + Math.random())).toString(36)"""
    v = int(1048576 * (1 + random.random()))
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while v:
        v, r = divmod(v, 36)
        s = digits[r] + s
    return s or "0"


def load_config():
    cfg = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    return cfg


def save_cookie(cookie_str):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie_str.strip())


def load_cookie():
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def parse_cookie_field(cookie_str, name):
    m = re.search(name + r"\s*=\s*([^;]+)", cookie_str)
    return m.group(1).strip() if m else ""


# ----------------------------------------------------------------------------
# 协议主体
# ----------------------------------------------------------------------------

class Yuketang:
    def __init__(self, cookie_str, speed="10", mode="all"):
        self.speed = speed          # sim / 5 / 10 / 20
        self.mode = mode            # all / video / homework
        self.user_id = None
        self.user_name = ""
        self.university_id = parse_cookie_field(cookie_str, "university_id")
        self.stats = {"video_done": 0, "video_skip": 0, "video_fail": 0,
                      "hw_done": 0, "hw_skip": 0, "hw_fail": 0}
        self.failed_items = []      # (类型, 名称, 原因) 供收尾汇总

        self.sess = requests.Session()
        csrftoken = parse_cookie_field(cookie_str, "csrftoken")
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": cookie_str.strip(),
            "Referer": BASE + "/v2/web/index",
            "x-csrftoken": csrftoken,
            "xtbz": "ykt",
            "dnt": "1",
        })
        if self.university_id:
            self.sess.headers["university-id"] = self.university_id

    # ---------- 底层请求：登录失效 / 限流 / 网络抖动统一处理 ----------

    def _request(self, method, url, *, tries=3, **kwargs):
        for attempt in range(1, tries + 1):
            try:
                resp = self.sess.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as e:
                if attempt == tries:
                    raise
                wait = 2 ** attempt
                log("⚠️ 网络异常（%s），%d 秒后重试 %d/%d" % (e, wait, attempt, tries))
                time.sleep(wait)
                continue

            body = resp.text or ""
            # Cookie 失效：雨课堂会 302 到登录页，或直接吐 HTML
            if resp.status_code in (401, 403) or (
                    "<html" in body[:200].lower() and "json" not in body[:50]):
                if "Expected available" in body:
                    pass  # 403 也可能是限流页，落到下面处理
                else:
                    raise CookieExpiredError(
                        "接口返回 %s，Cookie 大概率已失效，请重新配置（setcookie 或 GUI）"
                        % resp.status_code)
            if "Expected available in" in body:
                m = re.search(r"Expected available in\s*([\d.]+)\s*second", body)
                delay = float(m.group(1)) + 1.0 if m else 5.0
                log("⚠️ 服务端限流，等待 %.1f 秒后自动重试..." % delay)
                time.sleep(delay)
                continue  # 重发（不消耗重试次数的话可能死循环，消耗即可）
            return resp
        raise RateLimitedError("限流重试次数用尽：%s" % url)

    def _get_json(self, url, **kwargs):
        resp = self._request("GET", url, **kwargs)
        try:
            return resp.json()
        except ValueError:
            raise CookieExpiredError(
                "接口返回非 JSON（%s...），Cookie 失效或接口改版" % resp.text[:80])

    def _post_json(self, url, payload, **kwargs):
        resp = self._request("POST", url, data=json.dumps(payload), **kwargs)
        try:
            return resp.json()
        except ValueError:
            # 提交类接口限流时返回的是文本，这里给出原始内容便于排障
            raise RateLimitedError("POST 返回非 JSON：%s" % resp.text[:120])

    # ---------- 用户 / 课程 ----------

    def whoami(self):
        """获取 user_id，同时验证 Cookie 有效性"""
        js = self._get_json(BASE + "/v/course_meta/user_info")
        data = js.get("data") or js
        uid = data.get("user_id") if isinstance(data, dict) else None
        if uid is None:
            m = re.search(r'"user_id":\s*(\d+)', json.dumps(js))
            uid = int(m.group(1)) if m else None
        if uid is None:
            raise CookieExpiredError("user_info 里找不到 user_id，响应结构异常")
        self.user_id = int(uid)
        self.user_name = (data.get("name") if isinstance(data, dict) else "") or ""
        # 顺手补 university_id（mooc-api 通道要用）
        if not self.university_id:
            m = re.search(r'"university_id":\s*"?(\d+)"?', json.dumps(js))
            if m:
                self.university_id = m.group(1)
        return self.user_id

    def courses(self):
        """v2 web 通道课程列表"""
        js = self._get_json(BASE + "/v2/api/web/courses/list?identity=2")
        out = []
        for ins in (js.get("data", {}).get("list") or []):
            course = ins.get("course") or {}
            out.append({
                "course_name": course.get("name") or ins.get("course_name") or "未命名课程",
                "classroom_id": ins.get("classroom_id"),
                "course_id": course.get("id") or ins.get("course_id"),
                "course_sign": ins.get("university_course_series_id") or "",
            })
        return out

    # ---------- 视频通道（v2 web + video-log） ----------

    def coursewares(self, classroom_id):
        """学习日志里的课件列表（type==2），自动翻页"""
        acts, page = [], 0
        while True:
            url = (BASE + "/v2/api/web/logs/learn/%s?actype=-1&page=%d&offset=20&sort=-1"
                   % (classroom_id, page))
            js = self._get_json(url)
            items = (js.get("data", {}).get("activities")) or []
            if not items:
                break
            acts.extend(items)
            if len(items) < 20:
                break
            page += 1
        out = []
        for a in acts:
            if a.get("type") == 2:  # 2 = 课件
                out.append({
                    "title": a.get("title") or "未命名课件",
                    "cards_id": a.get("courseware_id"),
                    "id": a.get("id"),
                })
        return out

    def coursewares_from_chapter(self, classroom_id, course_sign):
        """从 mooc-api 章节树拿视频课件（leaf_type==0）。

        当 v2 web 的 logs/learn 学习日志为空时（用户从没点开过课件），
        视频叶子只存在于章节树里，这个方法兜底。
        media 类视频的 video_id = leaf.id；心跳时再调 leaf_info 拿 ccid/sku_id。
        返回 [{title, id, kind:"media"}, ...]
        """
        if not course_sign:
            mc = self._mooc_courses()
            course_sign = (mc.get(classroom_id) or {}).get(
                "university_course_series_id") or ""
        if not course_sign:
            return []
        url = (BASE + "/mooc-api/v1/lms/learn/course/chapter?cid=%s&%s&sign=%s"
               % (classroom_id, self._mooc_params(), course_sign))
        js = self._get_json(url)
        out = []
        for leaf in self._iter_leaves(js):
            if leaf.get("leaf_type") != LEAF_TYPE["video"]:
                continue
            out.append({
                "title": leaf.get("name") or "未命名视频",
                "id": leaf.get("id"),
                "kind": "media",
            })
        return out

    def _leaf_info_full(self, classroom_id, leaf_id):
        """调 leaf_info 拿完整 leaf_data（含 ccid/sku_id/course_id 等）。
        注意：leaf_info 必须带 query classroom_id（否则报 CLASSROOM ID IS REQUIRED）。"""
        url = (BASE + "/mooc-api/v1/lms/learn/leaf_info/%s/%s/?%s&classroom_id=%s"
               % (classroom_id, leaf_id, self._mooc_params(), classroom_id))
        js = self._get_json(url)
        return js.get("data") or {}

    def _get_video_duration(self, ccid):
        """用 playurl 接口拿视频 URL，再用 cv2 拿视频时长（秒）。
        用于没看过的视频（进度查询返回空 data，拿不到 video_length）。
        参考 RainFlush 项目的 getVideoUrl + getDuration。"""
        try:
            import cv2, math
            r = self._get_json(BASE + "/api/open/audiovideo/playurl", params={
                "video_id": ccid, "provider": "cc", "file_type": "1",
                "is_single": "0", "domain": "changjiang.yuketang.cn"})
            sources = ((r.get("data") or {}).get("playurl") or {}).get("sources") or {}
            video_url = None
            for k in sources:
                if "quality" in k and sources[k]:
                    video_url = sources[k][0]
                    break
            if not video_url:
                return 0
            cap = cv2.VideoCapture(video_url)
            for _ in range(3):
                if cap.isOpened():
                    fps = cap.get(5)
                    n = cap.get(7)
                    cap.release()
                    return int(math.ceil(n / fps)) if fps > 0 else 0
                time.sleep(2)
            cap.release()
        except Exception as e:
            log("⚠️ 获取视频时长失败：%s" % e)
        return 0

    def watch_progress_media(self, cid, classroom_id, video_id):
        """media 类视频进度查询（新版格式 video_type=video&vtype=rate）。
        返回 (video_length, completed, rate, last_point) 或 None。"""
        url = (BASE + "/video-log/get_video_watch_progress/?user_id=%s&cid=%s"
               "&video_type=video&vtype=rate&video_id=%s&snapshot=1&term=latest"
               "&classroom_id=%s"
               % (self.user_id, cid, video_id, classroom_id))
        js = self._get_json(url)
        # 返回结构：{"data": {"{video_id}": {video_length, completed, rate, last_point, ...}}}
        info = (js.get("data") or {}).get(str(video_id))
        if not isinstance(info, dict):
            return None
        return (
            float(info.get("video_length") or 0),
            bool(info.get("completed")) or str(info.get("completed")) == "1",
            float(info.get("rate") or 0),
            float(info.get("last_point") or 0),
        )

    def watch_video_media(self, course, w):
        """刷 media 类视频（章节树视频叶子，t=video）。
        字段格式参考 RainFlush 项目（changjiang.yuketang.cn 同域名）。
        返回 True 完成 / False 失败。"""
        cid = course["course_id"]
        crid = course["classroom_id"]
        leaf_id = w["id"]

        # 先调 leaf_info 拿 ccid / sku_id（心跳必需，且 cv2 拿时长也要 ccid）
        leaf_data = self._leaf_info_full(crid, leaf_id)
        media = (leaf_data.get("content_info") or {}).get("media") or {}
        ccid = media.get("ccid") or ""
        sku_id = leaf_data.get("sku_id") or ""
        if not ccid:
            log("⚠️ 拿不到 ccid（video_id=%s），跳过" % leaf_id)
            return False

        # 进度查询（看过的视频能拿到 video_length + rate）
        prog = self.watch_progress_media(cid, crid, leaf_id)
        if prog and prog[0] > 0:
            video_length, _done, _rate, last_point = prog
        else:
            # 没看过：进度查询返回空 data，用 playurl + cv2 拿 video_length
            log("🔍 视频未看过，正在获取视频时长（约 3-8 秒）...")
            video_length = self._get_video_duration(ccid)
            if video_length <= 0:
                log("⚠️ 拿不到视频时长（video_id=%s），跳过" % leaf_id)
                return False
            log("📏 视频时长：%d 秒（%d 分 %d 秒）" % (video_length, video_length // 60, video_length % 60))
            _done, _rate, last_point = False, 0.0, 0.0

        # media 类视频心跳策略：step 固定 15（和 ts 的 15 秒一致），保证 cp 和
        # watch_length 同步推进。不能用 cp 加速（cp 打满后服务端停止累加 watch_length，
        # rate 卡住，completed 不翻）。加速靠连续发多批：一个 N 秒视频约需 N/750 批，
        # 每批瞬间发完，总计几秒。
        step = 15
        cp = 0  # 从 0 开始推进；到 video_length 后重置 0，让 watch_length 持续累加
        ts_ms = int(time.time() * 1000)
        batch = 50
        stall_count = 0
        last_rate = _rate

        def make_heart(et, cp_val, sq):
            return {
                "c": cid, "cards_id": "", "cc": ccid,
                "classroomid": crid, "cp": round(cp_val, 1),
                "d": round(video_length, 1), "et": et,
                "fp": 0, "i": 5, "lob": "ykt",
                "n": "ali-cdn.xuetangx.com", "p": "web",
                "pg": "%s_n0ga" % leaf_id, "skuid": str(sku_id),
                "slide": 0, "sp": 1, "sq": sq,
                "t": "video", "tp": 0, "ts": str(ts_ms),
                "u": self.user_id, "uip": "",
                "v": leaf_id, "v_url": "",
            }

        while True:
            # 完成判定（先查再刷，断点续跑安全）
            prog = self.watch_progress_media(cid, crid, leaf_id)
            if prog is not None:
                _len, completed, rate, last_point = prog
                if completed or rate >= 0.999:
                    # 发 videoend 收尾心跳，触发 completed 翻转
                    try:
                        self._post_json(BASE + "/video-log/heartbeat/",
                                        {"heart_data": [make_heart("videoend", 0, 50)]})
                    except Exception:
                        pass
                    return True
                # 不用 last_point 锚定 cp：media 类 cp 从 0 循环推进累加 watch_length
            # else: 进度查询为空（视频没看过/心跳还没创建记录），继续发心跳

            # 构造一批心跳：cp 从当前位置推进，到 video_length 后重置 0（让 watch_length 持续累加）
            hearts = [make_heart("loadeddata", cp, 45)]
            for _ in range(batch):
                cp += step
                if cp >= video_length:
                    cp = 0  # 推到尾后重置，继续累加 watch_length（last_point 不回退）
                ts_ms += 15000
                hearts.append(make_heart("heartbeat", cp, 45))

            try:
                self._post_json(BASE + "/video-log/heartbeat/", {"heart_data": hearts})
            except RateLimitedError as e:
                log("⚠️ 心跳被限流：%s，等待后重试" % e)
                time.sleep(5)
                continue

            # 进度检查 + 降速自愈
            prog = self.watch_progress_media(cid, crid, leaf_id)
            if prog and prog[2] > last_rate:
                last_rate = prog[2]
                stall_count = 0
                log("⏳ %s 进度 %.1f%%（%d/%d 秒）"
                    % (w["title"], prog[2] * 100, int(prog[3]), int(video_length)))
            else:
                stall_count += 1
                if stall_count >= 3 and step > 15:
                    step = max(step // 2, 15)
                    log("⚠️ 进度不涨，降速到 step=%d" % step)
                if stall_count >= 6:
                    log("⚠️ 连续 6 批进度不涨，放弃该视频（重跑可续）")
                    return False
            # cp 打满后不提前返回：继续发心跳让 watch_length/rate 涨到达标
            # 完成判定由循环开头的 completed or rate>=0.999 处理

    @staticmethod
    def _videos_in_cards_js(js):
        """从 detlist 响应解析视频清单：[(v_url, slide页码), ...]"""
        out, num = [], 1
        for slide in (js.get("data", {}).get("Slides") or []):
            for shape in (slide.get("Shapes") or []):
                if "playurl" in shape and shape.get("file_type") == 1:
                    url = shape.get("URL") or ""
                    if url:
                        out.append((url, str(num)))
            num += 1
        return out

    def videos_in_cards(self, cards_id, classroom_id):
        url = "%s/v2/api/web/cards/detlist/%s?classroom_id=%s" % (BASE, cards_id, classroom_id)
        return self._videos_in_cards_js(self._get_json(url))

    def watch_progress(self, cid, classroom_id, cards_id, v_url, slide):
        """查询单个视频进度。返回 (video_length, completed, rate, last_point)"""
        url = (BASE + "/video-log/get_video_watch_progress/?user_id=%s&cid=%s"
               "&video_type=ykt_cards&classroom_id=%s&cards_id=%s&v_url=%s&slide=%s"
               % (self.user_id, cid, classroom_id, cards_id, v_url, slide))
        js = self._get_json(url)
        m = re.search(r"\d+", v_url)
        if not m:
            return None
        key = "%s:%s" % (slide, m.group())
        info = js.get(key)
        if info is None:  # 新版响应可能直接把 data 平铺
            info = (js.get("data") or {}).get(key)
        if not isinstance(info, dict):
            return None
        return (
            float(info.get("video_length") or 0),
            bool(info.get("completed")) or str(info.get("completed")) == "1",
            float(info.get("rate") or 0),
            float(info.get("last_point") or 0),
        )

    def _heart_batch(self, cid, classroom_id, cards_id, v_url, slide,
                     start_cp, step, ts_ms, video_length, batch=50):
        """构造并发送一批心跳。返回 (下一批起始 cp, 下一批起始 ts)"""
        hearts = []
        cp, ts = start_cp, ts_ms
        for _ in range(batch):
            cp = min(cp + step, video_length if video_length else cp + step)
            ts += 15000  # 每条心跳代表 15 秒虚拟时间（雨课堂原生节奏）
            hearts.append({
                "i": 5, "et": "loadeddata", "p": "web",
                "n": "ali-cdn.xuetangx.com", "lob": "ykt",
                "cp": round(cp, 1), "fp": 0, "tp": 0, "sp": 1,
                "ts": str(ts), "u": self.user_id, "uip": "",
                "c": cid, "v": cards_id, "skuid": "",
                "classroomid": classroom_id, "cc": "",
                "d": round(video_length, 1),
                "pg": "%s_%s" % (cards_id, rand36()),
                "sq": 2, "t": "ykt_cards",
                "cards_id": cards_id, "slide": slide, "v_url": v_url,
            })
        self._post_json(BASE + "/video-log/heartbeat/", {"heart_data": hearts})
        return cp, ts

    def watch_video(self, course_name, cid, classroom_id, cards_id, v_url, slide):
        """刷单个视频。返回 True 完成 / False 失败（已完成的在上游就跳过了）"""
        prog = self.watch_progress(cid, classroom_id, cards_id, v_url, slide)
        if prog is None:
            log("⚠️ 进度接口解析失败（slide=%s），跳过该视频" % slide)
            return False
        video_length, _done, _rate, last_point = prog
        if video_length <= 0:
            log("⚠️ 拿不到视频时长（slide=%s），跳过" % slide)
            return False

        speed = self.speed
        if speed == "sim":
            step = 15  # 真实时钟：每 15 秒推进 15 秒内容
        else:
            step = 15 * int(speed)

        cp = last_point if last_point > 0 else 0
        ts_ms = int(time.time() * 1000)
        batch = 50 if speed != "sim" else 4
        real_interval = 60 if speed == "sim" else 0.8  # sim: 每 60s 发一小批
        stall_count = 0
        last_rate = _rate

        while True:
            # 完成判定（先查再刷，断点续跑安全）
            prog = self.watch_progress(cid, classroom_id, cards_id, v_url, slide)
            if prog is None:
                return False
            _len, completed, rate, last_point = prog
            if completed or rate >= 0.999:
                return True
            if last_point > cp:  # 服务端比我们记得多，锚到服务端
                cp = last_point
                ts_ms = int(time.time() * 1000)

            # 进度不涨检测：连续 3 批无进展 → 降速；再 3 批 → 放弃
            if rate <= last_rate + 1e-6 and cp > 0:
                stall_count += 1
                if stall_count == 3 and step > 15:
                    step = max(15, step // 2)
                    log("⚠️ 进度不涨，自动降速：step→%d" % step)
                if stall_count >= 6:
                    log("❌ 进度持续不涨（rate=%.2f），放弃该视频，稍后可重跑续刷" % rate)
                    return False
            else:
                stall_count = 0
            last_rate = rate

            if cp >= video_length:  # 已打满但服务端没翻完成位：再补一小批收尾
                cp = max(0, video_length - step)
                ts_ms = int(time.time() * 1000)

            try:
                cp, ts_ms = self._heart_batch(
                    cid, classroom_id, cards_id, v_url, slide,
                    cp, step, ts_ms, video_length, batch)
            except RateLimitedError as e:
                log("⚠️ 心跳被限流：%s" % e)
                return False

            if speed == "sim":
                time.sleep(real_interval)
            else:
                time.sleep(real_interval)
                # 虚拟时间推进快于真实时钟，校准 ts 防止越飘越远
                drift = ts_ms - int(time.time() * 1000)
                if drift > 300000:  # 虚拟时间领先真实 5 分钟以上：拉回
                    ts_ms = int(time.time() * 1000) + 300000

    # ---------- 作业通道（mooc-api，答案直出） ----------

    def _mooc_params(self):
        uv = load_config().get("homework", {}).get("uv_id") or self.university_id or ""
        return "term=latest" + (("&uv_id=" + uv) if uv else "")

    def _mooc_courses(self):
        """mooc-api 通道课程（拿 course_sign 用）。失败时返回空，由 v2 列表兜底"""
        try:
            js = self._get_json(
                BASE + "/mooc-api/v1/lms/user/user-courses/?status=1&page=1&no_page=1&"
                + self._mooc_params())
            out = {}
            for ins in (js.get("data", {}).get("product_list") or []):
                out[ins.get("classroom_id")] = ins
            return out
        except Exception as e:
            log("⚠️ mooc-api 课程列表不可用（%s），作业通道将只依赖 v2 sign" % e)
            return {}

    @staticmethod
    def _iter_leaves(chapter_js):
        """遍历章节树所有叶子：data.course_chapter[].section_leaf_list[].leaf_list[]"""
        for ch in (chapter_js.get("data", {}).get("course_chapter") or []):
            for sec in (ch.get("section_leaf_list") or []):
                leaves = sec.get("leaf_list")
                if leaves:
                    for leaf in leaves:
                        yield leaf
                else:
                    yield sec

    def homework_leaf_ids(self, classroom_id, course_sign):
        url = (BASE + "/mooc-api/v1/lms/learn/course/chapter?cid=%s&%s&sign=%s"
               % (classroom_id, self._mooc_params(), course_sign))
        js = self._get_json(url)
        out = []
        for leaf in self._iter_leaves(js):
            if leaf.get("leaf_type") == LEAF_TYPE["homework"]:
                out.append({"id": leaf.get("id"), "name": leaf.get("name") or "未命名作业"})
        return out

    def do_homework(self, classroom_id, leaf):
        """刷一份作业：题目+答案都在 get_exercise_list 里，逐题回传即可"""
        try:
            info = self._get_json(
                "%s/mooc-api/v1/lms/learn/leaf_info/%s/%s/?%s"
                % (BASE, classroom_id, leaf["id"], self._mooc_params()))
            leaf_type_id = info["data"]["content_info"]["leaf_type_id"]
        except (KeyError, TypeError, CookieExpiredError):
            self.stats["hw_skip"] += 1
            log("⚠️ 作业「%s」拿不到 leaf_info，跳过" % leaf["name"])
            return
        except RateLimitedError:
            self.stats["hw_fail"] += 1
            self.failed_items.append(("作业", leaf["name"], "限流"))
            return

        try:
            ex = self._get_json(
                "%s/mooc-api/v1/lms/exercise/get_exercise_list/%s/?%s"
                % (BASE, leaf_type_id, self._mooc_params()))
            problems = ex["data"]["problems"]
        except (KeyError, TypeError):
            self.stats["hw_skip"] += 1
            log("⚠️ 作业「%s」题目列表异常（可能不是习题类叶子），跳过" % leaf["name"])
            return

        submitted = skipped = 0
        for pro in problems:
            user = pro.get("user") or {}
            content = pro.get("content") or {}
            if user.get("is_show_answer"):
                skipped += 1
                continue
            ptype = str(content.get("Type") or "")
            try:
                if "FillBlank" not in ptype:          # 单选 / 多选 / 判断
                    answer = user.get("answer")
                    key = "answer"
                    if answer in (None, ""):
                        skipped += 1
                        continue
                    if isinstance(answer, list):
                        answer = answer[0] if len(answer) == 1 else ",".join(map(str, answer))
                else:                                  # 填空题
                    answer = user.get("answers")
                    if not isinstance(answer, dict) or not answer:
                        skipped += 1
                        continue
                    answer = {k: (v[0] if isinstance(v, list) and v else v)
                              for k, v in answer.items()}
                    key = "answers"
            except Exception:
                skipped += 1
                continue

            payload = {
                "classroom_id": int(classroom_id),
                "problem_id": content.get("ProblemID"),
                key: answer,
            }
            try:
                self._post_json(
                    "%s/mooc-api/v1/lms/exercise/problem_apply/?%s"
                    % (BASE, self._mooc_params()), payload)
                submitted += 1
                time.sleep(0.5)
            except RateLimitedError as e:
                log("⚠️ 提交被限流：%s" % e)
                break

        self.stats["hw_done"] += 1
        log("✅ 作业「%s」完成：提交 %d 题，跳过 %d 题" % (leaf["name"], submitted, skipped))

    # ---------- 课程级编排 ----------

    def run_course(self, course):
        name = course["course_name"]
        cid = course["course_id"]
        crid = course["classroom_id"]
        sign = course["course_sign"]
        log("")
        log("=" * 52)
        log("当前课程：%s" % name)
        log("=" * 52)

        # ---- 视频部分 ----
        if self.mode in ("all", "video"):
            wares = self.coursewares(crid)
            if not wares:
                # v2 web 学习日志为空（用户从没点开过课件），fallback 到章节树
                # 章节树里的视频叶子（leaf_type==0）才是课件的真实来源
                log("📚 学习日志为空，从章节树扫描视频叶子...")
                try:
                    wares = self.coursewares_from_chapter(crid, sign)
                except CookieExpiredError:
                    raise
                except Exception as e:
                    log("⚠️ 章节树扫描失败：%s" % e)
                    wares = []
            log("📚 课件 %d 个" % len(wares))
            # media 类视频（章节树 video 叶子，t=video）：每个课件 = 1 个视频
            if wares and wares[0].get("kind") == "media":
                total = len(wares)
                done = 0
                log("📊 任务点: 0/%d" % total)
                for w in wares:
                    tag = w["title"]
                    try:
                        prog = self.watch_progress_media(cid, crid, w["id"])
                        if prog and (prog[1] or prog[2] >= 0.999):
                            log("✅ %s 已完成，跳过" % tag)
                            self.stats["video_skip"] += 1
                        elif self.watch_video_media(course, w):
                            log("✅ %s 刷课完成" % tag)
                            self.stats["video_done"] += 1
                        else:
                            self.stats["video_fail"] += 1
                            self.failed_items.append(("视频", tag, "进度不涨或解析失败"))
                    except CookieExpiredError:
                        raise
                    except Exception as e:
                        self.stats["video_fail"] += 1
                        self.failed_items.append(("视频", tag, str(e)))
                        log("❌ %s 异常：%s" % (tag, e))
                    done += 1
                    log("📊 任务点: %d/%d" % (done, total))
            else:
                # cards 类视频（v2 web 学习日志，原逻辑）
                tasks = []  # 先全量收集，用于任务点计数
                for w in wares:
                    try:
                        vids = self.videos_in_cards(w["cards_id"], crid)
                    except CookieExpiredError:
                        raise
                    except Exception as e:
                        log("⚠️ 课件「%s」视频清单获取失败：%s" % (w["title"], e))
                        vids = []
                    tasks.append((w, vids))

                total = sum(len(v) for _, v in tasks)
                done = 0
                log("📊 任务点: 0/%d" % total)
                for w, vids in tasks:
                    if not vids:
                        continue
                    log("进入课件：%s（%d 个视频）" % (w["title"], len(vids)))
                    for v_url, slide in vids:
                        tag = "视频 slide=%s" % slide
                        try:
                            prog = self.watch_progress(cid, crid, w["cards_id"], v_url, slide)
                            if prog and (prog[1] or prog[2] >= 0.999):
                                log("✅ %s 已完成，跳过" % tag)
                                self.stats["video_skip"] += 1
                            elif self.watch_video(name, cid, crid, w["cards_id"], v_url, slide):
                                log("✅ %s 刷课完成" % tag)
                                self.stats["video_done"] += 1
                            else:
                                self.stats["video_fail"] += 1
                                self.failed_items.append(("视频", "%s/%s" % (w["title"], tag), "进度不涨或解析失败"))
                        except CookieExpiredError:
                            raise
                        except Exception as e:
                            self.stats["video_fail"] += 1
                            self.failed_items.append(("视频", "%s/%s" % (w["title"], tag), str(e)))
                            log("❌ %s 异常：%s" % (tag, e))
                        done += 1
                        log("📊 任务点: %d/%d" % (done, total))

        # ---- 作业部分 ----
        if self.mode in ("all", "homework"):
            if not sign:
                mc = self._mooc_courses()
                sign = (mc.get(crid) or {}).get("course_sign") or ""
            if not sign:
                log("⚠️ 拿不到 course_sign，作业通道无法进入（视频不受影响）")
            else:
                try:
                    leaves = self.homework_leaf_ids(crid, sign)
                    log("📝 作业 %d 份" % len(leaves))
                    for leaf in leaves:
                        self.do_homework(crid, leaf)
                except CookieExpiredError:
                    raise
                except Exception as e:
                    log("❌ 作业通道异常：%s" % e)
                    self.stats["hw_fail"] += 1

    def summary(self):
        s = self.stats
        log("")
        log("=" * 52)
        log("📊 本次运行汇总")
        log("  视频：完成 %d | 已完成跳过 %d | 失败 %d"
            % (s["video_done"], s["video_skip"], s["video_fail"]))
        log("  作业：完成 %d | 跳过 %d | 失败 %d"
            % (s["hw_done"], s["hw_skip"], s["hw_fail"]))
        if self.failed_items:
            log("  失败清单（重跑一般可续）：")
            for kind, name, why in self.failed_items:
                log("    · [%s] %s —— %s" % (kind, name, why))
        log("=" * 52)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _ask_cookie():
    """交互读取 Cookie；无终端（GUI 子进程/管道）时友好退出而非 traceback"""
    try:
        return input("Cookie> ").strip()
    except (EOFError, OSError):
        log("❌ 当前环境无法交互输入（无终端）。请用图形窗口粘贴，或先跑 setcookie。")
        sys.exit(1)


def ensure_cookie(cli_cookie):
    """Cookie 来源：命令行 > cookie.txt > 交互输入"""
    cookie = (cli_cookie or "").strip() or load_cookie()
    if not cookie:
        log("未找到 Cookie。两种获取方式：")
        log("  A. 自动（推荐）：另开命令行跑  python browser_login.py")
        log("     （会弹浏览器，登录后自动抓取写入 cookie.txt）")
        log("  B. 手动：用 Chrome/Edge 登录 https://changjiang.yuketang.cn")
        log("     F12 → Network → 任选一个 changjiang.yuketang.cn 请求 → 标头 →")
        log("     请求标头里 cookie 整行复制，粘贴到下面（回车提交）：")
        cookie = _ask_cookie()
        if not cookie:
            log("❌ 未输入 Cookie，退出")
            sys.exit(1)
        save_cookie(cookie)
        log("已保存到 cookie.txt（下次自动复用，失效后重新 setcookie 覆盖）")
    # 关键字段体检
    if not parse_cookie_field(cookie, "sessionid"):
        log("⚠️ Cookie 里没看到 sessionid，大概率刷不动（正常 Cookie 必含 sessionid/csrftoken）")
    return cookie


def print_courses(courses):
    if not courses:
        log("（课程列表为空：要么真没课，要么 Cookie 失效/接口改版）")
        return
    log("")
    log("共 %d 门课程：" % len(courses))
    for i, c in enumerate(courses, 1):
        log("  %d. %s" % (i, c["course_name"]))
    log("")
    log("💡 刷课填行首数字，0 或 all = 全部课程")


def main():
    args = sys.argv[1:]
    speed = str(load_config().get("video_speed", "10"))
    cli_cookie = ""

    # 解析可选参数
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--speed":
            i += 1
            speed = args[i] if i < len(args) else speed
        elif args[i] == "--cookie":
            i += 1
            cli_cookie = args[i] if i < len(args) else ""
        else:
            positional.append(args[i])
        i += 1
    if speed not in SPEED_CHOICES:
        speed = "10"

    cmd = positional[0] if positional else ""
    if cmd == "setcookie":
        log("请粘贴浏览器 Cookie（整行，含 sessionid/csrftoken）：")
        cookie = _ask_cookie()
        if not cookie:
            log("❌ 未输入，退出")
            sys.exit(1)
        save_cookie(cookie)
        log("✅ 已保存到 cookie.txt，正在验证...")
        cmd = "test"
    if not cmd:
        log(__doc__)
        sys.exit(0)

    ykt = Yuketang(ensure_cookie(cli_cookie), speed=speed)

    # 连接验证（ls/test/刷课都需要）
    try:
        uid = ykt.whoami()
        log("✅ Cookie 有效：用户 %s（ID %s）"
            % (ykt.user_name or "未知姓名", uid))
    except CookieExpiredError as e:
        log("❌ %s" % e)
        sys.exit(2)
    except Exception as e:
        log("❌ 连接失败：%s" % e)
        sys.exit(3)

    if cmd == "test":
        log("✅ 测试通过，可以开刷")
        sys.exit(0)

    # 课程列表
    try:
        courses = ykt.courses()
    except CookieExpiredError as e:
        log("❌ %s" % e)
        sys.exit(2)

    if cmd == "ls":
        print_courses(courses)
        sys.exit(0)

    if cmd == "debug":
        # 诊断某门课的课件扫描：对比 logs/learn 和 chapter 树的结构
        if len(positional) < 2 or not positional[1].isdigit():
            log("用法：python yuketang.py debug <课程序号>")
            log("  打印该课程的 logs/learn 和章节树视频叶子，定位「课件 0 个」原因")
            sys.exit(1)
        idx = int(positional[1])
        if not 1 <= idx <= len(courses):
            log("❌ 序号超出范围（1~%d）" % len(courses))
            sys.exit(1)
        course = courses[idx - 1]
        crid = course["classroom_id"]
        sign = course["course_sign"]
        log("=" * 52)
        log("诊断课程：%s" % course["course_name"])
        log("classroom_id=%s  course_id=%s  course_sign=%s"
            % (crid, course["course_id"], sign or "(空)"))
        log("=" * 52)

        log("\n--- v2 web logs/learn（学习日志，只含已点开过的课件）---")
        try:
            wares = ykt.coursewares(crid)
            log("课件（type==2）：%d 个" % len(wares))
            for w in wares[:5]:
                log("  · %s  (cards_id=%s)" % (w["title"], w["cards_id"]))
        except CookieExpiredError:
            raise
        except Exception as e:
            log("❌ logs/learn 失败：%s" % e)

        log("\n--- mooc-api chapter 章节树（全部视频叶子 leaf_type==0）---")
        if not sign:
            mc = ykt._mooc_courses()
            sign = (mc.get(crid) or {}).get("university_course_series_id") or ""
            log("(从 mooc-api 补 course_sign=%s)" % (sign or "仍为空，无法查 chapter"))
        if sign:
            try:
                wares2 = ykt.coursewares_from_chapter(crid, sign)
                log("视频叶子：%d 个" % len(wares2))
                for w in wares2[:5]:
                    log("  · %s  (cards_id=%s  leaf_id=%s)"
                        % (w["title"], w["cards_id"] or "(空)", w["id"]))
                    if w.get("_raw_content_keys"):
                        log("    leaf_info content_info 字段: %s"
                            % ", ".join(w["_raw_content_keys"]))
            except CookieExpiredError:
                raise
            except Exception as e:
                log("❌ chapter 扫描失败：%s" % e)
        log("\n💡 如果 chapter 有视频叶子但 cards_id 为空，把上面 content_info 字段")
        log("   贴给我，我据此精确提取 cards_id 字段名。")
        sys.exit(0)

    # 目标课程
    if cmd == "all" or cmd == "0":
        targets = courses
    elif cmd.isdigit():
        idx = int(cmd)
        if not 1 <= idx <= len(courses):
            log("❌ 序号超出范围（1~%d）" % len(courses))
            sys.exit(1)
        targets = [courses[idx - 1]]
    else:
        log("❌ 无法识别的命令：%s（看文件头部用法）" % cmd)
        sys.exit(1)

    # 模式：第二个位置参数（视频/作业/全部）
    mode = positional[1] if len(positional) > 1 else "all"
    mode_map = {"视频": "video", "作业": "homework", "全部": "all",
                "all": "all", "video": "video", "homework": "homework"}
    ykt.mode = mode_map.get(mode, "all")

    log("运行参数：课程 %s | 模式 %s | 倍速 %s"
        % (cmd, ykt.mode, "仿真1×" if speed == "sim" else speed + "×"))
    log("⚠️ 刷课期间 Cookie 对应的账号不要在浏览器里退出登录")

    try:
        for course in targets:
            ykt.run_course(course)
    except CookieExpiredError as e:
        log("❌ 中途 Cookie 失效：%s" % e)
        log("   进度不丢：重新 setcookie 后再跑一遍，已完成的会自动跳过")
    except KeyboardInterrupt:
        log("\n⚠️ 手动中断，进度已实时上报，重跑自动续")
    finally:
        ykt.summary()


if __name__ == "__main__":
    main()
