# -*- coding: utf-8 -*-
"""
爱液下载器 v1.0.0
直接连爱液视频的服务器拿列表/搜索/播放地址，用内置 ffmpeg 存成 MP4。
全程鼠标操作：点分类或搜标题 -> 点视频 -> 点下载。
只用 Python 标准库 + 随包的 ffmpeg.exe。
"""
import base64
import concurrent.futures
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

VERSION = "v1.0.3"
APP_NAME = "爱液下载器"

FALLBACK_HOSTS = [
    "https://0826.aiyejikou0001.com",
    "https://0826.aiyejikou0002.com",
    "https://0826.aiyejikou0003.com",
    "https://0826.aiyejikou0004.com",
    "https://1121.aiyejikou0004.com",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ---------------- 基础网络 ----------------

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=15):
    raw = http_get(url, timeout=timeout)
    return json.loads(raw.decode("utf-8", "replace"))


def discover_hosts():
    """学 App 的做法：查 host.aiye.me / hostnew.aiye.me 的 DNS 记录拿当天接口域名。"""
    hosts = []
    # host.aiye.me TXT -> 单个地址
    try:
        d = http_json("https://dns.alidns.com/resolve?name=host.aiye.me&short=true&type=16", timeout=10)
        if isinstance(d, list):
            for item in d:
                s = str(item).strip().strip('"').strip()
                if s.startswith("http"):
                    hosts.append(s)
    except Exception:
        pass
    # hostnew.aiye.me TXT -> base64 的 JSON 地址列表
    try:
        d = http_json("https://dns.alidns.com/resolve?name=hostnew.aiye.me&type=16", timeout=10)
        for ans in (d.get("Answer") or []):
            data = str(ans.get("data", "")).strip().strip('"')
            try:
                obj = json.loads(base64.b64decode(data).decode("utf-8", "replace"))
                for u in obj.get("url", []):
                    if str(u).startswith("http"):
                        hosts.append(str(u))
            except Exception:
                pass
    except Exception:
        pass
    for h in FALLBACK_HOSTS:
        if h not in hosts:
            hosts.append(h)
    return hosts


def pick_working_host(log=None):
    for h in discover_hosts():
        try:
            d = http_json(h + "/api/vod/list?page=1&limit=1", timeout=8)
            if d.get("code") == 200:
                if log:
                    log("已连上服务器")
                return h
        except Exception:
            continue
    return None


# ---------------- 接口 ----------------

class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        d = http_json(url, timeout=20)
        if d.get("code") != 200:
            raise RuntimeError(str(d.get("message") or "服务器返回异常"))
        return d.get("data") or {}

    def latest(self, page=1, limit=20):
        data = self._get("/api/vod/list", {"page": page, "limit": limit})
        return data.get("list") or []

    def by_type(self, type_id, page=1, limit=20):
        data = self._get("/api/vod/list", {"types": type_id, "page": page, "limit": limit})
        return data.get("list") or []

    def search(self, wd, page=1, limit=20):
        data = self._get("/api/vod/clever", {"wd": wd, "page": page, "limit": limit})
        return data.get("list") or []

    def types(self):
        data = self._get("/api/vod/type")
        return data.get("list") or []

    def info(self, vod_id):
        data = self._get("/api/vod/info", {"id": vod_id})
        return data

    def lines(self, vod_id):
        try:
            data = self._get("/api/vod/line/list", {"vodid": vod_id})
            return data.get("list") or []
        except Exception:
            return []


# ---------------- 下载 ----------------

