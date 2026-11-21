# -*- coding: utf-8 -*-
"""
爱液下载器 v1.0.0
直接连爱液视频的服务器拿列表/搜索/播放地址，用内置 ffmpeg 存成 MP4。
全程鼠标操作：点分类或搜标题 -> 点视频 -> 点下载。
只用 Python 标准库 + 随包的 ffmpeg.exe。
"""
import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

VERSION = "v1.0.0"
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


def download_mp4(api, item, out_dir, progress_cb=None, stop_flag=None, log=None):
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
    last_err = None
    for idx, url in enumerate(urls):
        tmp_path = out_path + ".part.mp4"
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1", "-i", url, "-c", "copy", tmp_path]
        if log:
            log("开始下载%s：%s" % (("（线路%d）" % (idx + 1)) if idx else "", title))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace")
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                if stop_flag and stop_flag.is_set():
                    proc.kill()
                    raise RuntimeError("已停止")
                if progress_cb and duration > 0 and line.startswith("out_time_us="):
                    try:
                        done = int(line.split("=", 1)[1].strip()) / 1000000.0
                        progress_cb(min(99, int(done * 100 / duration)))
                    except Exception:
                        pass
            err = proc.stderr.read() if proc.stderr else ""
            rc = proc.wait()
            if rc == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                os.replace(tmp_path, out_path)
                if progress_cb:
                    progress_cb(100)
                return out_path
            last_err = (err or "下载失败").strip().splitlines()[-1] if err else "下载失败"
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
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
    root.geometry("980x620")
    root.minsize(860, 540)

    state = {
        "api": None,
        "mode": "latest",      # latest / type / search
        "type_id": None,
        "wd": "",
        "page": 1,
        "items": [],
        "downloading": False,
        "stop": threading.Event(),
    }
    q = queue.Queue()

    default_dir = os.path.join(os.path.expanduser("~"), "Downloads", "爱液下载")
    try:
        os.makedirs(default_dir, exist_ok=True)
    except Exception:
        default_dir = os.path.expanduser("~")
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
        log("这一页有 %d 个视频，点一个再点下载" % len(items) if items else "这一页没有视频")

    def load_async(kind):
        def work():
            try:
                if kind == "latest":
                    items = state["api"].latest(state["page"])
                elif kind == "type":
                    items = state["api"].by_type(state["type_id"], state["page"])
                else:
                    items = state["api"].search(state["wd"], state["page"])
                q.put(("items", items))
            except Exception as e:
                q.put(("error", "加载失败：%s" % e))
        threading.Thread(target=work, daemon=True).start()
        log("正在加载…")

    def do_search():
        wd = search_var.get().strip()
        if not wd:
            messagebox.showinfo("提示", "先输入要搜的标题或番号")
            return
        state["mode"] = "search"
        state["wd"] = wd
        state["page"] = 1
        load_async("search")

    tk.Button(top, text="搜索", width=8, command=do_search).pack(side="left")
    tk.Button(top, text="看最新", width=8, command=lambda: (state.update(mode="latest", page=1), load_async("latest"))[1]).pack(side="left", padx=6)
    ent.bind("<Return>", lambda e: do_search())

    # 中部：左分类 + 右列表
    mid = tk.Frame(root)
    mid.pack(fill="both", expand=True, padx=10, pady=4)
    tk.Label(mid, text="分类（点一下）").pack(side="top", anchor="w")
    left = tk.Frame(mid)
    left.pack(side="left", fill="y")
    cat_list = tk.Listbox(left, width=22, exportselection=False)
    cat_list.pack(side="left", fill="y")
    cat_ids = []

    right = tk.Frame(mid)
    right.pack(side="left", fill="both", expand=True, padx=(10, 0))
    lst = tk.Listbox(right, exportselection=False)
    lst.pack(fill="both", expand=True)

    def on_cat(_e=None):
        sel = cat_list.curselection()
        if not sel:
            return
        tid = cat_ids[sel[0]]
        state["mode"] = "type"
        state["type_id"] = tid
        state["page"] = 1
        load_async("type")

    cat_list.bind("<<ListboxSelect>>", on_cat)

    # 翻页 + 下载
    bar = tk.Frame(root)
    bar.pack(fill="x", padx=10, pady=4)
    tk.Button(bar, text="上一页", width=8, command=lambda: turn_page(-1)).pack(side="left")
    tk.Label(bar, textvariable=page_var).pack(side="left", padx=6)
    tk.Button(bar, text="下一页", width=8, command=lambda: turn_page(1)).pack(side="left")

    def turn_page(delta):
        np = state["page"] + delta
        if np < 1:
            return
        state["page"] = np
        load_async(state["mode"])

    dl_btn = tk.Button(bar, text="下载选中视频", width=14, bg="#2e7d32", fg="white")
    dl_btn.pack(side="right")
    stop_btn = tk.Button(bar, text="停止", width=8, command=lambda: state["stop"].set())
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

    tk.Button(bot, text="换文件夹", width=10, command=choose_dir).pack(side="left")
    prog = ttk.Progressbar(root, maximum=100)
    prog.pack(fill="x", padx=10)
    tk.Label(root, textvariable=status_var, anchor="w", fg="#444").pack(fill="x", padx=10, pady=(2, 8))

    def do_download():
        if state["downloading"]:
            return
        sel = lst.curselection()
        if not sel:
            messagebox.showinfo("提示", "先在列表里点一个视频")
            return
        item = state["items"][sel[0]]
        out_dir = dir_var.get().strip() or default_dir
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("出错", "文件夹建不了：%s" % e)
            return
        state["downloading"] = True
        state["stop"].clear()
        dl_btn.config(state="disabled")
        prog["value"] = 0

        def work():
            try:
                path = download_mp4(
                    state["api"], item, out_dir,
                    progress_cb=lambda p: q.put(("progress", p)),
                    stop_flag=state["stop"],
                    log=lambda m: q.put(("status", m)),
                )
                q.put(("done", path))
            except Exception as e:
                q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    dl_btn.config(command=do_download)
    lst.bind("<Double-Button-1>", lambda e: do_download())

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "items":
                    set_items(val)
                elif kind == "progress":
                    prog["value"] = val
                elif kind == "status":
                    log(val)
                elif kind == "done":
                    state["downloading"] = False
                    dl_btn.config(state="normal")
                    prog["value"] = 100
                    log("下载完成：%s" % val)
                    messagebox.showinfo("完成", "已存好：\n%s" % val)
                elif kind == "error":
                    state["downloading"] = False
                    dl_btn.config(state="normal")
                    log("出错：" + val)
                    messagebox.showerror("出错", val)
                elif kind == "ready":
                    log("已连上服务器，点分类或直接点视频下载")
                    load_async("latest")
                    # 填分类
                    def fill_cats():
                        try:
                            cats = state["api"].types()
                            pairs = []
                            for parent in cats:
                                for child in (parent.get("child") or []):
                                    pairs.append(("%s" % child.get("name"), child.get("id")))
                            q.put(("cats", pairs))
                        except Exception:
                            pass
                    threading.Thread(target=fill_cats, daemon=True).start()
                elif kind == "cats":
                    cat_list.delete(0, "end")
                    cat_ids.clear()
                    for name, cid in val:
                        cat_list.insert("end", name)
                        cat_ids.append(cid)
        except queue.Empty:
            pass
        root.after(120, poll)

    def connect():
        base = pick_working_host()
        if not base:
            q.put(("error", "连不上它的服务器，检查一下网络再重开"))
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