def find_ffmpeg():
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    cands = [
        os.path.join(here, "ffmpeg.exe"),
        os.path.join(here, "ffmpeg"),
        os.path.join(getattr(sys, "_MEIPASS", here), "ffmpeg.exe"),
        os.path.join(getattr(sys, "_MEIPASS", here), "ffmpeg"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def sanitize_filename(name, max_len=70):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", str(name or "")).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "无标题"
    return name[:max_len].rstrip()


def fmt_duration(sec):
    try:
        sec = int(sec)
    except Exception:
        return ""
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def _fetch_bytes(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise last


def _fetch_full(url, timeout=45, max_rounds=12):
    """带断点续传的下载：这个分片服务器经常下到一半断线，
    断了就用 Range 从断点接着下，已下的字节不浪费。"""
    buf = bytearray()
    total = None
    for _round in range(max_rounds):
        headers = {"User-Agent": UA}
        if buf:
            headers["Range"] = "bytes=%d-" % len(buf)
        req = urllib.request.Request(url, headers=headers)
        failed = False
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if buf and r.status == 200:
                    buf = bytearray()
                cl = r.headers.get("Content-Length")
                if cl is not None:
                    total = (len(buf) + int(cl)) if r.status == 206 else int(cl)
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    buf += chunk
        except Exception:
            failed = True
        if total is not None and len(buf) >= total:
            return bytes(buf[:total])
        if not failed and total is None and buf:
            return bytes(buf)
        time.sleep(0.5)
    if total is None and buf:
        return bytes(buf)
    raise _Incomplete(buf, total)


class _Incomplete(Exception):
    """分片实在下不全时带回已下到的部分字节。"""

    def __init__(self, partial, total):
        super().__init__("分片下载不完整（%d/%s 字节）" % (len(partial), total))
        self.partial = bytes(partial)
        self.total = total


def _parse_attrs(s):
    attrs = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', s):
        v = m.group(2).strip()
        if v.startswith('"'):
            v = v[1:-1]
        attrs[m.group(1)] = v
    return attrs


def _parse_m3u8(text, base_url):
    segs = []
    key_uri = None
    key_iv = None
    media_seq = 0
    variant = None
    pending_variant = False
    for raw in text.splitlines():
        l = raw.strip()
        if l.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_seq = int(l.split(":", 1)[1])
            except Exception:
                pass
        elif l.startswith("#EXT-X-KEY:"):
            attrs = _parse_attrs(l.split(":", 1)[1])
            if attrs.get("METHOD", "NONE") != "NONE":
                if attrs.get("URI"):
                    key_uri = urllib.parse.urljoin(base_url, attrs["URI"])
                iv = attrs.get("IV", "")
                if iv.lower().startswith("0x"):
                    try:
                        key_iv = bytes.fromhex(iv[2:].rjust(32, "0")[-32:])
                    except Exception:
                        key_iv = None
        elif l.startswith("#EXT-X-STREAM-INF"):
            pending_variant = True
        elif l and not l.startswith("#"):
            u = urllib.parse.urljoin(base_url, l)
            if pending_variant:
                variant = u
                pending_variant = False
            else:
                segs.append(u)
    return segs, key_uri, key_iv, media_seq, variant


def _download_hls_parallel(m3u8_url, out_path, progress_cb=None, stop_flag=None):
    """并行下载 HLS 分片：和列表走同一条网络（带系统代理），10 个分片同时下，
    解密拼接后用 ffmpeg 在本地封装（不走网络）。"""
    from Crypto.Cipher import AES  # pycryptodome，缺失时抛错由外层回退
    text = _fetch_full(m3u8_url, timeout=20).decode("utf-8", "replace")
    segs, key_uri, key_iv, media_seq, variant = _parse_m3u8(text, m3u8_url)
    if variant and not segs:
        text = _fetch_full(variant, timeout=20).decode("utf-8", "replace")
        segs, key_uri, key_iv, media_seq, _v = _parse_m3u8(text, variant)
    if not segs:
        raise RuntimeError("播放列表里没有分片")
    key = None
    if key_uri:
        key = _fetch_full(key_uri, timeout=20)
        if len(key) not in (16, 24, 32):
            raise RuntimeError("密钥长度不对")
    total = len(segs)
    tmpdir = tempfile.mkdtemp(prefix="aiye_dl_")
    counter = {"n": 0, "bytes": 0}
    lock = threading.Lock()

    def one(idx, url):
        if stop_flag and stop_flag.is_set():
            raise RuntimeError("已停止")
        complete = True
        try:
            data = _fetch_full(url, timeout=45)
        except _Incomplete as e:
            # 个别分片在服务器上本身就是残的：用下到的部分顶上，
            # 播放时那几秒可能有点花，但整片能下完，不整单报废。
            if not e.partial:
                raise
            data = e.partial
            complete = False
        if key is not None:
            iv = key_iv if key_iv is not None else (media_seq + idx).to_bytes(16, "big")
            if complete:
                data = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
                if data:
                    pad = data[-1]
                    if 0 < pad <= 16:
                        data = data[:-pad]
            else:
                n = len(data) - (len(data) % 16)
                data = AES.new(key, AES.MODE_CBC, iv).decrypt(data[:n]) if n else b""
        with open(os.path.join(tmpdir, "%06d.ts" % idx), "wb") as f:
            f.write(data)
        with lock:
            counter["n"] += 1
            counter["bytes"] += len(data)
            if progress_cb:
                progress_cb(min(99, int(counter["n"] * 100 / total)),
                            counter["bytes"] / 1048576.0)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(one, i, u) for i, u in enumerate(segs)]
            for fu in concurrent.futures.as_completed(futs):
                fu.result()
        if stop_flag and stop_flag.is_set():
            raise RuntimeError("已停止")
        ts_path = out_path + ".part.ts"
        with open(ts_path, "wb") as out:
            for i in range(total):
                with open(os.path.join(tmpdir, "%06d.ts" % i), "rb") as f:
                    while True:
                        chunk = f.read(1024 * 512)
                        if not chunk:
                            break
                        out.write(chunk)
        ffmpeg = find_ffmpeg()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        err_path = ts_path + ".log"
        with open(err_path, "wb") as ef:
            rc = subprocess.call([ffmpeg, "-y", "-loglevel", "error", "-i", ts_path,
                                  "-c", "copy", out_path],
                                 stdout=subprocess.DEVNULL, stderr=ef,
                                 creationflags=creationflags)
        err = ""
        try:
            with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                err = f.read().strip()
        except Exception:
            pass
        for p in (ts_path, err_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("封装失败：%s" % (err.splitlines()[-1] if err else "未知"))
        if progress_cb:
            progress_cb(100, 0.0)
        return out_path
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def download_mp4(api, item, out_dir, progress_cb=None, stop_flag=None, log=None):
    """先用并行引擎下；不行的线路再回退给 ffmpeg 直拉。成功返回文件路径。"""
    vod_id = item["id"]
    info = api.info(vod_id)
    play_url = info.get("play_url") or ""
    urls = []
    if play_url:
        urls.append(play_url)
    for ln in api.lines(vod_id):
        u = ln.get("domain") or ""
        if u and u not in urls:
            urls.append(u)
    if not urls:
        raise RuntimeError("没拿到播放地址（这个视频可能需要 VIP 解锁）")
    title = sanitize_filename(info.get("title") or item.get("title"))
    out_path = os.path.join(out_dir, title + ".mp4")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        out_path = os.path.join(out_dir, "%s_%s.mp4" % (title, vod_id))
    last_err = None
    for idx, url in enumerate(urls):
        if log:
            log("开始下载%s：%s" % (("（线路%d）" % (idx + 1)) if idx else "", title))
        try:
            return _download_hls_parallel(url, out_path, progress_cb, stop_flag)
        except RuntimeError as e:
            if str(e) == "已停止":
                raise
            last_err = str(e)
        except Exception as e:
            last_err = str(e)
    if log:
        log("并行下载没成功，换普通方式再试…")
    try:
        return _download_mp4_ffmpeg(api, item, out_dir, progress_cb, stop_flag, log)
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("下载失败：%s" % (last_err or "未知错误"))


def _download_mp4_ffmpeg(api, item, out_dir, progress_cb=None, stop_flag=None, log=None):
    """item: 列表里的一条（含 id/title/duration）。成功返回文件路径。"""
    vod_id = item["id"]
    info = api.info(vod_id)
    play_url = info.get("play_url") or ""
    urls = []
    if play_url:
        urls.append(play_url)
    for ln in api.lines(vod_id):
        u = ln.get("domain") or ""
        if u and u not in urls:
            urls.append(u)
    if not urls:
        raise RuntimeError("没拿到播放地址（这个视频可能需要 VIP 解锁）")

    title = sanitize_filename(info.get("title") or item.get("title"))
    out_path = os.path.join(out_dir, title + ".mp4")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        out_path = os.path.join(out_dir, "%s_%s.mp4" % (title, vod_id))
    duration = 0
    try:
        duration = int(item.get("duration") or 0)
    except Exception:
        duration = 0

    ffmpeg = find_ffmpeg()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    last_err = None
    for idx, url in enumerate(urls):
        tmp_path = out_path + ".part.mp4"
        err_path = tmp_path + ".log"
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1", "-i", url, "-c", "copy", tmp_path]
        if log:
            log("开始下载%s：%s" % (("（线路%d）" % (idx + 1)) if idx else "", title))
        try:
            err_f = open(err_path, "wb")
            try:
                env = dict(os.environ)
                try:
                    for _k, _v in urllib.request.getproxies().items():
                        if _v:
                            env[_k + "_proxy"] = _v
                except Exception:
                    pass
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=creationflags, env=env)
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    if stop_flag and stop_flag.is_set():
                        proc.kill()
                        raise RuntimeError("已停止")
                    if progress_cb and line.startswith("out_time"):
                        pct = None
                        try:
                            if line.startswith("out_time_us="):
                                done = int(line.split("=", 1)[1].strip()) / 1000000.0
                                if duration > 0:
                                    pct = min(99, int(done * 100 / duration))
                            elif line.startswith("out_time="):
                                t = line.split("=", 1)[1].strip()
                                hh, mm, ss = t.split(":")
                                done = int(hh) * 3600 + int(mm) * 60 + float(ss)
                                if duration > 0:
                                    pct = min(99, int(done * 100 / duration))
                        except Exception:
                            pct = None
                        mb = 0.0
                        try:
                            if os.path.exists(tmp_path):
                                mb = os.path.getsize(tmp_path) / 1048576.0
                        except Exception:
                            pass
                        progress_cb(pct, mb)
                rc = proc.wait()
            finally:
                err_f.close()
            if rc == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                os.replace(tmp_path, out_path)
                try:
                    os.remove(err_path)
                except Exception:
                    pass
                if progress_cb:
                    progress_cb(100, 0.0)
                return out_path
            err = ""
            try:
                with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                    err = f.read().strip()
            except Exception:
                pass
            last_err = err.splitlines()[-1] if err else "下载失败"
            for p in (tmp_path, err_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
    raise RuntimeError("下载失败：%s" % (last_err or "未知错误"))


# ---------------- 界面 ----------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("%s %s" % (APP_NAME, VERSION))
    root.geometry("980x700")
    root.minsize(860, 620)

    state = {
        "api": None,
        "wd": "",
        "page": 1,
        "items": [],
        "queue": [],           # 下载队列：[{item, status}]
        "worker_running": False,
        "out_dir": "",
        "stop": threading.Event(),
    }
    q = queue.Queue()

    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    default_dir = os.path.join(app_dir, "下载")
    try:
        os.makedirs(default_dir, exist_ok=True)
    except Exception:
        default_dir = app_dir
    dir_var = tk.StringVar(value=default_dir)
    status_var = tk.StringVar(value="正在连接服务器…")
    page_var = tk.StringVar(value="第 1 页")

    # 顶部：搜索行
    top = tk.Frame(root)
    top.pack(fill="x", padx=10, pady=(10, 4))
    tk.Label(top, text="搜标题/番号：").pack(side="left")
    search_var = tk.StringVar()
    ent = tk.Entry(top, textvariable=search_var, width=34)
    ent.pack(side="left", padx=(0, 6))

    def log(msg):
        status_var.set(msg)

    def set_items(items):
        state["items"] = items
        lst.delete(0, "end")
        for it in items:
            dur = fmt_duration(it.get("duration"))
            views = it.get("views")
            tail = ""
            if dur:
                tail += "  时长 " + dur
            if views:
                tail += "  播放 %s" % views
            lst.insert("end", (it.get("title") or "无标题") + tail)
        page_var.set("第 %d 页" % state["page"])
        log("这一页有 %d 个视频，点一个再点「加入下载队列」" % len(items) if items else "这一页没有视频")

    def load_async():
        def work():
            try:
                items = state["api"].search(state["wd"], state["page"])
                q.put(("items", items))
            except Exception as e:
                q.put(("loaderror", "加载失败：%s（再点一次搜索重试）" % e))
        threading.Thread(target=work, daemon=True).start()
        log("正在搜索…")

    def do_search():
        wd = search_var.get().strip()
        if not wd:
            messagebox.showinfo("提示", "先输入要搜的标题或番号")
            return
        state["wd"] = wd
        state["page"] = 1
        load_async()

    tk.Button(top, text="搜索", width=8, command=do_search).pack(side="left")
    ent.bind("<Return>", lambda e: do_search())

    # 中部：搜索结果列表（占满）
    mid = tk.Frame(root)
    mid.pack(fill="both", expand=True, padx=10, pady=4)
    lst = tk.Listbox(mid, exportselection=False)
    lst.pack(fill="both", expand=True)

    # 翻页 + 下载
    bar = tk.Frame(root)
    bar.pack(fill="x", padx=10, pady=4)
    tk.Button(bar, text="上一页", width=8, command=lambda: turn_page(-1)).pack(side="left")
    tk.Label(bar, textvariable=page_var).pack(side="left", padx=6)
    tk.Button(bar, text="下一页", width=8, command=lambda: turn_page(1)).pack(side="left")

    def turn_page(delta):
        if not state["wd"]:
            return
        np = state["page"] + delta
        if np < 1:
            return
        state["page"] = np
        load_async()

    add_btn = tk.Button(bar, text="加入下载队列", width=14, bg="#2e7d32", fg="white")
    add_btn.pack(side="right")
    stop_btn = tk.Button(bar, text="停止当前", width=8, command=lambda: state["stop"].set())
    stop_btn.pack(side="right", padx=6)

    # 保存位置 + 进度 + 状态
    bot = tk.Frame(root)
    bot.pack(fill="x", padx=10, pady=(0, 6))
    tk.Label(bot, text="存到：").pack(side="left")
    tk.Entry(bot, textvariable=dir_var, width=52).pack(side="left", padx=(0, 6))

    def choose_dir():
        d = filedialog.askdirectory(initialdir=dir_var.get())
        if d:
            dir_var.set(d)
            state["out_dir"] = d

    tk.Button(bot, text="换文件夹", width=10, command=choose_dir).pack(side="left")
    prog = ttk.Progressbar(root, maximum=100)
    prog.pack(fill="x", padx=10)
    tk.Label(root, textvariable=status_var, anchor="w", fg="#444").pack(fill="x", padx=10, pady=(2, 8))

    # 下载队列：从上往下一个一个下
    qframe = tk.LabelFrame(root, text="下载队列（从上往下，一个一个下）")
    qframe.pack(fill="x", padx=10, pady=(0, 8))
    qlist = tk.Listbox(qframe, height=6, exportselection=False)
    qlist.pack(side="left", fill="both", expand=True)
    qbtns = tk.Frame(qframe)
    qbtns.pack(side="right", fill="y", padx=4)

    def remove_from_queue():
        sel = qlist.curselection()
        if not sel:
            messagebox.showinfo("提示", "先在队列里点一个")
            return
        e = state["queue"][sel[0]]
        if e["status"].startswith("下载中"):
            log("这个正在下载，要停就点「停止当前」")
            return
        del state["queue"][sel[0]]
        render_queue()
        log("已从队列移除：%s" % (e["item"].get("title") or "无标题"))

    def clear_done():
        state["queue"] = [e for e in state["queue"]
                           if not (e["status"].startswith("已完成") or e["status"].startswith("失败"))]
        render_queue()

    tk.Button(qbtns, text="从队列移除", width=10, command=remove_from_queue).pack(pady=2)
    tk.Button(qbtns, text="清掉已完成", width=10, command=clear_done).pack(pady=2)

    def render_queue():
        sel = qlist.curselection()
        qlist.delete(0, "end")
        for e in state["queue"]:
            qlist.insert("end", "%s  ——  %s" % (e["item"].get("title") or "无标题", e["status"]))
        if sel and sel[0] < len(state["queue"]):
            qlist.selection_set(sel[0])

    def queue_worker():
        try:
            while True:
                nxt = None
                for e in state["queue"]:
                    if e["status"] == "等下载":
                        nxt = e
                        break
                if nxt is None:
                    break
                e = nxt
                e["status"] = "下载中"
                q.put(("queue", None))
                out_dir = state["out_dir"] or default_dir
                try:
                    os.makedirs(out_dir, exist_ok=True)
                except Exception:
                    pass
                state["stop"].clear()
                state["cur_title"] = e["item"].get("title") or "视频"
                last_pct = [-1]

                def cb(pct, mb, e=e, last_pct=last_pct):
                    q.put(("progress", (pct, mb)))
                    if pct is not None and pct // 10 != last_pct[0] // 10:
                        last_pct[0] = pct
                        e["status"] = "下载中 %d%%" % pct
                        q.put(("queue", None))

                try:
                    download_mp4(
                        state["api"], e["item"], out_dir,
                        progress_cb=cb,
                        stop_flag=state["stop"],
                        log=lambda m: q.put(("status", m)),
                    )
                    e["status"] = "已完成"
                except Exception as ex:
                    msg = str(ex)
                    e["status"] = "已停止" if msg == "已停止" else ("失败：" + msg[:50])
                q.put(("queue", None))
                q.put(("progress", (0, 0)))
        finally:
            state["worker_running"] = False
            q.put(("queue_done", None))

    def ensure_worker():
        if not state["worker_running"]:
            state["worker_running"] = True
            threading.Thread(target=queue_worker, daemon=True).start()

    def add_to_queue():
        sel = lst.curselection()
        if not sel:
            messagebox.showinfo("提示", "先在列表里点一个视频")
            return
        item = state["items"][sel[0]]
        for e in state["queue"]:
            if e["item"]["id"] == item["id"] and (
                    e["status"] == "等下载" or e["status"].startswith("下载中")):
                log("这个已经在队列里了：%s" % (item.get("title") or "无标题"))
                return
        state["out_dir"] = dir_var.get().strip() or default_dir
        state["queue"].append({"item": item, "status": "等下载"})
        render_queue()
        log("已加入队列：%s（队列里 %d 个）" % (item.get("title") or "无标题", len(state["queue"])))
        ensure_worker()

    add_btn.config(command=add_to_queue)
    lst.bind("<Double-Button-1>", lambda e: add_to_queue())

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "items":
                    set_items(val)
                elif kind == "loaderror":
                    log(val)
                elif kind == "progress":
                    pct, mb = val
                    if pct is not None:
                        prog["value"] = pct
                    if mb and mb > 0:
                        log("正在下载：%s（已下 %.1f MB%s）" % (
                            state.get("cur_title", "视频"), mb,
                            ("，%d%%" % pct) if pct is not None else ""))
                elif kind == "status":
                    log(val)
                elif kind == "queue":
                    render_queue()
                elif kind == "queue_done":
                    done_n = sum(1 for e in state["queue"] if e["status"] == "已完成")
                    if done_n:
                        log("队列下完了：共完成 %d 个，都在下载文件夹里" % done_n)
                    prog["value"] = 0
                elif kind == "ready":
                    log("已连上服务器，在上面输入标题或番号，点搜索")
        except queue.Empty:
            pass
        root.after(120, poll)

    def connect():
        base = pick_working_host()
        if not base:
            q.put(("loaderror", "连不上它的服务器，检查一下网络再重新打开软件"))
            return
        state["api"] = Api(base)
        q.put(("ready", base))

    threading.Thread(target=connect, daemon=True).start()
    poll()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        # 命令行自检：连服务器 -> 搜索 -> 打印第一条（不下载）
        base = pick_working_host()
        print("base:", base)
        api = Api(base)
        r = api.search("DNJR", 1, 3)
        for x in r:
            print(x["id"], x.get("title", "")[:40], x.get("duration"))
        print("latest:", [x["id"] for x in api.latest(1, 3)])
        print("types ok:", len(api.types()))
    else:
        run_gui()
