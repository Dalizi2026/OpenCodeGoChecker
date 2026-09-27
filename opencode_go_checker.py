#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coding Plan 额度查询
- 单文件 exe 友好，DPI 感知，无控制台启动
- 支持多 Key 记忆、并发查询、本地 JSON 存储
- 云端渠道：OpenCode Go / Command Code / Cline Pass / 阶跃星辰 StepFun / Grok Build
- 本地用量：dsh 账本、opencode 本地库、Grok 本机会话库（找不到就跳过，不报错）
- 所有路径都自动探测，可在「设置 → 数据源路径」里手动指定
"""
import json, os, sys, threading, time, urllib.request, urllib.error, sqlite3, re, shutil, math, copy, functools
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

# DPI 感知 (Windows)
try:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Per-monitor aware
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

APP_NAME = "OpenCodeGoChecker"
APP_VERSION = "3.0.0"
APP_TITLE = "Coding Plan 额度查询"

# ============================================================================
# 通用安全助手
# ============================================================================
def _finite(v):
    """把 NaN / ±Infinity 统一成 None。

    为什么必须做：pywebview 回传 JS 时用 json.dumps(allow_nan=True)，会写出
    JSON 规范不允许的裸 NaN / Infinity 字面量；前端 JSON.parse 会抛 SyntaxError，
    而这个异常发生在回调表项被删除之后、resolve/reject 之前 —— Promise 永不
    settle，按钮就永久停在「查询中」，且因为 query_key 会回传整份密钥列表，
    任何一个 Key 的缓存里混进一个 NaN 就会让**所有渠道**的查询全部卡死。
    """
    try:
        f = float(v)
    except Exception:
        return None
    return f if math.isfinite(f) else None


def _json_safe(obj, _depth=0, _seen=None):
    """递归净化：非有限浮点 → None，并把不可序列化的对象降级成字符串。

    所有 js_api 的返回值都必须先过这里，否则可能永久卡死前端。

    实现要点：
      · 用 id() 去重，避免自引用结构（a=[a,a]）在深度上限内指数爆炸 ——
        只靠深度上限的话，扇出 ≥2 的环会让调用方假死十几秒。
      · 兜底 str() 也必须包在 try 里：__repr__ 抛异常的对象会直接把异常
        抛出本函数。
    """
    if _seen is None:
        _seen = set()
    if _depth > 24:
        return None
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    oid = id(obj)
    if oid in _seen:
        return None                      # 环状引用
    if isinstance(obj, dict):
        _seen.add(oid)
        try:
            out = {}
            for k, v in obj.items():
                try:
                    kk = k if isinstance(k, str) else str(k)
                except Exception:
                    kk = repr(type(k))
                out[kk] = _json_safe(v, _depth + 1, _seen)
            return out
        finally:
            _seen.discard(oid)
    if isinstance(obj, (list, tuple, set, frozenset)):
        _seen.add(oid)
        try:
            return [_json_safe(v, _depth + 1, _seen) for v in obj]
        finally:
            _seen.discard(oid)
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    try:
        json.dumps(obj)
        return obj
    except Exception:
        pass
    try:
        return str(obj)
    except Exception:
        return "<unrepresentable %s>" % type(obj).__name__


def _safe_json_loads(text):
    """解析远端/本地 JSON，并把 NaN / Infinity / 1e999 一律降级为 None。

    Python 的 json.loads 默认接受裸 NaN / Infinity 字面量，超大数字还会溢出成
    inf —— 这些值一旦进入缓存就会被 _json_safe 拦下，但更早拦掉能少一堆脏数据。
    """
    def _bad(_tok):
        return None
    def _num(s):
        try:
            return _finite(float(s))
        except Exception:
            return None
    return json.loads(text, parse_constant=_bad, parse_float=_num)


def _safe_int(v, default=0):
    try:
        f = float(v)
    except Exception:
        return default
    if not math.isfinite(f):
        return default
    try:
        return int(f)
    except Exception:
        return default


def _safe_float(v, default=0.0):
    f = _finite(v)
    return default if f is None else f


def _tail_lines(path, max_bytes=2 * 1024 * 1024, max_lines=20000):
    """从文件尾部往前读若干字节，按「新 → 旧」返回行。

    用途：Grok CLI 的 unified.jsonl 可能有几百 MB，只需要最后一条 billing 记录。
    直接用 readlines() 会把整个文件读进内存，在查询线程里造成长时间卡顿。
    """
    try:
        size = path.stat().st_size
    except Exception:
        return []
    try:
        with open(path, "rb") as f:
            start = max(0, size - max_bytes)
            f.seek(start)
            chunk = f.read()
    except Exception:
        return []
    text = chunk.decode("utf-8", errors="ignore")
    lines = text.split("\n")
    if start > 0 and lines:
        lines = lines[1:]          # 首行可能是被截断的半行，丢弃
    lines = [ln for ln in lines if ln.strip()]
    lines.reverse()                # 新 → 旧
    return lines[:max_lines]


API_URL = "https://opencode.ai/zen/go/v1/usage"
STEPFUN_ACCOUNT_URL = "https://api.stepfun.com/v1/accounts"

# ---------- Command Code ----------
COMMANDCODE_BASE = "https://api.commandcode.ai"
COMMANDCODE_CLI_VERSION = "1.53.0"
# 套餐表（与官方 CLI 1.53.0 plan maps 同步）：planId 前缀 → (名称, 月额度$)
COMMANDCODE_PLANS = {
    "individual-go":        ("Go",      10),
    "individual-goat":      ("GOAT",    70),
    "individual-pro":       ("Pro",     30),
    "individual-pro-v1":    ("Pro",     80),
    "individual-provider":  ("Provider", 15),
    "individual-max":       ("Max",     150),
    "individual-ultra":     ("Ultra",   300),
    "teams-pro":            ("Teams Pro", 40),
}

# ---------- 渠道定义 ----------
CHANNEL_OPENCODE = "opencode-go"
CHANNEL_STEPFUN = "stepfun"
CHANNEL_COMMANDCODE = "commandcode"
CHANNEL_GROK = "grok-build"
CHANNEL_CLINE = "cline"
CHANNEL_LABELS = {CHANNEL_OPENCODE: "OpenCode Go", CHANNEL_STEPFUN: "阶跃星辰 StepFun",
                  CHANNEL_COMMANDCODE: "Command Code", CHANNEL_GROK: "Grok Build",
                  CHANNEL_CLINE: "Cline Pass"}
CHANNEL_SHORT  = {CHANNEL_OPENCODE: "Go", CHANNEL_STEPFUN: "阶跃", CHANNEL_COMMANDCODE: "CC",
                  CHANNEL_GROK: "Grok", CHANNEL_CLINE: "Cline"}

# ---------- Cline（cline.bot / Cline Pass）----------
CLINE_BASE = "https://api.cline.bot"
# 实测可用的官方端点（其余 /usage /credits /limits /quota 等均 404）：
#   GET /api/v1/users/me            账户资料
#   GET /api/v1/users/me/plan       订阅套餐 + inferenceCapThreshold 上限阈值 + 计费周期
#   GET /api/v1/users/{id}/balance  余额（官方未标注量纲，保持原值展示）
CLINE_ME_PATH = "/api/v1/users/me"
CLINE_PLAN_PATH = "/api/v1/users/me/plan"
CLINE_LIMITS_PATH = "/api/v1/users/me/plan/usage-limits"   # 5h / 周 / 月 三个窗口的官方百分比
CLINE_BALANCE_PATH = "/api/v1/users/{uid}/balance"
CLINE_DAILY_PATH = "/api/v1/users/{uid}/usages/daily"       # 官方按天/按模型汇总（服务端聚合）
CLINE_RECORDS_PATH = "/api/v1/users/{uid}/usages"           # 官方逐条调用记录（含 cachedTokens）
# 余额量纲已标定（2026-09-23）：面板显示 Credits 0.2425，接口返回 242476 → 1e6 微美元 = 1 Credit
CLINE_BALANCE_DIVISOR = 1_000_000
# costUsd 同为微美元量纲；官方逐条接口每页最多返回 200 条，用 nextToken 翻页
CLINE_COST_DIVISOR = 1_000_000
CLINE_PAGE_LIMIT = 200
CLINE_MAX_PAGES = 12
_CLINE_USAGE_CACHE = {"at": 0.0, "uid": "", "time_key": "", "data": None, "warn": ""}
_CLINE_CACHE_LOCK = threading.RLock()

# ---------- StepFun Step Plan ----------
# 月池档位（M Credit / 月）
STEPFUN_TIERS = [400, 1600, 8000, 40000]
STEPFUN_TIER_LABELS = {
    400:   "Flash Mini  ¥49 · 400M",
    1600:  "Flash Plus ¥99 · 1600M",
    8000:  "Flash Pro  ¥199 · 8000M",
    40000: "Flash Max  ¥699 · 40B",
}
DEFAULT_STEP_TIER = 1600
# 阶跃星辰（StepFun）官方定价（¥ / 1M tokens）：输入未命中 / 输入命中(缓存) / 输出
# 来源：https://platform.stepfun.com / 官方开放平台
STEPFUN_PRICES = {
    "step-5-preview":      (7.0, 0.35, 20.0),
    "step-3.7-flash":      (1.35, 0.27, 8.1),
    "step-3.5-flash":      (0.7, 0.14, 2.1),
    "step-3.5-flash-2603": (0.7, 0.14, 2.1),
    "step-2":              (1.0, 0.20, 2.0),
    "step-2-mini":         (1.0, 0.20, 2.0),
    "step-1-8k":           (5.0, 1.0, 20.0),
    "step-1-32k":          (15.0, 3.0, 70.0),
    "step-1-128k":         (40.0, 8.0, 200.0),
    "step-1-256k":         (70.0, 14.0, 350.0),
    "step-1v-8k":          (5.0, 1.0, 20.0),
    "step-1v-32k":         (15.0, 3.0, 70.0),
}
# 1M Credit = ¥1（月末清零，不结转）

# OpenCode Go / Zen 官方全量定价主表（$ / 1M tokens）：input / output / cachedInput
# 来源：https://opencode.ai/docs/zen 与 https://opencode.ai/docs/go
OPENCODE_GO_PRICES = {
    "deepseek-v4.1-flash":           (0.30, 1.20, 0.006),
    "deepseek-v4-pro":              (1.74, 3.48, 0.145),
    "deepseek-v4-flash":            (0.14, 0.28, 0.028),
    "deepseek-v4-flash-vision-exp": (0.14, 0.28, 0.028),
    "glm-5.3-flash":                (0.15, 0.50, 0.03),
    "glm-5.3":                      (1.40, 4.40, 0.26),
    "glm-5.2":                      (1.40, 4.40, 0.26),
    "glm-5.1":                      (1.40, 4.40, 0.26),
    "glm-5":                        (1.00, 3.20, 0.20),
    "kimi-k3":                      (3.00, 15.00, 0.30),
    "kimi-k2.7-code":               (0.95, 4.00, 0.19),
    "kimi-k2.6":                    (0.95, 4.00, 0.16),
    "kimi-k2.5":                    (0.60, 3.00, 0.10),
    "minimax-m3":                   (0.30, 1.20, 0.06),
    "minimax-m2.7":                 (0.30, 1.20, 0.06),
    "minimax-m2.5":                 (0.30, 1.20, 0.06),
    "qwen3.8-max":                  (2.00, 6.00, 0.25),
    "qwen3.8-flash":                (0.15, 0.47, 0.016),
    "qwen3.7-max":                  (2.50, 7.50, 0.50),
    "qwen3.7-plus":                 (0.40, 1.60, 0.04),
    "qwen3.6-plus":                 (0.50, 3.00, 0.05),
    "qwen3.5-plus":                 (0.20, 1.20, 0.02),
    "grok-4.6":                     (2.00, 6.00, 0.50),
    "grok-4.5":                     (2.00, 6.00, 0.30),
    "grok-build-0.1":               (1.00, 2.00, 0.20),
    "gpt-5.6-luna":                 (0.20, 1.20, 0.02),
    "gpt-5.6-terra":                (2.00, 12.00, 0.20),
    "gpt-5.6-sol":                  (4.00, 20.00, 0.40),
    "gpt-5.5":                      (5.00, 30.00, 0.50),
    "gpt-5.4":                      (2.50, 15.00, 0.25),
    "gpt-5.4-mini":                 (0.75, 4.50, 0.075),
    "gpt-5.4-nano":                 (0.20, 1.25, 0.02),
    "gpt-5.3-codex":                (1.75, 14.00, 0.175),
    "gpt-5.3-codex-spark":          (1.75, 14.00, 0.175),
    "gpt-5.2-codex":                (1.75, 14.00, 0.175),
    "gpt-5.2":                      (1.75, 14.00, 0.175),
    "gpt-5.1-codex":                (1.07, 8.50, 0.107),
    "gpt-5.1-codex-max":            (1.25, 10.00, 0.125),
    "gpt-5.1-codex-mini":           (0.25, 2.00, 0.025),
    "gpt-5.1":                      (1.07, 8.50, 0.107),
    "gpt-5-codex":                  (1.07, 8.50, 0.107),
    "gpt-5":                        (1.07, 8.50, 0.107),
    "gpt-5-nano":                   (0.05, 0.40, 0.005),
    "claude-sonnet-5":              (2.00, 10.00, 0.20),
    "claude-sonnet-4-6":            (3.00, 15.00, 0.30),
    "claude-sonnet-4-5":            (3.00, 15.00, 0.30),
    "claude-opus-5":                (5.00, 25.00, 0.50),
    "claude-opus-4-8":              (5.00, 25.00, 0.50),
    "claude-opus-4-7":              (5.00, 25.00, 0.50),
    "claude-opus-4-6":              (5.00, 25.00, 0.50),
    "claude-opus-4-5":              (5.00, 25.00, 0.50),
    "claude-fable-5":               (10.00, 50.00, 1.00),
    "claude-fable-5.1":             (10.00, 50.00, 0.25),
    "claude-haiku-4-5":             (1.00, 5.00, 0.10),
    "gemini-3.8-flash":             (1.50, 7.50, 0.15),
    "gemini-3.7-flash":             (1.50, 7.50, 0.15),
    "gemini-3.7-flash-high":        (1.50, 7.50, 0.15),
    "gemini-3.6-flash":             (1.50, 7.50, 0.15),
    "gemini-3.5-flash":             (1.50, 9.00, 0.15),
    "gemini-3.5-flash-lite":        (0.30, 2.50, 0.03),
    "gemini-3.1-pro":               (2.00, 12.00, 0.20),
    "gemini-3-flash":               (0.50, 3.00, 0.05),
    "mimo-v2.5":                    (0.14, 0.28, 0.0028),
    "mimo-v2.5-pro":                (0.435, 0.87, 0.003625),
    "mimo-v2-omni":                 (0.14, 0.28, 0.0028),
    "mimo-v2-pro":                  (0.435, 0.87, 0.003625),
    "hy3":                          (0.14, 0.58, 0.035),
    "hy3-preview":                  (0.14, 0.58, 0.035),
    "longcat-2.0":                  (0.30, 1.20, 0.006),
    "muse-spark-1.2-contributor":   (0.10, 0.20, 0.002),
    "muse-spark-1.3-contributor":   (0.10, 0.20, 0.002),
    # 官方免费/福利模型
    "ox-alpha-free":                (0.0, 0.0, 0.0),
    "omen-alpha":                   (0.0, 0.0, 0.0),
    "mimo-v2.5-free":               (0.0, 0.0, 0.0),
    "nemotron-3-ultra-free":        (0.0, 0.0, 0.0),
    "nemotron-3.5-lightning-free":  (0.0, 0.0, 0.0),
    "big-pickle":                   (0.0, 0.0, 0.0),
    "jev-1.13-free":                (0.0, 0.0, 0.0),
}

# Command Code 官方定价主表（$ / 1M tokens）：input / output / cachedInput
# 来源：https://commandcode.ai
COMMANDCODE_PRICES = {
    "deepseek/deepseek-v4.1-flash":  (0.30, 1.20, 0.006),
    "deepseek/deepseek-v4-flash":    (0.14, 0.28, 0.028),
    "deepseek/deepseek-v4-pro":      (1.32, 3.96, 0.044),
    "minimax/minimax-m3-free":       (0.0, 0.0, 0.0),
    "z-ai/glm-5.3-flash":            (0.15, 0.50, 0.03),
    "deepseek-v4.1-flash":           (0.30, 1.20, 0.006),
    "deepseek-v4-flash":             (0.14, 0.28, 0.028),
    "deepseek-v4-pro":               (1.32, 3.96, 0.044),
    "glm-5.3-flash":                 (0.15, 0.50, 0.03),
}

def get_storage_dir():
    """用户数据目录。固定落在用户配置目录下，**绝不在程序目录/仓库目录读写任何密钥**。

    历史版本会把密钥镜像回源码目录（作者本机那个 opencode 项目文件夹），那正是
    「上传 GitHub 会把本地秘钥一起传上去」的根因。这里彻底删掉该行为，也不再从
    桌面/程序目录「播种」密钥文件 —— 新用户从空开始，自己在界面里添加即可。
    """
    base = os.getenv("APPDATA") or os.getenv("XDG_CONFIG_HOME")
    candidates = []
    if base:
        candidates.append(Path(base) / APP_NAME)
    candidates.append(Path.home() / ("." + APP_NAME))
    candidates.append(Path.home() / APP_NAME)
    for ad in candidates:
        try:
            ad.mkdir(parents=True, exist_ok=True)
            probe = ad / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return ad
        except Exception:
            continue
    return candidates[0]

STORAGE_DIR = get_storage_dir()
KEYS_FILE = STORAGE_DIR / "opencode_go_keys.json"
SETTINGS_FILE = STORAGE_DIR / "app_settings.json"
LOG_FILE = STORAGE_DIR / "startup.log"

_KEYS_LOCK = threading.RLock()
_MUTATE_LOCK = threading.RLock()
_LAST_SAVE_ERROR = [""]

def log_line(msg):
    """启动/运行日志。EXE 是 console=False，没有它出问题时完全无从下手。"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass

def load_app_settings():
    if not SETTINGS_FILE.exists():
        return {"theme": "obsidian"}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            d = _safe_json_loads(f.read())
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {"theme": "obsidian"}

def save_app_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_json_safe(settings), f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

def norm_key_item(it):
    """旧数据迁移：补齐 channel / step_tier 字段，非法条目丢弃"""
    if not isinstance(it, dict): return None
    if not it.get("key"): return None
    it.setdefault("alias", "")
    ch = it.get("channel") or CHANNEL_OPENCODE
    if ch not in CHANNEL_LABELS: ch = CHANNEL_OPENCODE
    it["channel"] = ch
    if ch == CHANNEL_STEPFUN:
        it["step_tier"] = _safe_int(it.get("step_tier"), 0)
    it.setdefault("last_result", None)
    it.setdefault("last_update", None)
    # 上一次查询失败的原因；前端据此在卡片上标红，而不是继续显示旧数据当作新数据
    it.setdefault("last_error", None)
    return it

def key_channel(it):
    return (it or {}).get("channel") or CHANNEL_OPENCODE

def _chan_label(code):
    return "阶跃星辰 StepFun" if code == CHANNEL_STEPFUN else "OpenCode Go（opencode zen）"

def _chan_code(label):
    return CHANNEL_STEPFUN if str(label).startswith("阶跃") else CHANNEL_OPENCODE

_LAST_LOAD_ERROR = [""]

def _parse_keys_payload(d):
    raw = []
    if isinstance(d, dict) and "keys" in d: raw = d["keys"]
    elif isinstance(d, list): raw = d
    elif isinstance(d, dict) and d.get("key"): raw = [d]
    out = []
    for it in raw:
        n = norm_key_item(_json_safe(it))
        if n: out.append(n)
    return out

def load_keys():
    with _KEYS_LOCK:
        if not KEYS_FILE.exists():
            bak = KEYS_FILE.with_suffix(".bak")
            if bak.exists():
                try:
                    shutil.copy2(bak, KEYS_FILE)
                except Exception:
                    pass
        if not KEYS_FILE.exists():
            return []
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                return _parse_keys_payload(_safe_json_loads(f.read()))
        except Exception as e:
            # 文件损坏：先原样留证，再从 .bak 恢复 —— 绝不静默清空
            _LAST_LOAD_ERROR[0] = "密钥文件解析失败：%s" % e
            try:
                shutil.copy2(KEYS_FILE, KEYS_FILE.with_name(
                    KEYS_FILE.name + ".corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
            except Exception:
                pass
            bak = KEYS_FILE.with_suffix(".bak")
            if bak.exists():
                try:
                    with open(bak, "r", encoding="utf-8") as f:
                        out = _parse_keys_payload(_safe_json_loads(f.read()))
                    if out:
                        _LAST_LOAD_ERROR[0] += "（已从 .bak 恢复 %d 条）" % len(out)
                        return out
                except Exception:
                    pass
            return []

def save_keys(keys):
    """原子写入。

    修正两点历史缺陷：
      1) 不再把密钥镜像到源码目录（那是上传 GitHub 泄露秘钥的直接原因）；
      2) .bak 保存的是「本次写入之前」的上一份，才真正具备回滚价值
         （旧实现把新数据同时写进 .bak，等于没有备份）。
    """
    with _KEYS_LOCK:
        tmp_file = KEYS_FILE.with_name(KEYS_FILE.name + ".%d.tmp" % os.getpid())
        try:
            KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
            safe = _json_safe(keys)
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(safe, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if KEYS_FILE.exists():
                try:
                    shutil.copy2(KEYS_FILE, KEYS_FILE.with_suffix(".bak"))
                except Exception:
                    pass
            os.replace(tmp_file, KEYS_FILE)
            _LAST_SAVE_ERROR[0] = ""
            return True
        except Exception as e:
            _LAST_SAVE_ERROR[0] = str(e)
            try:
                if tmp_file.exists():
                    tmp_file.unlink()
            except Exception:
                pass
            return False

def mask_key(k):
    """掩码显示。短 Key 不再暴露首尾片段（旧实现会把 9~12 位的 Key 露出 8 位）。"""
    k = (k or "").strip()
    n = len(k)
    if n == 0:
        return ""
    if n < 12:
        return "•" * n
    if n >= 20:
        return k[:4] + "·" * 6 + k[-4:]
    return k[:3] + "·" * 6 + k[-3:]

def format_reset(sec):
    try: sec=int(sec)
    except: return str(sec)
    if sec<=0: return "即将重置"
    d=sec//86400; h=(sec%86400)//3600; m=(sec%3600)//60
    if d>0: return f"{d}天 {h}小时 {m}分后重置"
    if h>0: return f"{h}小时 {m}分后重置"
    return f"{m}分钟后重置"

def fetch_usage(apikey, timeout=12):
    req=urllib.request.Request(API_URL, method="GET")
    req.add_header("Authorization", f"Bearer {apikey}")
    req.add_header("Accept","application/json")
    req.add_header("User-Agent","opencode-go-checker/2.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body=r.read().decode("utf-8", errors="ignore")
            data=_safe_json_loads(body)
            if not isinstance(data, dict):
                return None, f"接口返回了非预期的数据格式（期望 JSON 对象）: {body[:200]}"
            return data, None
    except urllib.error.HTTPError as e:
        body=""
        try: body=e.read().decode("utf-8", errors="ignore")
        except: pass
        if e.code==401: return None, f"401 未授权：Key 无效或非 Go 订阅\n{body[:300]}"
        if e.code==404: return None, f"404 接口不存在\n{body[:200]}"
        return None, f"HTTP {e.code}: {body[:400]}"
    except urllib.error.URLError as e: return None, f"网络错误: {e.reason}"
    except Exception as e: return None, f"异常: {e}"

def parse_usage(data):
    if not isinstance(data, dict):
        return {"rolling": {}, "weekly": {}, "monthly": {}, "useBalance": None, "_raw": data}
    usage_wrapper = data.get("usage") if isinstance(data.get("usage"), dict) else None
    def get_win(name):
        cand=[data.get(f"{name}Usage"), data.get(f"{name}_usage"), data.get(name), (usage_wrapper or {}).get(name), (data.get("windows") or {}).get(name)]
        for c in cand:
            if isinstance(c, dict): return c
        return {}
    def gp(o):
        for k in ["usagePercent","usage_percent","percent","usage","value"]:
            if k in o:
                try: return float(o[k])
                except: pass
        return None
    def gr(o):
        for k in ["resetInSec","reset_in_sec","resetsInSec","resets_in_sec"]:
            if k in o:
                try: return int(o[k])
                except: pass
        for k in ["resetsAt","resetAt","resets_at","reset_at"]:
            if k in o:
                try:
                    v=o[k]
                    dt=datetime.fromisoformat(v.replace("Z","+00:00"))
                    return max(0, int((dt - datetime.now(dt.tzinfo)).total_seconds()))
                except: pass
        return None
    def gs(o): return o.get("status") or o.get("state") or "ok"
    out={}
    for n in ["rolling","weekly","monthly"]:
        w=get_win(n)
        out[n]={"percent":gp(w), "reset":gr(w), "status":gs(w), "raw":w}
    ub=data.get("useBalance")
    if ub is None: ub=data.get("use_balance")
    if ub is None and usage_wrapper: ub=usage_wrapper.get("useBalance") or usage_wrapper.get("use_balance")
    out["useBalance"]=ub
    out["_raw"]=data
    return out

# ---------- Token 本地消耗 ----------
def _cfg_path(key):
    """读取用户在「设置 → 数据源路径」里手动指定的目录/文件；没填就返回 None。"""
    try:
        v = load_app_settings().get(key)
        if v:
            return Path(str(v)).expanduser()
    except Exception:
        pass
    return None

def get_opencode_db_path():
    """自动探测 opencode 本地库。

    历史版本硬编码了作者机器上的用户目录与某个盘符下的自定义数据目录，
    对别人既无用又危险（可能把别人的同名目录误当成「你的用量」）。这里只保留
    标准位置 + 环境变量 + 用户手动指定。
    """
    cands = []
    p = _cfg_path("opencode_db")
    if p:
        # 用户显式指定就认它，**哪怕文件当前不存在** —— 否则「我填了路径却还是
        # 读别的库」无法解释。设置页会把它显示成「✕ 未找到」，便于发现写错。
        return p if p.suffix.lower() == ".db" else p / "opencode.db"
    for env in ("OPENCODE_DATA", "OPENCODE_HOME", "XDG_DATA_HOME"):
        v = os.getenv(env)
        if v:
            base = Path(v)
            cands.append(base / "opencode" / "opencode.db")
            cands.append(base / "opencode.db")
    appdata = os.getenv("APPDATA")
    local = os.getenv("LOCALAPPDATA")
    home = Path.home()
    if appdata:
        cands.append(Path(appdata) / "opencode" / "opencode.db")
    if local:
        cands.append(Path(local) / "opencode" / "opencode.db")
    cands.append(home / ".local" / "share" / "opencode" / "opencode.db")
    cands.append(home / ".opencode" / "opencode.db")
    # opencode 的数据目录有时会被搬到别的盘（官方文档里的 OpenCodeData 目录名）。
    # 这里按「盘符 + 约定目录名」探测，不含任何用户名，因此对任何人都成立。
    for letter in ("D", "E", "F", "C"):
        cands.append(Path("%s:/OpenCodeData/opencode.db" % letter))
        cands.append(Path("%s:/OpenCodeData/storage/opencode.db" % letter))
    for c in cands:
        try:
            if c.exists() and c.is_file():
                return c
        except Exception:
            pass
    return cands[0] if cands else home / ".local" / "share" / "opencode" / "opencode.db"

def format_tokens(n):
    try: n=int(n)
    except: return "--"
    if n>=1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n>=1_000_000: return f"{n/1_000_000:.2f}M"
    if n>=1000: return f"{n/1000:.1f}K"
    return str(n)

def format_cost(c):
    try: c=float(c)
    except: return "--"
    if c==0: return "$0"
    if c<0.01: return f"${c:.4f}"
    if c<1: return f"${c:.3f}"
    return f"${c:.2f}"

def format_credit(m):
    """Credit 数（单位 M）格式化"""
    try: m=float(m)
    except Exception: return "--"
    if m >= 1000: return f"{m/1000:.2f}B"
    if m >= 1: return f"{m:.1f}M"
    return f"{m*1000:.0f}K"

def get_time_range(key):
    now = datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key=="今日":
        s = int(today0.timestamp()*1000)
        e = int(now.timestamp()*1000)+86400000
    elif key=="近7天":
        s = int((today0 - timedelta(days=6)).timestamp()*1000)
        e = int((now+timedelta(days=1)).timestamp()*1000)
    elif key=="近30天":
        s = int((today0 - timedelta(days=29)).timestamp()*1000)
        e = int((now+timedelta(days=1)).timestamp()*1000)
    elif key=="本月":
        month0 = today0.replace(day=1)
        s = int(month0.timestamp()*1000)
        e = int((now+timedelta(days=1)).timestamp()*1000)
    else:  # 全部
        s = 0; e = 9999999999999
    return s,e

def query_token_stats(time_key="全部"):
    db = get_opencode_db_path()
    try:
        db_exists = db.exists()
    except Exception:
        db_exists = False
    if not db_exists:
        return {"error": f"未找到 opencode 本地库：{db}\n可在「设置 → 数据源路径」里手动指定 opencode.db 的位置。",
                "totals": None, "per_model": []}
    start,end = get_time_range(time_key)
    uri = f"file:{db.as_posix()}?mode=ro"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=3.0, check_same_thread=False)
        con.execute("PRAGMA query_only=ON;")
        con.execute("PRAGMA cache_size=-64000;")
        cur = con.cursor()
        # 优先 session_v2，回退 session
        tables = ["session_v2","session"]
        per_model = []
        totals = {"sessions":0,"cost":0,"tokens":0,"input":0,"output":0}
        last_err = None
        for tbl in tables:
            try:
                cur.execute(f"SELECT count(*) FROM {tbl}")
                if cur.fetchone()[0]==0: continue
                # 构造带时间过滤的聚合
                # 按模型去重（同一模型不同 provider 合并），只保留有实际消耗的模型
                sql = f"""
                SELECT
                  json_extract(model,'$.id') as mid,
                  MAX(json_extract(model,'$.providerID')) as prov,
                  COUNT(*) as cnt,
                  SUM(cost) as cost,
                  SUM(tokens_input) as inp,
                  SUM(tokens_output) as outp,
                  SUM(tokens_reasoning) as rea,
                  SUM(tokens_cache_read) as cr,
                  SUM(tokens_cache_write) as cw
                FROM {tbl}
                WHERE time_created BETWEEN ? AND ?
                  AND model IS NOT NULL
                  AND json_extract(model,'$.id') IS NOT NULL
                GROUP BY mid
                HAVING (SUM(tokens_input)+SUM(tokens_output)+SUM(tokens_reasoning)) > 0
                ORDER BY cost DESC
                """
                cur.execute(sql, (start,end))
                rows = cur.fetchall()
                if rows:
                    per_model = []
                    for r in rows:
                        mid, prov, cnt, cost, inp, outp, rea, cr, cw = r
                        if not mid: mid="(未知)"
                        if not prov: prov="-"
                        cost = _safe_float(cost); inp=_safe_int(inp); outp=_safe_int(outp)
                        rea=_safe_int(rea); cr=_safe_int(cr); cw=_safe_int(cw)
                        total_tok = inp + outp + rea  # 折叠（计费口径）
                        total_with_cache = total_tok + cr + cw
                        per_model.append({
                            "model": mid, "provider": prov,
                            "count": _safe_int(cnt), "cost": cost,
                            "input": inp, "output": outp, "reasoning": rea,
                            "cache": cr + cw,
                            "tokens": total_tok,
                            "tokens_with_cache": total_with_cache,
                        })
                    cur.execute(f"SELECT COUNT(*), SUM(cost), SUM(tokens_input), SUM(tokens_output), SUM(tokens_reasoning), SUM(tokens_cache_read), SUM(tokens_cache_write) FROM {tbl} WHERE time_created BETWEEN ? AND ?", (start,end))
                    c2, cc, ci, co, cr2, crc, cwc = cur.fetchone()
                    ci=_safe_int(ci); co=_safe_int(co); cr2=_safe_int(cr2)
                    crc=_safe_int(crc); cwc=_safe_int(cwc)
                    totals = {"sessions": _safe_int(c2), "cost": _safe_float(cc), "input": ci, "output": co,
                              "reasoning": cr2, "cache": crc+cwc, "tokens": ci+co+cr2,
                              "tokens_with_cache": ci+co+cr2+crc+cwc}
                    return {"per_model": per_model, "totals": totals, "error": None, "db": str(db)}
            except Exception as e:
                # 只有「表不存在」才继续试下一张表；其它错误（列缺失、库被占用等）
                # 必须原样报出来，否则界面会把「读失败」显示成「用量为 0」。
                last_err = e
                if "no such table" in str(e).lower():
                    continue
                break
        if last_err is not None:
            return {"per_model": [], "totals": None,
                    "error": f"读取 opencode 本地库失败：{last_err}", "db": str(db)}
        return {"per_model": [], "totals": totals, "error": None, "db": str(db)}
    except Exception as e:
        return {"error": f"读取 opencode 本地库失败：{e}", "totals": None, "per_model": []}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

# ---------- dsh 本地账本数据源（主数据源） ----------
def _dsh_home_candidates():
    """DSH 数据目录候选（按优先级）：
    1) app_settings.json 里手工指定的 dsh_home（设置界面可填，最可靠）
    2) 环境变量 DSH_HOME / DSH_DATA_DIR —— dsh-desktop（Electron）用它指向数据目录
    3) 环境变量 DSH_PROFILE_DIR 反推（<home>/profiles/<profile>）
    4) 常见默认位置（~/.dsh、%APPDATA%\\dsh、%LOCALAPPDATA%\\DeepSeekHarness\\data 等）

    注意：候选顺序只决定「优先级」，真正的挑选由 dsh_home() 按「哪个目录里真的有
    账本/凭据」来决定 —— 否则一台机器上同时存在旧 CLI 的 ~/.dsh 空壳和 desktop 的
    真实数据目录时，会挑到空壳，导致所有 Key 都显示 0。
    """
    out = []
    p = _cfg_path("dsh_home")
    if p:
        out.append(p)
    for env in ("DSH_HOME", "DSH_DATA_DIR"):
        v = os.getenv(env)
        if v:
            out.append(Path(v))
    v = os.getenv("DSH_PROFILE_DIR")
    if v:
        try:
            p = Path(v)
            if p.parent.name == "profiles":
                out.append(p.parent.parent)
        except Exception:
            pass
    home = Path.home()
    appdata = os.getenv("APPDATA")
    local = os.getenv("LOCALAPPDATA")
    out.append(home / ".dsh")
    if appdata:
        out.append(Path(appdata) / "dsh")
    if local:
        out.append(Path(local) / "dsh")
        out.append(Path(local) / "DeepSeekHarness" / "data")
    out.append(home / "DeepSeekHarness" / "data")
    for letter in ("D", "E", "C"):
        out.append(Path(f"{letter}:/DeepSeekHarness/data"))
    seen, uniq = set(), []
    for p in out:
        try:
            k = str(p).lower()
        except Exception:
            continue
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq

def _dsh_home_score(p):
    """目录「像不像」真的 DSH 数据目录。

    有账本 2 分（最权威）；有凭据 / profiles / settings.yaml / **非空的**会话缓存 各 1 分。
    会话缓存必须计分：没装 cost-meter 插件的机器上，一个 dsh home 可能只有
    storages/session_projcache —— 不计分的话它得 0 分，dsh_home() 的「显式指定」
    分支会把它当成无效目录跳过，于是用户明明填了路径，程序却去读机器上另一个
    dsh 目录的数据。
    但**空目录不算**：否则一个刚建好的空壳 session_projcache 会把真正有账本的
    目录挤掉（实测边界场景）。
    """
    score = 0
    try:
        if (p / "storages" / "cost-meter" / "ledger.json").exists():
            score += 2
        if (p / ".credentials.yaml").exists():
            score += 1
        if (p / "profiles").exists():
            score += 1
        if (p / "settings.yaml").exists():
            score += 1
        sc = p / "storages" / "session_projcache"
        agg = p / "storages" / "session_projcache.json"
        if sc.is_dir() and next(sc.glob("sessions/*.json"), None) is not None:
            score += 1
        elif agg.is_file() and agg.stat().st_size > 64:
            score += 1
    except Exception:
        pass
    return score

def _has_ledger(p):
    try:
        return (p / "storages" / "cost-meter" / "ledger.json").exists()
    except Exception:
        return False

def _dsh_explicit_homes():
    """用户**显式**指定的 dsh 目录（设置页 / 环境变量）。"""
    out = []
    p = _cfg_path("dsh_home")
    if p:
        out.append(p)
    for env in ("DSH_HOME", "DSH_DATA_DIR"):
        v = os.getenv(env)
        if v:
            out.append(Path(v))
    v = os.getenv("DSH_PROFILE_DIR")
    if v:
        try:
            pp = Path(v)
            if pp.parent.name == "profiles":
                out.append(pp.parent.parent)
        except Exception:
            pass
    seen, uniq = set(), []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq

def dsh_home():
    """实际使用的 DSH 数据目录。

    优先级规则（重要）：
      1) 用户**显式指定**的目录（设置页填的，或 DSH_HOME / DSH_DATA_DIR 环境变量）
         —— 只要里面确实有账本或凭据，就无条件采用，绝不被其它目录「打分盖过」；
      2) 否则在全部候选里挑「最像数据目录」的那个（有 ledger.json 得 2 分，
         有 .credentials.yaml / profiles / settings.yaml 各得 1 分）。

    第 2 条是为了解决一台机器上同时存在旧 CLI 的空壳 ~/.dsh 和 desktop 真实数据
    目录时挑错地方的问题；但它不能反过来压过用户的显式指定。
    """
    explicit = _dsh_explicit_homes()
    for p in explicit:
        try:
            if p.exists() and _dsh_home_score(p) > 0:
                return p
        except Exception:
            pass

    cands = _dsh_home_candidates()
    existing = []
    for p in cands:
        try:
            if p.exists():
                existing.append(p)
        except Exception:
            pass
    if not existing:
        return cands[0]
    # 分数相同时，**有账本的目录赢** —— 否则同分的诱饵目录（凭据+profiles+
    # settings.yaml = 3 分）只因为候选顺序靠前就会胜出，而它的账本根本不存在。
    best, best_score, best_ledger = existing[0], -1, False
    for p in existing:
        s = _dsh_home_score(p)
        has = _has_ledger(p)
        if s > best_score or (s == best_score and has and not best_ledger):
            best, best_score, best_ledger = p, s, has
    return best

def dsh_home_warning():
    """显式指定的 dsh 目录被忽略时，给一句人话说明。

    没有这句，用户会看到「设置里明明填了路径，面板却显示别的目录的数据」，
    而且完全不知道为什么 —— 实测边界场景。
    """
    try:
        chosen = dsh_home()
    except Exception:
        return ""
    for p in _dsh_explicit_homes():
        try:
            if p.exists() and p != chosen:
                return ("你指定的 dsh 目录 %s 里没有账本 / 凭据 / 非空会话缓存，"
                        "已改用 %s。若前者才是你的数据目录，请确认它下面有 "
                        "storages/cost-meter/ledger.json 或 .credentials.yaml。"
                        % (p, chosen))
        except Exception:
            continue
    return ""

def dsh_ledger_path():
    """账本 = <dsh_home()>/storages/cost-meter/ledger.json。

    **只认 dsh_home() 选中的那个目录**，不再去别的候选里「顺手找一个」。
    旧实现是「候选里任意一个存在就返回」，于是会出现「设置页显示用的是 A 目录、
    实际读的却是 B 目录的账本」这种自相矛盾。而 dsh_home() 的评分本身已经把
    「有账本」的目录排在「只有凭据」的前面，所以正常机器上不会挑错。
    选中的目录里确实没有账本时，上层会自动回退到会话缓存数据源。
    """
    return dsh_home() / "storages" / "cost-meter" / "ledger.json"

def _dsh_credentials_path():
    """凭据库 = <dsh_home()>/.credentials.yaml（新版仍在 home 根，格式未变：refs: {ENV名: 值}）

    同样只认 dsh_home()，保持与账本、设置页显示一致。
    """
    return dsh_home() / ".credentials.yaml"

def _dsh_provider_config_paths():
    """provider 定义来源：
    旧版 CLI：<home>/settings.yaml（providers 在根级或任意 llm-* 段内）
    新版 desktop（0.1.6+）：<home>/profiles/<profile>/cordis.patch.yml
        —— 顶层是「插件条目列表」，providers 位于 entry.config.providers

    只读「当前活动 profile」优先，并跳过备份/归档目录：历史版本会把所有 profile
    目录（含 desktop-backup-* 快照）一起读进来，再按字母序 setdefault 取第一个，
    结果可能用几个月前的旧配置去解析你的 Key。

    **只从 dsh_home() 选中的那个数据目录里读**：旧实现遍历全部候选目录，会把机器上
    其它 dsh 安装（旧 CLI 残留、另一个 desktop 数据目录）的 profile 一起合并进来，
    那些安装独有的 provider id 可能混进 keymap，把 Key 映射到错误的 provider。
    """
    home = dsh_home()
    out = [home / "settings.yaml"]
    active = os.getenv("DSH_PROFILE_DIR")
    active_name = ""
    if active:
        try:
            p = Path(active)
            if p.parent.name == "profiles":
                active_name = p.name
        except Exception:
            pass
    skip_words = ("backup", "bak", "old", "archive", "retired", "tmp", "test")
    prof = home / "profiles"
    try:
        if prof.exists():
            dirs = [d for d in sorted(prof.iterdir()) if d.is_dir()]
            dirs.sort(key=lambda d: (0 if d.name == active_name else 1, d.name))
            for d in dirs:
                if active_name and d.name != active_name:
                    continue
                if any(w in d.name.lower() for w in skip_words):
                    continue
                for pat in ("cordis.patch.yml", "cordis.yml"):
                    f = d / pat
                    try:
                        if f.exists() and f not in out:
                            out.append(f)
                    except Exception:
                        pass
    except Exception:
        pass
    return out

def _providers_from_yaml_doc(doc):
    """兼容三种形态提取 providers 字典"""
    provs = {}
    if isinstance(doc, dict):
        if isinstance(doc.get("providers"), dict):
            provs.update(doc["providers"])
        for v in doc.values():
            if isinstance(v, dict) and isinstance(v.get("providers"), dict):
                for pid, pv in v["providers"].items():
                    provs.setdefault(pid, pv)
    elif isinstance(doc, list):
        for e in doc:
            if not isinstance(e, dict):
                continue
            cfg = e.get("config") if isinstance(e.get("config"), dict) else e
            if isinstance(cfg.get("providers"), dict):
                for pid, pv in cfg["providers"].items():
                    provs.setdefault(pid, pv)
    return provs

_LEDGER_LOCK = threading.RLock()
_LEDGER_CACHE = {"path": "", "at": 0.0, "data": None, "warn": ""}

def load_dsh_ledger(force=False, max_age=10.0):
    """读 dsh cost-meter 账本；写入竞争时重试，失败回退上次缓存。返回 (data|None, warn|None)

    缓存整体一次性替换（不逐字段 update），避免并发下读到「新路径 + 旧数据」。
    """
    p = dsh_ledger_path()
    now = time.time()
    with _LEDGER_LOCK:
        snap = dict(_LEDGER_CACHE)
    if (not force and snap["data"] is not None
            and snap["path"] == str(p)
            and (now - snap["at"]) < max_age):
        return snap["data"], (snap["warn"] or None)
    last_err = None
    for _ in range(3):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = _safe_json_loads(f.read())
            if not isinstance(data, dict):
                raise ValueError("账本根节点不是 JSON 对象")
            with _LEDGER_LOCK:
                _LEDGER_CACHE.update({"path": str(p), "at": now, "data": data, "warn": ""})
            return data, None
        except FileNotFoundError:
            return None, f"未找到 dsh 账本：{p}\n可在「设置 → 数据源路径」里手动指定 DSH 数据目录。"
        except Exception as e:
            last_err = e
            time.sleep(0.2)
    with _LEDGER_LOCK:
        if _LEDGER_CACHE["data"] is not None and _LEDGER_CACHE["path"] == str(p):
            w = f"账本读取失败，使用缓存数据（{last_err}）"
            _LEDGER_CACHE["warn"] = w
            return _LEDGER_CACHE["data"], w
    return None, f"读取 dsh 账本失败：{last_err}（文件：{p}）"

_KEYMAP_CACHE = {"map": None, "at": 0.0, "err": ""}
_KEYMAP_TTL = 300.0
_KEYMAP_LOCK = threading.RLock()

_KEYMAP_BUILDING = [False]

def load_dsh_keymap(force=False):
    """{API Key 值: [providerId, ...]}，来自 $DSH_HOME/.credentials.yaml + provider 配置
    （旧版 CLI 在 settings.yaml；新版 desktop 在 profiles/<profile>/cordis.patch.yml）

    缓存带 TTL，且**失败不缓存** —— 旧实现把异常吞掉后把空表缓存一辈子，
    界面于是长期误报「该 Key 未在 dsh 配置中使用」。
    并发：query_all 的每个 worker 都会经 providers_for_key 走到这里，必须加锁，
    否则会重复解析 YAML 并争抢同一个 dict。
    """
    now = time.time()
    with _KEYMAP_LOCK:
        cached = _KEYMAP_CACHE["map"]
        cached_at = _KEYMAP_CACHE["at"]
    if (not force and cached is not None
            and (now - cached_at) < _KEYMAP_TTL):
        return cached
    if _KEYMAP_BUILDING[0]:
        # 防重入：构建过程中会读取账本/会话缓存，万一将来那条链路又绕回这里，
        # 直接返回空表，绝不允许无限递归。
        return {}
    _KEYMAP_BUILDING[0] = True
    try:
        with _KEYMAP_LOCK:
            return _build_dsh_keymap(now)
    finally:
        _KEYMAP_BUILDING[0] = False

def _build_dsh_keymap(now):
    keymap = {}
    if yaml is None:
        _KEYMAP_CACHE.update({"map": keymap, "at": now, "err": "未安装 PyYAML，无法读取 dsh 凭据"})
        return keymap
    err = ""
    try:
        refs = {}
        cp = _dsh_credentials_path()
        if cp.exists():
            with open(cp, "r", encoding="utf-8") as f:
                cd = yaml.safe_load(f) or {}
            for k, v in (cd.get("refs") or {}).items():
                if v: refs[str(k)] = str(v)
        provs = {}
        for sp in _dsh_provider_config_paths():
            try:
                if not sp.exists():
                    continue
                with open(sp, "r", encoding="utf-8") as f:
                    sd = yaml.safe_load(f)
            except Exception:
                continue
            for pid, pv in _providers_from_yaml_doc(sd).items():
                provs.setdefault(pid, pv)
        for pid, prov in (provs or {}).items():
            if not isinstance(prov, dict): continue
            key = refs.get(str(prov.get("apiKeyEnv") or ""))
            if not key and prov.get("apiKey"):
                key = str(prov.get("apiKey"))
            if key:
                keymap.setdefault(key, [])
                if str(pid) not in keymap[key]:
                    keymap[key].append(str(pid))
    except Exception as e:
        err = f"读取 dsh 凭据失败：{e}"
    # 补充：provider 配置里没有、由 dsh 插件注册的 provider（如 commandcode）。
    # 凭据名去掉 _API_KEY 后缀标准化后，在账本实际出现的 provider id 里做前缀匹配。
    # 例：COMMANDCODE_API_KEY → "commandcode" → 账本 commandcode:*
    #     DEEPSEEK_API_KEY   → "deepseek"   → 账本 deepseek-official:*
    try:
        if yaml is not None:
            cp = _dsh_credentials_path()
            if cp.exists():
                with open(cp, "r", encoding="utf-8") as f:
                    cd = yaml.safe_load(f) or {}
                refs = {str(k): str(v) for k, v in (cd.get("refs") or {}).items() if v}
                # 用 _load_dsh_any 而不是 load_dsh_ledger：没装 cost-meter 插件时
                # 账本不存在，但会话缓存里同样有 byProviderModel，照样能反查出
                # 「这个 Key 对应哪个 provider」。只用账本会让回退模式下所有 Key
                # 都匹配不到 provider，「仅当前 Key」永远是 0。
                data, _w, _fb = _load_dsh_any()
                if data is not None:
                    ledger_pids = set()
                    for d in _ledger_days(data).values():
                        for pm in (d.get("byProviderModel") or {}):
                            ledger_pids.add(pm.split(":", 1)[0])
                    for env, key in refs.items():
                        if not key or key in keymap:
                            continue
                        norm = env.lower()
                        for suffix in ("_api_key", "_key"):
                            if norm.endswith(suffix):
                                norm = norm[: -len(suffix)]
                                break
                        norm = norm.replace("_", "-")
                        for pid in ledger_pids:
                            if pid == norm or pid.startswith(norm + "-"):
                                keymap.setdefault(key, [])
                                if pid not in keymap[key]:
                                    keymap[key].append(pid)
    except Exception as e:
        if not err:
            err = f"从账本反查 provider 失败：{e}"
    # 只要拿到了任何映射就缓存；完全为空且出错时缓存一个很短的 TTL，便于稍后重试
    _KEYMAP_CACHE.update({"map": keymap, "at": now if keymap else now - _KEYMAP_TTL + 10.0, "err": err})
    return keymap

def keymap_error():
    return _KEYMAP_CACHE.get("err") or ""

def providers_for_key(apikey):
    return load_dsh_keymap().get((apikey or "").strip(), [])

def _day_keys_for(days_keys, time_key, utc=True):
    """按时间范围挑出要统计的「日」键。

    两个历史缺陷：
      1) dsh 账本 / Cline 官方的日桶是 **UTC 日期**，而这里原来用本机本地日期去比。
         在 UTC+8，本地 00:00~08:00 这段时间「今日」会挑到一个还没开始的桶 →
         整个面板显示 0；其余时间则静默漏掉当天前 8 小时。现在按数据源自身的
         时区口径取「今天」。
      2) 近 7/30 天原来取「有数据的最后 N 天」（ks[-n:]），中间有空洞时会悄悄
         跨到几个月前。现在按真正的自然日窗口过滤。
    """
    ks = sorted(days_keys)
    tk = str(time_key or "").strip()
    if tk.startswith("date:"):
        target = tk[5:].strip()
        return [k for k in ks if k == target]
    if re.match(r'^\d{4}-\d{2}-\d{2}$', tk):
        return [k for k in ks if k == tk]
    now = datetime.now(timezone.utc) if utc else datetime.now()
    if tk == "今日":
        today = now.strftime("%Y-%m-%d")
        return [k for k in ks if k == today]
    if tk == "本月":
        month_prefix = now.strftime("%Y-%m")
        return [k for k in ks if k.startswith(month_prefix)]
    n = {"近7天": 7, "近30天": 30}.get(tk)
    if n:
        # 必须有上界：旧实现改成 k >= lo 之后，时钟偏移产生的「未来日」桶会被算进来
        lo = (now.date() - timedelta(days=n - 1)).strftime("%Y-%m-%d")
        hi = now.strftime("%Y-%m-%d")
        return [k for k in ks if lo <= k <= hi]
    return ks

def _agg_by_provider_model(days, day_keys, providers=None):
    """聚合 days[day].byProviderModel；严格遵循官方文档与定价体系精准核算消费。"""
    provset = set(providers) if providers is not None else None
    per = {}
    totals = {"sessions": 0, "input": 0, "output": 0, "reasoning": 0,
              "cacheRead": 0, "cacheWrite": 0, "cost": 0.0}
    fx = get_fx()
    for dk in day_keys:
        d = days.get(dk) or {}
        bpm = d.get("byProviderModel") or {}
        if not isinstance(bpm, dict): continue
        for pm, v in bpm.items():
            if not isinstance(v, dict): continue
            if ":" in pm: pid, model = pm.split(":", 1)
            else: pid, model = pm, pm
            if provset is not None and pid not in provset: continue
            a = per.setdefault((pid, model), {"input":0,"output":0,"reasoning":0,
                                              "cacheRead":0,"cacheWrite":0,"calls":0,"cost":0.0,
                                              "cost_est": False})
            _inp = _safe_int(v.get("input"))
            _outp = _safe_int(v.get("output"))
            _rea = _safe_int(v.get("reasoning"))
            _cr = _safe_int(v.get("cacheRead"))
            _cw = _safe_int(v.get("cacheWrite"))
            a["input"]  += _inp
            a["output"] += _outp
            a["reasoning"] += _rea
            a["cacheRead"] += _cr
            a["cacheWrite"] += _cw
            a["calls"]  += _safe_int(v.get("calls"))
            v_cost = _safe_float(v.get("cost"))

            # 严格依据各官方渠道文档定价体系计算/校准单次模型费用：
            short_m = str(model or "").lower().split("/")[-1]
            pid_low = str(pid or "").lower()
            is_step = pid_low.startswith("step") or str(model or "").lower().startswith("step-") or short_m in STEPFUN_PRICES

            if is_step:
                # 1. 阶跃星辰（StepFun）：官方人民币定价（1M Credit = ¥1），按实时汇率折算美金
                sp = STEPFUN_PRICES.get(short_m) or STEPFUN_PRICES.get(str(model or "").lower())
                if sp:
                    miss_cny, hit_cny, outp_cny = sp
                    # 官方规则：缓存写入按未命中价计费（"包括首次写入缓存的内容"）
                    cny = ((_inp + _cw) * miss_cny + _cr * hit_cny
                           + (_outp + _rea) * outp_cny) / 1e6
                    v_cost = cny / fx
                    a["cost_est"] = True
            elif "deepseek" in short_m or "deepseek" in pid_low:
                # 2. DeepSeek 模型峰时精准分级计费（区分 v4.1-flash, v4-pro, v4-flash / vision）
                if "pro" in short_m:
                    # DeepSeek V4 Pro: OpenCode Go 为 $1.74/$3.48/$0.145；Command Code/官方为 $1.32/$3.96/$0.044
                    p_in, p_out, p_cr = (1.74, 3.48, 0.145) if pid_low.startswith("opencode-go") else (1.32, 3.96, 0.044)
                elif "v4-flash" in short_m or "vision" in short_m:
                    # DeepSeek V4 Flash / Vision: 官方定价 $0.14 / $0.28 / $0.028
                    p_in, p_out, p_cr = (0.14, 0.28, 0.028)
                else:
                    # 默认 DeepSeek V4.1 Flash 官方峰时价 $0.30 / $1.20 / $0.006
                    p_in, p_out, p_cr = (0.30, 1.20, 0.006)
                v_cost = (_inp * p_in + (_outp + _rea) * p_out + (_cr + _cw) * p_cr) / 1e6
            elif any(f in short_m for f in ("free", "omen-alpha", "ox-alpha")):
                # 3. 官方免费与开源福利模型，消费强制为 0
                v_cost = 0.0
            elif v_cost <= 0.0 and short_m in OPENCODE_GO_PRICES:
                # 4. 账本未定价模型按官方主表精准补齐估算
                p_in, p_out, p_cr = OPENCODE_GO_PRICES[short_m]
                v_cost = (_inp * p_in + (_outp + _rea) * p_out + (_cr + _cw) * p_cr) / 1e6
                a["cost_est"] = True

            a["cost"]   += v_cost
    for a in per.values():
        totals["sessions"] += a["calls"]; totals["input"] += a["input"]
        totals["output"] += a["output"]; totals["reasoning"] += a["reasoning"]
        totals["cacheRead"] += a["cacheRead"]; totals["cacheWrite"] += a["cacheWrite"]
        totals["cost"] += a["cost"]
    per_model = []
    for (pid, model), a in sorted(per.items(), key=lambda kv: -(kv[1]["input"]+kv[1]["output"]+kv[1]["reasoning"])):
        tok = a["input"] + a["output"] + a["reasoning"]
        per_model.append({
            "model": model, "provider": pid, "count": a["calls"],
            "input": a["input"], "output": a["output"], "reasoning": a["reasoning"],
            "cache": a["cacheRead"] + a["cacheWrite"],
            "cacheRead": a["cacheRead"], "cacheWrite": a["cacheWrite"],
            "tokens": tok, "tokens_with_cache": tok + a["cacheRead"] + a["cacheWrite"],
            "cost": a["cost"],
            "cost_est": a.get("cost_est", False),
        })
    return per_model, totals

def _ledger_provider_ids(days):
    ids = set()
    for d in days.values():
        bpm = d.get("byProviderModel") or {}
        if isinstance(bpm, dict):
            for pm in bpm:
                ids.add(pm.split(":", 1)[0])
    return ids

def _expand_providers(days, providers):
    """provider id 前缀扩展：opencode-go → opencode-go-completions / -responses（插件注册的变体）"""
    if not providers: return None
    out = set()
    for pid in _ledger_provider_ids(days):
        for p in providers:
            if pid == p or pid.startswith(p + "-"):
                out.add(pid); break
    return out

FX_DEFAULT = 6.6977          # 2026-09-20 当日汇率（离线兜底）
FX_URL = "https://open.er-api.com/v6/latest/USD"
_FX_MEM = {"fx": 0.0, "at": 0.0}
_FX_LOCK = threading.RLock()

def get_fx(force=False):
    """USD→CNY 汇率：实时接口（12 小时缓存）→ 本地缓存文件 → 默认值。
    汇率只影响费用的人民币折算显示，取不到不影响其他功能。"""
    now = time.time()
    with _FX_LOCK:
        mem = dict(_FX_MEM)
    if not force and mem["fx"] and (now - mem["at"]) < 12 * 3600:
        return mem["fx"]
    # 本地缓存文件
    cache_p = STORAGE_DIR / "fx_cache.json"
    try:
        if cache_p.exists() and not force:
            with open(cache_p, "r", encoding="utf-8") as f:
                c = _safe_json_loads(f.read())
            if isinstance(c, dict) and (now - _safe_float(c.get("at"))) < 12 * 3600:
                fx = _safe_float(c.get("fx"))
                if 5.0 < fx < 10.0:
                    # 继承缓存文件自身的写入时间，否则一个快过期的值会被续命成 12 小时
                    with _FX_LOCK:
                        _FX_MEM.update(fx=fx, at=_safe_float(c.get("at"), now))
                    return fx
    except Exception:
        pass
    # 实时接口
    try:
        req = urllib.request.Request(FX_URL, headers={"User-Agent": "coding-plan-checker/3.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            j = _safe_json_loads(r.read().decode("utf-8", errors="ignore"))
        fx = _safe_float((j.get("rates") or {}).get("CNY")) if isinstance(j, dict) else 0.0
        if 5.0 < fx < 10.0:
            with _FX_LOCK:
                _FX_MEM.update(fx=fx, at=now)
            try:
                with open(cache_p, "w", encoding="utf-8") as f:
                    json.dump({"fx": fx, "at": now}, f)
            except Exception:
                pass
            return fx
    except Exception:
        pass
    # 缓存过期但可用 → 仍先用缓存
    try:
        if cache_p.exists():
            with open(cache_p, "r", encoding="utf-8") as f:
                c = _safe_json_loads(f.read())
            fx = _safe_float(c.get("fx")) if isinstance(c, dict) else 0.0
            if 5.0 < fx < 10.0:
                with _FX_LOCK:
                    _FX_MEM.update(fx=fx, at=now)
                return fx
    except Exception:
        pass
    return FX_DEFAULT

# ---------- dsh 会话缓存回退数据源（未安装 dsh-cost-meter 插件时） ----------
# 背景：账本 ledger.json 是 dsh-cost-meter 插件写的。别人机器上没装这个插件时，
# 账本不存在 —— 但 dsh 自己仍然会在 storages/session_projcache/ 里缓存每个会话的
# 用量与费用（costUsage，字段结构与账本 byProviderModel 完全一致）。
# 这里直接聚合它，做到「没有费用插件也能用统计」，而不是干巴巴显示 0。
_DSH_SESSION_CACHE = {"at": 0.0, "home": "", "data": None, "warn": "", "meta": {}}
_DSH_SESSION_LOCK = threading.RLock()
_DSH_SESSION_MAX_FILES = 6000
_DSH_SESSION_MAX_AGE = 60.0

def cost_meter_installed():
    """dsh-cost-meter 插件是否安装（新版 desktop 装在 profiles/<profile>/node_modules）。"""
    home = dsh_home()
    try:
        prof = home / "profiles"
        if prof.exists():
            for d in prof.iterdir():
                try:
                    if (d / "node_modules" / "dsh-cost-meter").exists():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    for cand in (home / "dsh-plugins" / "dsh-cost-meter",
                 home / "dsh-plugins" / "cost-meter"):
        try:
            if cand.exists():
                return True
        except Exception:
            pass
    return False

def _session_day(created_at_ms):
    """会话创建时间 → 日期键。

    用**本地日期**：这样才和 dsh-cost-meter 账本的日键口径一致（插件用
    localDayKey 本地字段）。旧实现用 UTC，会让「装了插件」和「没装插件」两种
    情况下同一个 dsh 源的按日口径不一样。
    """
    try:
        ts = _safe_float(created_at_ms) / 1000.0
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return ""

def _read_session_record(path):
    """读单个会话缓存文件，返回 record 字典（带 mtime 缓存）。"""
    try:
        st = path.stat()
        sig = (st.st_mtime, st.st_size)
    except Exception:
        return None
    key = str(path)
    with _DSH_SESSION_LOCK:
        cached = _DSH_SESSION_CACHE.setdefault("_files", {}).get(key)
        if cached and cached[0] == sig:
            return cached[1]
    rec = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = _safe_json_loads(f.read())
        if isinstance(doc, dict):
            rec = doc.get("record") if isinstance(doc.get("record"), dict) else doc
    except Exception:
        rec = None
    with _DSH_SESSION_LOCK:
        files = _DSH_SESSION_CACHE.setdefault("_files", {})
        if len(files) > _DSH_SESSION_MAX_FILES * 2:
            files.clear()
        files[key] = (sig, rec)
    return rec

def _collect_session_records(home):
    """返回 (records, warn)。优先用逐会话目录；没有再退回聚合大文件。"""
    sess_dir = home / "storages" / "session_projcache" / "sessions"
    agg_file = home / "storages" / "session_projcache.json"
    records, warn = [], ""
    if sess_dir.exists():
        try:
            paths = []
            for p in sess_dir.glob("*.json"):
                paths.append(p)
                if len(paths) >= _DSH_SESSION_MAX_FILES:
                    warn = "会话缓存文件超过 %d 个，只统计了前 %d 个" % (_DSH_SESSION_MAX_FILES, _DSH_SESSION_MAX_FILES)
                    break
            for p in paths:
                rec = _read_session_record(p)
                if isinstance(rec, dict):
                    records.append(rec)
        except Exception as e:
            warn = "读取 dsh 会话缓存目录失败：%s" % e
    if not records and agg_file.exists():
        try:
            with open(agg_file, "r", encoding="utf-8") as f:
                doc = _safe_json_loads(f.read())
            tables = (doc or {}).get("tables") if isinstance(doc, dict) else None
            sess = (tables or {}).get("sessions") if isinstance(tables, dict) else None
            if isinstance(sess, dict):
                for rec in sess.values():
                    if isinstance(rec, dict):
                        records.append(rec)
        except Exception as e:
            warn = "读取 dsh 会话缓存汇总文件失败：%s" % e
    return records, warn

def load_dsh_session_usage(force=False, max_age=_DSH_SESSION_MAX_AGE):
    """把 dsh 会话缓存聚合成与 ledger.json 的 days 同构的结构。

    返回 (data|None, warn)。data = {"days": {...}, "config": {...}}，可直接喂给
    _agg_by_provider_model，所以 dsh_usage() 的上层逻辑一行都不用改。
    """
    home = dsh_home()
    now = time.time()
    with _DSH_SESSION_LOCK:
        if (not force and _DSH_SESSION_CACHE["data"] is not None
                and _DSH_SESSION_CACHE["home"] == str(home)
                and (now - _DSH_SESSION_CACHE["at"]) < max_age):
            return _DSH_SESSION_CACHE["data"], _DSH_SESSION_CACHE["warn"]

    records, warn = _collect_session_records(home)
    if not records:
        msg = ("未找到 dsh 账本，也没找到 dsh 会话缓存。"
               "若你的 dsh 数据目录不在默认位置，请在「设置 → 数据源路径」里指定。")
        return None, (warn + "；" + msg) if warn else msg

    days = {}
    n_cost = 0
    undated = 0
    for rec in records:
        rows = rec.get("rows") if isinstance(rec.get("rows"), dict) else {}
        cu = (rows.get("costUsage") or {}) if isinstance(rows.get("costUsage"), dict) else {}
        val = cu.get("val") if isinstance(cu.get("val"), dict) else None
        if not val:
            continue
        bpm = val.get("byProviderModel")
        if not isinstance(bpm, dict) or not bpm:
            continue
        ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
        day = _session_day(ident.get("createdAt"))
        if not day:
            undated += 1
            continue
        d = days.setdefault(day, {"date": day, "input": 0, "output": 0, "cacheRead": 0,
                                  "cacheWrite": 0, "reasoning": 0, "calls": 0, "cost": 0.0,
                                  "apiCost": 0.0, "byProviderModel": {}})
        for pm, v in bpm.items():
            if not isinstance(v, dict):
                continue
            a = d["byProviderModel"].setdefault(pm, {"input": 0, "output": 0, "cacheRead": 0,
                                                     "cacheWrite": 0, "reasoning": 0,
                                                     "calls": 0, "cost": 0.0, "apiCost": 0.0})
            for fld in ("input", "output", "cacheRead", "cacheWrite", "reasoning"):
                a[fld] += _safe_int(v.get(fld))
            c = _safe_float(v.get("cost"))
            a["cost"] += c
            a["apiCost"] += c
            a["calls"] += _safe_int(v.get("calls")) or 1
        n_cost += 1
    if not days:
        return None, "dsh 会话缓存里没有可用的用量记录（costUsage 为空）"

    for d in days.values():
        for fld in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "calls"):
            d[fld] = sum(a[fld] for a in d["byProviderModel"].values())
        d["cost"] = round(sum(a["cost"] for a in d["byProviderModel"].values()), 8)
        d["apiCost"] = d["cost"]

    data = {"version": 1, "days": days, "config": {},
            "_source": "session_projcache"}
    notes = []
    if warn:
        notes.append(warn)
    notes.append("未检测到 dsh-cost-meter 插件，已回退到 dsh 会话缓存统计"
                 "（%d 个会话，其中 %d 个含费用）" % (len(records), n_cost))
    notes.append("按日归属以「会话创建日」为准：跨天会话会整段计入创建日，"
                 "安装 dsh-cost-meter 插件后按日数据才精确")
    if undated:
        notes.append("%d 个会话没有创建时间，未纳入按日统计" % undated)
    warn = "；".join(notes)

    with _DSH_SESSION_LOCK:
        _DSH_SESSION_CACHE.update({"at": now, "home": str(home), "data": data,
                                   "warn": warn,
                                   "meta": {"records": len(records), "with_cost": n_cost,
                                            "undated": undated}})
    return data, warn


def _ledger_days(data):
    """安全取出账本的 days 字典。

    账本是外部文件（用户可能手改过、或插件版本变化），days 完全可能是 list /
    None / 字符串。旧实现写的是 `data.get("days") or {}` —— 只有 None/空字典会
    被兜住，一个 list 会在下游 .keys() 处抛 AttributeError，把整次查询打断。
    """
    if not isinstance(data, dict):
        return {}
    d = data.get("days")
    return d if isinstance(d, dict) else {}


def _ledger_day_convention(data):
    """判定账本日键用的是**本地日期**还是 **UTC 日期**。

    为什么必须判：写账本的 dsh-cost-meter 插件用的是 `localDayKey()`（`new Date()`
    的本地字段，源码 store.js:275-279，注释明写「本地日期键(宿主机时区)」），所以
    正常是本地口径。但历史数据可能被迁移/回填过，硬编码任何一种口径，在另一种
    口径的账本上都会静默错数（「今日显示昨天」或「今日显示 0」）。

    判定方法（用账本自证，不靠猜）：每天桶里存有当天的 `sessions[].at`（毫秒）。
    看「当天最早那次会话」落在哪：
      · 落在该日**本地**午夜后的头一小时 → 桶边界是本地午夜 → 本地口径；
      · 落在该日 **UTC** 午夜后的头一小时（UTC+8 下即本地 08:00 前后）→ UTC 口径。
    取票数多的一方；两边都没票时按插件源码的实际行为回落到「本地」。
    """
    days = _ledger_days(data)
    if not days:
        return "local"
    local_votes = utc_votes = 0
    for k, v in days.items():
        if not isinstance(v, dict):
            continue
        ss = v.get("sessions")
        if not isinstance(ss, list) or not ss:
            continue
        ats = []
        for s in ss:
            if isinstance(s, dict) and s.get("at"):
                try:
                    ats.append(float(s["at"]))
                except Exception:
                    pass
        if not ats:
            continue
        try:
            base = datetime.strptime(k, "%Y-%m-%d")
        except Exception:
            continue
        first = min(ats) / 1000.0
        # 本地午夜
        loc_mid = base.timestamp()
        # 该日的 UTC 午夜，换算成本地时间戳
        utc_mid = base.replace(tzinfo=timezone.utc).timestamp()
        if 0 <= (first - loc_mid) < 3600:
            local_votes += 1
        elif 0 <= (first - utc_mid) < 3600:
            utc_votes += 1
    if local_votes == 0 and utc_votes == 0:
        return "local"
    return "local" if local_votes >= utc_votes else "utc"


def _load_dsh_any(force=False):
    """账本优先，不存在或读不出来则回退到会话缓存。

    返回 (data|None, note, used_fallback)。note 在成功时是「提示」，失败时是「错误」。
    """
    data, warn = load_dsh_ledger(force=force)
    if data is not None:
        return data, warn, False
    try:
        ledger_exists = dsh_ledger_path().exists()
    except Exception:
        ledger_exists = False
    fb, fb_warn = load_dsh_session_usage(force=force)
    if fb is not None:
        if ledger_exists and warn:
            # 账本文件**在**，只是读不出来（截断 / 半截写入 / 权限）。
            # 这时真正的错误是账本那条，绝不能被「未找到账本」顶掉 ——
            # 否则界面会说「路径不对」，用户按提示去改路径也修不好。
            note = "%s；已临时改用 dsh 会话缓存统计（按日归属以会话创建日为准）" % warn
        else:
            note = fb_warn
        return fb, note, True
    if ledger_exists:
        return None, warn, False
    return None, (fb_warn or warn), False


def dsh_usage(providers=None, time_key="全部", force=False):
    """dsh 账本用量。providers=None → 全部渠道。

    账本不存在时（最常见的原因：没装 dsh-cost-meter 插件）自动回退到
    dsh 自己的会话缓存，保证「没有费用插件也能用统计」。
    """
    data, warn, fallback = _load_dsh_any(force=force)
    if data is None:
        return {"error": warn, "totals": None, "per_model": [],
                "source": "dsh", "hint": warn,
                "db": str(dsh_ledger_path()), "daily_series": []}
    days = _ledger_days(data)
    # 日键口径由账本自身的时间戳判定（见 _ledger_day_convention），不再硬编码：
    # dsh-cost-meter 用的是本地日期键，但历史账本可能被迁移过。
    day_conv = _ledger_day_convention(data)
    day_keys = _day_keys_for(list(days.keys()), time_key, utc=(day_conv == "utc"))
    if providers is not None and not providers:
        # 明确给了空列表 = 该 Key 未映射到任何 provider，结果必须为 0 而不是"全部"
        zero = {"sessions":0,"input":0,"output":0,"reasoning":0,"cacheRead":0,"cacheWrite":0,
                "cost":0.0,"cache":0,"tokens":0,"tokens_with_cache":0}
        return {"per_model": [], "totals": zero, "error": warn, "source": "dsh",
                "db": str(dsh_ledger_path()), "days": len(day_keys),
                "hint": "该 Key 未在 dsh 配置中使用（统计为 0）", "daily_series": []}
    per_model, totals = _agg_by_provider_model(days, day_keys, _expand_providers(days, providers))
    # 附加官网参考价（dsh 账本价格表，零额外 IO）
    for row in per_model:
        try:
            row["price"] = official_price(data, row.get("provider"), row.get("model"))
        except Exception:
            row["price"] = None
    # dsh 账本未定价的模型（阶跃星辰套餐模型，Credit 池计费 → 账本 cost=0）：
    # 按官方定价估算费用（¥/1M tokens → 折 USD），让费用列不再是 0
    fx = get_fx()
    est_total = 0.0
    for row in per_model:
        if row.get("cost"):
            continue
        short_m = str(row.get("model") or "").lower().split("/")[-1]
        price = STEPFUN_PRICES.get(short_m) or STEPFUN_PRICES.get(row.get("model"))
        if not price:
            continue
        miss, hit, outp = price
        cny = (row.get("input", 0) * miss + row.get("cache", 0) * hit
               + (row.get("output", 0) + row.get("reasoning", 0)) * outp) / 1e6
        est = cny / fx
        if est > 0:
            row["cost"] = round(est, 6)
            row["cost_est"] = True
            est_total += est
    totals = dict(totals)
    totals["fx"] = fx
    if est_total > 0:
        totals["cost"] = totals.get("cost", 0.0) + est_total
    totals["cost_estimated"] = any(r.get("cost_est") for r in per_model)
    totals["tokens"] = totals["input"] + totals["output"] + totals["reasoning"]
    totals["tokens_with_cache"] = totals["tokens"] + totals["cacheRead"] + totals["cacheWrite"]
    totals["cache"] = totals["cacheRead"] + totals["cacheWrite"]

    # 生成每日趋势时序 (用于近期趋势折线图)
    daily_series = []
    trend_day_keys = sorted(days.keys())[-14:] if (not time_key or time_key in ["全部", "近30天", "本月"]) else sorted(day_keys)
    exp_provs = _expand_providers(days, providers)
    for dk in trend_day_keys:
        _, day_tot = _agg_by_provider_model(days, [dk], exp_provs)
        d_tokens = (day_tot.get("input", 0) + day_tot.get("output", 0) + day_tot.get("reasoning", 0) + day_tot.get("cacheRead", 0) + day_tot.get("cacheWrite", 0))
        d_cost = day_tot.get("cost", 0.0)
        d_calls = day_tot.get("sessions", 0)
        short_d = dk[5:] if len(dk) >= 10 else dk
        daily_series.append({
            "date": dk,
            "label": short_d,
            "tokens": d_tokens,
            "cost": round(d_cost, 4),
            "calls": d_calls
        })

    out = {"per_model": per_model, "totals": totals, "error": None,
           "source": ("dsh-sessions" if fallback else "dsh"),
           "db": str(dsh_ledger_path()), "days": len(day_keys), "daily_series": daily_series}
    if fallback:
        # 注意：这是「提示」不是「错误」。前端只要看到 error 就会清空面板，
        # 把回退说明塞进 error 会让数据明明算出来了却显示空白。
        out["fallback"] = True
        out["db"] = str(dsh_home() / "storages" / "session_projcache")
        out["hint"] = (str(out.get("hint")) + "；" + str(warn)) if out.get("hint") else str(warn)
    elif warn:
        out["error"] = warn
    # 用户显式指定的目录被忽略时，必须说出来（否则「我填了路径却显示别的目录」无从解释）
    _hw = dsh_home_warning()
    if _hw:
        out["hint"] = (str(out.get("hint")) + "；" + _hw) if out.get("hint") else _hw
    # 账本新鲜度：dsh-cost-meter 插件停跑时账本会「静默停更」——数字不会变，但看不出原因。
    # 这里显式提示最后记录日，避免把陈旧数据误当成实时数据。
    # （回退模式下不适用：会话缓存的按日归属本来就不是精确的，已在 hint 里说明。）
    if not fallback:
        try:
            _all_days = sorted(days.keys())
            if _all_days:
                last_day = _all_days[-1]
                today = datetime.now().strftime("%Y-%m-%d")
                if last_day < today:
                    stale = f"账本最后记录 {last_day}（今日 {today} 无新数据）：dsh-cost-meter 插件可能未运行"
                    out["stale"] = True
                    out["last_day"] = last_day
                    out["hint"] = (str(out.get("hint")) + "；" + stale) if out.get("hint") else stale
        except Exception:
            pass
    if providers is not None and not per_model and not warn:
        if not providers:
            _msg = "该 Key 未在 dsh 配置中使用（统计为 0）"
        else:
            _msg = "该 Key 在所选时间范围内没有记录"
        out["hint"] = (str(out.get("hint")) + "；" + _msg) if out.get("hint") else _msg
    return out

def stepfun_estimate(providers=None, month=None):
    """StepFun Step Plan 月池估算：tokens × 官方价格 = Credit（1M Credit = ¥1，月末清零）"""
    data, warn, _fb = _load_dsh_any()
    if data is None:
        return {"error": warn, "credit_used": 0, "credit_used_m": 0.0,
                "unpriced_tokens": 0, "per_model": [], "source": "dsh"}
    month = month or datetime.now().strftime("%Y-%m")
    days = _ledger_days(data)
    day_keys = [k for k in sorted(days.keys()) if k.startswith(month)]
    if providers is not None and not providers:
        return {"credit_used": 0, "credit_used_m": 0.0, "unpriced_tokens": 0,
                "per_model": [], "month": month, "error": warn, "source": "dsh",
                "hint": "该 Key 未在 dsh 配置中使用（月池估算为 0）"}
    per_model, _totals = _agg_by_provider_model(days, day_keys, _expand_providers(days, providers))
    credit = 0.0; unpriced = 0; detail = []
    for row in per_model:
        tok_bill = row["input"] + row["cache"] + row["output"] + row["reasoning"]
        short_m = str(row["model"]).lower().split("/")[-1]
        price = STEPFUN_PRICES.get(short_m) or STEPFUN_PRICES.get(row["model"])
        if not price:
            unpriced += tok_bill
            detail.append({"model": row["model"], "credit": None, "tokens": tok_bill})
            continue
        miss, hit, outp = price
        # 官方计费规则（StepFun 定价说明原文）：
        #   「未命中缓存的输入 token（**包括首次写入缓存的内容**）按未命中价格计费；
        #     已命中缓存的输入 token 按缓存命中价格计费；
        #     模型推理过程和最终回复产生的 token 均按输出价格计费。」
        # 所以缓存写入要按 miss 价，而不是 hit 价。
        cw = _safe_int(row.get("cacheWrite"))
        cr = _safe_int(row.get("cacheRead")) if row.get("cacheRead") is not None else _safe_int(row.get("cache"))
        c = ((row["input"] + cw) * miss + cr * hit
             + (row["output"] + row["reasoning"]) * outp)   # 绝对 Credit 数
        credit += c
        detail.append({"model": row["model"], "credit": c, "tokens": tok_bill})
    hint = None
    if not per_model:
        hint = "该 Key 在 dsh 账本中本月没有使用记录"
    elif credit <= 0 and unpriced > 0:
        hint = "本月有 " + format_tokens(unpriced) + " tokens，但所用模型不在价格表中，Credit 估算为 0"
    if _fb:
        # 回退说明是「提示」不是「错误」：数据算出来了就别让前端当成失败
        hint = (str(hint) + "；" + str(warn)) if hint else str(warn)
        warn = None
    return {"credit_used": credit, "credit_used_m": credit / 1e6, "unpriced_tokens": unpriced,
            "per_model": detail, "month": month, "error": warn, "source": "dsh", "hint": hint}

def stepfun_today_tokens(providers=None):
    d = dsh_usage(providers, "今日")
    t = d.get("totals") or {}
    return {"tokens": t.get("tokens_with_cache", 0), "calls": t.get("sessions", 0),
            "error": d.get("error"), "hint": d.get("hint")}

def fetch_stepfun_account(apikey, timeout=12):
    req = urllib.request.Request(STEPFUN_ACCOUNT_URL, method="GET")
    req.add_header("Authorization", f"Bearer {apikey}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "coding-plan-checker/3.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return json.loads(body), None
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="ignore")
        except Exception: pass
        if e.code == 401: return None, f"401 未授权：Key 无效\n{body[:300]}"
        if e.code == 404: return None, f"404 接口不存在\n{body[:200]}"
        return None, f"HTTP {e.code}: {body[:400]}"
    except urllib.error.URLError as e: return None, f"网络错误: {e.reason}"
    except Exception as e: return None, f"异常: {e}"

# ---------- Command Code 额度查询 ----------
def _cc_plan_info(plan_id):
    """planId → (名称, 月额度$)，最长前缀匹配（与官方 CLI 一致）"""
    pid = str(plan_id or "").lower().replace("_", "-")
    best = None
    for prefix, info in COMMANDCODE_PLANS.items():
        if pid.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, info)
    return best[1] if best else (None, None)

def _cc_get(url, apikey, timeout):
    """单个端点 GET；返回 (record|None, status|None)。网络异常向上抛。"""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {apikey}")
    req.add_header("Accept", "application/json")
    req.add_header("x-command-code-version", COMMANDCODE_CLI_VERSION)
    req.add_header("x-cli-environment", "production")
    req.add_header("User-Agent", "coding-plan-checker/3.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return json.loads(body), r.status
    except urllib.error.HTTPError as e:
        return None, e.code

def _cc_num(v, default=0.0):
    """把任意输入转成有限浮点数。NaN / ±Infinity / 超大数字一律回落到 default。

    旧实现只过滤 NaN，放过了 Infinity —— 下游 int() 会抛 OverflowError 直接把
    整次查询打断。
    """
    try:
        f = float(v)
    except Exception:
        return default
    return f if math.isfinite(f) else default

def _cc_window(block):
    b = block if isinstance(block, dict) else {}
    return {
        "used": _cc_num(b.get("used")),
        "cap": _cc_num(b.get("cap")),
        "exceeded": b.get("exceeded") is True,
        "resetAt": int(_cc_num(b.get("resetAt"))),
    }

def _norm_price_entry(p):
    """统一两种价格 schema → {input, output, cacheHit}（单位 $/1M tokens）"""
    if not isinstance(p, dict):
        return None
    # 优先提取 peak 峰时定价（如果存在）
    target = p.get("peak") if isinstance(p.get("peak"), dict) else p
    inp = target.get("input"); out = target.get("output"); ch = target.get("cacheHit")
    if inp is None and "cacheMiss" in target: inp = target.get("cacheMiss")   # 全局表口径
    if out is None: out = target.get("output")
    if ch is None: ch = target.get("cachedInput")
    def f(v):
        try:
            x = float(v); return x if x == x else None
        except Exception: return None
    inp, out, ch = f(inp), f(out), f(ch)
    if inp is None and out is None: return None
    return {"input": inp if inp is not None else 0.0,
            "output": out if out is not None else 0.0,
            "cacheHit": ch if ch is not None else 0.0}

def _price_source(p):
    """价格的来源说明；官方未公布(unpriced)的返回 None（不编造）"""
    if not isinstance(p, dict) or p.get("unpriced") is True:
        return None
    if isinstance(p.get("peak"), dict):
        return "官方峰时价"
    url = p.get("sourceUrl")
    if url: return "来源 " + str(url)
    return "官方价"

def official_price(data, provider, model):
    """从权威官方定价主表与 dsh 账本价格表取模型的官网参考价。
    优先级：
    1) 阶跃星辰（step* 渠道或 step-* 模型）：返回 STEPFUN_PRICES 官方人民币及汇率折算
    2) Command Code（commandcode*）：返回 COMMANDCODE_PRICES 官方价
    3) OpenCode Go（opencode-go*）：返回 OPENCODE_GO_PRICES 官方价
    4) dsh 账本价格表（全局 models 表 → 厂商表）
    5) 兜底 OPENCODE_GO_PRICES 全量官方参考表
    返回 {input, output, cacheHit, source}|None"""
    prov_str = str(provider or "").lower()
    mid = str(model or "")
    m_low = mid.lower()
    short = m_low.split("/")[-1]

    # 1) 阶跃星辰（StepFun）
    if prov_str.startswith("step") or m_low.startswith("step-") or short in STEPFUN_PRICES:
        sp = STEPFUN_PRICES.get(short) or STEPFUN_PRICES.get(m_low)
        if sp:
            miss_cny, hit_cny, outp_cny = sp
            fx = get_fx()
            return {
                "input": round(miss_cny / fx, 4),
                "output": round(outp_cny / fx, 4),
                "cacheHit": round(hit_cny / fx, 4),
                "cny": {"input": miss_cny, "output": outp_cny, "cacheHit": hit_cny},
                "source": f"阶跃星辰官方 (¥{miss_cny}/¥{outp_cny})"
            }

    # 2) Command Code
    if prov_str.startswith("commandcode"):
        cp = COMMANDCODE_PRICES.get(m_low) or COMMANDCODE_PRICES.get(short) or OPENCODE_GO_PRICES.get(short)
        if cp:
            inp, outp, ch = cp
            src = "Command Code 官方免费" if inp == 0 and outp == 0 else "Command Code 官方价"
            return {"input": inp, "output": outp, "cacheHit": ch, "source": src}

    # 3) OpenCode Go
    if prov_str.startswith("opencode-go"):
        op = OPENCODE_GO_PRICES.get(short) or OPENCODE_GO_PRICES.get(m_low)
        if op:
            inp, outp, ch = op
            src = "OpenCode Go 官方免费" if inp == 0 and outp == 0 else "OpenCode Go 官方价"
            return {"input": inp, "output": outp, "cacheHit": ch, "source": src}

    # 4) dsh 账本价格表
    prices = ((data or {}).get("config") or {}).get("prices") or {}
    models = prices.get("models") or {}
    provs = prices.get("providers") or {}
    for key in (mid, short):
        p = models.get(key)
        if p:
            np = _norm_price_entry(p); src = _price_source(p)
            if np and src: return dict(np, source=src)
    maker = mid.split("/")[0] if "/" in mid else None
    if maker and isinstance(provs.get(maker), dict):
        p = (provs[maker].get("models") or {}).get(short)
        if p:
            np = _norm_price_entry(p); src = _price_source(p)
            if np and src: return dict(np, source=src)
    for pname, ptbl in provs.items():
        if pname == "opencode-go" or pname == provider: continue
        if not isinstance(ptbl, dict): continue
        p = (ptbl.get("models") or {}).get(short)
        if p:
            np = _norm_price_entry(p); src = _price_source(p)
            if np and src: return dict(np, source=src)

    # 5) 权威主表兜底
    if short in OPENCODE_GO_PRICES:
        inp, outp, ch = OPENCODE_GO_PRICES[short]
        src = "官方免费" if inp == 0 and outp == 0 else "官方参考价"
        return {"input": inp, "output": outp, "cacheHit": ch, "source": src}

    return None

def fetch_commandcode_usage(apikey, timeout=12):
    """Command Code 额度：whoami + usage/summary + billing/credits + subscriptions。
    每个端点独立降级（单个失败不影响其余），全部失败才整体报错。"""
    base = COMMANDCODE_BASE
    failures, statuses = [], []
    def get(path):
        try:
            rec, st = _cc_get(base + path, apikey, timeout)
        except urllib.error.URLError as e:
            failures.append(f"{path}: 网络错误 {e.reason}"); statuses.append(None); return None
        except Exception as e:
            failures.append(f"{path}: {e}"); statuses.append(None); return None
        if rec is None:
            failures.append(f"{path}: HTTP {st}"); statuses.append(st); return None
        return rec

    report = {"failures": failures}
    who = get("/alpha/whoami")
    if isinstance(who, dict) and isinstance(who.get("user"), dict):
        u = who["user"]
        report["account"] = {"id": str(u.get("id") or ""), "name": str(u.get("name") or ""),
                             "email": str(u.get("email") or "")}
    usage = get("/alpha/usage/summary")
    if isinstance(usage, dict):
        report["usage"] = {
            "totalCount": int(_cc_num(usage.get("totalCount"))),
            "completedCount": int(_cc_num(usage.get("completedCount"))),
            "failedCount": int(_cc_num(usage.get("failedCount"))),
            "successRate": _cc_num(usage.get("successRate")),
            "totalCost": _cc_num(usage.get("totalCost")),
            "totalTokensIn": int(_cc_num(usage.get("totalTokensIn"))),
            "totalTokensOut": int(_cc_num(usage.get("totalTokensOut"))),
            "totalCredits": _cc_num(usage.get("totalCredits")),
            "periodBasis": str(usage.get("periodBasis") or "billing-period"),
        }
    credits = get("/alpha/billing/credits")
    if isinstance(credits, dict):
        cd = credits.get("credits") if isinstance(credits.get("credits"), dict) else {}
        wl = credits.get("windowLimits") if isinstance(credits.get("windowLimits"), dict) else {}
        report["credits"] = {
            "monthlyCredits": _cc_num(cd.get("monthlyCredits")),
            "purchasedCredits": _cc_num(cd.get("purchasedCredits")),
            "freeCredits": _cc_num(cd.get("freeCredits")),
            "fiveHour": _cc_window(wl.get("fiveHour")),
            "weekly": _cc_window(wl.get("weekly")),
        }
    subs = get("/alpha/billing/subscriptions")
    plan_id = None
    if isinstance(subs, dict) and isinstance(subs.get("data"), dict):
        plan_id = subs["data"].get("planId")
    if plan_id is None and isinstance(credits, dict) and isinstance(credits.get("credits"), dict):
        plan_id = credits["credits"].get("planId")
    if plan_id:
        name, monthly_cap = _cc_plan_info(plan_id)
        sd = subs.get("data") if isinstance(subs, dict) and isinstance(subs.get("data"), dict) else {}
        period_end = sd.get("currentPeriodEnd")
        pe_ms = 0
        if period_end:
            try:
                pe_ms = int(datetime.fromisoformat(str(period_end).replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                pe_ms = 0
        report["plan"] = {"planId": str(plan_id), "name": name or str(plan_id),
                          "monthlyCap": monthly_cap, "status": str(sd.get("status") or ""),
                          "periodEndMs": pe_ms}
    # 全失败归类（与官方插件一致）
    got_any = report.get("account") or report.get("usage") or report.get("credits") or report.get("plan")
    if not got_any:
        codes = [s for s in statuses if s is not None]
        if len(codes) >= 4 and all(c == 401 for c in codes):
            return None, "401 未授权：Key 无效或已过期"
        if len(codes) >= 4 and all(c >= 500 for c in codes):
            return None, "Command Code 服务暂不可用（5xx），稍后再试"
        return None, "无法连接 Command Code 服务：检查网络。" + ("；".join(failures[:2]))
    return report, None

# ---------- Cline（cline.bot / Cline Pass）----------
def _cline_get(url, apikey, timeout=12):
    """单个端点 GET；返回 (record|None, status|None)。HTTP 错误不抛，返回状态码。"""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {apikey}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "coding-plan-checker/3.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return json.loads(body), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except urllib.error.URLError:
        return None, None
    except Exception:
        return None, None

def fetch_cline_account(apikey, timeout=12):
    """Cline Pass 账户 / 订阅 / 余额。三个端点各自独立降级，全部失败才整体报错。

    注意：Cline **没有公开的用量或配额查询接口**（实测 /usage、/credits、/limits、
    /quota、/billing 等一律 404），所以"用了多少"只能取自 dsh 账本的 cline provider；
    这里拿到的 periodStart/End 用来界定「本期」，caps 是官方给的推理上限阈值。
    余额字段官方未标注量纲（既非美元也非分数），按原值展示并标注 unit=unknown。"""
    base = CLINE_BASE
    failures = []
    report = {"failures": failures}

    me, st = _cline_get(base + CLINE_ME_PATH, apikey, timeout)
    if isinstance(me, dict) and isinstance(me.get("data"), dict):
        u = me["data"]
        report["account"] = {
            "id": str(u.get("id") or ""),
            "email": str(u.get("email") or ""),
            "name": str(u.get("displayName") or ""),
            "createdAt": str(u.get("createdAt") or ""),
        }
    else:
        failures.append("/users/me: HTTP %s" % st)

    plan_rec, st = _cline_get(base + CLINE_PLAN_PATH, apikey, timeout)
    if isinstance(plan_rec, dict) and isinstance(plan_rec.get("data"), dict):
        pd = plan_rec["data"]
        pl = pd.get("plan") if isinstance(pd.get("plan"), dict) else {}
        ents = pl.get("entitlements") if isinstance(pl.get("entitlements"), dict) else {}
        ent = ents.get("cline_pass") if isinstance(ents.get("cline_pass"), dict) else {}
        caps = ent.get("inferenceCapThreshold") if isinstance(ent.get("inferenceCapThreshold"), dict) else {}
        report["plan"] = {
            "name": str(pl.get("displayName") or pl.get("name") or ""),
            "interval": str(pl.get("interval") or ""),
            "priceCents": _cc_num(pl.get("pricePerSeatCents")),
            "active": pl.get("isActive") is True,
            "passEnabled": ent.get("enabled") is True,
            "caps": {k: _cc_num(v) for k, v in (caps or {}).items()},
            "periodStart": str(pd.get("currentPeriodStart") or ""),
            "periodEnd": str(pd.get("currentPeriodEnd") or ""),
            "cancelAt": str(pd.get("cancelAt") or ""),
            "canceledAt": str(pd.get("canceledAt") or ""),
        }
    else:
        failures.append("/users/me/plan: HTTP %s" % st)

    uid = (report.get("account") or {}).get("id")
    if uid:
        bal, st = _cline_get(base + CLINE_BALANCE_PATH.format(uid=uid), apikey, timeout)
        if isinstance(bal, dict) and isinstance(bal.get("data"), dict):
            raw = _cc_num(bal["data"].get("balance"))
            report["balance"] = {
                "raw": raw,
                "credits": round(raw / CLINE_BALANCE_DIVISOR, 4),
                "unit": "credits",
            }
        else:
            failures.append("/users/{id}/balance: HTTP %s" % st)

    # 官方额度窗口：5小时 / 每周 / 每月 的已用百分比 + 重置时间
    lim_rec, st = _cline_get(base + CLINE_LIMITS_PATH, apikey, timeout)
    windows = {}
    if isinstance(lim_rec, dict) and isinstance(lim_rec.get("data"), dict):
        norm = {"five_hour": "rolling", "fivehour": "rolling", "5h": "rolling",
                "weekly": "weekly", "monthly": "monthly"}
        for item in (lim_rec["data"].get("limits") or []):
            if not isinstance(item, dict):
                continue
            t = str(item.get("type") or "").strip().lower()
            if not t:
                continue
            windows[norm.get(t, t)] = {
                "percent": _cc_num(item.get("percentUsed")),
                "resetsAt": str(item.get("resetsAt") or ""),
                "raw_type": t,
            }
        report["windows"] = windows
    else:
        failures.append("/users/me/plan/usage-limits: HTTP %s" % st)

    if not (report.get("account") or report.get("plan")):
        if st == 401:
            return None, "401 未授权：Cline Key 无效或已过期"
        return None, "无法连接 Cline 服务：检查网络。" + ("；".join(failures[:2]))
    return report, None

_CLINE_UID_CACHE = {"key": "", "uid": "", "at": 0.0}

def cline_user_id(apikey, force=False):
    """取 Cline 用户 id（按天/逐条用量接口都需要它）。1 小时缓存。"""
    now = time.time()
    with _CLINE_CACHE_LOCK:
        c = dict(_CLINE_UID_CACHE)
    if (not force) and c["uid"] and c["key"] == apikey and (now - c["at"]) < 3600:
        return c["uid"]
    rec, _st = _cline_get(CLINE_BASE + CLINE_ME_PATH, apikey)
    uid = ""
    if isinstance(rec, dict) and isinstance(rec.get("data"), dict):
        uid = str(rec["data"].get("id") or "")
    if uid:
        with _CLINE_CACHE_LOCK:
            _CLINE_UID_CACHE.update(key=apikey, uid=uid, at=now)
    return uid

def fetch_cline_usage_daily(apikey, uid, start_date, end_date, timeout=15):
    """官方按天/按模型汇总（服务端聚合、无分页）。返回 (rows, status)。"""
    url = (CLINE_BASE + CLINE_DAILY_PATH.format(uid=uid)
           + "?startDate=%s&endDate=%s" % (start_date, end_date))
    rec, st = _cline_get(url, apikey, timeout)
    rows = []
    if isinstance(rec, dict) and isinstance(rec.get("data"), dict):
        for it in (rec["data"].get("items") or []):
            if not isinstance(it, dict):
                continue
            p = int(_cc_num(it.get("promptTokens")))
            c = int(_cc_num(it.get("completionTokens")))
            rows.append({
                "date": str(it.get("date") or ""),
                "model": str(it.get("aiModelName") or "(未知)"),
                "prompt": p, "completion": c, "total": p + c,
                "cost": _cc_num(it.get("costUsd")) / CLINE_COST_DIVISOR,
            })
    return rows, st

def _cline_local_day(ts):
    """Cline 官方 createdAt（UTC ISO 串）→ **本地**日期键。

    官方接口按 UTC 给时间戳，但面板要和 dsh（本地日键）、Grok（本地日键）合并成
    一张趋势图，三个源必须同口径；否则同一根柱子会把 dsh 的
    「09-26 08:00 → 09-27 08:00」和 Cline 的「09-26 00:00 → 24:00」相加。
    """
    s = str(ts or "").strip()
    if not s:
        return ""
    try:
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return s[:10]


def fetch_cline_records(apikey, uid, max_pages=CLINE_MAX_PAGES, timeout=15):
    """官方逐条调用记录（每页 ≤200，nextToken 翻页，新→旧）。
    含 promptTokens / completionTokens / cachedTokens / costUsd / createdAt。
    返回 (records, truncated, failed)：failed=True 表示翻页中途失败、合计不完整
    （旧实现把「中途失败」和「翻到底」混为一谈，不完整数据被当成完整数据展示）。"""
    out, cursor, pages = [], None, 0
    failed = False
    while pages < max_pages:
        url = CLINE_BASE + CLINE_RECORDS_PATH.format(uid=uid) + "?limit=%d" % CLINE_PAGE_LIMIT
        if cursor:
            url += "&cursor=" + urllib.parse.quote(str(cursor))
        rec, st = _cline_get(url, apikey, timeout)
        if not isinstance(rec, dict) or not isinstance(rec.get("data"), dict):
            # 区分「翻到底了」和「中途失败了」：旧实现一律 break，
            # 于是不完整的合计被当成完整数据展示给用户。
            if cursor or pages == 0:
                failed = True
            break
        items = rec["data"].get("items") or []
        out.extend([x for x in items if isinstance(x, dict)])
        pages += 1
        cursor = rec["data"].get("nextToken")
        if not cursor or not items:
            break
    return out, bool(cursor), failed

def cline_usage_official(apikey, uid, time_key="全部", force=False, max_pages=CLINE_MAX_PAGES):
    """用**官方接口**重建「用量与模型分析」面板需要的数据（与账本结果同构）。

    与本地账本的关键差别：
      · 数据源 = Cline 服务端，包含**所有客户端**的用量，与官网图表一致；
      · 分日采用官方 date 字段（createdAt 的 UTC 日期），与官网图表同口径；
      · 含 cachedTokens 明细，所以缓存率列是真的。
    逐条接口每页上限 200 条，超过 CLINE_MAX_PAGES 页时标记 truncated。"""
    ck = _CLINE_USAGE_CACHE
    # max_pages 也要进缓存键：否则一次窄窗口调用会把结果污染给默认调用。
    # 注意写入处也必须存 max_pages，否则这里永远拿不到匹配值、缓存永不命中。
    with _CLINE_CACHE_LOCK:
        hit = (not force) and ck["data"] is not None and ck["uid"] == uid \
            and ck["time_key"] == time_key and ck.get("max_pages") == max_pages \
            and (time.time() - ck["at"]) < 60.0
        cached = copy.deepcopy(ck["data"]) if hit else None
    if hit:
        return cached

    recs, truncated, failed = fetch_cline_records(apikey, uid, max_pages=max_pages)
    daily = {}
    for r in recs:
        day = _cline_local_day(r.get("createdAt"))
        if not day:
            continue
        prompt = int(_cc_num(r.get("promptTokens")))
        cache = int(_cc_num(r.get("cachedTokens")))
        outp = int(_cc_num(r.get("completionTokens")))
        cost = _cc_num(r.get("costUsd")) / CLINE_COST_DIVISOR
        uncached = max(0, prompt - cache)
        d = daily.setdefault(day, {"tokens": 0, "tokens_with_cache": 0, "cost": 0.0, "calls": 0})
        d["tokens"] += uncached + outp
        d["tokens_with_cache"] += prompt + outp
        d["cost"] += cost
        d["calls"] += 1
        d["_models"] = None

    day_keys = set(_day_keys_for(list(daily.keys()), time_key))
    per_model = []
    totals = {"sessions": 0, "input": 0, "output": 0, "reasoning": 0,
              "cache": 0, "cacheRead": 0, "cacheWrite": 0,
              "cost": 0.0, "tokens": 0, "tokens_with_cache": 0, "fx": get_fx()}
    per = {}
    for r in recs:
        if _cline_local_day(r.get("createdAt")) not in day_keys:
            continue
        prompt = int(_cc_num(r.get("promptTokens")))
        cache = int(_cc_num(r.get("cachedTokens")))
        outp = int(_cc_num(r.get("completionTokens")))
        cost = _cc_num(r.get("costUsd")) / CLINE_COST_DIVISOR
        model = str(r.get("aiModelName") or "(未知)")
        uncached = max(0, prompt - cache)
        a = per.setdefault(model, {"input": 0, "output": 0, "cache": 0, "calls": 0, "cost": 0.0})
        a["input"] += uncached
        a["output"] += outp
        a["cache"] += cache
        a["calls"] += 1
        a["cost"] += cost
        totals["sessions"] += 1
        totals["input"] += uncached
        totals["output"] += outp
        totals["cache"] += cache
        totals["cacheRead"] += cache
        totals["cost"] += cost
    for model, a in sorted(per.items(), key=lambda kv: -(kv[1]["input"] + kv[1]["output"] + kv[1]["cache"])):
        tok = a["input"] + a["output"]
        per_model.append({
            "model": model, "provider": "cline", "count": a["calls"],
            "input": a["input"], "output": a["output"], "reasoning": 0,
            "cache": a["cache"], "tokens": tok, "tokens_with_cache": tok + a["cache"],
            "cost": a["cost"], "cost_est": False,
            "price": {"input": None, "output": None, "cacheHit": None, "source": "Cline 官方用量接口"},
        })
    totals["tokens"] = totals["input"] + totals["output"]
    totals["tokens_with_cache"] = totals["tokens"] + totals["cache"]
    totals["cost"] = round(totals["cost"], 4)

    daily_series = []
    for dk in sorted(day_keys):
        d = daily.get(dk) or {}
        daily_series.append({"date": dk, "label": dk[5:] if len(dk) >= 10 else dk,
                             "tokens": d.get("tokens_with_cache", 0),
                             "cost": round(d.get("cost", 0.0), 4),
                             "calls": d.get("calls", 0)})
    out = {
        "per_model": per_model, "totals": totals, "daily_series": daily_series,
        "error": None, "source": "cline-official", "db": "api.cline.bot/users/%s/usages" % uid,
        "days": len(day_keys),
        "hint": ("官网口径（含所有客户端）；明细已取最近 %d 条%s%s"
                 % (len(recs),
                    "，更早的记录未纳入" if truncated else "",
                    "；⚠ 翻页中途失败，以上合计不完整" if failed else "")),
        "truncated": truncated,
        "partial": bool(failed),
        "all_daily": [{"date": k, "tokens": v["tokens_with_cache"], "cost": round(v["cost"], 4), "calls": v["calls"]}
                      for k, v in sorted(daily.items())],
    }
    with _CLINE_CACHE_LOCK:
        _CLINE_USAGE_CACHE.update(at=time.time(), uid=uid, time_key=time_key,
                                  max_pages=max_pages, data=out)
    return out

# ---------- Grok Build 本地免密查询 ----------
def _grok_home_candidates():
    """Grok 数据目录候选。历史版本把 ~/.grok 写死，profile 被搬走就永远显示 0。"""
    out = []
    p = _cfg_path("grok_home")
    if p:
        out.append(p)
    for env in ("GROK_HOME", "GROK_CONFIG_DIR"):
        v = os.getenv(env)
        if v:
            out.append(Path(v))
    out.append(Path.home() / ".grok")
    appdata = os.getenv("APPDATA")
    if appdata:
        out.append(Path(appdata) / "grok")
    return out

def grok_home():
    cands = _grok_home_candidates()
    for p in cands:
        try:
            if (p / "sessions").exists() or (p / "auth.json").exists():
                return p
        except Exception:
            pass
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return cands[-1]

def _grok_sessions_dir():
    return grok_home() / "sessions"

GROK_SESSIONS_DIR = _grok_sessions_dir()
_GROK_QUOTA_CACHE = {"at": 0.0, "data": None}
_GROK_QUOTA_LOCK = threading.RLock()

def fetch_grok_quota(force=False):
    """从 ~/.grok 获取 Grok Heavy/SuperGrok 官方周额度、使用百分比与重置倒计时。
    优先通过 ~/.grok/auth.json 中的 Bearer Token 请求官方 cli-chat-proxy /billing 端点；
    若 Token 过期则尝试使用 refresh_token 刷新；
    若网络不可用则自动读取 ~/.grok/logs/unified.jsonl 最近一次记录，百分之百高可用。"""
    now = time.time()
    with _GROK_QUOTA_LOCK:
        qc = dict(_GROK_QUOTA_CACHE)
    if not force and qc["data"] and (now - qc["at"]) < 60.0:
        return qc["data"]

    home = grok_home()
    auth_file = home / "auth.json"
    settings_file = home / "settings_cache.json"
    log_file = home / "logs" / "unified.jsonl"

    tier = "SuperGrok Heavy"
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                sc = _safe_json_loads(f.read())
            if isinstance(sc, dict):
                payload_str = sc.get("payload")
                if payload_str:
                    payload = _safe_json_loads(payload_str) if isinstance(payload_str, str) else payload_str
                    if isinstance(payload, dict):
                        settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
                        tier = settings.get("subscription_tier_display") or tier
        except Exception:
            pass

    token = None
    refresh_tok = None
    client_id = "b1a00492-073a-47ea-816f-4c329264a828"
    if auth_file.exists():
        try:
            with open(auth_file, "r", encoding="utf-8") as f:
                ad = _safe_json_loads(f.read())
            if isinstance(ad, dict):
                for v in ad.values():
                    if isinstance(v, dict) and "key" in v:
                        token = v.get("key")
                        refresh_tok = v.get("refresh_token")
                        client_id = v.get("oidc_client_id") or client_id
                        break
        except Exception:
            pass

    result = None
    if token:
        try:
            url = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "grok-shell/1.0.34"
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = _safe_json_loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401 and refresh_tok:
                try:
                    ref_url = "https://auth.x.ai/oauth2/token"
                    data = urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": refresh_tok
                    }).encode("utf-8")
                    ref_req = urllib.request.Request(ref_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
                    with urllib.request.urlopen(ref_req, timeout=5) as ref_resp:
                        ref_data = json.loads(ref_resp.read().decode("utf-8"))
                        new_tok = ref_data.get("access_token")
                        if new_tok:
                            token = new_tok
                            req2 = urllib.request.Request(url, headers={"Authorization": f"Bearer {new_tok}", "User-Agent": "grok-shell/1.0.34"})
                            with urllib.request.urlopen(req2, timeout=5) as resp2:
                                result = json.loads(resp2.read().decode("utf-8"))
                except Exception:
                    pass
        except Exception:
            pass

    # 若在线查询失败，降级回退读取 unified.jsonl 最近一次记录
    src_kind = "api" if result else ""
    log_ts = ""
    if not result and log_file.exists():
        # 只读文件尾部：Grok CLI 的 unified.jsonl 可以长到几百 MB，
        # 旧实现 f.readlines() 会把整个文件读进内存并阻塞查询线程。
        for line in _tail_lines(log_file, max_bytes=2 * 1024 * 1024, max_lines=20000):
            try:
                if "billing: fetched credits config" not in line:
                    continue
                ld = _safe_json_loads(line)
                if not isinstance(ld, dict):
                    continue
                ctx = ld.get("ctx") or {}
                cfg = ctx.get("config")
                if isinstance(cfg, dict):
                    result = {"config": cfg}
                    src_kind = "log"
                    log_ts = str(ld.get("ts") or "")
                    if ctx.get("subscriptionTier"):
                        tier = ctx["subscriptionTier"]
                    break
            except Exception:
                continue

    if isinstance(result, dict) and isinstance(result.get("config"), dict):
        cfg = result["config"]
        # 字段缺失 / 类型异常时绝不能默默当成 0% —— 那会显示成「剩余 100%」
        used_pct = _finite(cfg.get("creditUsagePercent"))
        if used_pct is None:
            return {"tier": tier, "success": False,
                    "error": "额度接口返回的数据里没有可用的 creditUsagePercent 字段"}
        used_pct = max(0.0, min(100.0, used_pct))
        rem_pct = round(max(0.0, 100.0 - used_pct), 1)
        cur_period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
        p_start = cur_period.get("start") or cfg.get("billingPeriodStart")
        p_end = cur_period.get("end") or cfg.get("billingPeriodEnd")
        p_type = str(cur_period.get("type") or "USAGE_PERIOD_TYPE_WEEKLY")
        p_label = "每周额度" if "WEEKLY" in p_type else ("每月额度" if "MONTHLY" in p_type else "周期额度")

        res_data = {
            "tier": tier,
            "used_percent": used_pct,
            "remaining_percent": rem_pct,
            "period_type": p_type,
            "period_label": p_label,
            "period_start": p_start,
            "period_end": p_end,
            "is_unified_billing": cfg.get("isUnifiedBillingUser", True),
            "success": True,
            "source": src_kind or "api",
            "log_ts": log_ts,
            "stale": (src_kind == "log"),
            "stale_hint": ("额度取自本机日志回退（%s），非实时接口：Grok 登录可能已失效，"
                           "请运行 `grok login` 重新登录" % log_ts[:19]) if src_kind == "log" else ""
        }
        with _GROK_QUOTA_LOCK:
            _GROK_QUOTA_CACHE["data"] = res_data
            _GROK_QUOTA_CACHE["at"] = now
        return res_data

    # 失败：不返回上一次的「成功」结果冒充新数据；只回上次数据并明确标记为陈旧
    fallback = {"tier": tier, "success": False,
                "error": "无法获取 Grok 额度：本机未找到 ~/.grok/auth.json，或登录已失效。"}
    prev = qc.get("data")
    if isinstance(prev, dict) and prev.get("success"):
        stale = dict(prev)
        stale["stale"] = True
        stale["stale_hint"] = "本次刷新失败，以下为上次成功获取的额度（非实时）"
        with _GROK_QUOTA_LOCK:
            _GROK_QUOTA_CACHE["at"] = now
        return stale
    with _GROK_QUOTA_LOCK:
        _GROK_QUOTA_CACHE["data"] = fallback
        _GROK_QUOTA_CACHE["at"] = now
    return fallback

def _parse_iso_to_local_date(iso_str):
    """将 Grok 的 UTC ISO 时间戳转换为本机本地时区日期与完整时间"""
    try:
        clean_str = (iso_str or "").strip()
        if not clean_str:
            return "", ""
        if clean_str.endswith('Z'):
            clean_str = clean_str[:-1] + '+00:00'
        if '.' in clean_str:
            head, tail = clean_str.split('.', 1)
            tz_idx = -1
            for tz_char in ['+', '-']:
                idx = tail.rfind(tz_char)
                if idx > tz_idx:
                    tz_idx = idx
            if tz_idx != -1:
                frac = tail[:tz_idx][:6]
                tz_part = tail[tz_idx:]
                clean_str = f"{head}.{frac}{tz_part}"
            else:
                clean_str = f"{head}.{tail[:6]}"
        dt_obj = datetime.fromisoformat(clean_str)
        local_dt = dt_obj.astimezone()
        return local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (iso_str or "")[:10], (iso_str or "")[:19].replace('T', ' ')

_GROK_CACHE = {
    "at": 0.0,
    "daily": {},
    "turns": [],
    "sessions_count": 0,
    "last_active": ""
}

_GROK_SCAN_CACHE = {}          # {path: (mtime, size, parsed_dict)}
_GROK_SCAN_LOCK = threading.RLock()
_GROK_SCAN_MAX_FILES = 4000    # 硬上限，防止 sessions 目录异常膨胀时卡死查询

def _grok_usage_files():
    """列出 <grok_home>/sessions 下的 usage.json，并**按 sessionId 去重**。
    同一会话被 resume/fork 到不同工作目录时，磁盘上可能留下多份同 sessionId 的
    usage.json，直接 rglob 逐份累加会重复计数。这里每个 sessionId 只保留最完整的
    一份（按 session.totalTokens 比较，缺失时退回累加 turns 的 totalTokens）。
    无 sessionId 的文件无法判定归属，按原样各自计入。

    性能：按 (mtime, size) 缓存解析结果，并且只扫前 N 个文件。
    旧实现每次查询都对整个目录树 rglob + 全量 json.load，且 _query_grok_backend
    用的是 force=True —— 目录越大点一次查询越慢，没有上限。
    """
    out, best = [], {}
    try:
        root = GROK_SESSIONS_DIR
        if not root.exists():
            return out
        paths = []
        for p in root.rglob("usage.json"):
            paths.append(p)
            if len(paths) >= _GROK_SCAN_MAX_FILES:
                break
    except Exception:
        return out
    for p in paths:
        try:
            st = p.stat()
            key = str(p)
            sig = (st.st_mtime, st.st_size)
        except Exception:
            continue
        d = None
        with _GROK_SCAN_LOCK:
            cached = _GROK_SCAN_CACHE.get(key)
            if cached and cached[0] == sig:
                d = cached[1]
        if d is None:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = _safe_json_loads(f.read())
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            with _GROK_SCAN_LOCK:
                if len(_GROK_SCAN_CACHE) > _GROK_SCAN_MAX_FILES * 2:
                    _GROK_SCAN_CACHE.clear()
                _GROK_SCAN_CACHE[key] = (sig, d)
        sid = str(d.get("sessionId") or "").strip()
        if not sid:
            out.append((p, d))
            continue
        sess = d.get("session") if isinstance(d.get("session"), dict) else {}
        score = _safe_int(sess.get("totalTokens"))
        if not score:
            score = 0
            for t in (d.get("turns") or []):
                if isinstance(t, dict):
                    score += _safe_int(t.get("totalTokens"))
        prev = best.get(sid)
        if prev is None or score > prev[0]:
            best[sid] = (score, p, d)
    for _s, p, d in best.values():
        out.append((p, d))
    return out

def _grok_archive_path():
    return get_storage_dir() / "grok_usage_archive.json"

_GROK_ARCHIVE_LOCK = threading.RLock()

def _grok_archive_load():
    """本地 Grok 用量档案：按 turn 去重保存曾扫到过的全部用量。
    目的：~/.grok/sessions 一旦被清理/迁移，历史用量不再永久丢失。

    注意：解析失败时**不能**当作空档案返回 —— 那会让下一次保存把整份历史覆盖掉，
    恰好毁掉这个档案存在的意义。这里改为保留原文件并明确报错。
    """
    p = _grok_archive_path()
    if not p.exists():
        return {"version": 1, "turns": {}}
    try:
        d = _safe_json_loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("turns"), dict):
            return d
        raise ValueError("档案结构不符合预期")
    except Exception as e:
        try:
            shutil.copy2(p, p.with_name(p.name + ".corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
        except Exception:
            pass
        log_line("Grok 用量档案解析失败，已保留原文件并跳过本次合并：%s" % e)
        return {"version": 1, "turns": {}, "_load_failed": True}

def _grok_archive_save(d):
    try:
        p = _grok_archive_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".%d.tmp" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_json_safe(d), f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception as e:
        log_line("Grok 用量档案写入失败：%s" % e)

def _grok_archive_merge(turns_list, daily):
    """把本次扫到的 turn 并入档案，并把档案里"本机已不存在"的 turn 补回来。
    返回 (补回的 turn 数, 档案累计会话数, 档案累计 turn 数)。"""
    with _GROK_ARCHIVE_LOCK:
        return _grok_archive_merge_locked(turns_list, daily)

def _grok_archive_merge_locked(turns_list, daily):
    arc = _grok_archive_load()
    if arc.get("_load_failed"):
        return 0, 0, 0
    store = arc["turns"]
    changed = False
    live_keys = set()
    for t in turns_list:
        k = t.get("k")
        if not k:
            continue
        live_keys.add(k)
        rec = {kk: vv for kk, vv in t.items() if kk != "k"}
        if store.get(k) != rec:
            store[k] = rec
            changed = True
    restored = 0
    for k, rec in list(store.items()):
        if k in live_keys or not isinstance(rec, dict):
            continue
        try:
            t = dict(rec)
            dt = t.get("date") or ""
            if not dt:
                continue
            turns_list.append(t)
            daily.setdefault(dt, {"tokens": 0, "tokens_with_cache": 0, "cost": 0.0,
                                  "calls": 0, "input": 0, "output": 0, "cache": 0, "reasoning": 0})
            daily[dt]["tokens"] += _safe_int(t.get("tokens"))
            daily[dt]["tokens_with_cache"] += _safe_int(t.get("tokens_with_cache"))
            daily[dt]["input"] += _safe_int(t.get("input"))
            daily[dt]["output"] += _safe_int(t.get("output"))
            daily[dt]["cache"] += _safe_int(t.get("cache"))
            daily[dt]["reasoning"] += _safe_int(t.get("reasoning"))
            daily[dt]["cost"] += _safe_float(t.get("cost"))
            daily[dt]["calls"] += _safe_int(t.get("calls"))
            restored += 1
        except Exception:
            continue
    if changed:
        _grok_archive_save(arc)
    return restored, len({k.split("|")[0] for k in store}), len(store)

_GROK_LOCK = threading.RLock()

def load_grok_data(force=False):
    now = time.time()
    with _GROK_LOCK:
        snap_at = _GROK_CACHE["at"]
    if not force and snap_at and (now - snap_at) < 5.0:
        return _GROK_CACHE
    with _GROK_LOCK:
        return _load_grok_data_locked(force, now)

def _load_grok_data_locked(force, now):
    daily = {}
    turns_list = []
    sessions_count = 0
    last_active = ""

    if GROK_SESSIONS_DIR.exists():
        for p, d in _grok_usage_files():
            sessions_count += 1
            try:
                sess_info = d.get("session") if isinstance(d.get("session"), dict) else {}
                fallback_mid = sess_info.get("primaryModelId") or "grok-4.7-build"
                turns = d.get("turns") or []
                
                # 若 turns 为空但 session 摘要存在，直接将 session 计入
                if not turns and sess_info:
                    raw_inp = _safe_int(sess_info.get("inputTokens"))
                    cache = _safe_int(sess_info.get("cachedReadTokens"))
                    uncached_inp = max(0, raw_inp - cache)
                    outp = _safe_int(sess_info.get("outputTokens"))
                    rea = _safe_int(sess_info.get("reasoningTokens"))
                    ticks = _safe_int(sess_info.get("costUsdTicks"))
                    cost = ticks / 1e10
                    raw_calls = sess_info.get("modelCalls")
                    calls = _safe_int(raw_calls) if raw_calls is not None else (1 if (raw_inp > 0 or outp > 0) else 0)
                    if raw_inp > 0 or outp > 0 or cache > 0 or calls > 0:
                        # 没有 updatedAt 就跳过 last_active，不要用「现在」冒充真实活跃时间
                        up_at = d.get("updatedAt") or ""
                        if up_at:
                            dt, dt_full = _parse_iso_to_local_date(up_at)
                        else:
                            dt, dt_full = "", ""
                        if dt_full and dt_full > last_active:
                            last_active = dt_full
                        outp_net = max(0, outp - rea)
                        tok = uncached_inp + outp_net + rea
                        tot = tok + cache
                        mid = sess_info.get("primaryModelId") or fallback_mid
                        turns_list.append({
                            "date": dt, "model": mid, "input": uncached_inp,
                            "output": outp_net, "cache": cache, "reasoning": rea,
                            "tokens": tok, "tokens_with_cache": tot, "cost": cost, "calls": calls,
                            "k": str(p) + "|session"
                        })
                        daily.setdefault(dt, {
                            "tokens": 0, "tokens_with_cache": 0, "cost": 0.0,
                            "calls": 0, "input": 0, "output": 0, "cache": 0, "reasoning": 0
                        })
                        daily[dt]["tokens"] += tok
                        daily[dt]["tokens_with_cache"] += tot
                        daily[dt]["input"] += uncached_inp
                        daily[dt]["output"] += outp_net
                        daily[dt]["cache"] += cache
                        daily[dt]["reasoning"] += rea
                        daily[dt]["cost"] += cost
                        daily[dt]["calls"] += calls

                for t in turns:
                    ea = t.get("endedAt") or ""
                    if not ea:
                        continue
                    dt, dt_full = _parse_iso_to_local_date(ea)
                    if dt_full > last_active:
                        last_active = dt_full

                    raw_inp = _safe_int(t.get("inputTokens"))
                    cache = _safe_int(t.get("cachedReadTokens"))
                    # Grok 的 inputTokens 包含已命中的 cachedReadTokens，未命中输入需减去 cache
                    uncached_inp = max(0, raw_inp - cache)
                    outp = _safe_int(t.get("outputTokens"))
                    rea = _safe_int(t.get("reasoningTokens"))
                    ticks = _safe_int(t.get("costUsdTicks"))
                    cost = ticks / 1e10

                    # 准确提取 modelCalls：Grok CLI 会为工具/命令执行生成空 turn（modelCalls: 0, inputTokens: 0, outputTokens: 0）
                    # 纯工具执行轮次绝不能虚增模型调用次数！
                    raw_calls = t.get("modelCalls")
                    calls = _safe_int(raw_calls) if raw_calls is not None else (1 if (raw_inp > 0 or outp > 0) else 0)

                    # 过滤纯工具执行/Shell空轮次（0 Token 且 0 调用），绝不虚增幽灵模型与请求次数
                    if raw_inp == 0 and outp == 0 and cache == 0 and rea == 0 and calls == 0:
                        continue

                    # 修正重复计数：Grok 的 outputTokens 已包含 reasoningTokens
                    outp_net = max(0, outp - rea)
                    tok = uncached_inp + outp_net + rea      # == uncached_inp + outp
                    tot = tok + cache
                    mid = t.get("primaryModelId") or fallback_mid

                    turns_list.append({
                        "date": dt,
                        "model": mid,
                        "input": uncached_inp,
                        "output": outp_net,
                        "cache": cache,
                        "reasoning": rea,
                        "tokens": tok,
                        "tokens_with_cache": tot,
                        "cost": cost,
                        "calls": calls,
                        "k": str(p) + "|" + ea
                    })

                    daily.setdefault(dt, {
                        "tokens": 0, "tokens_with_cache": 0, "cost": 0.0,
                        "calls": 0, "input": 0, "output": 0, "cache": 0, "reasoning": 0
                    })
                    daily[dt]["tokens"] += tok
                    daily[dt]["tokens_with_cache"] += tot
                    daily[dt]["input"] += uncached_inp
                    daily[dt]["output"] += outp_net
                    daily[dt]["cache"] += cache
                    daily[dt]["reasoning"] += rea
                    daily[dt]["cost"] += cost
                    daily[dt]["calls"] += calls
            except Exception as e:
                # 单个会话文件解析失败只跳过这一份，并留下日志；旧实现是静默 pass，
                # 出问题时用户和开发者都完全看不到。
                log_line("Grok 会话文件解析失败，已跳过 %s：%s" % (p, e))
                continue

    # 与本地档案合并：会话库被清理/迁移后，历史用量仍能从档案补回（只增不减）
    try:
        _restored, _arc_sess, _arc_turns = _grok_archive_merge(turns_list, daily)
    except Exception as e:
        log_line("Grok 用量档案合并失败：%s" % e)
        _restored, _arc_sess, _arc_turns = 0, 0, 0

    # 整份替换而不是逐字段 update：读者要么看到旧的、要么看到新的，
    # 不会读到「新 daily + 旧 turns」这种半成品（账本缓存用的是同一套做法）。
    global _GROK_CACHE
    _GROK_CACHE = {
        "at": now,
        "daily": daily,
        "turns": turns_list,
        "sessions_count": sessions_count,
        "last_active": last_active,
        "archive_restored": _restored,
        "archive_sessions": _arc_sess,
        "archive_turns": _arc_turns
    }
    return _GROK_CACHE

def grok_usage(time_key="全部", force=False, fx=None):
    if fx is None:
        fx = get_fx()
    gdata = load_grok_data(force=force)
    daily = gdata["daily"]
    # Grok 的日键是**本地日期**（_parse_iso_to_local_date 的结果），所以这里不能用 UTC 口径
    day_keys = set(_day_keys_for(list(daily.keys()), time_key, utc=False))

    totals = {
        "sessions": 0, "input": 0, "output": 0, "reasoning": 0,
        "cache": 0, "cacheRead": 0, "cacheWrite": 0, "cost": 0.0,
        "tokens": 0, "tokens_with_cache": 0, "fx": fx
    }
    per_model_map = {}

    for t in gdata["turns"]:
        if t["date"] not in day_keys:
            continue
        m = t["model"]
        per_model_map.setdefault(m, {
            "model": m, "provider": "grok-build", "count": 0,
            "input": 0, "output": 0, "reasoning": 0, "cache": 0,
            "tokens": 0, "tokens_with_cache": 0, "cost": 0.0,
            "price": {"input": 2.0, "output": 6.0, "cacheHit": 0.5, "source": "xAI 官方"}
        })
        info = per_model_map[m]
        info["count"] += t["calls"]
        info["input"] += t["input"]
        info["output"] += t["output"]
        info["reasoning"] += t["reasoning"]
        info["cache"] += t["cache"]
        info["tokens"] += t["tokens"]
        info["tokens_with_cache"] += t["tokens_with_cache"]
        info["cost"] += t["cost"]

        totals["sessions"] += t["calls"]
        totals["input"] += t["input"]
        totals["output"] += t["output"]
        totals["reasoning"] += t["reasoning"]
        totals["cache"] += t["cache"]
        totals["cacheRead"] += t["cache"]
        totals["cost"] += t["cost"]
        totals["tokens"] += t["tokens"]
        totals["tokens_with_cache"] += t["tokens_with_cache"]

    totals["cost"] = round(totals["cost"], 4)
    per_model = sorted(per_model_map.values(), key=lambda r: -r["tokens_with_cache"])
    for r in per_model:
        r["cost"] = round(r["cost"], 4)

    # 近期趋势走向
    daily_series = []
    trend_day_keys = sorted(daily.keys())[-14:] if (not time_key or time_key in ["全部", "近30天", "本月"]) else sorted(day_keys)
    for dk in trend_day_keys:
        d = daily.get(dk) or {}
        daily_series.append({
            "date": dk,
            "label": dk[5:] if len(dk) >= 10 else dk,
            "tokens": d.get("tokens_with_cache", 0),
            "cost": round(d.get("cost", 0.0), 4),
            "calls": d.get("calls", 0)
        })

    return {
        "per_model": per_model,
        "totals": totals,
        "error": None,
        "source": "grok",
        "db": str(GROK_SESSIONS_DIR),
        "days": len(day_keys),
        "daily_series": daily_series,
        "sessions_count": gdata["sessions_count"],
        "hint": ("本机口径：仅统计 ~/.grok/sessions 会话库（%d 个会话），"
                 "不含该账号在其它设备/客户端的用量；Grok 官方未提供用量接口，"
                 "额度请以卡片上的官方百分比为准" % int(gdata.get("sessions_count") or 0)),
        "last_active": gdata["last_active"],
        "quota": (dict(_GROK_QUOTA_CACHE) or {}).get("data")
    }

def merge_usage(dsh, grok):
    """将 dsh 账本数据与 Grok Build 会话数据深度合并为全渠道看板"""
    t1 = dsh.get("totals") or {}
    t2 = grok.get("totals") or {}
    fx = t1.get("fx") or t2.get("fx") or 6.6977

    totals = {
        "sessions": (t1.get("sessions") or 0) + (t2.get("sessions") or 0),
        "input": (t1.get("input") or 0) + (t2.get("input") or 0),
        "output": (t1.get("output") or 0) + (t2.get("output") or 0),
        "reasoning": (t1.get("reasoning") or 0) + (t2.get("reasoning") or 0),
        "cache": (t1.get("cache") or 0) + (t2.get("cache") or 0),
        "cacheRead": (t1.get("cacheRead") or 0) + (t2.get("cacheRead") or 0),
        "cacheWrite": (t1.get("cacheWrite") or 0) + (t2.get("cacheWrite") or 0),
        "cost": round((t1.get("cost") or 0.0) + (t2.get("cost") or 0.0), 4),
        "tokens": (t1.get("tokens") or 0) + (t2.get("tokens") or 0),
        "tokens_with_cache": (t1.get("tokens_with_cache") or 0) + (t2.get("tokens_with_cache") or 0),
        "fx": fx
    }

    per_model = (dsh.get("per_model") or []) + (grok.get("per_model") or [])
    per_model.sort(key=lambda r: -(r.get("tokens_with_cache") or r.get("tokens") or 0))

    s_map = {}
    for s in (dsh.get("daily_series") or []):
        dk = s["date"]
        it = s_map.setdefault(dk, {"date": dk, "label": s.get("label", dk[5:]), "tokens": 0, "cost": 0.0, "calls": 0})
        it["tokens"] += s.get("tokens", 0)
        it["cost"] += s.get("cost", 0.0)
        it["calls"] += s.get("calls", 0)
    for s in (grok.get("daily_series") or []):
        dk = s["date"]
        it = s_map.setdefault(dk, {"date": dk, "label": s.get("label", dk[5:]), "tokens": 0, "cost": 0.0, "calls": 0})
        it["tokens"] += s.get("tokens", 0)
        it["cost"] += s.get("cost", 0.0)
        it["calls"] += s.get("calls", 0)

    daily_series = []
    for dk in sorted(s_map.keys()):
        it = s_map[dk]
        it["cost"] = round(it["cost"], 4)
        daily_series.append(it)

    return {
        "totals": totals,
        "per_model": per_model,
        "daily_series": daily_series,
        "source": "全渠道汇总",
        "db": "dsh账本 + grok会话库",
        "days": max(dsh.get("days", 0), grok.get("days", 0)),
        "error": dsh.get("error") or grok.get("error"),
        "hint": dsh.get("hint") or grok.get("hint")
    }


try:
    import webview
except ImportError as _e:  # 源码运行时缺依赖：给一句人话，而不是一堆 traceback
    sys.stderr.write(
        "缺少依赖 pywebview：请先执行  pip install -r requirements.txt\n(%s)\n" % _e)
    raise

def get_asset_path(filename):
    """在若干可能的位置里找资源文件（assets/ 子目录、脚本同级、EXE 同级、_MEIPASS）。"""
    cands = []
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', Path(sys.executable).parent)
        cands.append(Path(base) / filename)
        cands.append(Path(base) / "assets" / filename)
        cands.append(Path(sys.executable).parent / filename)
        cands.append(Path(sys.executable).parent / "assets" / filename)
    here = Path(__file__).resolve().parent
    cands.append(here / filename)
    cands.append(here / "assets" / filename)
    cands.append(here.parent / "assets" / filename)
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return None


def get_asset_text(filename):
    try:
        p = get_asset_path(filename)
        if p is not None:
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Coding Plan 额度查询</title>
<script>
__TAILWIND_SCRIPT_INLINE__
</script>
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    background: #f1f5f9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", "PingFang SC", "Microsoft YaHei UI", sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    user-select: none;
    color: #1e293b;
  }

  /* ============ 全盘重构主题体系与全要素色彩变量 ============ */
  :root {
    --theme-name: 'blue';
    --bg-app: #f1f5f9;
    --bg-sidebar: #f8fafc;
    --bg-card: #ffffff;
    --bg-card-subtle: #f8fafc;
    --bg-input: #ffffff;
    --border-main: #e2e8f0;
    --border-subtle: #edf2f7;
    --text-title: #0f172a;
    --text-body: #1e293b;
    --text-secondary: #64748b;
    --text-muted: #94a3b8;
    --text-mono: #334155;
    --table-row-hover: #f8fafc;
    --card-shadow: 0 4px 16px -4px rgba(0, 0, 0, 0.04);
    --brand-primary: #0078d4;
    --brand-primary-hover: #006cbe;
    --brand-grad-start: #0284c7;
    --brand-grad-end: #0078d4;
    --brand-shadow: rgba(0, 120, 212, 0.32);
    --brand-glow: rgba(0, 120, 212, 0.12);
    --brand-subtle-bg: #eff6ff;
    --brand-subtle-border: #bfdbfe;
    --brand-subtle-text: #0369a1;
    --brand-badge-bg: #e0f2fe;
    --brand-badge-text: #0369a1;
    --accent-green: #059669;
    --accent-cache: #0f766e;
    --accent-amber: #d97706;
    --capsule-track-bg: #e2e8f0;
  }

  /* 方案一：暗黑曜石极客 (Obsidian Dark) - 彻底沉浸全暗黑、亮白高对比字色、电光青蓝 */
  [data-theme="obsidian"], [data-theme="obsidian-dark"], [data-theme="dark"] {
    --theme-name: 'obsidian';
    --bg-app: #0b0f19;
    --bg-sidebar: #111827;
    --bg-card: #162032;
    --bg-card-subtle: #0f1726;
    --bg-input: #0c1422;
    --border-main: #243247;
    --border-subtle: #1a2638;
    --text-title: #f8fafc;
    --text-body: #cbd5e1;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --text-mono: #93c5fd;
    --table-row-hover: #1e2b40;
    --card-shadow: 0 8px 24px -4px rgba(0, 0, 0, 0.5);
    --brand-primary: #38bdf8;
    --brand-primary-hover: #0ea5e9;
    --brand-grad-start: #0284c7;
    --brand-grad-end: #0369a1;
    --brand-shadow: rgba(56, 189, 248, 0.35);
    --brand-glow: rgba(56, 189, 248, 0.2);
    --brand-subtle-bg: #152942;
    --brand-subtle-border: #1e3e66;
    --brand-subtle-text: #7dd3fc;
    --brand-badge-bg: #1e3a5f;
    --brand-badge-text: #7dd3fc;
    --accent-green: #34d399;
    --accent-cache: #6ee7b7;
    --accent-amber: #fbbf24;
    --capsule-track-bg: #1f2c3f;
  }

  /* 方案二：日系暖米纸质 (Warm Kraft Paper / Notion 禅意暖咖) - 温暖质朴、深焙咖啡字色、暖陶土朱红 */
  [data-theme="paper"], [data-theme="warm-paper"] {
    --theme-name: 'paper';
    --bg-app: #f5f1ea;
    --bg-sidebar: #ebe3d7;
    --bg-card: #ffffff;
    --bg-card-subtle: #f8f5ef;
    --bg-input: #fcfbf9;
    --border-main: #ded4c5;
    --border-subtle: #e8e0d4;
    --text-title: #2b2017;
    --text-body: #48392e;
    --text-secondary: #7a6a5c;
    --text-muted: #a39486;
    --text-mono: #3d2f24;
    --table-row-hover: #f3ede3;
    --card-shadow: 0 4px 18px -2px rgba(60, 42, 28, 0.06);
    --brand-primary: #c25438;
    --brand-primary-hover: #aa452b;
    --brand-grad-start: #d46347;
    --brand-grad-end: #b8472c;
    --brand-shadow: rgba(194, 84, 56, 0.3);
    --brand-glow: rgba(194, 84, 56, 0.15);
    --brand-subtle-bg: #faebe6;
    --brand-subtle-border: #f7cbbf;
    --brand-subtle-text: #b8472c;
    --brand-badge-bg: #fbeae4;
    --brand-badge-text: #9c351c;
    --accent-green: #2d7a58;
    --accent-cache: #2e7458;
    --accent-amber: #c2781b;
    --capsule-track-bg: #e7ded1;
  }

  /* 方案三：冰海冷萃 Slate (Nordic Ice Slate / 现代商务冷灰) - 冰川雾蓝灰底、深海青墨字色、冷海钛青 */
  [data-theme="slate"], [data-theme="ice-slate"], [data-theme="indigo"] {
    --theme-name: 'slate';
    --bg-app: #eaf0f7;
    --bg-sidebar: #f4f8fc;
    --bg-card: #ffffff;
    --bg-card-subtle: #eff5fb;
    --bg-input: #ffffff;
    --border-main: #cfdeec;
    --border-subtle: #dfe9f3;
    --text-title: #091e36;
    --text-body: #1a3454;
    --text-secondary: #496582;
    --text-muted: #7e99b2;
    --text-mono: #0c2b4e;
    --table-row-hover: #e8f2fa;
    --card-shadow: 0 4px 18px -2px rgba(10, 36, 62, 0.06);
    --brand-primary: #0284c7;
    --brand-primary-hover: #0369a1;
    --brand-grad-start: #0ea5e9;
    --brand-grad-end: #0284c7;
    --brand-shadow: rgba(2, 132, 199, 0.32);
    --brand-glow: rgba(2, 132, 199, 0.15);
    --brand-subtle-bg: #f0f8ff;
    --brand-subtle-border: #b9e2fe;
    --brand-subtle-text: #0284c7;
    --brand-badge-bg: #e0f2fe;
    --brand-badge-text: #0369a1;
    --accent-green: #0d9488;
    --accent-cache: #0d9488;
    --accent-amber: #d97706;
    --capsule-track-bg: #d7e6f4;
  }

  /* 方案四：暮色星云暗紫 (Midnight Nebula / 极光暗夜未来感) - 深邃夜空暗紫、极光皓月白字色、霓虹紫蓝 */
  [data-theme="violet"], [data-theme="midnight-nebula"], [data-theme="teal"] {
    --theme-name: 'violet';
    --bg-app: #0d0e1c;
    --bg-sidebar: #141528;
    --bg-card: #1b1b34;
    --bg-card-subtle: #131326;
    --bg-input: #101021;
    --border-main: #2b2a4e;
    --border-subtle: #21203d;
    --text-title: #fbfaff;
    --text-body: #dcdbf2;
    --text-secondary: #9996be;
    --text-muted: #68658f;
    --text-mono: #c4b5fd;
    --table-row-hover: #252445;
    --card-shadow: 0 8px 28px -4px rgba(0, 0, 0, 0.6);
    --brand-primary: #8b5cf6;
    --brand-primary-hover: #7c3aed;
    --brand-grad-start: #a78bfa;
    --brand-grad-end: #7c3aed;
    --brand-shadow: rgba(139, 92, 246, 0.38);
    --brand-glow: rgba(139, 92, 246, 0.22);
    --brand-subtle-bg: #231f42;
    --brand-subtle-border: #3f3870;
    --brand-subtle-text: #c4b5fd;
    --brand-badge-bg: #312759;
    --brand-badge-text: #ddd6fe;
    --accent-green: #34d399;
    --accent-cache: #7dd3fc;
    --accent-amber: #fbbf24;
    --capsule-track-bg: #272647;
  }

  /* ============ 全局应用主题变量与全盘色彩覆写 ============ */
  html[data-theme="obsidian"], html[data-theme="obsidian-dark"], html[data-theme="dark"],
  html[data-theme="violet"], html[data-theme="midnight-nebula"], html[data-theme="teal"] {
    color-scheme: dark;
  }
  html[data-theme="paper"], html[data-theme="warm-paper"],
  html[data-theme="slate"], html[data-theme="ice-slate"], html[data-theme="indigo"],
  html:not([data-theme]) {
    color-scheme: light;
  }

  [data-theme] {
    background: var(--bg-app);
    color: var(--text-body);
  }
  [data-theme] .app-container {
    background: var(--bg-app);
  }
  [data-theme] .sidebar {
    background: var(--bg-sidebar);
    border-color: var(--border-main);
  }
  [data-theme] .glass-card {
    background: var(--bg-card);
    border-color: var(--border-main);
    box-shadow: var(--card-shadow);
  }
  [data-theme] .mini-kpi {
    background: var(--bg-card-subtle);
    border-color: var(--border-subtle);
  }
  [data-theme] .capsule-track {
    background: var(--capsule-track-bg);
  }
  [data-theme] .pivot-container {
    background: var(--bg-card-subtle);
    border-color: var(--border-subtle);
  }
  [data-theme] .pivot-btn {
    color: var(--text-secondary);
  }
  [data-theme] .pivot-btn:hover {
    color: var(--text-title);
  }
  [data-theme] .pivot-btn.active {
    background: var(--bg-card);
    color: var(--brand-primary);
  }
  [data-theme] .toolbar-chip {
    background: var(--bg-card-subtle);
    border-color: var(--border-subtle);
    color: var(--text-secondary);
    box-shadow: none;
  }
  [data-theme] .toolbar-chip:hover {
    background: var(--bg-card);
    border-color: var(--border-main);
    color: var(--text-title);
  }
  [data-theme] .toolbar-chip.active, [data-theme] .toolbar-chip.toolbar-chip-active {
    background: var(--brand-glow) !important;
    border-color: var(--brand-primary) !important;
    color: var(--brand-primary) !important;
  }
  [data-theme] .toolbar-badge {
    background: var(--bg-card-subtle);
    color: var(--text-muted);
    border-color: var(--border-subtle);
  }
  [data-theme] #model-table-header {
    background-color: var(--bg-card) !important;
    border-color: var(--border-subtle) !important;
    color: var(--text-secondary) !important;
  }
  [data-theme] .model-row {
    border-color: var(--border-subtle) !important;
  }
  [data-theme] .model-row:hover {
    background-color: var(--table-row-hover) !important;
  }
  [data-theme] .summary-row {
    background-color: var(--bg-card-subtle) !important;
    border-color: var(--border-main) !important;
    color: var(--text-title) !important;
  }

  /* 彻底重构全盘文字配色体系，覆盖 Tailwind 实用类 */
  [data-theme] .text-slate-900,
  [data-theme] .text-slate-800 {
    color: var(--text-title) !important;
  }
  [data-theme] .text-slate-700,
  [data-theme] .text-slate-600 {
    color: var(--text-body) !important;
  }
  [data-theme] .text-slate-500,
  [data-theme] .text-slate-400 {
    color: var(--text-secondary) !important;
  }
  [data-theme] .text-slate-300 {
    color: var(--text-muted) !important;
  }
  [data-theme] .text-emerald-600,
  [data-theme] .text-emerald-700 {
    color: var(--accent-cache) !important;
  }
  [data-theme] .text-amber-600,
  [data-theme] .text-amber-700 {
    color: var(--accent-amber) !important;
  }

  /* 背景与边框全盘重构 */
  [data-theme] .bg-white {
    background-color: var(--bg-card) !important;
  }
  [data-theme] .bg-slate-50,
  [data-theme] .bg-slate-100,
  [data-theme] .bg-slate-100\/70,
  [data-theme] .bg-slate-100\/80 {
    background-color: var(--bg-card-subtle) !important;
  }
  [data-theme] .border-slate-100,
  [data-theme] .border-slate-200,
  [data-theme] .border-slate-200\/90,
  [data-theme] .border-slate-200\/50 {
    border-color: var(--border-subtle) !important;
  }

  /* 侧边栏交互组件 */
  [data-theme] .sidebar-action-btn {
    color: var(--text-secondary);
  }
  [data-theme] .sidebar-action-btn:hover {
    background: var(--border-subtle);
    color: var(--text-title);
  }
  [data-theme] .key-item {
    color: var(--text-title);
  }
  [data-theme] .key-item:hover {
    background: var(--bg-card);
    border-color: var(--border-main);
  }

  /* 搜索框与下拉菜单 */
  [data-theme] .modal-input,
  [data-theme] .modal-select {
    background-color: var(--bg-input) !important;
    color: var(--text-body) !important;
    border-color: var(--border-main) !important;
  }
  [data-theme] select:not(.toolbar-chip-select) {
    background-color: var(--bg-input) !important;
    color: var(--text-body) !important;
    border-color: var(--border-main) !important;
  }
  [data-theme] .modal-input::placeholder {
    color: var(--text-muted) !important;
  }
  [data-theme] .btn-action-secondary {
    background-color: var(--bg-card) !important;
    color: var(--text-body) !important;
    border-color: var(--border-main) !important;
  }
  [data-theme] .btn-action-secondary:hover {
    background-color: var(--bg-card-subtle) !important;
    border-color: var(--brand-primary) !important;
  }

  /* 模态框与深色适配 */
  [data-theme] .modal-card {
    background: var(--bg-card) !important;
    border-color: var(--border-main) !important;
    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.45) !important;
  }
  [data-theme] .modal-header,
  [data-theme] .modal-footer {
    border-color: var(--border-subtle) !important;
  }
  [data-theme] .modal-title {
    color: var(--text-title) !important;
  }
  [data-theme] .modal-label {
    color: var(--text-secondary) !important;
  }
  [data-theme] .channel-option,
  [data-theme] .tier-option {
    background: var(--bg-card-subtle) !important;
    border-color: var(--border-main) !important;
    color: var(--text-body) !important;
  }
  [data-theme] .channel-option:hover,
  [data-theme] .tier-option:hover {
    border-color: var(--brand-primary) !important;
  }

  /* 暗色模式下的专属微调（深色下让徽章不刺眼） */
  [data-theme="obsidian"] .badge-step,
  [data-theme="violet"] .badge-step {
    background: rgba(251, 191, 36, 0.16) !important;
    color: #fbbf24 !important;
    border: 1px solid rgba(251, 191, 36, 0.3) !important;
  }
  [data-theme="obsidian"] .badge-cc,
  [data-theme="violet"] .badge-cc {
    background: rgba(52, 211, 153, 0.16) !important;
    color: #34d399 !important;
    border: 1px solid rgba(52, 211, 153, 0.3) !important;
  }
  [data-theme="obsidian"] .badge-grok,
  [data-theme="violet"] .badge-grok {
    background: rgba(192, 132, 252, 0.16) !important;
    color: #c084fc !important;
    border: 1px solid rgba(192, 132, 252, 0.3) !important;
  }
  [data-theme="obsidian"] .badge-cline,
  [data-theme="violet"] .badge-cline {
    background: rgba(129, 140, 248, 0.16) !important;
    color: #818cf8 !important;
    border: 1px solid rgba(129, 140, 248, 0.3) !important;
  }
  html[data-theme="obsidian"] .key-pill-ok,
  html[data-theme="obsidian-dark"] .key-pill-ok,
  html[data-theme="dark"] .key-pill-ok,
  html[data-theme="midnight-nebula"] .key-pill-ok,
  html[data-theme="teal"] .key-pill-ok,
  html[data-theme="violet"] .key-pill-ok {
    background: rgba(52, 211, 153, 0.16) !important;
    color: #34d399 !important;
    border-color: rgba(52, 211, 153, 0.3) !important;
  }
  html[data-theme="obsidian"] .key-pill-amber,
  html[data-theme="obsidian-dark"] .key-pill-amber,
  html[data-theme="dark"] .key-pill-amber,
  html[data-theme="midnight-nebula"] .key-pill-amber,
  html[data-theme="teal"] .key-pill-amber,
  html[data-theme="violet"] .key-pill-amber {
    background: rgba(251, 191, 36, 0.16) !important;
    color: #fbbf24 !important;
    border-color: rgba(251, 191, 36, 0.3) !important;
  }
  html[data-theme="obsidian"] .key-pill-rose,
  html[data-theme="obsidian-dark"] .key-pill-rose,
  html[data-theme="dark"] .key-pill-rose,
  html[data-theme="midnight-nebula"] .key-pill-rose,
  html[data-theme="teal"] .key-pill-rose,
  html[data-theme="violet"] .key-pill-rose {
    background: rgba(244, 63, 94, 0.16) !important;
    color: #f43f5e !important;
    border-color: rgba(244, 63, 94, 0.3) !important;
  }
  html[data-theme="obsidian"] .key-pill-none,
  html[data-theme="obsidian-dark"] .key-pill-none,
  html[data-theme="dark"] .key-pill-none,
  html[data-theme="midnight-nebula"] .key-pill-none,
  html[data-theme="teal"] .key-pill-none,
  html[data-theme="violet"] .key-pill-none {
    background: #1e293b !important;
    color: #64748b !important;
  }

  /* 渠道分类手风琴样式 */
  .channel-group {
    margin-bottom: 6px;
  }
  .channel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 10px;
    border-radius: 8px;
    cursor: pointer;
    user-select: none;
    transition: all 0.15s ease;
    background-color: rgba(241, 245, 249, 0.85);
    border: 1px solid rgba(226, 232, 240, 0.8);
  }
  .channel-header:hover {
    background-color: #e2e8f0;
    border-color: #cbd5e1;
  }
  .channel-chevron {
    transform-origin: center;
    transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    color: #94a3b8;
  }
  .channel-chevron.-rotate-90 {
    transform: rotate(-90deg);
  }
  .channel-header-title {
    font-size: 11.5px;
    font-weight: 600;
    color: #334155;
    letter-spacing: 0.1px;
  }
  .channel-count-badge {
    font-size: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    padding: 1px 6px;
    border-radius: 9999px;
    background: #ffffff;
    color: #64748b;
    font-weight: 600;
    border: 1px solid rgba(203, 213, 225, 0.7);
  }

  [data-theme] .channel-header {
    background-color: var(--bg-card-subtle);
    border-color: var(--border-subtle);
  }
  [data-theme] .channel-header:hover {
    background-color: var(--table-row-hover) !important;
    border-color: var(--border-main) !important;
  }
  [data-theme] .channel-header-title {
    color: var(--text-body) !important;
  }
  [data-theme] .channel-header:hover .channel-header-title {
    color: var(--text-title) !important;
  }
  [data-theme] .channel-count-badge {
    background-color: var(--bg-card) !important;
    color: var(--text-muted) !important;
    border-color: var(--border-main) !important;
  }

  /* 设置模态框主题卡片交互 */
  .theme-select-card {
    transition: all 0.18s ease;
  }
  .theme-select-card:hover {
    border-color: var(--brand-primary) !important;
    transform: translateY(-1px);
  }
  .theme-select-card.active {
    border-color: var(--brand-primary) !important;
    box-shadow: 0 0 0 2px var(--brand-glow) !important;
  }

  /* 趋势图表样式 */
  .trend-svg-line {
    fill: none;
    stroke-width: 2.2;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: stroke 0.3s ease;
  }
  .trend-point {
    transition: r 0.15s ease, fill 0.15s ease;
    cursor: pointer;
  }
  .trend-point:hover {
    r: 5.5;
  }
  [data-theme] .trend-card-box {
    background-color: var(--bg-card-subtle) !important;
    border-color: var(--border-subtle) !important;
  }
  [data-theme] .trend-chart-inner {
    background-color: var(--bg-card) !important;
    border-color: var(--border-subtle) !important;
  }
  [data-theme] #trend-tooltip {
    background-color: var(--bg-card) !important;
    border: 1px solid var(--border-main) !important;
    color: var(--text-body) !important;
    box-shadow: var(--card-shadow) !important;
  }
  [data-theme] input[type="date"] {
    background-color: var(--bg-input) !important;
    color: var(--text-body) !important;
    border-color: var(--border-main) !important;
  }

  /* 优雅细滚动条适配 */
  ::-webkit-scrollbar {
    width: 6px;
    height: 6px;
  }
  ::-webkit-scrollbar-track {
    background: transparent;
  }
  ::-webkit-scrollbar-thumb {
    background: var(--border-main);
    border-radius: 9999px;
  }
  ::-webkit-scrollbar-thumb:hover {
    background: var(--text-muted);
  }

  .brand-icon { color: var(--brand-primary); }
  .brand-stroke { stroke: var(--brand-primary); }

  .app-container {
    display: flex;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    background: var(--bg-app);
  }

  /* Left Sidebar */
  .sidebar {
    width: 260px;
    height: 100%;
    background: #f8fafc;
    border-right: 1px solid #e2e8f0;
    display: flex;
    flex-direction: column;
    padding: 14px 12px;
    flex-shrink: 0;
  }

  .key-item {
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 5px;
    cursor: pointer;
    transition: all 0.15s ease;
    background: transparent;
    border: 1px solid transparent;
    position: relative;
    overflow: hidden;
  }
  .key-item:hover {
    background: #ffffff;
    border-color: #e2e8f0;
  }
  .key-item.active {
    background: linear-gradient(135deg, var(--brand-grad-start), var(--brand-grad-end)) !important;
    color: #ffffff !important;
    box-shadow: 0 4px 14px -2px var(--brand-shadow);
    border-color: transparent;
  }
  .key-item.active * { color: #ffffff !important; }
  .key-item.active .key-mask { color: rgba(255, 255, 255, 0.85) !important; }
  .key-item.active .badge { background: rgba(255, 255, 255, 0.22) !important; color: #ffffff !important; border: none !important; }
  .key-item.active .key-status-text { color: #ffffff !important; }
  .key-item.active .key-status-dot {
    background: #ffffff !important;
    box-shadow: 0 0 6px rgba(255, 255, 255, 0.9) !important;
  }
  .key-item.active .key-progress-track {
    background: rgba(255, 255, 255, 0.25) !important;
  }
  .key-item.active .key-progress-bar {
    background: #ffffff !important;
    box-shadow: 0 0 8px rgba(255, 255, 255, 0.8) !important;
  }

  /* 底部细线微进度条 */
  .key-progress-track {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: rgba(0, 0, 0, 0.06);
    overflow: hidden;
  }
  .key-progress-bar {
    height: 100%;
    border-radius: 0 1px 1px 0;
    background: var(--brand-primary);
    box-shadow: 0 0 6px var(--brand-glow);
    transition: width 0.3s ease;
  }
  .key-progress-bar.warning {
    background: var(--accent-amber) !important;
    box-shadow: 0 0 6px rgba(251, 191, 36, 0.5) !important;
  }
  .key-progress-bar.danger {
    background: #f43f5e !important;
    box-shadow: 0 0 6px rgba(244, 63, 94, 0.5) !important;
  }

  .key-status-text {
    color: var(--brand-primary);
  }
  .key-status-text.warning {
    color: var(--accent-amber) !important;
  }
  .key-status-text.danger {
    color: #f43f5e !important;
  }

  .key-status-dot {
    display: inline-block;
    width: 5px;
    height: 5px;
    border-radius: 9999px;
    flex-shrink: 0;
  }
  .key-status-dot.normal {
    background: var(--brand-primary);
    box-shadow: 0 0 5px var(--brand-glow);
  }
  .key-status-dot.warning {
    background: var(--accent-amber);
    box-shadow: 0 0 5px rgba(251, 191, 36, 0.6);
  }
  .key-status-dot.danger {
    background: #f43f5e;
    box-shadow: 0 0 5px rgba(244, 63, 94, 0.6);
  }

  /* 深色主题进度底轨微光 */
  [data-theme="obsidian"] .key-progress-track,
  [data-theme="obsidian-dark"] .key-progress-track,
  [data-theme="dark"] .key-progress-track,
  [data-theme="midnight-nebula"] .key-progress-track,
  [data-theme="teal"] .key-progress-track,
  [data-theme="violet"] .key-progress-track {
    background: rgba(255, 255, 255, 0.08);
  }

  .badge {
    font-size: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-weight: 600;
    padding: 1.5px 6px;
    border-radius: 4px;
    letter-spacing: 0.2px;
  }
  .badge-go { background: var(--brand-badge-bg); color: var(--brand-badge-text); border: 1px solid var(--brand-subtle-border); }
  .badge-step { background: #fef3c7; color: #b45309; border: 1px solid #fde68a; }
  .badge-cc { background: #dcfce7; color: #047857; border: 1px solid #bbf7d0; }
  .badge-grok { background: #f3e8ff; color: #7e22ce; border: 1px solid #e9d5ff; }
  .badge-cline { background: #e0e7ff; color: #4338ca; border: 1px solid #c7d2fe; }

  /* ============ 模态卡片（Fluent 风） ============ */
  .modal-backdrop {
    position: fixed; inset: 0; z-index: 100;
    background: rgba(15, 23, 42, 0.45);
    backdrop-filter: blur(3px); -webkit-backdrop-filter: blur(3px);
    display: flex; align-items: center; justify-content: center;
    opacity: 0; pointer-events: none;
    transition: opacity 0.15s ease;
  }
  .modal-backdrop.open { opacity: 1; pointer-events: auto; }
  .modal-card {
    width: 408px; max-width: calc(100vw - 48px);
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    box-shadow: 0 16px 48px -8px rgba(15, 23, 42, 0.28);
    transform: scale(0.96) translateY(-4px);
    transition: transform 0.15s cubic-bezier(0.4, 0, 0.2, 1);
    overflow: hidden;
    user-select: text;
  }
  .modal-backdrop.open .modal-card { transform: scale(1) translateY(0); }
  .modal-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px 12px; border-bottom: 1px solid #f1f5f9;
  }
  .modal-title { font-size: 13.5px; font-weight: 600; color: #0f172a; letter-spacing: 0.2px; }
  .modal-x {
    width: 24px; height: 24px; border-radius: 5px; border: none; cursor: pointer;
    background: transparent; color: #94a3b8; display: inline-flex; align-items: center; justify-content: center;
    transition: all 0.15s ease;
  }
  .modal-x:hover { background: #f1f5f9; color: #475569; }
  .modal-body { padding: 16px 18px 4px; display: flex; flex-direction: column; gap: 13px; }
  .modal-label {
    display: block; font-size: 10.5px; font-weight: 600; color: #64748b;
    letter-spacing: 0.4px; margin-bottom: 5px; text-transform: uppercase;
  }
  .modal-input {
    width: 100%; box-sizing: border-box;
    padding: 7px 10px; font-size: 12.5px; color: #1e293b;
    background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px;
    outline: none; transition: all 0.15s ease;
  }
  .modal-input:focus { border-color: var(--brand-primary); box-shadow: 0 0 0 3px var(--brand-glow); }
  .modal-input.mono { font-family: Consolas, monospace; font-size: 12px; letter-spacing: 0.3px; }
  .modal-input::placeholder { color: #cbd5e1; }
  .modal-select {
    width: 100%; padding: 7px 10px; font-size: 12.5px; color: #1e293b;
    background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px;
    outline: none; cursor: pointer; transition: all 0.15s ease;
  }
  .modal-select:focus { border-color: var(--brand-primary); box-shadow: 0 0 0 3px var(--brand-glow); }
  .channel-option {
    display: flex; align-items: center; gap: 10px; width: 100%; box-sizing: border-box;
    padding: 10px 12px; margin-bottom: 8px; cursor: pointer;
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;
    transition: all 0.15s ease; text-align: left;
  }
  .channel-option:hover { border-color: #94a3b8; background: #f8fafc; }
  .channel-option.selected { border-color: var(--brand-primary); background: var(--brand-subtle-bg); box-shadow: 0 0 0 3px var(--brand-glow); }
  .channel-option .co-icon {
    width: 30px; height: 30px; border-radius: 7px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
  }
  .channel-option .co-title { font-size: 12.5px; font-weight: 600; color: #1e293b; line-height: 1.3; }
  .channel-option .co-desc { font-size: 10.5px; color: #94a3b8; margin-top: 1px; }
  .channel-option .co-radio {
    margin-left: auto; width: 15px; height: 15px; border-radius: 50%; flex-shrink: 0;
    border: 1.5px solid #cbd5e1; position: relative; transition: all 0.15s ease;
  }
  .channel-option.selected .co-radio { border-color: var(--brand-primary); }
  .channel-option.selected .co-radio::after {
    content: ''; position: absolute; inset: 3px; border-radius: 50%; background: var(--brand-primary);
  }
  .modal-error {
    display: none; padding: 8px 11px; border-radius: 6px;
    background: #fef2f2; border: 1px solid #fecaca;
    font-size: 11.5px; color: #b91c1c; line-height: 1.4;
  }
  .modal-error.show { display: block; }
  .modal-footer {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 13px 18px 15px; border-top: 1px solid #f1f5f9; margin-top: 13px;
  }
  .btn-danger {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 14px; border-radius: 6px; border: none; cursor: pointer;
    font-size: 12px; font-weight: 500; color: #ffffff;
    background: linear-gradient(135deg, #f43f5e, #e11d48);
    box-shadow: 0 2px 6px rgba(225, 29, 72, 0.28); transition: all 0.15s ease;
  }
  .btn-danger:hover { background: linear-gradient(135deg, #fb7185, #f43f5e); transform: translateY(-0.5px); }
  .tier-option {
    display: flex; align-items: center; gap: 10px; width: 100%; box-sizing: border-box;
    padding: 9px 12px; margin-bottom: 7px; cursor: pointer;
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;
    transition: all 0.15s ease; text-align: left; font-size: 12px; color: #334155;
  }
  .tier-option:hover { border-color: #94a3b8; background: #f8fafc; }
  .tier-option.selected { border-color: var(--brand-primary); background: var(--brand-subtle-bg); box-shadow: 0 0 0 3px var(--brand-glow); }
  .tier-option .t-credit { margin-left: auto; font-family: Consolas, monospace; font-size: 11px; font-weight: 600; color: var(--brand-primary); }
  .input-row { display: flex; gap: 7px; align-items: stretch; }
  .input-row .modal-input { flex: 1; min-width: 0; }
  .btn-eye {
    flex-shrink: 0; padding: 0 10px; border: 1px solid #cbd5e1; border-radius: 6px;
    background: #ffffff; color: #64748b; font-size: 11px; cursor: pointer; transition: all 0.15s ease;
  }
  .btn-eye:hover { background: #f8fafc; color: #334155; border-color: #94a3b8; }

  /* ============ Toast 悬浮轻提示 ============ */
  .toast-container {
    position: fixed;
    top: 16px;
    right: 20px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 8px;
    pointer-events: none;
  }
  .toast-item {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 9px 14px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 500;
    box-shadow: 0 6px 20px -2px rgba(15, 23, 42, 0.16);
    pointer-events: auto;
    animation: toast-in 0.22s cubic-bezier(0.16, 1, 0.3, 1);
    transition: opacity 0.25s ease, transform 0.25s ease;
    max-width: 380px;
    line-height: 1.4;
  }
  .toast-item.leaving {
    opacity: 0;
    transform: translateY(-8px) scale(0.95);
  }
  .toast-success {
    background: #ffffff;
    border: 1px solid #bbf7d0;
    color: #15803d;
  }
  .toast-info {
    background: #ffffff;
    border: 1px solid #bfdbfe;
    color: #1d4ed8;
  }
  .toast-warning {
    background: #ffffff;
    border: 1px solid #fde68a;
    color: #b45309;
  }
  .toast-error {
    background: #ffffff;
    border: 1px solid #fecaca;
    color: #b91c1c;
  }
  @keyframes toast-in {
    from { opacity: 0; transform: translateY(-10px) scale(0.96); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  /* Main Canvas */
  .main-canvas {
    flex: 1;
    height: 100%;
    display: flex;
    flex-direction: column;
    padding: 16px 22px;
    overflow-y: auto;
    gap: 12px;
    min-width: 0;
  }

  /* High-end White Cards with Soft Shadow */
  .glass-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    box-shadow: 0 4px 16px -4px rgba(0, 0, 0, 0.04);
    transition: border-color 0.25s ease, box-shadow 0.25s ease, background 0.25s ease;
  }
  .card-danger {
    border-color: #fca5a5 !important;
    background: linear-gradient(145deg, #ffffff 65%, #fff1f2) !important;
    box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.4), 0 8px 24px -6px rgba(244, 63, 94, 0.12) !important;
  }
  .card-warning {
    border-color: #fcd34d !important;
    background: linear-gradient(145deg, #ffffff 65%, #fffbeb) !important;
    box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.4), 0 8px 24px -6px rgba(245, 158, 11, 0.12) !important;
  }
  [data-theme="obsidian"] .card-warning,
  [data-theme="violet"] .card-warning {
    border-color: rgba(245, 158, 11, 0.55) !important;
    background: linear-gradient(145deg, var(--bg-card) 55%, rgba(245, 158, 11, 0.15)) !important;
    box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.35), 0 8px 24px -6px rgba(245, 158, 11, 0.22) !important;
  }
  [data-theme="obsidian"] .card-danger,
  [data-theme="violet"] .card-danger {
    border-color: rgba(244, 63, 94, 0.55) !important;
    background: linear-gradient(145deg, var(--bg-card) 55%, rgba(244, 63, 94, 0.15)) !important;
    box-shadow: 0 0 0 1px rgba(244, 63, 94, 0.35), 0 8px 24px -6px rgba(244, 63, 94, 0.22) !important;
  }
  [data-theme="paper"] .card-warning {
    border-color: #f6c06a !important;
    background: linear-gradient(145deg, #ffffff 65%, #fdf8ee) !important;
  }
  [data-theme="paper"] .card-danger {
    border-color: #fca5a5 !important;
    background: linear-gradient(145deg, #ffffff 65%, #fff5f5) !important;
  }
  .badge-danger {
    background: #ffe4e6;
    color: #e11d48;
    border: 1px solid #fecdd3;
    font-size: 10px;
    padding: 1px 7px;
    border-radius: 9999px;
    font-weight: 600;
  }
  .badge-warning {
    background: #fef3c7;
    color: #d97706;
    border: 1px solid #fde68a;
    font-size: 10px;
    padding: 1px 7px;
    border-radius: 9999px;
    font-weight: 600;
  }
  [data-theme="obsidian"] .badge-warning,
  [data-theme="violet"] .badge-warning {
    background: rgba(245, 158, 11, 0.22) !important;
    color: #fbbf24 !important;
    border: 1px solid rgba(245, 158, 11, 0.45) !important;
  }
  [data-theme="obsidian"] .badge-danger,
  [data-theme="violet"] .badge-danger {
    background: rgba(244, 63, 94, 0.22) !important;
    color: #f43f5e !important;
    border: 1px solid rgba(244, 63, 94, 0.45) !important;
  }
  .key-pill {
    display: inline-flex;
    align-items: center;
    font-size: 9.5px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    padding: 0.5px 5px;
    border-radius: 9999px;
    line-height: 1.25;
    font-weight: 500;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .key-pill-ok { background: #ecfdf5; color: #047857; border: 1px solid #d1fae5; }
  .key-pill-amber { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
  .key-pill-rose { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
  .key-pill-none { background: #f1f5f9; color: #94a3b8; }
  .summary-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 6px 6px;
    background: #f8fafc;
    border-top: 1.5px solid #e2e8f0;
    font-size: 11px;
    font-weight: 700;
    color: #1e293b;
    border-radius: 0 0 6px 6px;
    white-space: nowrap;
  }
  .sidebar-action-btn {
    width: 25px;
    height: 25px;
    border-radius: 5px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #64748b;
    background: transparent;
    border: none;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .sidebar-action-btn:hover {
    background: #e2e8f0;
    color: #1e293b;
  }
  .sidebar-action-btn.btn-trash:hover {
    background: #fee2e2;
    color: #ef4444;
  }

  .capsule-track {
    height: 5px;
    background: #e2e8f0;
    border-radius: 9999px;
    overflow: hidden;
    position: relative;
  }
  .capsule-fill {
    height: 100%;
    border-radius: 9999px;
    transition: width 0.4s cubic-bezier(0.4, 0, 0.2, 1);
  }

  /* Fluent Buttons */
  .btn-action {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
    border: none;
    outline: none;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .btn-action-primary {
    background: linear-gradient(135deg, var(--brand-grad-start), var(--brand-grad-end));
    color: #ffffff;
    box-shadow: 0 2px 6px var(--brand-shadow);
  }
  .btn-action-primary:hover {
    filter: brightness(1.08);
    box-shadow: 0 4px 10px var(--brand-shadow);
    transform: translateY(-0.5px);
  }
  .btn-action-secondary {
    background: #ffffff;
    color: #334155;
    border: 1px solid #cbd5e1;
  }
  .btn-action-secondary:hover {
    background: #f8fafc;
    border-color: #94a3b8;
  }

  /* ============ 底部栏：统一视觉标准体系 ============ */
  /* Pivot Tabs (左侧视图切换器) */
  .pivot-container {
    display: inline-flex;
    flex-wrap: nowrap;
    flex-shrink: 0;
    gap: 2px;
    background: #f1f5f9;
    padding: 2.5px;
    border-radius: 7px;
    border: 1px solid #e2e8f0;
    white-space: nowrap;
    height: 30px;
    box-sizing: border-box;
    align-items: center;
  }
  .pivot-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 23px;
    padding: 0 8px;
    font-size: 11.5px;
    font-weight: 500;
    color: #64748b;
    border-radius: 5px;
    cursor: pointer;
    transition: all 0.15s ease;
    background: transparent;
    border: none;
    white-space: nowrap;
    flex-shrink: 0;
    box-sizing: border-box;
  }
  .pivot-btn:hover { color: #1e293b; }
  .pivot-btn.active {
    background: #ffffff;
    color: var(--brand-primary, #2563eb);
    font-weight: 600;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }

  /* Toolbar Chips (右侧统一控件胶囊：搜索 / 周期 / Key过滤 / 复制) */
  .toolbar-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 30px;
    padding: 0 8px;
    border-radius: 7px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    font-size: 11.5px;
    color: #475569;
    white-space: nowrap;
    flex-shrink: 0;
    transition: all 0.15s ease;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
    user-select: none;
    box-sizing: border-box;
    line-height: 1;
  }
  .toolbar-chip:hover {
    border-color: #cbd5e1;
    background: #ffffff;
    color: #1e293b;
  }
  .toolbar-chip.active, .toolbar-chip.toolbar-chip-active {
    background: #eff6ff !important;
    border-color: #3b82f6 !important;
    color: #2563eb !important;
    font-weight: 600;
  }
  .toolbar-chip input,
  .toolbar-chip select {
    background: transparent !important;
    border: none !important;
    outline: none !important;
    box-shadow: none !important;
    color: inherit !important;
    font-family: inherit;
  }
  .toolbar-chip-input {
    font-size: 11.5px;
    padding: 0;
    margin: 0;
  }
  .toolbar-chip-input::placeholder {
    color: #94a3b8;
  }
  [data-theme] .toolbar-chip-input::placeholder {
    color: var(--text-muted) !important;
  }
  .toolbar-chip-select {
    font-size: 11.5px;
    padding: 0;
    margin: 0;
  }
  .toolbar-chip-select option {
    background-color: var(--bg-card, #ffffff);
    color: var(--text-body, #1e293b);
  }
  .toolbar-chip-date {
    font-size: 11.5px !important;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
    font-weight: 600 !important;
    color: var(--brand-primary, #2563eb) !important;
    padding: 0 0 0 2px;
    margin: 0;
  }
  .toolbar-badge {
    display: inline-flex;
    align-items: center;
    height: 20px;
    padding: 0 5px;
    border-radius: 4px;
    font-size: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    background: #f1f5f9;
    color: #64748b;
    border: 1px solid #e2e8f0;
    flex-shrink: 0;
    white-space: nowrap;
  }

  /* Card numbers: 100% matched to Preview Segoe UI Bold / Tabular Numbers */
  #c1-num, #c2-num, #c3-num, .card-metric-num {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Display", "Segoe UI", sans-serif !important;
    font-variant-numeric: tabular-nums;
    font-feature-settings: "tnum";
    letter-spacing: -0.025em;
  }

  /* Monospace for API tokens & raw JSON only - use clean Consolas without dotted zeros */
  .font-mono {
    font-family: Consolas, "Liberation Mono", "Courier New", monospace !important;
  }

  /* Stacked Bar */
  .stacked-bar-container {
    height: 6px;
    background: #e2e8f0;
    border-radius: 9999px;
    display: flex;
    overflow: hidden;
  }
  .bar-segment {
    height: 100%;
    transition: width 0.3s ease;
  }

  .mini-kpi {
    background: #f8fafc;
    border: 1px solid #edf2f7;
    border-radius: 8px;
    padding: 7px 11px;
    display: flex;
    flex-direction: column;
    gap: 1px;
    white-space: nowrap;
    overflow: hidden;
  }
  .mini-kpi span {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .model-row {
    display: flex;
    align-items: center;
    gap: 12px;               /* 与表头 gap-3 一致，保证列对齐 */
    padding: 6px 6px;        /* 水平内边距与表头 px-1.5 一致 */
    border-radius: 6px;
    transition: background 0.15s ease;
    border-bottom: 1px solid #f1f5f9;
    white-space: nowrap;
  }
  .model-row:hover {
    background: #f8fafc;
  }
</style>
</head>
<body>
<div id="toast-container" class="toast-container"></div>

<div class="app-container">
  <!-- Left Sidebar -->
  <div class="sidebar">
    <div class="flex items-center justify-between mb-3 px-1">
      <div class="flex items-center gap-2">
        <svg class="brand-stroke" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
          <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path>
        </svg>
        <span id="sidebar-keys-count" class="text-xs font-semibold text-slate-700 tracking-wider">已存密钥 (0)</span>
      </div>
      <button onclick="openAddKeyModal()" title="添加密钥" class="w-6 h-6 rounded flex items-center justify-center text-slate-500 hover:text-blue-600 hover:bg-blue-50 transition border-none bg-transparent cursor-pointer">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
          <line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line>
        </svg>
      </button>
    </div>

    <!-- Key Cards (Clickable) -->
    <div id="key-list-container" class="flex-1 overflow-y-auto space-y-1 pr-0.5"></div>

    <!-- Sidebar Bottom Actions -->
    <div class="pt-2.5 border-t border-slate-200/90 flex items-center justify-between text-slate-500 px-0.5">
      <!-- 密钥排序与管理组 -->
      <div class="flex items-center gap-0.5 bg-slate-100/70 p-0.5 rounded-md border border-slate-200/50">
        <button onclick="moveCurrentKey('up')" title="上移选中密钥" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polyline points="18 15 12 9 6 15"></polyline></svg>
        </button>
        <button onclick="moveCurrentKey('down')" title="下移选中密钥" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polyline points="6 9 12 15 18 9"></polyline></svg>
        </button>
        <button onclick="moveCurrentKey('top')" title="置顶选中密钥（移到同渠道最前）" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline><line x1="5" y1="21" x2="19" y2="21"></line></svg>
        </button>
        <button onclick="openEditKeyModal()" title="编辑选中密钥" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>
        </button>
        <button onclick="editCurrentTier()" title="修改月池档位" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line></svg>
        </button>
      </div>

      <!-- 数据与系统组 -->
      <div class="flex items-center gap-0.5 bg-slate-100/70 p-0.5 rounded-md border border-slate-200/50">
        <button onclick="openSettingsModal()" title="系统与外观设置" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06-.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
        </button>
        <button onclick="openBackupModal()" title="配置备份与还原" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
        </button>
        <button onclick="openStorageDir()" title="打开存储目录" class="sidebar-action-btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
        </button>
        <button onclick="deleteCurrentKey()" title="删除选中密钥" class="sidebar-action-btn btn-trash">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      </div>
    </div>
  </div>

  <!-- Main Canvas -->
  <div class="main-canvas">
    <!-- Top Action Bar -->
    <div class="flex items-center justify-between gap-2 flex-nowrap min-w-0 mb-3">
      <div class="flex-1 min-w-0 pr-2">
        <div class="flex items-center gap-2 flex-nowrap whitespace-nowrap min-w-0">
          <h1 id="hero-alias" class="text-lg font-bold text-slate-900 tracking-tight whitespace-nowrap shrink-0">请选择密钥</h1>
          <span id="hero-channel-badge" class="badge badge-go whitespace-nowrap shrink-0">OpenCode Go</span>
          <span id="hero-status-pill" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-slate-100 text-slate-600 border border-slate-200 min-w-0 max-w-[260px] truncate" title="">
            <span class="w-1.5 h-1.5 rounded-full bg-slate-400 shrink-0"></span>
            <span class="truncate">待查询</span>
          </span>
        </div>
        <div id="hero-sub" class="text-[11px] text-slate-400 mt-0.5 font-mono whitespace-nowrap truncate">在左侧选择或添加 Key 后查询</div>
      </div>

      <div class="flex items-center gap-1.5 shrink-0 flex-nowrap whitespace-nowrap">
        <div class="inline-flex items-center gap-1.5 bg-white hover:bg-slate-50 border border-slate-200/90 text-slate-600 rounded-md px-2.5 py-1.5 text-xs font-medium transition shadow-2xs focus-within:border-blue-400 focus-within:ring-2 focus-within:ring-blue-100/60 shrink-0 whitespace-nowrap">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-slate-400 shrink-0">
            <circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline>
          </svg>
          <select id="auto-refresh-select" onchange="onAutoRefreshChange(this.value)" title="定时自动刷新额度" class="bg-transparent border-none text-slate-600 text-xs font-medium outline-none cursor-pointer pr-0.5 whitespace-nowrap shrink-0">
            <option value="0">自动刷新: 关</option>
            <option value="300">每 5 分钟</option>
            <option value="900">每 15 分钟</option>
            <option value="1800">每 30 分钟</option>
          </select>
        </div>
        <button id="btn-query-current" onclick="doQueryCurrent()" class="btn-action btn-action-primary shadow-xs">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
            <circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>
          </svg>
          <span>立即查询</span>
        </button>
        <button id="btn-query-all" onclick="doQueryAll()" class="btn-action btn-action-secondary shadow-2xs">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
          </svg>
          <span>全部刷新</span>
        </button>
        <button onclick="openSettingsModal()" title="系统与外观设置" class="btn-action btn-action-secondary shadow-2xs">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06-.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
          <span>设置</span>
        </button>
      </div>
    </div>

    <!-- 3 Metric Cards -->
    <div class="grid grid-cols-3 gap-3">
      <!-- Card 1 -->
      <div id="card-c1" class="glass-card p-4 flex flex-col justify-between h-[142px] overflow-hidden">
        <div class="flex items-center justify-between gap-1.5 min-w-0">
          <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-600 shrink-0">
            <svg class="brand-stroke" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
              <circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline>
            </svg>
            <span id="c1-title">5小时窗口</span>
            <span id="c1-warn"></span>
          </div>
          <span id="c1-limit" class="text-[10.5px] font-mono font-medium px-2 py-0.5 rounded bg-slate-100 text-slate-600 truncate max-w-[140px] shrink-0" title="">$12.00</span>
        </div>
        <div>
          <div class="flex items-baseline gap-1">
            <span id="c1-num" class="text-[27px] font-extrabold tracking-tight text-slate-900">--</span>
            <span id="c1-unit" class="text-xs font-semibold text-slate-400">%</span>
          </div>
          <div class="capsule-track mt-1.5 mb-2">
            <div id="c1-bar" class="capsule-fill bg-emerald-500 shadow-sm" style="width: 0%;"></div>
          </div>
        </div>
        <div class="flex items-center justify-between text-[10.5px] pt-1 border-t border-slate-100 min-w-0">
          <span id="c1-used" class="text-slate-600 font-medium truncate min-w-0 mr-1.5" title="">已用 -- · 剩余 --</span>
          <span id="c1-reset" class="text-slate-400 whitespace-nowrap shrink-0 text-right" title="">-- 后重置</span>
        </div>
      </div>

      <!-- Card 2 -->
      <div id="card-c2" class="glass-card p-4 flex flex-col justify-between h-[142px] overflow-hidden">
        <div class="flex items-center justify-between gap-1.5 min-w-0">
          <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-600 shrink-0">
            <svg class="brand-stroke" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
              <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
              <line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line>
              <line x1="3" y1="10" x2="21" y2="10"></line>
            </svg>
            <span id="c2-title">每周窗口</span>
            <span id="c2-warn"></span>
          </div>
          <span id="c2-limit" class="text-[10.5px] font-mono font-medium px-2 py-0.5 rounded bg-slate-100 text-slate-600 truncate max-w-[140px] shrink-0" title="">$30.00</span>
        </div>
        <div>
          <div class="flex items-baseline gap-1">
            <span id="c2-num" class="text-[27px] font-extrabold tracking-tight text-slate-900">--</span>
            <span id="c2-unit" class="text-xs font-semibold text-slate-400">%</span>
          </div>
          <div class="capsule-track mt-1.5 mb-2">
            <div id="c2-bar" class="capsule-fill bg-blue-500 shadow-sm" style="width: 0%;"></div>
          </div>
        </div>
        <div class="flex items-center justify-between text-[10.5px] pt-1 border-t border-slate-100 min-w-0">
          <span id="c2-used" class="text-slate-600 font-medium truncate min-w-0 mr-1.5" title="">已用 -- · 剩余 --</span>
          <span id="c2-reset" class="text-slate-400 whitespace-nowrap shrink-0 text-right" title="">-- 后重置</span>
        </div>
      </div>

      <!-- Card 3 -->
      <div id="card-c3" class="glass-card p-4 flex flex-col justify-between h-[142px] overflow-hidden">
        <div class="flex items-center justify-between gap-1.5 min-w-0">
          <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-600 shrink-0">
            <svg class="brand-stroke" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
              <polyline points="23 6 13.5 15.5 8.5 10.5 1 18"></polyline>
              <polyline points="17 6 23 6 23 12"></polyline>
            </svg>
            <span id="c3-title">每月窗口</span>
            <span id="c3-warn"></span>
          </div>
          <span id="c3-limit" class="text-[10.5px] font-mono font-medium px-2 py-0.5 rounded bg-slate-100 text-slate-600 truncate max-w-[140px] shrink-0" title="">$60.00</span>
        </div>
        <div>
          <div class="flex items-baseline gap-1">
            <span id="c3-num" class="text-[27px] font-extrabold tracking-tight text-slate-900">--</span>
            <span id="c3-unit" class="text-xs font-semibold text-slate-400">%</span>
          </div>
          <div class="capsule-track mt-1.5 mb-2">
            <div id="c3-bar" class="capsule-fill bg-amber-500 shadow-sm" style="width: 0%;"></div>
          </div>
        </div>
        <div class="flex items-center justify-between text-[10.5px] pt-1 border-t border-slate-100 min-w-0">
          <span id="c3-used" class="text-slate-600 font-medium truncate min-w-0 mr-1.5" title="">已用 -- · 剩余 --</span>
          <span id="c3-reset" class="text-slate-400 whitespace-nowrap shrink-0 text-right" title="">-- 后重置</span>
        </div>
      </div>
    </div>

    <!-- Bottom Panel -->
    <div class="glass-card flex-1 p-3.5 flex flex-col min-h-0">
      <div class="flex items-center justify-between pb-2.5 border-b border-slate-100 gap-2 flex-nowrap min-w-0 overflow-x-auto">
        <div class="pivot-container shrink-0 flex-nowrap">
          <button id="tab-btn-0" onclick="switchTab(0)" class="pivot-btn active shrink-0 whitespace-nowrap">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0">
              <line x1="18" y1="20" x2="18" y2="10"></line>
              <line x1="12" y1="20" x2="12" y2="4"></line>
              <line x1="6" y1="20" x2="6" y2="14"></line>
            </svg>
            <span class="whitespace-nowrap">用量与模型分析</span>
          </button>
          <button id="tab-btn-1" onclick="switchTab(1)" class="pivot-btn shrink-0 whitespace-nowrap">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0">
              <polyline points="4 17 10 11 4 5"></polyline>
              <line x1="12" y1="19" x2="20" y2="19"></line>
            </svg>
            <span class="whitespace-nowrap">原始技术回执</span>
          </button>
        </div>

        <div id="tab-filters" class="flex items-center gap-1.5 text-xs shrink-0 flex-nowrap whitespace-nowrap">
          <!-- 1. 搜索模型 -->
          <div class="toolbar-chip" id="model-search-wrapper" onclick="document.getElementById('model-search-input').focus()">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" class="opacity-60 shrink-0">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <input id="model-search-input" type="text" placeholder="搜索模型..." oninput="onSearchModels(this.value)" class="toolbar-chip-input w-16 focus:w-24 transition-all shrink-0">
          </div>

          <!-- 2. 周期与指定日期选择 -->
          <div class="toolbar-chip" id="period-filter-chip">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="opacity-60 shrink-0">
              <circle cx="12" cy="12" r="10"></circle>
              <polyline points="12 6 12 12 16 14"></polyline>
            </svg>
            <select id="period-select" onchange="onPeriodChange(this.value)" class="toolbar-chip-select font-medium cursor-pointer shrink-0">
              <option value="近7天" selected>近 7 天</option>
              <option value="今日">今日</option>
              <option value="本月">本月</option>
              <option value="近30天">近 30 天</option>
              <option value="全部">全部记录</option>
              <option value="custom">指定日期...</option>
            </select>
            <input type="date" id="custom-date-picker" onchange="onCustomDateChange(this.value)" class="hidden toolbar-chip-date shrink-0" title="选择指定日期">
          </div>

          <!-- 3. 仅当前 Key 过滤 (精致交互胶囊) -->
          <button type="button" id="filter-only-key-btn" onclick="toggleOnlyKeyFilter()" class="toolbar-chip cursor-pointer active" title="当前：仅查看当前选中 Key 消耗（点击切换为全渠道汇总）">
            <svg id="filter-only-key-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="shrink-0 transition-transform">
              <path d="M21 2l-2 2m-1.5 1.5L16 7m-1.5 1.5L13 10m-1.5 1.5L10 13m0 0l-4 4a2.828 2.828 0 1 1-4-4l4-4m4 4l-4-4"></path>
              <circle cx="7.5" cy="16.5" r="1.5"></circle>
            </svg>
            <span id="filter-only-key-text" class="select-none">仅当前 Key</span>
          </button>

          <!-- 4. 复制表格 (统一风格按钮) -->
          <button type="button" onclick="copyModelTable()" title="复制用量数据为 Markdown 表格" class="toolbar-chip cursor-pointer">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="opacity-60 shrink-0">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
            <span class="shrink-0">复制表格</span>
          </button>

          <!-- 5. 数据源徽章与提示 -->
          <span id="stats-ledger-badge" class="toolbar-badge">dsh账本</span>
          <span id="stats-hint" class="text-xs text-amber-500 font-medium shrink-0 whitespace-nowrap"></span>
        </div>
      </div>

      <!-- Tab 0: Analytics -->
      <div id="tab-content-0" class="flex-1 pt-2.5 flex flex-col min-h-0">
        <!-- 4 Mini KPIs -->
        <div class="grid grid-cols-4 gap-2.5 mb-2.5">
          <div class="mini-kpi">
            <span class="text-[10.5px] text-slate-400">调用次数</span>
            <span id="kpi-sessions" class="text-sm font-bold text-slate-800">-- 次</span>
          </div>
          <div class="mini-kpi">
            <span class="text-[10.5px] text-slate-400">总消耗 Tokens</span>
            <span id="kpi-tokens" class="text-sm font-bold text-slate-800">-- 含缓存</span>
          </div>
          <div class="mini-kpi">
            <span class="text-[10.5px] text-slate-400">缓存命中 Tokens</span>
            <span id="kpi-cache" class="text-sm font-bold text-emerald-600">--</span>
          </div>
          <div class="mini-kpi">
            <span class="text-[10.5px] text-slate-400">账本折合费用</span>
            <span id="kpi-cost" class="text-sm font-bold text-slate-800">--</span>
          </div>
        </div>

        <!-- 折叠式：近期用量趋势走向折线卡片（平时极窄折叠，点击展开；支持勾选Token、费用、次数） -->
        <div class="trend-card-box mb-2 bg-slate-50/70 border border-slate-200/80 rounded-lg p-2 transition-all shadow-2xs">
          <div class="flex items-center justify-between cursor-pointer select-none flex-nowrap whitespace-nowrap" onclick="toggleTrendCard()" title="点击展开/折叠近期用量走势图">
            <div class="flex items-center gap-2 shrink-0 whitespace-nowrap">
              <span class="text-xs font-semibold text-slate-700 flex items-center gap-1.5 whitespace-nowrap shrink-0">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" class="text-blue-500 shrink-0"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"></polyline><polyline points="17 6 23 6 23 12"></polyline></svg>
                近期用量趋势走向
              </span>
              <span id="trend-summary-text" class="text-[10.5px] text-slate-400 font-mono whitespace-nowrap shrink-0">近 7 天走向</span>
            </div>
            <div class="flex items-center gap-2 shrink-0 whitespace-nowrap">
              <span id="trend-status-hint" class="text-[10.5px] text-slate-400 font-medium whitespace-nowrap shrink-0">(点击展开)</span>
              <svg id="trend-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" class="text-slate-400 transition-transform duration-200 shrink-0"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </div>
          </div>
          <!-- 展开内容区 -->
          <div id="trend-card-panel" class="hidden mt-2 pt-2 border-t border-slate-200/60">
            <div class="flex items-center justify-between mb-1.5 px-1 flex-nowrap whitespace-nowrap">
              <div class="flex items-center gap-3 text-[11px] text-slate-600 shrink-0 flex-nowrap whitespace-nowrap">
                <label class="flex items-center gap-1.5 cursor-pointer select-none shrink-0 whitespace-nowrap">
                  <input type="checkbox" id="chk-trend-tokens" checked onchange="drawTrendChart()" class="rounded border-slate-300 text-sky-500 focus:ring-sky-400 cursor-pointer shrink-0">
                  <span class="w-2 h-2 rounded-full bg-sky-400 inline-block shrink-0"></span>
                  <span class="whitespace-nowrap">Tokens (百万)</span>
                </label>
                <label class="flex items-center gap-1.5 cursor-pointer select-none shrink-0 whitespace-nowrap">
                  <input type="checkbox" id="chk-trend-cost" checked onchange="drawTrendChart()" class="rounded border-slate-300 text-orange-400 focus:ring-orange-400 cursor-pointer shrink-0">
                  <span class="w-2 h-2 rounded-full bg-orange-400 inline-block shrink-0"></span>
                  <span class="whitespace-nowrap">折合费用 ($)</span>
                </label>
                <label class="flex items-center gap-1.5 cursor-pointer select-none shrink-0 whitespace-nowrap">
                  <input type="checkbox" id="chk-trend-calls" checked onchange="drawTrendChart()" class="rounded border-slate-300 text-purple-400 focus:ring-purple-400 cursor-pointer shrink-0">
                  <span class="w-2 h-2 rounded-full bg-purple-400 inline-block shrink-0"></span>
                  <span class="whitespace-nowrap">请求次数</span>
                </label>
              </div>
              <span class="text-[10px] text-slate-400 shrink-0 whitespace-nowrap">悬停查看详情</span>
            </div>
            <!-- SVG 折线图容器 -->
            <div id="trend-chart-container" class="trend-chart-inner relative w-full h-[120px] bg-white/70 rounded border border-slate-200/60 overflow-visible">
              <svg id="trend-svg" class="w-full h-full" preserveAspectRatio="none"></svg>
              <!-- 悬浮 Tooltip -->
              <div id="trend-tooltip" class="hidden absolute pointer-events-none z-30 bg-slate-900/95 text-white rounded-md px-2.5 py-1.5 text-[11px] shadow-xl backdrop-blur-md transition-opacity duration-150" style="min-width: 175px;"></div>
            </div>
          </div>
        </div>

        <!-- 模型 Token 占比条（无图例文字） -->
        <div class="mb-2">
          <div class="flex items-center justify-between text-[11px] text-slate-500 mb-1">
            <span class="font-medium">模型 Token 占比</span>
          </div>
          <div id="stacked-bar" class="stacked-bar-container"></div>
        </div>

        <!-- 模型列表（表头与数据行处于同一滚动容器，保证同宽与严格居中对齐） -->
        <div class="flex-1 overflow-y-auto min-h-0 pr-0.5">
          <div id="model-table-header" class="sticky top-0 bg-white z-10 flex items-center gap-3 text-[11px] text-slate-400 font-medium px-1.5 py-1.5 border-b border-slate-100 select-none whitespace-nowrap">
            <div class="flex-1 min-w-0 pl-1 text-left whitespace-nowrap">模型</div>
            <div class="w-[58px] text-center cursor-pointer hover:text-slate-600 transition shrink-0 whitespace-nowrap" onclick="sortBy('count')" id="hdr-count" title="点击按请求次数排序">请求次数<span class="sort-ind"></span></div>
            <div class="w-[58px] text-center cursor-pointer hover:text-slate-600 transition shrink-0 whitespace-nowrap" onclick="sortBy('input')" id="hdr-input" title="点击按输入 Tokens 排序">输入<span class="sort-ind"></span></div>
            <div class="w-[58px] text-center cursor-pointer hover:text-slate-600 transition shrink-0 whitespace-nowrap" onclick="sortBy('output')" id="hdr-output" title="点击按输出 Tokens（含推理）排序">输出<span class="sort-ind"></span></div>
            <div class="w-[58px] text-center cursor-pointer hover:text-slate-600 transition shrink-0 whitespace-nowrap" onclick="sortBy('cache')" id="hdr-cache" title="点击按缓存 Tokens 排序">缓存<span class="sort-ind"></span></div>
            <div class="w-[62px] text-center cursor-pointer hover:text-slate-600 transition shrink-0 whitespace-nowrap" onclick="sortBy('tokens')" id="hdr-tokens" title="点击按总 Tokens（输入+输出+缓存）排序">总Token<span class="sort-ind"></span></div>
            <div class="w-[54px] text-center cursor-pointer hover:text-slate-600 transition shrink-0 whitespace-nowrap" onclick="sortBy('rate')" id="hdr-rate" title="点击按总缓存率排序">缓存率<span class="sort-ind"></span></div>
            <div class="w-[78px] text-center cursor-pointer hover:text-slate-600 transition shrink-0 whitespace-nowrap" onclick="sortBy('cost')" id="hdr-cost" title="点击按实际花费排序"><span class="inline-flex items-center justify-center gap-0.5 whitespace-nowrap">费用<span class="sort-ind"></span></span><span id="ccy-toggle" onclick="event.stopPropagation(); toggleCostCcy()" title="切换币种（美元 / 人民币）" class="ml-1 px-1 py-[1px] rounded bg-slate-100 hover:bg-slate-200 text-slate-500 font-mono text-[10px] cursor-pointer align-middle shrink-0 whitespace-nowrap">$</span></div>
          </div>

          <!-- Model Leaderboard -->
          <div id="model-list" class="space-y-0.5 pt-0.5">
            <div class="text-xs text-slate-400 text-center py-6">正在载入模型用量...</div>
          </div>
          <!-- Model Summary Footer Row -->
          <div id="model-summary-row" class="summary-row sticky bottom-0 z-10" style="display:none;"></div>
        </div>
      </div>

      <!-- Tab 1: Raw JSON -->
      <div id="tab-content-1" class="flex-1 pt-2.5 hidden overflow-hidden flex flex-col">
        <div class="flex items-center justify-between mb-2">
          <span class="text-[11px] text-slate-400 font-mono">GET /zen/go/v1/usage</span>
          <button onclick="copyRawJson()" class="text-xs text-blue-600 hover:text-blue-800 font-medium flex items-center gap-1 border-none bg-transparent cursor-pointer">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
            <span>复制内容</span>
          </button>
        </div>
        <pre id="raw-json-pre" class="flex-1 p-3 rounded bg-slate-900 text-emerald-400 font-mono text-[11px] overflow-y-auto leading-relaxed border border-slate-800">暂无数据，点击上方“立即查询”获取最新额度。</pre>
      </div>
    </div>
  </div>
</div>

<script>
// Synchronously injected initial state from Python
let appState = __INITIAL_STATE_JSON__;
let currentKeyIndex = (appState && typeof appState.selected === 'number') ? appState.selected : 0;
let _persistedSelectedIndex = currentKeyIndex;
const DEFAULT_TIER_FALLBACK = 1600;   // 档位数据缺失时的兜底值

/* ---------- 通用小工具 ---------- */

/* 把字符串安全地放进 innerHTML / 属性。别名、模型名、远端返回的错误原文
   都可能带引号或尖括号，直接拼接会撑破 DOM（一个普通双引号就够了）。 */
function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* Key 掩码：短 Key 不能暴露首尾片段（旧实现 14 位以内的 Key 会被完整显示）。 */
function maskKey(k) {
    k = String(k == null ? '' : k);
    const n = k.length;
    if (n === 0) return '••••';
    if (n < 12) return '•'.repeat(n);
    if (n >= 20) return k.slice(0, 4) + '······' + k.slice(-4);
    return k.slice(0, 3) + '······' + k.slice(-3);
}

/* 把任意值格式化成有限数字，杜绝 NaN/Infinity 渲染到界面 */
function num(v, dflt) {
    const n = Number(v);
    return Number.isFinite(n) ? n : (dflt === undefined ? 0 : dflt);
}

function _formatPillPct(pct) {
    if (pct === undefined || pct === null || isNaN(pct) || pct <= 0) return '0%';
    if (pct >= 100) return '100%';
    if (pct < 0.05) return '<0.1%';
    if (Math.abs(Math.round(pct) - pct) < 0.01) return Math.round(pct) + '%';
    return pct.toFixed(1) + '%';
}

function _getKeyQuotaIndicator(it) {
    if (!it || !it.last_result) {
        return {
            rightHtml: '<span class="font-mono text-[10.5px] text-slate-400">待查询</span>',
            bottomTrack: ''
        };
    }
    const ch = it.channel || 'opencode-go';
    const res = it.last_result;
    let pct = 0;
    let title = '';

    if (ch === 'stepfun') {
        const est = res.estimate || {};
        const used = parseFloat(est.credit_used_m || 0);
        const tier = parseFloat(it.step_tier || res.tier || 1600);
        pct = tier > 0 ? (used / tier * 100) : 0;
        const rem = Math.max(0, tier - used);
        title = `阶跃月池已用: ${used.toFixed(1)}M / ${tier}M (${pct.toFixed(1)}%) · 剩余: ${rem.toFixed(1)}M`;
    } else if (ch === 'commandcode') {
        const credits = res.credits || {};
        const w = credits.weekly || {};
        const f = credits.fiveHour || {};
        const wUsed = parseFloat(w.used || 0);
        const wCap = parseFloat(w.cap || 0);
        const fUsed = parseFloat(f.used || 0);
        const fCap = parseFloat(f.cap || 0);
        const wPct = wCap > 0 ? (wUsed / wCap * 100) : 0;
        const fPct = fCap > 0 ? (fUsed / fCap * 100) : 0;
        const exceeded = w.exceeded === true || f.exceeded === true;

        pct = (fPct >= 70 && fPct > wPct) ? fPct : wPct;
        title = `Command Code · 周: $${wUsed.toFixed(2)} / $${wCap > 0 ? wCap.toFixed(2) : '--'} (${wPct.toFixed(1)}%) · 5h: $${fUsed.toFixed(2)} / $${fCap > 0 ? fCap.toFixed(2) : '--'} (${fPct.toFixed(1)}%)` + (exceeded ? ' · ⚠️ 已超限' : '');
    } else if (ch === 'grok-build') {
        const quota = res.quota || {};
        if (quota.success && (quota.remaining_percent !== undefined || quota.used_percent !== undefined)) {
            const usedPct = quota.used_percent !== undefined ? parseFloat(quota.used_percent) : Math.max(0, 100 - parseFloat(quota.remaining_percent));
            const remPct = quota.remaining_percent !== undefined ? parseFloat(quota.remaining_percent) : Math.max(0, 100 - usedPct);
            pct = usedPct;
            title = `${esc(quota.tier || 'SuperGrok Heavy')} · 周额度已用: ${pct.toFixed(1)}% · 剩余: ${remPct.toFixed(1)}%`;
        } else {
            const weekTok = (res.week && res.week.tokens) || (res.total && res.total.tokens) || 0;
            const weekTokStr = weekTok >= 1e9 ? ((weekTok/1e9).toFixed(1) + 'B') : ((weekTok/1e6).toFixed(0) + 'M');
            return {
                rightHtml: `<span class="key-status-text font-mono text-[11px]" title="近7天消耗 ${weekTokStr}">${weekTokStr}</span>`,
                bottomTrack: ''
            };
        }
    } else if (ch === 'cline') {
        const plan = res.plan || {};
        const win = res.windows || {};
        const num = (k) => {
            const w = win[k] || {};
            return (w.percent === undefined || w.percent === null) ? null : parseFloat(w.percent);
        };
        const fPct = num('rolling'), wPct = num('weekly'), mPct = num('monthly');
        const all = [fPct, wPct, mPct].filter(v => v !== null && !isNaN(v));
        pct = (fPct !== null && fPct >= 70 && fPct > (wPct || 0)) ? fPct : (wPct !== null ? wPct : (fPct !== null ? fPct : (mPct !== null ? mPct : 0)));
        const state = plan.active ? (plan.canceledAt ? '已取消(到期前可用)' : '生效中') : '未生效';
        const fmt = (v) => (v === null || isNaN(v)) ? '--' : (v.toFixed(1) + '%');
        title = `${esc(plan.name || 'Cline Pass')} · ${state} · 5h: ${fmt(fPct)} · 周: ${fmt(wPct)} · 月: ${fmt(mPct)}`;
        if (all.length === 0) {
            return {
                rightHtml: `<span class="font-mono text-[10.5px] text-slate-400" title="${esc(title)}">额度未知</span>`,
                bottomTrack: ''
            };
        }
    } else {
        // opencode-go
        const usage = res.usage || {};
        const winRoll = usage.rolling || {};
        const winWeek = usage.weekly || {};
        const winMonth = usage.monthly || {};
        const pRoll = winRoll.percent !== undefined && winRoll.percent !== null ? parseFloat(winRoll.percent) : 0;
        const pWeek = winWeek.percent !== undefined && winWeek.percent !== null ? parseFloat(winWeek.percent) : 0;
        const pMonth = winMonth.percent !== undefined && winMonth.percent !== null ? parseFloat(winMonth.percent) : 0;

        pct = (pRoll >= 70 && pRoll > pWeek) ? pRoll : (pWeek > 0 ? pWeek : (pMonth > 0 ? pMonth : 0));
        title = `OpenCode Go · 周窗口: ${pWeek.toFixed(1)}% · 5h: ${pRoll.toFixed(1)}% · 月窗口: ${pMonth.toFixed(1)}%`;
    }

    // title 会进 innerHTML 的属性位，必须转义（title 里含 plan.name / quota.tier 等外部字符串）
    title = esc(title);
    let statusCls = 'normal';
    let barClass = '';

    if (pct >= 90) {
        statusCls = 'danger';
        barClass = 'danger';
    } else if (pct >= 70) {
        statusCls = 'warning';
        barClass = 'warning';
    }

    const rightHtml = `<span class="key-status-text ${statusCls} font-mono text-[11.5px] font-semibold flex items-center gap-1.5" title="${title}"><span class="key-status-dot ${statusCls}"></span>${_formatPillPct(pct)}</span>`;
    const bottomTrack = `<div class="key-progress-track"><div class="key-progress-bar ${barClass}" style="width: ${Math.min(100, Math.max(0, pct))}%;"></div></div>`;

    return { rightHtml, bottomTrack };
}

let channelCollapsedState = {};

const CHANNEL_DEFS = [
    { id: 'opencode-go', name: 'OpenCode Go', short: 'Go', badgeClass: 'badge-go' },
    { id: 'commandcode', name: 'Command Code', short: 'CC', badgeClass: 'badge-cc' },
    { id: 'stepfun', name: '阶跃星辰 · StepFun', short: '阶跃', badgeClass: 'badge-step' },
    { id: 'grok-build', name: 'Grok Build', short: 'Grok', badgeClass: 'badge-grok' },
    { id: 'cline', name: 'Cline Pass', short: 'Cline', badgeClass: 'badge-cline' },
];

function toggleChannelCollapse(channelId) {
    channelCollapsedState[channelId] = !channelCollapsedState[channelId];
    const bodyEl = document.getElementById(`channel-body-${channelId}`);
    const chevEl = document.getElementById(`channel-chev-${channelId}`);
    if (bodyEl) {
        bodyEl.style.display = channelCollapsedState[channelId] ? 'none' : 'block';
    }
    if (chevEl) {
        if (channelCollapsedState[channelId]) {
            chevEl.classList.add('-rotate-90');
        } else {
            chevEl.classList.remove('-rotate-90');
        }
    }
}

function renderKeyList() {
    const container = document.getElementById('key-list-container');
    const countBadge = document.getElementById('sidebar-keys-count');
    if (!container) return;
    container.innerHTML = '';

    const keys = (appState && appState.keys) ? appState.keys : [];
    if (countBadge) countBadge.innerText = `已存密钥 (${keys.length})`;

    if (keys.length === 0) {
        container.innerHTML = '<div class="text-xs text-slate-400 p-3 text-center">暂无密钥，点击上方＋添加</div>';
        return;
    }

    // 按渠道分组分桶
    const groups = {};
    CHANNEL_DEFS.forEach(c => { groups[c.id] = []; });
    const otherKeys = [];

    keys.forEach((it, idx) => {
        const ch = it.channel || 'opencode-go';
        if (groups[ch]) {
            groups[ch].push({ item: it, originalIndex: idx });
        } else {
            otherKeys.push({ item: it, originalIndex: idx });
        }
    });

    // 渲染各渠道手风琴
    CHANNEL_DEFS.forEach(c => {
        const list = groups[c.id] || [];
        if (list.length === 0) return; // 暂无该渠道密钥则不占位

        const isCollapsed = !!channelCollapsedState[c.id];
        const groupDiv = document.createElement('div');
        groupDiv.className = 'channel-group mb-2';
        groupDiv.id = `channel-group-${c.id}`;

        const headerDiv = document.createElement('div');
        headerDiv.className = 'channel-header';
        headerDiv.onclick = () => toggleChannelCollapse(c.id);
        headerDiv.innerHTML = `
            <div class="flex items-center gap-1.5 min-w-0">
                <svg id="channel-chev-${c.id}" class="channel-chevron w-3.5 h-3.5 ${isCollapsed ? '-rotate-90' : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
                <span class="channel-header-title truncate">${c.name}</span>
            </div>
            <span class="channel-count-badge">${list.length}</span>
        `;
        groupDiv.appendChild(headerDiv);

        const bodyDiv = document.createElement('div');
        bodyDiv.id = `channel-body-${c.id}`;
        bodyDiv.className = 'channel-body space-y-1 mt-1';
        bodyDiv.style.display = isCollapsed ? 'none' : 'block';

        list.forEach(({ item: it, originalIndex: idx }) => {
            const isStep = c.id === 'stepfun';
            const badgeText = isStep ? `月池 ¥${it.step_tier || 1600}` : c.short;
            const mask = (c.id === 'grok-build') ? '本地免密 · 会话库' : maskKey(it.key);
            const qInfo = _getKeyQuotaIndicator(it);

            const card = document.createElement('div');
            card.id = `key-${idx}`;
            card.setAttribute('data-key-index', idx);
            card.className = `key-item ${idx === currentKeyIndex ? 'active' : ''}`;
            card.onclick = () => selectKey(idx);
            card.ondblclick = () => doQueryCurrent();
            card.innerHTML = `
                <div class="flex items-center justify-between mb-1">
                    <span class="text-[13px] font-semibold tracking-tight truncate max-w-[125px]" title="${esc(it.alias || '')}">${esc(it.alias || ('Key-' + (idx + 1)))}</span>
                    <span class="badge ${c.badgeClass}">${esc(badgeText)}</span>
                </div>
                <div class="flex items-center justify-between">
                    <span class="key-mask text-[11px] font-mono tracking-tight text-slate-400">${esc(mask)}</span>
                    ${qInfo.rightHtml}
                </div>
                ${qInfo.bottomTrack}
            `;
            bodyDiv.appendChild(card);
        });

        groupDiv.appendChild(bodyDiv);
        container.appendChild(groupDiv);
    });

    // 兜底渲染未知渠道
    if (otherKeys.length > 0) {
        const isCollapsed = !!channelCollapsedState['other'];
        const groupDiv = document.createElement('div');
        groupDiv.className = 'channel-group mb-2';
        groupDiv.id = 'channel-group-other';

        const headerDiv = document.createElement('div');
        headerDiv.className = 'channel-header';
        headerDiv.onclick = () => toggleChannelCollapse('other');
        headerDiv.innerHTML = `
            <div class="flex items-center gap-1.5 min-w-0">
                <svg id="channel-chev-other" class="channel-chevron w-3.5 h-3.5 ${isCollapsed ? '-rotate-90' : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
                <span class="channel-header-title truncate">其他渠道</span>
            </div>
            <span class="channel-count-badge">${otherKeys.length}</span>
        `;
        groupDiv.appendChild(headerDiv);

        const bodyDiv = document.createElement('div');
        bodyDiv.id = 'channel-body-other';
        bodyDiv.className = 'channel-body space-y-1 mt-1';
        bodyDiv.style.display = isCollapsed ? 'none' : 'block';

        otherKeys.forEach(({ item: it, originalIndex: idx }) => {
            const mask = maskKey(it.key);
            const qInfo = _getKeyQuotaIndicator(it);

            const card = document.createElement('div');
            card.id = `key-${idx}`;
            card.setAttribute('data-key-index', idx);
            card.className = `key-item ${idx === currentKeyIndex ? 'active' : ''}`;
            card.onclick = () => selectKey(idx);
            card.ondblclick = () => doQueryCurrent();
            card.innerHTML = `
                <div class="flex items-center justify-between mb-1">
                    <span class="text-[13px] font-semibold tracking-tight truncate max-w-[125px]" title="${esc(it.alias || '')}">${esc(it.alias || ('Key-' + (idx + 1)))}</span>
                    <span class="badge badge-go">${esc(it.channel || '未知')}</span>
                </div>
                <div class="flex items-center justify-between">
                    <span class="key-mask text-[11px] font-mono tracking-tight text-slate-400">${esc(mask)}</span>
                    ${qInfo.rightHtml}
                </div>
                ${qInfo.bottomTrack}
            `;
            bodyDiv.appendChild(card);
        });

        groupDiv.appendChild(bodyDiv);
        container.appendChild(groupDiv);
    }
}

function selectKey(idx) {
    const keys = (appState && appState.keys) ? appState.keys : [];
    if (keys.length === 0) {
        currentKeyIndex = 0;
        const hAlias = document.getElementById('hero-alias');
        if (hAlias) hAlias.innerText = '请选择密钥';
        const chBadge = document.getElementById('hero-channel-badge');
        if (chBadge) { chBadge.innerText = '未添加'; chBadge.className = 'badge badge-go'; }
        const hSub = document.getElementById('hero-sub');
        if (hSub) hSub.innerText = '暂无可用密钥，请点击上方“+”添加';
        resetCardsEmpty('opencode-go');
        // 旧实现直接 return，底部的 KPI / 模型表 / 趋势图仍显示被删掉那个 Key 的数据
        clearTokenStatsPanels();
        return;
    }
    if (idx < 0 || idx >= keys.length) {
        idx = 0;
    }
    currentKeyIndex = idx;
    // 记住选中项，重启后恢复（旧版 set_selected 接口存在但前端从未调用）
    if (idx !== _persistedSelectedIndex) {
        _persistedSelectedIndex = idx;
        if (window.pywebview && window.pywebview.api) apiCall('set_selected', [idx], 10000);
    }
    document.querySelectorAll('.key-item').forEach(el => {
        const kIdx = parseInt(el.getAttribute('data-key-index'), 10);
        if (kIdx === idx) {
            el.className = 'key-item active';
        } else {
            el.className = 'key-item';
        }
    });
    const it = keys[idx];
    if (!it) return;

    const ch = it.channel || 'opencode-go';
    // 未知渠道在 DOM 里归到 other 分组；旧实现直接用渠道名取 id，取不到就静默不展开
    const groupId = ['opencode-go', 'commandcode', 'stepfun', 'grok-build', 'cline'].includes(ch) ? ch : 'other';
    // 若当前选中的 Key 所在分组处于折叠状态，自动展开以确保可见
    if (channelCollapsedState[groupId]) {
        channelCollapsedState[groupId] = false;
        const bodyEl = document.getElementById(`channel-body-${groupId}`);
        const chevEl = document.getElementById(`channel-chev-${groupId}`);
        if (bodyEl) bodyEl.style.display = 'block';
        if (chevEl) chevEl.classList.remove('-rotate-90');
    }

    const isGo = ch === 'opencode-go';
    const isCC = ch === 'commandcode';
    const isGrok = ch === 'grok-build';
    const isCline = ch === 'cline';
    document.getElementById('hero-alias').innerText = it.alias || `Key-${idx+1}`;
    const chBadge = document.getElementById('hero-channel-badge');
    chBadge.innerText = isGo ? 'OpenCode Go' : (isCC ? 'Command Code' : (isGrok ? 'Grok Build' : (isCline ? 'Cline Pass' : `阶跃星辰 · ${it.step_tier || 1600}M`)));
    chBadge.className = `badge ${isGo ? 'badge-go' : (isCC ? 'badge-cc' : (isGrok ? 'badge-grok' : (isCline ? 'badge-cline' : 'badge-step')))}`;
    
    const mask = isGrok ? '本地免密 · ~/.grok/sessions' : maskKey(it.key);
    document.getElementById('hero-sub').innerText = `${mask}  ·  最后更新: ${it.last_update || '未查询'}${it.last_error ? '  ·  ⚠ 上次失败' : ''}`;

    if (it.last_result) {
        renderResultData(it.last_result, it);
    } else {
        resetCardsEmpty(ch);
    }

    // 切换 Key 时同步刷新底部用量表（"仅当前 Key" 勾选时尤其必要）
    if (window.pywebview && window.pywebview.api && typeof loadTokenStats === 'function') {
        loadTokenStats();
    }
}

function _applyCardWarning(cardId, warnId, pct) {
    const card = document.getElementById(cardId);
    const warn = document.getElementById(warnId);
    if (!card) return;
    pct = parseFloat(pct) || 0;
    if (pct >= 90) {
        card.className = 'glass-card card-danger p-4 flex flex-col justify-between h-[142px] overflow-hidden';
        if (warn) warn.innerHTML = '<span class="badge-danger ml-1.5">高耗预警</span>';
    } else if (pct >= 85) {
        card.className = 'glass-card card-warning p-4 flex flex-col justify-between h-[142px] overflow-hidden';
        if (warn) warn.innerHTML = '<span class="badge-warning ml-1.5">额度注意</span>';
    } else {
        card.className = 'glass-card p-4 flex flex-col justify-between h-[142px] overflow-hidden';
        if (warn) warn.innerHTML = '';
    }
}

function resetCardsEmpty(ch) {
    ch = ch || 'opencode-go';
    ['card-c1', 'card-c2', 'card-c3'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.className = 'glass-card p-4 flex flex-col justify-between h-[142px] overflow-hidden';
    });
    ['c1-warn', 'c2-warn', 'c3-warn'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = '';
    });
    if (ch === 'commandcode') {
        document.getElementById('c1-title').innerText = '5小时额度';
        document.getElementById('c1-limit').innerText = '--';
        document.getElementById('c2-title').innerText = '每周额度';
        document.getElementById('c2-limit').innerText = '--';
        document.getElementById('c3-title').innerText = '月额度';
        document.getElementById('c3-limit').innerText = '--';
    } else if (ch === 'stepfun') {
        document.getElementById('c1-title').innerText = '月池 Credit (估算)';
        document.getElementById('c1-limit').innerText = '月池档位';
        document.getElementById('c2-title').innerText = 'API 账户余额';
        document.getElementById('c2-limit').innerText = '实时余额';
        document.getElementById('c3-title').innerText = '今日 Tokens';
        document.getElementById('c3-limit').innerText = 'dsh 账本';
    } else if (ch === 'cline') {
        document.getElementById('c1-title').innerText = '5小时窗口';
        document.getElementById('c1-limit').innerText = 'Cline Pass';
        document.getElementById('c2-title').innerText = '每周窗口';
        document.getElementById('c2-limit').innerText = '官方额度';
        document.getElementById('c3-title').innerText = '每月窗口';
        document.getElementById('c3-limit').innerText = '官方额度';
    } else if (ch === 'grok-build') {
        document.getElementById('c1-title').innerText = '每周额度';
        document.getElementById('c1-limit').innerText = 'SuperGrok Heavy';
        document.getElementById('c2-title').innerText = '今日用量';
        document.getElementById('c2-limit').innerText = '今日调用';
        document.getElementById('c3-title').innerText = '近7天累计';
        document.getElementById('c3-limit').innerText = '周期累计';
    } else {
        document.getElementById('c1-title').innerText = '5小时窗口';
        document.getElementById('c1-limit').innerText = '$12.00';
        document.getElementById('c2-title').innerText = '每周窗口';
        document.getElementById('c2-limit').innerText = '$30.00';
        document.getElementById('c3-title').innerText = '每月窗口';
        document.getElementById('c3-limit').innerText = '$60.00';
    }
    ['c1', 'c2', 'c3'].forEach(k => {
        document.getElementById(`${k}-num`).innerText = '--';
        document.getElementById(`${k}-unit`).innerText = '%';
        const barEl = document.getElementById(`${k}-bar`);
        barEl.style.display = '';
        barEl.style.width = '0%';
        document.getElementById(`${k}-used`).innerText = '已用 -- · 剩余 --';
        document.getElementById(`${k}-reset`).innerText = '-- 后重置';
    });
    document.getElementById('hero-status-pill').innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-slate-400"></span> 待查询';
}

function renderResultData(data, item) {
    if (!data) return;
    try {
        const ch = data.channel || (item && item.channel) || 'opencode-go';
        if (ch === 'stepfun') {
            renderStepFun(data, item);
        } else if (ch === 'commandcode') {
            renderCommandCode(data, item);
        } else if (ch === 'grok-build') {
            renderGrokBuild(data, item);
        } else if (ch === 'cline') {
            renderCline(data, item);
        } else {
            renderGo(data);
        }
        const pre = document.getElementById('raw-json-pre');
        if (pre) pre.innerText = JSON.stringify(data, null, 2);
    } catch(err) {
        console.error("renderResultData error:", err);
    }
}

/* 没有数据时把「原始技术回执」恢复成占位提示，避免残留上一个 Key 的 JSON */
function resetRawJsonPane() {
    const pre = document.getElementById('raw-json-pre');
    if (pre) pre.innerText = '暂无数据，点击上方“立即查询”获取最新额度。';
}

function formatCountdown(resetsAt) {
    if (!resetsAt) return '-- 后重置';
    const target = new Date(resetsAt).getTime();
    const now = Date.now();
    const sec = Math.floor((target - now) / 1000);
    if (isNaN(sec) || sec <= 0) return '已到重置期';
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return `${d}天 ${h}小时 后重置`;
    if (h > 0) return `${h}小时 ${m}分钟 后重置`;
    if (m > 0) return `${m}分钟 后重置`;
    return '即将重置';
}

function renderGo(data) {
    document.getElementById('c1-title').innerText = '5小时窗口';
    document.getElementById('c1-limit').innerText = '$12.00';
    document.getElementById('c2-title').innerText = '每周窗口';
    document.getElementById('c2-limit').innerText = '$30.00';
    document.getElementById('c3-title').innerText = '每月窗口';
    document.getElementById('c3-limit').innerText = '$60.00';

    const usage = (data.usage || {});
    const limits = { rolling: 12, weekly: 30, monthly: 60 };
    const mapping = { rolling: 'c1', weekly: 'c2', monthly: 'c3' };

    for (const [name, cardId] of Object.entries(mapping)) {
        const u = usage[name] || {};
        const p = u.percent !== undefined ? parseFloat(u.percent) : null;
        if (p !== null && !isNaN(p)) {
            document.getElementById(`${cardId}-num`).innerText = p.toFixed(1);
            document.getElementById(`${cardId}-unit`).innerText = '%';
            const bar = document.getElementById(`${cardId}-bar`);
            bar.style.display = '';
            bar.style.width = Math.min(100, Math.max(0, p)) + '%';
            
            let colorClass = 'bg-emerald-500';
            if (p >= 90) colorClass = 'bg-rose-500';
            else if (p >= 70) colorClass = 'bg-amber-500';
            else if (p >= 40) colorClass = 'bg-blue-500';
            bar.className = `capsule-fill ${colorClass} shadow-sm`;

            const lim = limits[name];
            const used = lim * p / 100;
            document.getElementById(`${cardId}-used`).innerText = `已用 $${used.toFixed(2)} · 剩余 $${(lim - used).toFixed(2)}`;
            document.getElementById(`${cardId}-reset`).innerText = formatCountdown(u.resetsAt);
            _applyCardWarning(`card-${cardId}`, `${cardId}-warn`, p);
        } else {
            _applyCardWarning(`card-${cardId}`, `${cardId}-warn`, 0);
        }
    }
    const ub = data.useBalance;
    const pill = document.getElementById('hero-status-pill');
    if (ub) {
        pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-emerald-50 text-emerald-700 border border-emerald-200';
        pill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span> 正常可用 · useBalance 开启';
    } else {
        pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-amber-50 text-amber-700 border border-amber-200';
        pill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span> useBalance 关闭';
    }
}

function renderStepFun(data, item) {
    const est = data.estimate || {};
    const acc = data.account || {};
    const tier = (item && item.step_tier) || data.tier || 1600;
    const usedM = parseFloat(est.credit_used_m || 0);

    document.getElementById('c1-title').innerText = '月池消耗 (估算)';
    document.getElementById('c1-limit').innerText = `月池 ¥${tier}`;
    document.getElementById('c2-title').innerText = 'API 账户余额';
    document.getElementById('c2-limit').innerText = acc.type || '官方余额';
    document.getElementById('c3-title').innerText = '今日 Tokens';
    document.getElementById('c3-limit').innerText = 'dsh 账本';

    const pct = tier > 0 ? (usedM / tier * 100) : 0;
    _applyCardWarning('card-c1', 'c1-warn', pct);
    _applyCardWarning('card-c2', 'c2-warn', 0);
    _applyCardWarning('card-c3', 'c3-warn', 0);
    document.getElementById('c1-num').innerText = pct.toFixed(1);
    document.getElementById('c1-unit').innerText = '%';
    document.getElementById('c1-bar').style.width = Math.min(100, pct) + '%';
    // 单位说明：1M Credit = ¥1，所以 credit_used_m 的数值就等于人民币金额
    document.getElementById('c1-used').innerText =
        `已用 ¥${usedM.toFixed(1)} · 剩余 ¥${Math.max(0, tier - usedM).toFixed(1)}`;
    document.getElementById('c1-reset').innerText = '每月 1 日 00:00 重置';
    const c1num = document.getElementById('c1-num');
    if (c1num) c1num.title = `按官方单价估算：未命中输入×7 + 缓存命中×0.35 + 输出×20（元/百万tokens）\n`
        + `1M Credit = ¥1，所以「已用 ¥${usedM.toFixed(1)}」= 你实际消耗的账单价值\n`
        + `分母「月池 ¥${tier}」是在密钥里手动设置的档位（不是官方接口读的）——`
        + `如果这个数字和你的真实套餐不符，百分比就会失真。\n`
        + `官方未提供套餐用量接口（已实测 25 个端点），此处为本地估算。`;
    document.getElementById('c1-reset').title = c1num ? c1num.title : '';

    const bal = acc.balance !== undefined ? parseFloat(acc.balance).toFixed(2) : '--';
    document.getElementById('c2-num').innerText = bal;
    document.getElementById('c2-unit').innerText = '元';
    document.getElementById('c2-bar').style.width = '100%';
    document.getElementById('c2-bar').className = 'capsule-fill bg-amber-500 shadow-sm';
    document.getElementById('c2-used').innerText = `充值 ¥${parseFloat(acc.total_cash_balance||0).toFixed(2)} · 赠送 ¥${parseFloat(acc.total_voucher_balance||0).toFixed(2)}`;
    document.getElementById('c2-reset').innerText = '通道实时余额';

    const todayTok = (data.today && data.today.tokens) || 0;
    document.getElementById('c3-num').innerText = (todayTok / 1000000).toFixed(2);
    document.getElementById('c3-unit').innerText = 'M';
    // 今日 Tokens 没有"进度"含义，隐藏进度条（Go 渠道渲染时会恢复显示）
    document.getElementById('c3-bar').style.display = 'none';
    document.getElementById('c3-used').innerText = `今日调用 ${(data.today && data.today.calls) || 0} 次`;
    document.getElementById('c3-reset').innerText = '今日有效';

    document.getElementById('hero-status-pill').className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-amber-50 text-amber-700 border border-amber-200';
    const stepHint = data.hint || est.hint || (data.today && data.today.hint) || '';
    // stepHint 可能来自远端接口的错误原文，必须转义后再进 innerHTML
    document.getElementById('hero-status-pill').innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span> 本地估算中 · ' + esc(est.month || '') + (stepHint ? ' · ' + esc(stepHint) : '');
    if (stepHint) {
        document.getElementById('c1-used').innerText = stepHint;
    }
}

/* ================= Command Code 渲染 ================= */
function ccWindowCard(cardId, title, limitText, win) {
    document.getElementById(`${cardId}-title`).innerText = title;
    document.getElementById(`${cardId}-limit`).innerText = limitText;
    const used = parseFloat(win.used || 0);
    const cap = parseFloat(win.cap || 0);
    const exceeded = win.exceeded === true;
    const pct = cap > 0 ? Math.min(100, used / cap * 100) : 0;
    _applyCardWarning(`card-${cardId}`, `${cardId}-warn`, pct);
    document.getElementById(`${cardId}-num`).innerText = pct.toFixed(1);
    document.getElementById(`${cardId}-unit`).innerText = '%';
    const bar = document.getElementById(`${cardId}-bar`);
    bar.style.display = '';
    bar.style.width = pct + '%';
    let colorClass = 'bg-emerald-500';
    if (exceeded || pct >= 100) colorClass = 'bg-rose-500';
    else if (pct >= 70) colorClass = 'bg-amber-500';
    else if (pct >= 40) colorClass = 'bg-blue-500';
    bar.className = `capsule-fill ${colorClass} shadow-sm`;
    const capTxt = cap > 0 ? `$${cap.toFixed(2)}` : '--';
    document.getElementById(`${cardId}-used`).innerText =
        `已用 $${used.toFixed(2)} / ${capTxt}` + (exceeded ? '  ·  ⚠️ 已超限' : '');
    document.getElementById(`${cardId}-reset`).innerText = win.resetAt
        ? ('重置 ' + formatCountdown(new Date(win.resetAt).toISOString()))
        : '暂无重置时间';
}

function renderCommandCode(data, item) {
    const credits = data.credits || {};
    const usage = data.usage || {};
    const plan = data.plan || {};
    const acc = data.account || {};
    const today = data.today || {};

    // 卡片1/2：5小时与每周窗口（真实 used/cap）
    ccWindowCard('c1', '5小时额度', credits.fiveHour && credits.fiveHour.cap ? '$' + credits.fiveHour.cap.toFixed(2) : '--', credits.fiveHour || {});
    ccWindowCard('c2', '每周额度', credits.weekly && credits.weekly.cap ? '$' + credits.weekly.cap.toFixed(2) : '--', credits.weekly || {});

    // 卡片3：月额度（剩余 / 本期已用 / 套餐周期）
    const monthlyLeft = parseFloat(credits.monthlyCredits || 0);
    const periodUsed = parseFloat(usage.totalCost || 0);
    const purchased = parseFloat(credits.purchasedCredits || 0);
    const free = parseFloat(credits.freeCredits || 0);
    document.getElementById('c3-title').innerText = '月额度';
    document.getElementById('c3-limit').innerText = plan.name || '--';
    document.getElementById('c3-num').innerText = monthlyLeft.toFixed(2);
    document.getElementById('c3-unit').innerText = '$';
    const c3bar = document.getElementById('c3-bar');
    c3bar.style.display = '';
    const denom = monthlyLeft + periodUsed;
    const pctM = denom > 0 ? Math.min(100, periodUsed / denom * 100) : 0;
    c3bar.style.width = pctM + '%';
    c3bar.className = 'capsule-fill ' + (pctM >= 90 ? 'bg-rose-500' : (pctM >= 70 ? 'bg-amber-500' : 'bg-blue-500')) + ' shadow-sm';
    _applyCardWarning('card-c3', 'c3-warn', pctM);
    document.getElementById('c3-used').innerText = `剩余 $${monthlyLeft.toFixed(2)} · 本期已用 $${periodUsed.toFixed(2)}`;
    document.getElementById('c3-reset').innerText = plan.periodEndMs
        ? ('套餐周期至 ' + new Date(plan.periodEndMs).toLocaleDateString('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit'}))
        : '--';

    // 状态 pill：账户 + 本期请求概况
    const pill = document.getElementById('hero-status-pill');
    pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-emerald-50 text-emerald-700 border border-emerald-200 min-w-0 max-w-[260px] truncate';
    const okCount = usage.completedCount || 0;
    const failCount = usage.failedCount || 0;
    const sr = (usage.successRate !== undefined && usage.successRate !== null) ? Number(usage.successRate).toFixed(1) : '--';
    let pillText = (acc.name ? acc.name + ' · ' : '') + `本期 ${okCount} 成功 / ${failCount} 失败 · ${sr}%`;
    if (data.failures && data.failures.length > 0) {
        pillText += ' · 异常: ' + data.failures.join(';');
    }
    pill.title = pillText;
    // acc.name 来自 whoami、data.failures 是远端报错原文 —— 都进 innerHTML，必须转义
    pill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0"></span> <span class="truncate">' + esc(pillText) + '</span>';
}

/* ================= Cline Pass 渲染 ================= */
function renderCline(data, item) {
    const plan = data.plan || {};
    const acc = data.account || {};
    const bal = data.balance || {};
    const win = data.windows || {};
    const usage = data.usage || {};
    const uToday = usage.today || {};
    const uPeriod = usage.period || {};
    const uModels = usage.models || [];
    const periodLabel = usage.period_start ? (String(usage.period_start).slice(5) + ' 起') : '本期';

    const state = plan.active ? (plan.canceledAt ? '已取消(到期前可用)' : '生效中') : '未生效';

    // 三张卡 = 官方额度窗口（5小时 / 每周 / 每月），形态与 OpenCode Go 一致：
    // 官方百分比 + 进度条 + 重置倒计时。数据来自 /users/me/plan/usage-limits。
    document.getElementById('c1-title').innerText = '5小时窗口';
    document.getElementById('c2-title').innerText = '每周窗口';
    document.getElementById('c3-title').innerText = '每月窗口';
    document.getElementById('c1-limit').innerText = plan.name || 'Cline Pass';
    document.getElementById('c1-limit').title = plan.name || 'Cline Pass';
    document.getElementById('c2-limit').innerText = '官方额度';
    document.getElementById('c2-limit').title = '每周额度窗口';
    document.getElementById('c3-limit').innerText = '官方额度';
    document.getElementById('c3-limit').title = '每月额度窗口';

    const mapping = { rolling: 'c1', weekly: 'c2', monthly: 'c3' };
    for (const [name, cardId] of Object.entries(mapping)) {
        const w = win[name] || {};
        const p = (w.percent === undefined || w.percent === null) ? null : parseFloat(w.percent);
        const bar = document.getElementById(`${cardId}-bar`);
        if (p !== null && !isNaN(p)) {
            document.getElementById(`${cardId}-num`).innerText = p.toFixed(1);
            document.getElementById(`${cardId}-unit`).innerText = '%';
            bar.style.display = '';
            bar.style.width = Math.min(100, Math.max(0, p)) + '%';
            let colorClass = 'bg-emerald-500';
            if (p >= 90) colorClass = 'bg-rose-500';
            else if (p >= 70) colorClass = 'bg-amber-500';
            else if (p >= 40) colorClass = 'bg-blue-500';
            bar.className = `capsule-fill ${colorClass} shadow-sm`;
            const usedEl = document.getElementById(`${cardId}-used`);
            usedEl.innerText = `已用 ${p.toFixed(1)}% · 剩余 ${(100 - p).toFixed(1)}%`;
            usedEl.title = usedEl.innerText;
            const rTime = (name === 'monthly' ? (w.resetsAt || plan.periodEnd) : w.resetsAt);
            const resetEl = document.getElementById(`${cardId}-reset`);
            if (rTime) {
                resetEl.innerText = formatCountdown(rTime);
                resetEl.title = `重置时间: ${rTime}`;
            } else if (name === 'monthly' && periodLabel) {
                resetEl.innerText = `${periodLabel}至今`;
                resetEl.title = `计费周期: ${periodLabel}至今`;
            } else {
                resetEl.innerText = '-- 后重置';
                resetEl.title = '';
            }
            _applyCardWarning(`card-${cardId}`, `${cardId}-warn`, p);
        } else {
            document.getElementById(`${cardId}-num`).innerText = '--';
            document.getElementById(`${cardId}-unit`).innerText = '%';
            bar.style.display = 'none';
            document.getElementById(`${cardId}-used`).innerText = '官方未返回该窗口';
            document.getElementById(`${cardId}-reset`).innerText = '--';
            _applyCardWarning(`card-${cardId}`, `${cardId}-warn`, 0);
        }
    }
    // 月度卡官方用量展示：在右上角徽章展示本期 Token 与金额消耗，悬停展示完整明细，保持底部排版对称绝不溢出
    if (uPeriod && (uPeriod.tokens !== undefined || uPeriod.cost !== undefined)) {
        const c3limit = document.getElementById('c3-limit');
        const tokStr = fmtTok(uPeriod.tokens || 0);
        const costStr = `$${(uPeriod.cost || 0).toFixed(2)}`;
        if (c3limit) {
            c3limit.innerText = `${tokStr} / ${costStr}`;
            c3limit.title = `本期 (${periodLabel}) 官方用量: ${tokStr} tokens / ${costStr}`;
        }
        const c3used = document.getElementById('c3-used');
        if (c3used) {
            c3used.title = `${c3used.innerText} · 本期 (${periodLabel}): ${tokStr} tokens / ${costStr}`;
        }
    }

    // 状态 pill：套餐 + 续订状态 + Credits + 官方用量
    const pill = document.getElementById('hero-status-pill');
    pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-indigo-50 text-indigo-700 border border-indigo-200 min-w-0 max-w-[420px] truncate';
    const credits = (bal.credits === undefined || bal.credits === null) ? '--' : Number(bal.credits).toFixed(4);
    let txt = (plan.name || 'Cline Pass') + ' · ' + state;
    if (plan.periodEnd) txt += ' · 到期 ' + String(plan.periodEnd).slice(0, 10);
    txt += ' · Credits ' + credits;
    txt += ` · 今日 ${fmtTok(uToday.tokens || 0)} tokens / $${(uToday.cost || 0).toFixed(2)}`;
    if (data.hint) txt += ' · ' + data.hint;
    pill.title = txt + (acc.email ? (' · ' + acc.email) : '')
        + ` · 官网口径（含所有客户端，含缓存）`
        + ` · 本期 ${periodLabel} ${fmtTok(uPeriod.tokens || 0)} tokens / $${(uPeriod.cost || 0).toFixed(2)}`
        + (uModels.length ? (' · 分模型: ' + uModels.map(m => `${m.model} ${fmtTok(m.tokens)}`).join(' / ')) : '');
    // txt 含 plan.name（远端）与 data.hint（远端 failures 原文）—— 必须转义
    pill.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-indigo-500 shrink-0"></span> <span class="truncate">' + esc(txt) + '</span>';
}

/* ================= Grok Build 渲染 ================= */
function renderGrokBuild(data, item) {
    const today = data.today || {};
    const week = data.week || {};
    const month = data.month || {};
    const total = data.total || {};
    const quota = data.quota || {};

    if (quota && quota.success && quota.used_percent !== undefined) {
        // 1. 每周额度卡片 (Card 1) - 显示 SuperGrok Heavy 剩余额度与进度条
        const usedPct = parseFloat(quota.used_percent) || 0;
        const remPct = quota.remaining_percent !== undefined ? parseFloat(quota.remaining_percent) : Math.max(0, 100 - usedPct);

        document.getElementById('c1-title').innerText = quota.period_label || '每周额度';
        document.getElementById('c1-limit').innerText = quota.tier || 'SuperGrok Heavy';
        document.getElementById('c1-num').innerText = usedPct.toFixed(1);
        document.getElementById('c1-unit').innerText = '%';

        const c1bar = document.getElementById('c1-bar');
        c1bar.style.display = '';
        c1bar.style.width = Math.min(100, usedPct) + '%';
        let colorClass = 'bg-emerald-500';
        if (usedPct >= 90) colorClass = 'bg-rose-500';
        else if (usedPct >= 70) colorClass = 'bg-amber-500';
        else if (usedPct >= 40) colorClass = 'bg-blue-500';
        c1bar.className = 'capsule-fill ' + colorClass + ' shadow-sm';
        _applyCardWarning('card-c1', 'c1-warn', usedPct);

        document.getElementById('c1-used').innerText = `已用 ${usedPct.toFixed(1)}% · 剩余 ${remPct.toFixed(1)}%`;
        document.getElementById('c1-reset').innerText = quota.period_end ? formatCountdown(quota.period_end) : '每周重置';

        // 2. 今日用量 (Card 2)
        document.getElementById('c2-title').innerText = '今日用量（本机）';
        const tCalls = today.calls || 0;
        const scN = (data.local_sessions === undefined) ? null : data.local_sessions;
        document.getElementById('c2-limit').innerText = tCalls > 0 ? `${tCalls} 次请求` : (scN === null ? '今日暂无' : `本机 ${scN} 个会话`);
        const tTokM = ((today.tokens || 0) / 1e6).toFixed(1);
        document.getElementById('c2-num').innerText = tTokM;
        document.getElementById('c2-unit').innerText = 'M';
        document.getElementById('c2-bar').style.display = 'none';
        _applyCardWarning('card-c2', 'c2-warn', 0);
        document.getElementById('c2-used').innerText = `今日折合 $${(today.cost || 0).toFixed(2)}`;
        document.getElementById('c2-num').title = data.local_scope_text || '';
        
        const lastActStr = data.last_active ? data.last_active.slice(5, 16) : '';
        if (tCalls > 0) {
            document.getElementById('c2-reset').innerText = `今日共 ${tCalls} 次请求 · 计入账本`;
            document.getElementById('c2-reset').title = `今日共执行 ${tCalls} 次模型调用`;
        } else {
            document.getElementById('c2-reset').innerText = lastActStr ? `最近活动 ${lastActStr}` : '今日暂无请求';
            document.getElementById('c2-reset').title = lastActStr ? `今日暂无新请求。上次会话活动时间：${String(data.last_active || '').slice(0, 32)}（已汇总至近7天累计与会话账本）` : '今日暂无模型请求';
        }

        // 3. 近7天累计 (Card 3)
        document.getElementById('c3-title').innerText = '近7天累计（本机）';
        document.getElementById('c3-limit').innerText = `${week.calls || 0} 次请求`;
        const wTokM = ((week.tokens || 0) / 1e6).toFixed(1);
        document.getElementById('c3-num').innerText = wTokM;
        document.getElementById('c3-unit').innerText = 'M';
        document.getElementById('c3-bar').style.display = 'none';
        _applyCardWarning('card-c3', 'c3-warn', 0);
        document.getElementById('c3-used').innerText = `近7天折合 $${(week.cost || 0).toFixed(2)}`;
        const mTokM = ((month.tokens || 0) / 1e6).toFixed(1);
        document.getElementById('c3-reset').innerText = `本月共 ${mTokM}M ($${(month.cost || 0).toFixed(2)})`;
        document.getElementById('c3-reset').title = `本月累计消耗: ${mTokM}M tokens / $${(month.cost || 0).toFixed(2)}`;

        // 4. Hero Status Pill
        const pill = document.getElementById('hero-status-pill');
        pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-purple-50 text-purple-700 border border-purple-200 min-w-0 max-w-[420px] truncate';
        const cd = quota.period_end ? formatCountdown(quota.period_end) : '周期有效';
        let pillText = `${esc(quota.tier || 'SuperGrok Heavy')} · 周额度剩余 ${remPct.toFixed(0)}% · ${esc(cd)}`;
        if (quota.stale) pillText += ' · ⚠️ 额度取自日志回退';
        if (data.local_scope_warning) pillText += ` · 本机仅 ${Number(data.local_sessions) || 0} 个会话`;
        pill.title = `${quota.tier || 'SuperGrok Heavy'} | 已用 ${usedPct}% | 剩余 ${remPct}% | 重置时间: ${quota.period_end || '--'}`
            + (quota.stale_hint ? ('\n⚠️ ' + quota.stale_hint) : '')
            + (data.local_scope_text ? ('\n' + data.local_scope_text) : '');
        const c1r = document.getElementById('c1-reset');
        if (c1r && quota.stale) c1r.title = quota.stale_hint || '';
        // pillText 含 quota.tier / 倒计时等外部字符串，且这里走 innerHTML —— 必须转义
        pill.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-purple-500 animate-pulse"></span> <span class="truncate">${esc(pillText)}</span>`;
    } else {
        // 未获取到配额时的优雅降级
        const tCallsFallback = today.calls || 0;
        document.getElementById('c1-title').innerText = '今日用量';
        document.getElementById('c1-limit').innerText = tCallsFallback > 0 ? `${tCallsFallback} 次请求` : '今日暂无';
        document.getElementById('c2-title').innerText = '近7天用量';
        document.getElementById('c2-limit').innerText = `${week.calls || 0} 次请求`;
        document.getElementById('c3-title').innerText = '本月累计';
        document.getElementById('c3-limit').innerText = `${month.calls || 0} 次请求`;

        _applyCardWarning('card-c1', 'c1-warn', 0);
        _applyCardWarning('card-c2', 'c2-warn', 0);
        _applyCardWarning('card-c3', 'c3-warn', 0);

        // 今日
        const tTokM = ((today.tokens || 0) / 1e6).toFixed(1);
        document.getElementById('c1-num').innerText = tTokM;
        document.getElementById('c1-unit').innerText = 'M';
        document.getElementById('c1-bar').style.display = 'none';
        document.getElementById('c1-used').innerText = `今日折合 $${(today.cost || 0).toFixed(2)}`;
        const lastActStr = data.last_active ? data.last_active.slice(5, 16) : '';
        document.getElementById('c1-reset').innerText = tCallsFallback > 0 ? `共 ${tCallsFallback} 次请求` : (lastActStr ? `最近活动 ${lastActStr}` : '今日暂无');

        // 近7天
        const wTokM = ((week.tokens || 0) / 1e6).toFixed(1);
        document.getElementById('c2-num').innerText = wTokM;
        document.getElementById('c2-unit').innerText = 'M';
        document.getElementById('c2-bar').style.display = 'none';
        document.getElementById('c2-used').innerText = `近7天折合 $${(week.cost || 0).toFixed(2)}`;
        document.getElementById('c2-reset').innerText = `共 ${week.calls || 0} 次请求`;

        // 本月
        const mTok = (month.tokens || 0);
        const mTokStr = mTok >= 1e9 ? (mTok / 1e9).toFixed(2) : (mTok / 1e6).toFixed(1);
        const mTokUnit = mTok >= 1e9 ? 'B' : 'M';
        document.getElementById('c3-num').innerText = mTokStr;
        document.getElementById('c3-unit').innerText = mTokUnit;
        document.getElementById('c3-bar').style.display = 'none';
        document.getElementById('c3-used').innerText = `本月折合 $${(month.cost || 0).toFixed(2)}`;
        document.getElementById('c3-reset').innerText = `共 ${month.calls || 0} 次请求`;

        const pill = document.getElementById('hero-status-pill');
        pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-purple-50 text-purple-700 border border-purple-200 min-w-0 max-w-[260px] truncate';
        const sCount = Number.isFinite(Number(data.sessions_count)) ? Number(data.sessions_count) : 0;
        const pillText = sCount > 0
            ? `本地会话 ${sCount} 个 · 实时自动追踪 · 纯本地免密`
            : '未检测到本机 Grok 会话记录（额度仍可正常查询）';
        pill.title = pillText;
        pill.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-purple-500 shrink-0"></span> <span class="truncate">${esc(pillText)}</span>`;
    }
}

/* ================= Toast 悬浮轻提示 ================= */
function showToast(msg, type = 'info', duration = 2200) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const item = document.createElement('div');
    item.className = 'toast-item toast-' + type;
    let icon = 'ℹ️';
    if (type === 'success') icon = '✓';
    else if (type === 'error') icon = '✕';
    else if (type === 'warning') icon = '⚠️';
    const ic = document.createElement('span');
    ic.style.cssText = 'font-weight:700;font-size:13px;line-height:1;';
    ic.textContent = icon;
    const tx = document.createElement('span');
    // textContent 而非 innerHTML：错误信息里常带远端返回的原文或备份文件片段
    tx.textContent = String(msg == null ? '' : msg);
    item.appendChild(ic);
    item.appendChild(tx);
    container.appendChild(item);
    setTimeout(() => {
        item.classList.add('leaving');
        setTimeout(() => {
            if (item.parentNode) item.parentNode.removeChild(item);
        }, 260);
    }, duration);
}

/* ================= 桥接调用统一入口（关键：永不挂起） =================
   pywebview 的 Promise 在极少数情况下会**永不 settle**：返回值里一旦出现
   NaN / Infinity，Python 侧 json.dumps 会写出 JSON 规范不允许的字面量，
   前端 JSON.parse 抛 SyntaxError —— 而这个异常发生在回调表项被删除之后、
   resolve/reject 之前，于是 finally 永远不执行，按钮就永久停在「查询中」。
   后端已做净化，这里再加一道超时兜底：任何调用超时都会返回 error，UI 必定恢复。 */
const API_TIMEOUT_MS = 150000;

function apiCall(name, args, timeoutMs) {
    const limit = timeoutMs || API_TIMEOUT_MS;
    return new Promise((resolve) => {
        if (!window.pywebview || !window.pywebview.api || typeof window.pywebview.api[name] !== 'function') {
            resolve({ error: '系统连接中，请稍候再试' });
            return;
        }
        let done = false;
        const timer = setTimeout(() => {
            if (done) return;
            done = true;
            resolve({ error: '请求超时（' + Math.round(limit / 1000) + ' 秒未返回），请重试；若反复超时请检查「设置 → 数据源路径」' });
        }, limit);
        let p;
        try {
            p = window.pywebview.api[name].apply(window.pywebview.api, args || []);
        } catch (e) {
            done = true; clearTimeout(timer); resolve({ error: String(e) }); return;
        }
        Promise.resolve(p).then((r) => {
            if (done) return;
            done = true; clearTimeout(timer); resolve(r);
        }).catch((e) => {
            if (done) return;
            done = true; clearTimeout(timer); resolve({ error: String(e) });
        });
    });
}

/* 桥未就绪时统一给一句提示，而不是静默 return（用户会以为按钮坏了） */
function requireBridge() {
    if (window.pywebview && window.pywebview.api) return true;
    showToast('系统连接中，请稍候...', 'info');
    return false;
}

/* 忙碌态 + 防重入：dataset.busy 保证同一按钮不会并发跑两次 */
function setBusy(btn, busy, busyText) {
    if (!btn) return false;
    if (busy) {
        if (btn.dataset.busy === '1') return false;
        btn.dataset.busy = '1';
        if (!btn.dataset.idleHtml) btn.dataset.idleHtml = btn.innerHTML;
        btn.disabled = true;
        btn.style.opacity = '0.65';
        btn.innerHTML = '<span>⏳</span> ' + (busyText || '处理中...');
        return true;
    }
    btn.dataset.busy = '';
    btn.disabled = false;
    btn.style.opacity = '';
    if (btn.dataset.idleHtml) btn.innerHTML = btn.dataset.idleHtml;
    return true;
}

function openStorageDir() {
    if (!requireBridge()) return;
    apiCall('open_storage').then((res) => {
        if (res && res.error) showToast('打开目录失败：' + res.error, 'error');
    });
}

async function doQueryCurrent() {
    if (!requireBridge()) return;
    const keys = (appState && appState.keys) ? appState.keys : [];
    if (keys.length === 0) {
        showToast('暂无密钥可查询，请先添加密钥', 'warning');
        return;
    }
    const currentKeyObj = keys[currentKeyIndex] || null;
    const currentKeyStr = currentKeyObj ? (currentKeyObj.key || '') : '';

    const btn = document.getElementById('btn-query-current');
    if (!setBusy(btn, true, '查询中...')) return;    // 防重入：双击/自动刷新叠加不再互相踩
    try {
        const res = await apiCall('query_key', [currentKeyIndex, currentKeyStr], 90000);
        if (!res || res.error) {
            const em = (res && res.error) ? res.error : '未知错误';
            showToast('查询失败: ' + em, 'error');
            const pill = document.getElementById('hero-status-pill');
            if (pill) {
                pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-rose-50 text-rose-700 border border-rose-200 min-w-0 max-w-[260px] truncate';
                pill.textContent = '查询失败: ' + em;
            }
            if (res && Array.isArray(res.keys)) { appState.keys = res.keys; renderKeyList(); }
        } else {
            if (res.keys && Array.isArray(res.keys)) {
                appState.keys = res.keys;
            } else if (res.key && 0 <= currentKeyIndex && currentKeyIndex < appState.keys.length) {
                appState.keys[currentKeyIndex] = res.key;
            }
            if (currentKeyStr) {
                const foundIdx = appState.keys.findIndex(k => k.key === currentKeyStr);
                if (foundIdx !== -1) {
                    currentKeyIndex = foundIdx;
                }
            }
            if (currentKeyIndex >= appState.keys.length) {
                currentKeyIndex = Math.max(0, appState.keys.length - 1);
            }
            renderKeyList();
            selectKey(currentKeyIndex);
            showToast('查询成功', 'success');
        }
    } catch(e) {
        showToast('查询失败: ' + e, 'error');
        const pill = document.getElementById('hero-status-pill');
        if (pill) {
            pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-rose-50 text-rose-700 border border-rose-200 min-w-0 max-w-[260px] truncate';
            pill.textContent = '异常错误: ' + e;
        }
    } finally {
        setBusy(btn, false);
    }
}

async function doQueryAll() {
    if (!requireBridge()) return;
    const keys = (appState && appState.keys) ? appState.keys : [];
    if (keys.length === 0) {
        showToast('暂无密钥可查询，请先点左侧「+」添加密钥', 'warning');
        return;
    }
    const currentKeyObj = keys[currentKeyIndex] || null;
    const currentKeyStr = currentKeyObj ? (currentKeyObj.key || '') : '';

    const btn = document.getElementById('btn-query-all');
    if (!setBusy(btn, true, '全部查询中...')) return;
    showToast('正在并发刷新所有密钥...', 'info', 1800);
    try {
        const res = await apiCall('query_all', [], 180000);
        if (!res || res.error) {
            showToast('全部查询失败: ' + ((res && res.error) || '未知错误'), 'error');
            return;
        }
        if (res.keys && Array.isArray(res.keys)) {
            appState.keys = res.keys;
        }
        if (currentKeyStr) {
            const foundIdx = appState.keys.findIndex(k => k.key === currentKeyStr);
            if (foundIdx !== -1) {
                currentKeyIndex = foundIdx;
            }
        }
        if (currentKeyIndex >= appState.keys.length) {
            currentKeyIndex = Math.max(0, appState.keys.length - 1);
        }
        renderKeyList();
        selectKey(currentKeyIndex);
        // 逐条汇报失败：旧实现无论成败都提示「全部密钥刷新完成」
        const failed = Number(res.failed || 0);
        if (failed > 0) {
            showToast(`${res.ok || 0} 个成功，${failed} 个失败（失败原因见卡片红色标记）`, 'warning', 4200);
        } else {
            showToast(`全部 ${res.ok || appState.keys.length} 个密钥刷新完成`, 'success');
        }
    } catch(e) {
        showToast('全部查询失败: ' + e, 'error');
    } finally {
        setBusy(btn, false);
    }
}

let _onlyCurrentKey = true;
let _isLoadingTokenStats = false;

function updateOnlyKeyButtonUI() {
    const btn = document.getElementById('filter-only-key-btn');
    const textEl = document.getElementById('filter-only-key-text');
    const iconEl = document.getElementById('filter-only-key-icon');
    if (!btn) return;
    if (_onlyCurrentKey) {
        btn.classList.add('active');
        if (textEl) textEl.innerText = '仅当前 Key';
        if (iconEl) iconEl.classList.remove('opacity-60');
        btn.title = '当前：仅查看当前选中 Key 消耗（点击切换为全渠道汇总）';
    } else {
        btn.classList.remove('active');
        if (textEl) textEl.innerText = '全渠道汇总';
        if (iconEl) iconEl.classList.add('opacity-60');
        btn.title = '当前：全渠道汇总用量（点击切换为仅当前 Key）';
    }
}

function toggleOnlyKeyFilter() {
    if (_isLoadingTokenStats) {
        showToast('正在统计用量，请稍候...', 'info', 800);
        return;
    }
    _onlyCurrentKey = !_onlyCurrentKey;
    updateOnlyKeyButtonUI();
    if (_onlyCurrentKey) {
        showToast('已开启：仅查看当前选中 Key 的用量', 'info', 1200);
    } else {
        showToast('已切回：查看全渠道汇总用量', 'info', 1200);
    }
    loadTokenStats();
}

let _loadTokenStatsReqId = 0;

/* 清空底部统计面板（删掉最后一个 Key 之后必须清，否则残留上一个 Key 的数字） */
function clearTokenStatsPanels() {
    // 关键：作废所有在飞的统计请求。否则删除前发出的那次 loadTokenStats 返回后
    // 仍会被当成「最新请求」，把 hint / 徽标又写回来（实测面板残留）。
    _loadTokenStatsReqId++;
    _isLoadingTokenStats = false;
    const spin = document.getElementById('filter-only-key-icon');
    if (spin) spin.classList.remove('animate-spin');
    ['kpi-sessions', 'kpi-tokens', 'kpi-cache'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.innerText = '--';
    });
    try { _lastTotals = { sessions: 0, tokens: 0, tokens_with_cache: 0, cache: 0, cost: 0.0, fx: 7.2 }; } catch (e) {}
    try { renderKpiCost(_lastTotals); } catch (e) {}
    try { _trendData = []; updateTrendSummary(); drawTrendChart(); } catch (e) {}
    try { renderLeaderboard([]); } catch (e) {}
    try { resetRawJsonPane(); } catch (e) {}
    const badge = document.getElementById('stats-ledger-badge');
    if (badge) { badge.innerText = '—'; badge.title = '暂无密钥'; }
    const hintEl = document.getElementById('stats-hint');
    if (hintEl) hintEl.innerText = '暂无密钥，先添加一个再查看用量';
}

async function loadTokenStats(overrideTimeKey) {
    if (!window.pywebview || !window.pywebview.api) return;
    const reqId = ++_loadTokenStatsReqId;
    _isLoadingTokenStats = true;
    // 兜底：即使某次请求永不返回（或返回被 requestId 守卫丢弃），也强制解除忙碌态
    setTimeout(() => {
        if (_isLoadingTokenStats && reqId === _loadTokenStatsReqId) {
            _isLoadingTokenStats = false;
            const ic = document.getElementById('filter-only-key-icon');
            if (ic) ic.classList.remove('animate-spin');
        }
    }, 170000);

    const hintEl = document.getElementById('stats-hint');
    const iconEl = document.getElementById('filter-only-key-icon');
    if (iconEl) iconEl.classList.add('animate-spin');
    if (hintEl && !hintEl.innerText) {
        hintEl.innerText = '正在统计用量...';
    }

    try {
        const rangeSelect = document.getElementById('period-select');
        let timeKey = overrideTimeKey;
        if (!timeKey) {
            if (rangeSelect && rangeSelect.value === 'custom') {
                const dp = document.getElementById('custom-date-picker');
                timeKey = dp && dp.value ? ('date:' + dp.value) : '今日';
            } else {
                timeKey = rangeSelect ? rangeSelect.value : '全部';
            }
        }
        const onlyKey = _onlyCurrentKey;
        const currentKeyObj = (appState && appState.keys && appState.keys[currentKeyIndex]) ? appState.keys[currentKeyIndex] : null;
        const currentKeyStr = currentKeyObj ? (currentKeyObj.key || '') : '';

        const res = await apiCall('get_token_stats', [timeKey, 'dsh 账本', onlyKey, currentKeyIndex, currentKeyStr], 150000);
        if (reqId !== _loadTokenStatsReqId) return;
        // 只有「确实没有数据」才清空面板。带 totals 的警告（例如未装
        // dsh-cost-meter 插件而走了会话缓存回退）必须照常渲染，只是把说明显示出来。
        if (res && res.error && !(res.totals || (res.per_model && res.per_model.length))) {
            if (hintEl) hintEl.innerText = res.error;
            try { renderLeaderboard([]); } catch (e) {}
            return;
        }
        if (res && res.error && hintEl) hintEl.innerText = res.error;

        // 无论数据是否为空，都明确刷新或清空 4 个 KPI 卡片，彻底杜绝残留上一个 Key 的数据！
        const t = (res && res.totals) ? res.totals : { sessions: 0, tokens: 0, tokens_with_cache: 0, cache: 0, cost: 0.0, fx: 7.2 };
        _lastTotals = t;
        _lastFx = parseFloat(t.fx) || 7.2;

        const sessEl = document.getElementById('kpi-sessions');
        if (sessEl) sessEl.innerText = `${t.sessions || 0} 次`;

        const totTok = (t.tokens_with_cache || t.tokens || 0);
        const tokStr = totTok >= 1e9 ? `${(totTok / 1e9).toFixed(2)}B` : `${((totTok)/1000000).toFixed(1)}M`;
        const tokEl = document.getElementById('kpi-tokens');
        if (tokEl) {
            tokEl.innerText = `${tokStr} 含缓存`;
            tokEl.title = `总消耗 Token 构成明细：\n· 未命中缓存输入: ${(t.input || 0).toLocaleString()} tokens\n· 命中缓存输入: ${(t.cache || 0).toLocaleString()} tokens\n· 生成输出: ${(t.output || 0).toLocaleString()} tokens` + (t.reasoning ? ` (含推理 ${t.reasoning.toLocaleString()})` : '');
        }

        const cTok = (t.cache || 0);
        const cTokStr = cTok >= 1e9 ? `${(cTok / 1e9).toFixed(2)}B` : `${((cTok)/1000000).toFixed(1)}M`;
        const pctC = totTok ? (cTok / totTok * 100).toFixed(1) : '0';
        const cacheEl = document.getElementById('kpi-cache');
        if (cacheEl) {
            cacheEl.innerHTML = `${cTokStr} <span class="text-[10px] px-1 rounded" style="color:var(--accent-cache);background:var(--bg-card-subtle);border:1px solid var(--border-subtle);">${pctC}%</span>`;
            cacheEl.title = `缓存命中说明：\n· 命中缓存读取: ${(t.cache || 0).toLocaleString()} tokens (${pctC}%)\n· 提示词缓存按官方优惠单价计费，大幅降低开销`;
        }

        renderKpiCost(t);

        // 更新近几天趋势走势折线图
        _trendData = (res && res.daily_series) || [];
        updateTrendSummary();
        drawTrendChart();

        const badge = document.getElementById('stats-ledger-badge');
        if (badge) {
            if (res && res.source === '全渠道汇总') {
                badge.innerText = '全渠道汇总';
                badge.title = '统计范围包含：dsh 账本全部模型 + 本地 Grok Build 历史会话';
            } else if (res && res.source === 'grok') {
                badge.innerText = 'grok会话库';
                badge.title = '统计数据来源：本地 ~/.grok/sessions 独立会话库';
            } else if (res && res.source === 'cline-official') {
                badge.innerText = 'Cline官网';
                badge.title = '统计数据来源：Cline 官方接口（api.cline.bot/users/{id}/usages），含所有客户端与缓存明细，与官网图表同口径';
            } else if (res && res.source === 'dsh-sessions') {
                badge.innerText = 'dsh会话缓存';
                badge.title = '未检测到 dsh-cost-meter 插件，已回退到 dsh 自己的会话缓存（storages/session_projcache）。'
                            + '合计准确；「按日」以会话创建日归属，跨天会话会整段计入创建日。';
            } else {
                badge.innerText = 'dsh账本';
                badge.title = '统计数据来源：本地 OpenCode / dsh 账本数据';
            }
        }

        if (res && res.per_model && res.per_model.length > 0) {
            renderLeaderboard(res.per_model);
            if (hintEl) hintEl.innerText = res.hint || '';
        } else {
            renderLeaderboard([]);
            if (hintEl) hintEl.innerText = (res && (res.error || res.hint)) ? (res.error || res.hint) : '该范围没有用量记录';
        }
    } catch(e) {
        console.error("Token stats error:", e);
        if (reqId === _loadTokenStatsReqId && hintEl) {
            hintEl.innerText = '用量统计失败: ' + (e.message || e);
        }
    } finally {
        if (reqId === _loadTokenStatsReqId) {
            _isLoadingTokenStats = false;
            if (iconEl) iconEl.classList.remove('animate-spin');
        }
    }
}

/* ================= 周期与自定义日期处理 ================= */
function onPeriodChange(val) {
    const dp = document.getElementById('custom-date-picker');
    const badge = document.getElementById('stats-ledger-badge');
    if (val === 'custom') {
        if (dp) {
            dp.classList.remove('hidden');
            if (!dp.value) {
                dp.value = new Date().toISOString().slice(0, 10);
            }
            loadTokenStats('date:' + dp.value);
            try {
                if (typeof dp.showPicker === 'function') {
                    dp.showPicker();
                }
            } catch(e) {}
        }
        if (badge) badge.classList.add('hidden');
    } else {
        if (dp) dp.classList.add('hidden');
        if (badge) badge.classList.remove('hidden');
        loadTokenStats(val);
    }
}

function onCustomDateChange(dateStr) {
    if (dateStr) {
        loadTokenStats('date:' + dateStr);
        showToast('已载入 ' + dateStr + ' 的用量数据', 'info', 1400);
    }
}

/* ================= 近期用量趋势曲线（折叠/展开 + SVG 平滑绘制） ================= */
let _trendData = [];
let _trendExpanded = false;

function toggleTrendCard() {
    _trendExpanded = !_trendExpanded;
    const panel = document.getElementById('trend-card-panel');
    const chevron = document.getElementById('trend-chevron');
    const hint = document.getElementById('trend-status-hint');
    if (!panel) return;
    if (_trendExpanded) {
        panel.classList.remove('hidden');
        if (chevron) chevron.style.transform = 'rotate(180deg)';
        if (hint) hint.innerText = '(点击收起)';
        setTimeout(drawTrendChart, 50);
    } else {
        panel.classList.add('hidden');
        if (chevron) chevron.style.transform = 'rotate(0deg)';
        if (hint) hint.innerText = '(点击展开)';
    }
}

function updateTrendSummary() {
    const el = document.getElementById('trend-summary-text');
    if (!el) return;
    if (!_trendData || _trendData.length === 0) {
        el.innerText = '暂无趋势时序';
        return;
    }
    el.innerText = `近 ${_trendData.length} 天用量走向`;
}

function drawTrendChart() {
    const svg = document.getElementById('trend-svg');
    if (!svg) return;
    if (!_trendExpanded) return;
    svg.innerHTML = '';
    const tooltip = document.getElementById('trend-tooltip');
    if (tooltip) tooltip.classList.add('hidden');

    if (!_trendData || _trendData.length === 0) {
        svg.innerHTML = `<text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" font-size="12" fill="var(--text-secondary)">所选范围内无按日走势数据</text>`;
        return;
    }

    const rect = svg.getBoundingClientRect();
    const w = rect.width || 700;
    const h = rect.height || 120;
    const padL = 48, padR = 44, padT = 14, padB = 22;
    const plotW = Math.max(10, w - padL - padR);
    const plotH = Math.max(10, h - padT - padB);

    const showTok = document.getElementById('chk-trend-tokens') ? document.getElementById('chk-trend-tokens').checked : true;
    const showCost = document.getElementById('chk-trend-cost') ? document.getElementById('chk-trend-cost').checked : true;
    const showCalls = document.getElementById('chk-trend-calls') ? document.getElementById('chk-trend-calls').checked : true;

    const maxTok = Math.max(1, ..._trendData.map(d => d.tokens || 0));
    const maxCost = Math.max(0.01, ..._trendData.map(d => d.cost || 0));
    const maxCalls = Math.max(1, ..._trendData.map(d => d.calls || 0));

    const N = _trendData.length;
    const getX = (i) => padL + (N > 1 ? (i / (N - 1)) * plotW : plotW / 2);

    // 格式化刻度数值
    function fmtTokScale(v) {
        if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
        if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + 'M';
        if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
        return v.toFixed(0);
    }
    function fmtCostScale(c) {
        return '$' + (c >= 100 ? c.toFixed(0) : (c >= 10 ? c.toFixed(1) : c.toFixed(2)));
    }

    const yTop = padT;
    const yMid = padT + plotH * 0.5;
    const yBot = h - padB;

    // 绘制横向参考背景线
    let gridHtml = `<line x1="${padL}" y1="${yBot}" x2="${w - padR}" y2="${yBot}" stroke="var(--border-main)" stroke-width="1"/>`;
    gridHtml += `<line x1="${padL}" y1="${yMid}" x2="${w - padR}" y2="${yMid}" stroke="var(--border-subtle)" stroke-dasharray="3,3" stroke-width="1"/>`;
    gridHtml += `<line x1="${padL}" y1="${yTop}" x2="${w - padR}" y2="${yTop}" stroke="var(--border-subtle)" stroke-dasharray="3,3" stroke-width="1"/>`;

    // 左侧 Y 轴刻度：Token 消耗量（青色 #38bdf8）
    if (showTok) {
        gridHtml += `<text x="${padL - 5}" y="${yTop + 3}" text-anchor="end" font-size="9" font-family="monospace" font-weight="700" fill="#38bdf8">${fmtTokScale(maxTok)}</text>`;
        gridHtml += `<text x="${padL - 5}" y="${yMid + 3}" text-anchor="end" font-size="9" font-family="monospace" fill="#38bdf8" opacity="0.8">${fmtTokScale(maxTok * 0.5)}</text>`;
        gridHtml += `<text x="${padL - 5}" y="${yBot + 3}" text-anchor="end" font-size="9" font-family="monospace" fill="var(--text-muted)">0</text>`;
    }

    // 右侧 Y 轴刻度：折合费用（橙色 #fb923c）
    if (showCost) {
        gridHtml += `<text x="${w - padR + 5}" y="${yTop + 3}" text-anchor="start" font-size="9" font-family="monospace" font-weight="700" fill="#fb923c">${fmtCostScale(maxCost)}</text>`;
        gridHtml += `<text x="${w - padR + 5}" y="${yMid + 3}" text-anchor="start" font-size="9" font-family="monospace" fill="#fb923c" opacity="0.8">${fmtCostScale(maxCost * 0.5)}</text>`;
        gridHtml += `<text x="${w - padR + 5}" y="${yBot + 3}" text-anchor="start" font-size="9" font-family="monospace" fill="var(--text-muted)">$0</text>`;
    }

    // X 轴日期标注
    let xLabelsHtml = '';
    const stepLabel = N > 10 ? 2 : 1;
    _trendData.forEach((d, i) => {
        if (i % stepLabel === 0 || i === N - 1) {
            const x = getX(i);
            xLabelsHtml += `<text x="${x}" y="${h - 6}" text-anchor="middle" font-size="9.5" font-family="monospace" fill="var(--text-secondary)">${esc(d.label || d.date)}</text>`;
        }
    });

    const defs = `
      <defs>
        <linearGradient id="grad-tok" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.25"/>
          <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0"/>
        </linearGradient>
        <linearGradient id="grad-cost" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#fb923c" stop-opacity="0.22"/>
          <stop offset="100%" stop-color="#fb923c" stop-opacity="0.0"/>
        </linearGradient>
        <linearGradient id="grad-calls" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#c084fc" stop-opacity="0.22"/>
          <stop offset="100%" stop-color="#c084fc" stop-opacity="0.0"/>
        </linearGradient>
      </defs>
    `;

    function createSpline(points) {
        if (points.length === 0) return '';
        if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
        let p = `M ${points[0].x} ${points[0].y}`;
        for (let i = 0; i < points.length - 1; i++) {
            const p0 = points[i];
            const p1 = points[i + 1];
            const cpx = (p0.x + p1.x) / 2;
            p += ` C ${cpx} ${p0.y}, ${cpx} ${p1.y}, ${p1.x} ${p1.y}`;
        }
        return p;
    }

    let seriesPaths = '';
    let circlesHtml = '';

    if (showTok) {
        const maxTok = Math.max(1, ..._trendData.map(d => d.tokens || 0));
        const pts = _trendData.map((d, i) => ({
            x: getX(i),
            y: padT + (1 - (d.tokens || 0) / maxTok) * plotH,
            data: d
        }));
        const lineD = createSpline(pts);
        const areaD = lineD + ` L ${pts[pts.length - 1].x} ${h - padB} L ${pts[0].x} ${h - padB} Z`;
        seriesPaths += `<path d="${areaD}" fill="url(#grad-tok)"/>`;
        seriesPaths += `<path d="${lineD}" stroke="#38bdf8" class="trend-svg-line"/>`;
        pts.forEach((pt, i) => {
            circlesHtml += `<circle cx="${pt.x}" cy="${pt.y}" r="3.2" fill="#38bdf8" stroke="var(--bg-card)" stroke-width="1.5" class="trend-point" onmouseenter="onHoverTrendPoint(event, ${i})" onmouseleave="onLeaveTrendPoint()"/>`;
        });
    }

    if (showCost) {
        const maxCost = Math.max(0.01, ..._trendData.map(d => d.cost || 0));
        const pts = _trendData.map((d, i) => ({
            x: getX(i),
            y: padT + (1 - (d.cost || 0) / maxCost) * plotH,
            data: d
        }));
        const lineD = createSpline(pts);
        const areaD = lineD + ` L ${pts[pts.length - 1].x} ${h - padB} L ${pts[0].x} ${h - padB} Z`;
        seriesPaths += `<path d="${areaD}" fill="url(#grad-cost)"/>`;
        seriesPaths += `<path d="${lineD}" stroke="#fb923c" class="trend-svg-line"/>`;
        pts.forEach((pt, i) => {
            circlesHtml += `<circle cx="${pt.x}" cy="${pt.y}" r="3.2" fill="#fb923c" stroke="var(--bg-card)" stroke-width="1.5" class="trend-point" onmouseenter="onHoverTrendPoint(event, ${i})" onmouseleave="onLeaveTrendPoint()"/>`;
        });
    }

    if (showCalls) {
        const maxCalls = Math.max(1, ..._trendData.map(d => d.calls || 0));
        const pts = _trendData.map((d, i) => ({
            x: getX(i),
            y: padT + (1 - (d.calls || 0) / maxCalls) * plotH,
            data: d
        }));
        const lineD = createSpline(pts);
        const areaD = lineD + ` L ${pts[pts.length - 1].x} ${h - padB} L ${pts[0].x} ${h - padB} Z`;
        seriesPaths += `<path d="${areaD}" fill="url(#grad-calls)"/>`;
        seriesPaths += `<path d="${lineD}" stroke="#c084fc" class="trend-svg-line"/>`;
        pts.forEach((pt, i) => {
            circlesHtml += `<circle cx="${pt.x}" cy="${pt.y}" r="3.2" fill="#c084fc" stroke="var(--bg-card)" stroke-width="1.5" class="trend-point" onmouseenter="onHoverTrendPoint(event, ${i})" onmouseleave="onLeaveTrendPoint()"/>`;
        });
    }

    // xLabelsHtml / seriesPaths 里已各自转义过外部字符串（d.label/d.date）；
    // 其余全部是数字坐标。这里再兜一层：这些片段拼进 SVG 也是 innerHTML 语义。
    svg.innerHTML = defs + gridHtml + xLabelsHtml + seriesPaths + circlesHtml;
}

function onHoverTrendPoint(evt, index) {
    const d = _trendData[index];
    if (!d) return;
    const tt = document.getElementById('trend-tooltip');
    if (!tt) return;
    const tokM = ((d.tokens || 0) / 1e6).toFixed(2);
    const costUsd = (d.cost || 0).toFixed(2);
    const costCny = ((d.cost || 0) * _lastFx).toFixed(2);
    tt.innerHTML = `
      <div style="font-weight:700; color:var(--text-title); border-bottom:1px solid var(--border-subtle); padding-bottom:3px; margin-bottom:4px;">📅 ${esc(d.date)}</div>
      <div style="color:#38bdf8; margin-bottom:1px;">• Tokens: <b>${tokM} M</b></div>
      <div style="color:#fb923c; margin-bottom:1px;">• 折合费用: <b>$${costUsd} (≈¥${costCny})</b></div>
      <div style="color:#c084fc;">• 请求次数: <b>${d.calls || 0} 次</b></div>
    `;
    tt.classList.remove('hidden');
    const container = document.getElementById('trend-chart-container');
    const cRect = container ? container.getBoundingClientRect() : { left: 0, top: 0 };
    let relX = evt.clientX - cRect.left;
    let relY = evt.clientY - cRect.top;

    // 当点位靠近顶部时（relY < 60），Tooltip 智能翻转至点位下方，绝不被顶部遮挡
    if (relY < 60) {
        tt.style.transform = 'translate(-50%, 14px)';
    } else {
        tt.style.transform = 'translate(-50%, -110%)';
    }

    // 左右边界限制防贴边溢出
    const maxW = cRect.width || 700;
    relX = Math.max(90, Math.min(maxW - 90, relX));
    tt.style.left = relX + 'px';
    tt.style.top = relY + 'px';
}

function onLeaveTrendPoint() {
    const tt = document.getElementById('trend-tooltip');
    if (tt) tt.classList.add('hidden');
}

/* ================= 低饱和·高明度柔和调色板 ================= */
const PASTEL_PALETTE = [
    '#818cf8', // 柔雾冰蓝
    '#5eead4', // 清透薄荷
    '#c084fc', // 丁香柔紫
    '#fb923c', // 柔杏暖珊瑚
    '#38bdf8', // 清冽天青
    '#f472b6', // 柔粉玫瑰
    '#a3e635'  // 青柠绿
];

/* ================= 模型用量表（六列 + 排序 + 占比条） ================= */
let _lastRows = [];
let _lastTotals = null;
let _lastFx = 7.2;
let _sortKey = null;        // count|input|output|cache|rate|cost
let _sortDesc = true;       // true=从高到低
let _costCcy = 'USD';
try {
    if (typeof window !== 'undefined' && 'localStorage' in window) {
        _costCcy = window.localStorage.getItem('opencode_cost_ccy') || 'USD';
    }
} catch(e) {}

function fmtCost(usd) {
    const v = parseFloat(usd) || 0;
    if (_costCcy === 'CNY') return '¥' + (v * _lastFx).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return '$' + v.toFixed(2);
}

function toggleCostCcy() {
    _costCcy = (_costCcy === 'USD') ? 'CNY' : 'USD';
    try {
        if (typeof window !== 'undefined' && 'localStorage' in window) {
            window.localStorage.setItem('opencode_cost_ccy', _costCcy);
        }
    } catch(e) {}
    const tg = document.getElementById('ccy-toggle');
    if (tg) tg.textContent = (_costCcy === 'USD') ? '$' : '¥';
    renderLeaderboard(_lastRows);
    if (_lastTotals) renderKpiCost(_lastTotals);
}

function renderKpiCost(t) {
    const el = document.getElementById('kpi-cost');
    if (!el) return;
    const v = parseFloat(t.cost || 0);
    el.innerText = (_costCcy === 'CNY')
        ? '¥' + (v * _lastFx).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' ≈ $' + v.toFixed(2)
        : '$' + v.toFixed(2) + ' ≈ ¥' + (v * _lastFx).toFixed(1);
    el.title = (t.cost_estimated ? '账本折合费用；含按官方价估算的未定价模型（阶跃星辰）' : '账本折合费用')
        + '；' + (_costCcy === 'CNY' ? '人民币展示（汇率 ' + _lastFx + '）' : '美元展示（汇率 ' + _lastFx + '）');
}

function fmtTok(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(n);
}

function _rowOutput(m) { return (m.output || 0) + (m.reasoning || 0); }
function _rowTotal(m) {
    return (m.tokens_with_cache !== undefined && m.tokens_with_cache !== null)
        ? (m.tokens_with_cache || 0)
        : ((m.input || 0) + _rowOutput(m) + (m.cache || 0));
}
function _rowRate(m) {
    const tot = _rowTotal(m);
    return tot > 0 ? ((m.cache || 0) / tot * 100) : -1;   // -1 = 无数据，排末尾
}
function _rowCost(m) { return parseFloat(m.cost || 0) || 0; }

function sortBy(key) {
    if (_sortKey === key) { _sortDesc = !_sortDesc; }
    else { _sortKey = key; _sortDesc = true; }
    renderLeaderboard(_lastRows);
}

function _updateSortIndicators() {
    ['count', 'input', 'output', 'cache', 'tokens', 'rate', 'cost'].forEach(k => {
        const hdr = document.getElementById('hdr-' + k);
        if (!hdr) return;
        const ind = hdr.querySelector('.sort-ind');
        const active = _sortKey === k;
        hdr.style.color = active ? 'var(--brand-primary)' : '';
        hdr.classList.toggle('font-semibold', active);
        if (ind) ind.textContent = active ? (_sortDesc ? ' ▲' : ' ▼') : '';
    });
}

let _searchKeyword = '';
function onSearchModels(kw) {
    _searchKeyword = (kw || '').trim().toLowerCase();
    renderLeaderboard(_lastRows);
}

function renderLeaderboard(perModel) {
    const container = document.getElementById('model-list');
    if (!container) return;
    const rawList = perModel || [];
    _lastRows = rawList.filter(m => (m.count || 0) > 0 || (m.tokens_with_cache || m.tokens || 0) > 0);

    // ---- 搜索过滤 ----
    let rows = _lastRows.slice();
    if (_searchKeyword) {
        rows = rows.filter(m => {
            const mod = (m.model || '').toLowerCase();
            const prov = (m.provider || '').toLowerCase();
            return mod.includes(_searchKeyword) || prov.includes(_searchKeyword);
        });
    }

    // ---- 排序 ----
    if (_sortKey) {
        const desc = _sortDesc;
        rows.sort((a, b) => {
            let va, vb;
            if (_sortKey === 'count') { va = a.count || 0; vb = b.count || 0; }
            else if (_sortKey === 'input') { va = a.input || 0; vb = b.input || 0; }
            else if (_sortKey === 'output') { va = _rowOutput(a); vb = _rowOutput(b); }
            else if (_sortKey === 'cache') { va = a.cache || 0; vb = b.cache || 0; }
            else if (_sortKey === 'tokens') { va = _rowTotal(a); vb = _rowTotal(b); }
            else if (_sortKey === 'rate') { va = _rowRate(a); vb = _rowRate(b); }
            else if (_sortKey === 'cost') { va = _rowCost(a); vb = _rowCost(b); }
            if (va < vb) return desc ? 1 : -1;
            if (va > vb) return desc ? -1 : 1;
            return 0;
        });
    }

    // ---- 占比条：前 4 名（采用高阶低饱和柔和色） + 柔和灰色「其余」段 ----
    const stacked = document.getElementById('stacked-bar');
    if (stacked) {
        stacked.innerHTML = '';
        const totalAll = _lastRows.reduce((acc, m) => acc + _rowTotal(m), 0) || 1;
        const top4 = _lastRows.slice(0, 4);
        top4.forEach((m, idx) => {
            const mTot = _rowTotal(m);
            const pct = (mTot / totalAll * 100);
            if (pct <= 0) return;
            const seg = document.createElement('div');
            seg.className = 'bar-segment';
            seg.style.backgroundColor = PASTEL_PALETTE[idx % PASTEL_PALETTE.length];
            seg.style.width = pct.toFixed(2) + '%';
            seg.title = (m.model || '') + (m.provider ? ' (' + m.provider + ')' : '') + ' · ' + fmtTok(mTot) + ' (' + pct.toFixed(1) + '%)';
            stacked.appendChild(seg);
        });
        const restTok = _lastRows.slice(4).reduce((acc, m) => acc + _rowTotal(m), 0);
        if (restTok > 0) {
            const seg = document.createElement('div');
            seg.className = 'bar-segment';
            seg.style.backgroundColor = 'var(--text-muted)';
            seg.style.opacity = '0.6';
            const restPct = (restTok / totalAll * 100);
            seg.style.width = restPct.toFixed(2) + '%';
            seg.title = '其余模型 · ' + fmtTok(restTok) + ' (' + restPct.toFixed(1) + '%)';
            stacked.appendChild(seg);
        }
    }

    _updateSortIndicators();

    // ---- 数据行与合计 ----
    container.innerHTML = '';
    const sumEl = document.getElementById('model-summary-row');

    if (rows.length === 0) {
        container.innerHTML = `<div class="text-xs text-slate-400 text-center py-6">${_searchKeyword ? '未找到匹配的模型或供应商' : '当前时间范围内无用量数据'}</div>`;
        if (sumEl) sumEl.style.display = 'none';
        return;
    }

    rows.forEach((m, idx) => {
        const dotColor = PASTEL_PALETTE[idx % PASTEL_PALETTE.length];
        const rate = _rowRate(m);
        const rateTxt = rate >= 0 ? rate.toFixed(1) + '%' : '--';
        const outVal = _rowOutput(m);
        const totVal = _rowTotal(m);
        const costVal = _rowCost(m);
        const costEst = m.cost_est === true;
        const isGrok = (m.provider === 'grok-build');
        let costTip = '这些 token 的实际花费（账本口径美元；按汇率 ' + _lastFx + ' 折算展示）';
        if (isGrok) {
            costTip = 'Grok 客户端会话原生记录的花费（10^10 ticks/USD 换算；按汇率 ' + _lastFx + ' 折算展示）';
        } else if (costEst) {
            costTip = 'dsh 账本未定价，按官方价估算（官方价为人民币；按汇率 ' + _lastFx + ' 折算展示）';
        }
        const div = document.createElement('div');
        div.className = 'model-row';

        const inpTip = m.cache ? `输入（未命中缓存）: ${fmtTok(m.input || 0)} / 完整输入: ${fmtTok((m.input || 0) + (m.cache || 0))}` : '输入 Tokens（未命中缓存）';
        const cacheTip = m.cache ? `命中缓存读取: ${fmtTok(m.cache || 0)} tokens（享官方优惠费率）` : '无缓存命中';
        const totTip = m.cache ? `总 Token = 未命中输入(${fmtTok(m.input || 0)}) + 缓存(${fmtTok(m.cache || 0)}) + 输出(${fmtTok(outVal)})` : `总 Token = 输入 + 输出`;

        div.innerHTML = `
            <div class="flex items-center gap-2.5 flex-1 min-w-0 pl-1">
                <span class="w-2.5 h-2.5 rounded-full shadow-sm flex-shrink-0" style="background-color:${dotColor};"></span>
                <div class="min-w-0 flex flex-col justify-center">
                    <div class="text-xs font-semibold text-slate-800 truncate" title="${esc(m.model || '')}">${esc(m.model || '')}</div>
                    <div class="text-[10px] text-slate-400 font-mono leading-tight truncate" title="${esc(m.provider || '')}">${esc(m.provider || '')}</div>
                </div>
            </div>
            <div class="text-slate-500 text-center w-[58px] text-xs font-mono shrink-0 whitespace-nowrap">${m.count || 0}</div>
            <div class="text-slate-800 font-medium text-center w-[58px] text-xs font-mono shrink-0 whitespace-nowrap" title="${inpTip}">${fmtTok(m.input || 0)}</div>
            <div class="text-slate-800 font-medium text-center w-[58px] text-xs font-mono shrink-0 whitespace-nowrap" title="输出（含推理 token）">${fmtTok(outVal)}</div>
            <div class="text-center w-[58px] text-xs font-mono font-medium shrink-0 whitespace-nowrap" style="color:var(--accent-cache);" title="${cacheTip}">${fmtTok(m.cache || 0)}</div>
            <div class="text-slate-800 font-semibold text-center w-[62px] text-xs font-mono shrink-0 whitespace-nowrap" title="${totTip}">${fmtTok(totVal)}</div>
            <div class="text-center w-[54px] text-xs font-mono font-medium shrink-0 whitespace-nowrap" style="color:var(--accent-cache);" title="总缓存率 = 缓存 ÷ 总 Token">${rateTxt}</div>
            <div class="text-slate-900 font-bold text-center w-[78px] text-xs font-mono shrink-0 whitespace-nowrap" title="${costTip}">${(costEst || isGrok) ? '≈' : ''}${fmtCost(costVal)}</div>
        `;
        container.appendChild(div);
    });

    // ---- 合计汇总行（固定吸底，同宽严格居中对齐） ----
    if (sumEl) {
        const totCount = rows.reduce((acc, m) => acc + (m.count || 0), 0);
        const totInput = rows.reduce((acc, m) => acc + (m.input || 0), 0);
        const totOutput = rows.reduce((acc, m) => acc + _rowOutput(m), 0);
        const totCache = rows.reduce((acc, m) => acc + (m.cache || 0), 0);
        const totTokens = rows.reduce((acc, m) => acc + _rowTotal(m), 0);
        const totCost = rows.reduce((acc, m) => acc + _rowCost(m), 0);
        const avgRate = totTokens > 0 ? (totCache / totTokens * 100) : 0;
        const avgRateTxt = totTokens > 0 ? avgRate.toFixed(1) + '%' : '--';

        sumEl.style.display = 'flex';
        sumEl.innerHTML = `
            <div class="flex items-center gap-2 flex-1 min-w-0 pl-1 font-bold text-slate-800 whitespace-nowrap">
                <span class="w-2 h-2 rounded-full bg-slate-400 shrink-0"></span>
                <span class="text-xs font-bold text-slate-800 tracking-tight truncate">合计 <span class="text-[10.5px] font-normal text-slate-500 font-mono">(${rows.length}个模型)</span></span>
            </div>
            <div class="text-slate-800 font-bold text-center w-[58px] text-xs font-mono shrink-0 whitespace-nowrap">${totCount}</div>
            <div class="text-slate-800 font-bold text-center w-[58px] text-xs font-mono shrink-0 whitespace-nowrap" title="总输入 Tokens">${fmtTok(totInput)}</div>
            <div class="text-slate-800 font-bold text-center w-[58px] text-xs font-mono shrink-0 whitespace-nowrap" title="总输出 Tokens（含推理）">${fmtTok(totOutput)}</div>
            <div class="text-emerald-600 font-bold text-center w-[58px] text-xs font-mono shrink-0 whitespace-nowrap" title="总命中缓存 Tokens">${fmtTok(totCache)}</div>
            <div class="text-slate-900 font-bold text-center w-[62px] text-xs font-mono shrink-0 whitespace-nowrap" title="总 Token 汇总">${fmtTok(totTokens)}</div>
            <div class="text-emerald-600 font-bold text-center w-[54px] text-xs font-mono shrink-0 whitespace-nowrap" title="平均缓存命中率">${avgRateTxt}</div>
            <div class="text-slate-900 font-bold text-center w-[78px] text-xs font-mono shrink-0 whitespace-nowrap" title="总折合费用">${fmtCost(totCost)}</div>
        `;
    }
}

function switchTab(t) {
    if (t === 0) {
        document.getElementById('tab-btn-0').className = 'pivot-btn active';
        document.getElementById('tab-btn-1').className = 'pivot-btn';
        document.getElementById('tab-content-0').classList.remove('hidden');
        document.getElementById('tab-content-1').classList.add('hidden');
    } else {
        document.getElementById('tab-btn-0').className = 'pivot-btn';
        document.getElementById('tab-btn-1').className = 'pivot-btn active';
        document.getElementById('tab-content-0').classList.add('hidden');
        document.getElementById('tab-content-1').classList.remove('hidden');
    }
}

/* 剪贴板：navigator.clipboard 在非安全上下文里可能是 undefined，
   此时 writeText(...) 抛的是**同步** TypeError，.catch 根本捕不到。
   所以先判存在性，再回退 execCommand。 */
function copyText(text, okMsg) {
    const fallback = () => {
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            document.body.removeChild(ta);
            if (ok) showToast(okMsg, 'success');
            else showToast('复制失败：当前环境不允许访问剪贴板', 'error');
        } catch (e) {
            showToast('复制失败: ' + e, 'error');
        }
    };
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        try {
            navigator.clipboard.writeText(text)
                .then(() => showToast(okMsg, 'success'))
                .catch(() => fallback());
            return;
        } catch (e) { /* 落到 fallback */ }
    }
    fallback();
}

function copyModelTable() {
    if (!_lastRows || _lastRows.length === 0) {
        showToast('当前没有模型用量数据可复制', 'warning');
        return;
    }
    const ccyHeader = _costCcy === 'CNY' ? '预估费用(¥)' : '折合费用($)';
    let md = `| 模型 | 供应商 | 调用次数 | 输入Tokens | 输出Tokens | 命中缓存 | 总Token | 缓存率 | ${ccyHeader} |\n`;
    md += '| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n';
    _lastRows.forEach(m => {
        const model = (m.model || '').replace(/\|/g, '\\|').replace(/[<>]/g, '');
        const provider = (m.provider || '').replace(/\|/g, '\\|').replace(/[<>]/g, '');
        const count = m.count || 0;
        const inTok = fmtTok(m.input || 0);
        const outTok = fmtTok(_rowOutput(m));
        const cacheTok = fmtTok(m.cache || 0);
        const totTok = fmtTok(_rowTotal(m));
        const rate = _rowRate(m);
        const rateTxt = rate >= 0 ? rate.toFixed(1) + '%' : '--';
        const costVal = _rowCost(m);
        const costTxt = fmtCost(costVal);
        md += `| ${model} | ${provider} | ${count} | ${inTok} | ${outTok} | ${cacheTok} | ${totTok} | ${rateTxt} | ${costTxt} |\n`;
    });
    copyText(md, `已复制 ${_lastRows.length} 个模型的用量表为 Markdown`);
}

function copyRawJson() {
    const text = document.getElementById('raw-json-pre').innerText;
    // 没有数据时 pre 里是占位提示文案，直接复制会「复制了一段中文还提示成功」。
    // 判据：内容必须能当 JSON 解析。
    let ok = false;
    try { JSON.parse(text); ok = true; } catch (e) { ok = false; }
    if (!ok) {
        showToast('当前没有原始数据可复制，请先点「立即查询」', 'warning');
        return;
    }
    copyText(text, '已复制 JSON 到剪贴板');
}

/* ================= 模态通用 ================= */
let _modalChannel = 'opencode-go';
let _modalTierValue = 0;
const _modalStack = [];

function openModal(id) {
    const el = document.getElementById(id);
    if (!el || el.classList.contains('open')) return;
    el.classList.add('open');
    _modalStack.push(id);
}

function closeModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('open');
    const i = _modalStack.lastIndexOf(id);
    if (i >= 0) _modalStack.splice(i, 1);
}

function closeTopModal() {
    if (_modalStack.length === 0) return false;
    closeModal(_modalStack[_modalStack.length - 1]);
    return true;
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeTopModal();
});

/* ================= 密钥模态（添加 / 编辑） ================= */
let _modalMode = 'add'; // 'add' | 'edit'
let _editingKeyIndex = -1;

function openAddKeyModal() {
    if (!window.pywebview || !window.pywebview.api) {
        return;
    }
    _modalMode = 'add';
    _editingKeyIndex = -1;
    const titleEl = document.getElementById('modal-key-title');
    if (titleEl) titleEl.innerText = '添加密钥';
    const subBtn = document.getElementById('btn-submit-key');
    if (subBtn) subBtn.innerText = '添加';

    // 重置表单
    document.getElementById('add-key-alias').value = '';
    document.getElementById('add-key-value').value = '';
    document.getElementById('add-key-error').className = 'modal-error';
    document.getElementById('add-key-error').innerText = '';
    document.getElementById('add-key-value').type = 'password';
    document.getElementById('add-key-eye').innerText = '显示';
    // 档位列：由已有阶跃 Key 推断默认档位
    const existingStepTier = ((appState && appState.keys) || [])
        .filter(k => key_channel_of(k) === 'stepfun' && k.step_tier)
        .map(k => k.step_tier)
        .sort((a, b) => b - a)[0];
    const defTier = existingStepTier || (appState && appState.default_tier) || DEFAULT_TIER_FALLBACK;
    fillTierSelect('add-key-tier', defTier);
    selectChannelInModal('opencode-go');
    openModal('modal-add-key');
    setTimeout(() => document.getElementById('add-key-value').focus(), 60);
}

function openEditKeyModal() {
    if (!window.pywebview || !window.pywebview.api) return;
    const keys = (appState && appState.keys) || [];
    if (!(0 <= currentKeyIndex && currentKeyIndex < keys.length)) {
        showToast('请先选择要编辑的密钥', 'warning');
        return;
    }
    const it = keys[currentKeyIndex];
    _modalMode = 'edit';
    _editingKeyIndex = currentKeyIndex;

    const titleEl = document.getElementById('modal-key-title');
    if (titleEl) titleEl.innerText = '编辑密钥';
    const subBtn = document.getElementById('btn-submit-key');
    if (subBtn) subBtn.innerText = '保存';

    document.getElementById('add-key-alias').value = it.alias || '';
    document.getElementById('add-key-value').value = it.key || '';
    document.getElementById('add-key-error').className = 'modal-error';
    document.getElementById('add-key-error').innerText = '';
    document.getElementById('add-key-value').type = 'password';
    document.getElementById('add-key-eye').innerText = '显示';

    const ch = key_channel_of(it);
    const tier = it.step_tier || (appState && appState.default_tier) || DEFAULT_TIER_FALLBACK;
    fillTierSelect('add-key-tier', tier);
    selectChannelInModal(ch);

    openModal('modal-add-key');
    setTimeout(() => document.getElementById('add-key-alias').focus(), 60);
}

function key_channel_of(it) {
    return (it && it.channel) || 'opencode-go';
}

function fillTierSelect(selectId, selected) {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    const tiers = (appState && appState.tiers) || [];
    const labels = (appState && appState.tier_labels) || {};
    sel.innerHTML = '';
    tiers.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t;
        opt.text = labels[t] || (t + 'M');
        if (parseInt(t) === parseInt(selected)) opt.selected = true;
        sel.appendChild(opt);
    });
    // 档位列表为空时的兜底
    if (tiers.length === 0) {
        const opt = document.createElement('option');
        opt.value = selected; opt.text = selected + 'M';
        sel.appendChild(opt);
    }
}

function selectChannelInModal(ch) {
    _modalChannel = ch;
    document.getElementById('channel-go').classList.toggle('selected', ch === 'opencode-go');
    document.getElementById('channel-stepfun').classList.toggle('selected', ch === 'stepfun');
    const ccEl = document.getElementById('channel-commandcode');
    if (ccEl) ccEl.classList.toggle('selected', ch === 'commandcode');
    const grokEl = document.getElementById('channel-grok');
    if (grokEl) grokEl.classList.toggle('selected', ch === 'grok-build');
    const clineEl = document.getElementById('channel-cline');
    if (clineEl) clineEl.classList.toggle('selected', ch === 'cline');
    document.getElementById('add-key-tier-row').style.display = (ch === 'stepfun') ? '' : 'none';

    const keyInp = document.getElementById('add-key-value');
    const aliasInp = document.getElementById('add-key-alias');
    const eyeBtn = document.getElementById('add-key-eye');
    const keyLabel = document.getElementById('add-key-value-label');
    if (ch === 'grok-build') {
        if (!aliasInp.value || aliasInp.value.startsWith('Key-')) aliasInp.value = 'Grok Build';
        keyInp.value = 'local-grok-session';
        keyInp.placeholder = '自动扫描本地 ~/.grok/sessions（免密）';
        keyInp.readOnly = true;
        keyInp.style.opacity = '0.65';
        if (eyeBtn) eyeBtn.style.display = 'none';
        if (keyLabel) keyLabel.innerText = '数据源 (本地免密)';
    } else {
        if (keyInp.value === 'local-grok-session') {
            keyInp.value = '';
            if (aliasInp.value === 'Grok Build') aliasInp.value = '';
        }
        keyInp.placeholder = 'sk-…';
        keyInp.readOnly = false;
        keyInp.style.opacity = '1';
        if (eyeBtn) eyeBtn.style.display = '';
        if (keyLabel) keyLabel.innerText = 'API Key';
    }
}

function toggleKeyVisibility() {
    const inp = document.getElementById('add-key-value');
    const btn = document.getElementById('add-key-eye');
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    btn.innerText = show ? '隐藏' : '显示';
}

function _showModalError(msg) {
    const el = document.getElementById('add-key-error');
    el.innerText = msg;
    el.className = 'modal-error show';
}

function submitKeyForm() {
    if (!window.pywebview || !window.pywebview.api) return;
    const alias = document.getElementById('add-key-alias').value.trim();
    let key = document.getElementById('add-key-value').value.trim();
    const channel = _modalChannel;
    let tier = 0;
    if (channel === 'stepfun') {
        const sel = document.getElementById('add-key-tier');
        tier = parseInt(sel.value || 0) || 0;
    }
    // 前端初校
    if (channel === 'grok-build') {
        if (!key) key = 'local-grok-session';
    } else {
        if (!key) { _showModalError('请输入 API Key'); document.getElementById('add-key-value').focus(); return; }
    }
    document.getElementById('add-key-error').className = 'modal-error';

    if (_modalMode === 'edit') {
        const oldKeyObj = (appState && appState.keys && appState.keys[_editingKeyIndex]) ? appState.keys[_editingKeyIndex] : null;
        const oldKeyStr = oldKeyObj ? (oldKeyObj.key || '') : '';
        apiCall('edit_key', [_editingKeyIndex, alias, key, channel, tier, oldKeyStr]).then(res => {
            if (res && res.error) {
                _showModalError(res.error);
                return;
            }
            if (res && res.keys) {
                appState.keys = res.keys;
                closeModal('modal-add-key');
                const foundIdx = appState.keys.findIndex(k => k.key === key);
                if (foundIdx !== -1) {
                    currentKeyIndex = foundIdx;
                }
                renderKeyList();
                selectKey(currentKeyIndex);
                showToast('密钥已保存', 'success');
                doQueryCurrent();
            }
        }).catch(e => _showModalError('修改失败: ' + e));
    } else {
        apiCall('add_key', [alias, key, channel, tier]).then(res => {
            if (res && res.error) {
                _showModalError(res.error);
                return;
            }
            if (res && res.keys) {
                appState.keys = res.keys;
                closeModal('modal-add-key');
                currentKeyIndex = (typeof res.new_index === 'number') ? res.new_index : appState.keys.length - 1;
                renderKeyList();
                selectKey(currentKeyIndex);
                showToast('密钥添加成功', 'success');
                // 添加后自动查询一次，直接看到数据
                doQueryCurrent();
            }
        }).catch(e => _showModalError('添加失败: ' + e));
    }
}
const submitAddKey = submitKeyForm;

/* ================= 月池档位模态 ================= */
function editCurrentTier() {
    const keys = (appState && appState.keys) || [];
    if (!(0 <= currentKeyIndex && currentKeyIndex < keys.length)) return;
    const it = keys[currentKeyIndex];
    if (key_channel_of(it) !== 'stepfun') {
        _openInfoModal('修改月池档位', '仅「阶跃星辰 StepFun」密钥需要设置月池档位。');
        return;
    }
    // 复位外壳（上次可能以「信息提示」形态打开过，按钮还是"知道了"）
    _restoreTierModal();
    const box = document.getElementById('tier-options');
    const tiers = (appState && appState.tiers) || [];
    const labels = (appState && appState.tier_labels) || {};
    _modalTierValue = parseInt(it.step_tier) || (appState.default_tier || DEFAULT_TIER_FALLBACK);
    document.getElementById('tier-key-name').innerText = '密钥: ' + (it.alias || 'Key-' + (currentKeyIndex + 1));
    box.innerHTML = '';
    if (tiers.length === 0) {
        box.innerHTML = '<div style="font-size:11.5px;color:#94a3b8;padding:8px 2px;">暂无档位数据</div>';
    }
    tiers.forEach(t => {
        const tv = parseInt(t);
        const div = document.createElement('div');
        div.className = 'tier-option' + (tv === _modalTierValue ? ' selected' : '');
        div.onclick = () => { _modalTierValue = tv; Array.from(box.children).forEach(c => c.classList.remove('selected')); div.classList.add('selected'); };
        div.innerHTML = '<span>' + (labels[t] || (t + 'M')) + '</span><span class="t-credit">' + t + 'M</span>';
        box.appendChild(div);
    });
    openModal('modal-tier');
}

function submitTier() {
    if (!requireBridge()) return;
    const it = (appState && appState.keys) ? appState.keys[currentKeyIndex] : null;
    const keyStr = it ? (it.key || '') : '';
    // 后端失败响应同样带 keys 字段，必须先判 error/success，否则「保存失败」也会关弹窗并提示成功
    apiCall('save_tier', [currentKeyIndex, _modalTierValue, keyStr]).then(res => {
        if (!res || res.error) {
            showToast('保存失败: ' + ((res && res.error) || '未知错误'), 'error');
            return;
        }
        appState.keys = res.keys || appState.keys;
        closeModal('modal-tier');
        renderKeyList();
        selectKey(currentKeyIndex);
        showToast('档位已保存', 'success');
        doQueryCurrent();
    });
}

/* 通用提示小模态（复用档位模态外壳改标题） */
let _tierModalReadonly = false;

function _openInfoModal(title, text) {
    _tierModalReadonly = true;
    document.querySelector('#modal-tier .modal-title').innerText = title;
    document.getElementById('tier-key-name').innerText = text;
    document.getElementById('tier-options').innerHTML = '';
    // 隐藏保存按钮语义：临时改
    const saveBtn = document.querySelector('#modal-tier .modal-footer .btn-action-primary');
    saveBtn.innerText = '知道了';
    saveBtn.onclick = () => { closeModal('modal-tier'); _restoreTierModal(); };
    openModal('modal-tier');
}
function _restoreTierModal() {
    _tierModalReadonly = false;
    document.querySelector('#modal-tier .modal-title').innerText = '月池档位';
    const saveBtn = document.querySelector('#modal-tier .modal-footer .btn-action-primary');
    saveBtn.innerText = '保存';
    saveBtn.onclick = submitTier;
}

/* ================= 删除确认模态 ================= */
function deleteCurrentKey() {
    const keys = (appState && appState.keys) || [];
    if (!(0 <= currentKeyIndex && currentKeyIndex < keys.length)) return;
    const it = keys[currentKeyIndex];
    // 别名是用户可控字符串：用 esc() 转义，一个双引号就能撑破这段 HTML
    document.getElementById('delete-confirm-text').innerHTML =
        '确定删除「<b>' + esc(it.alias || ('Key-' + (currentKeyIndex + 1))) + '</b>」吗？<br>' +
        '<span style="font-family:Consolas,monospace;color:#94a3b8;">' + esc(maskKey(it.key)) + '</span><br>删除后无法恢复。';
    openModal('modal-delete');
}

function confirmDeleteKey() {
    if (!requireBridge()) return;
    const it = (appState && appState.keys) ? appState.keys[currentKeyIndex] : null;
    const keyStr = it ? (it.key || '') : '';
    apiCall('delete_key', [currentKeyIndex, keyStr]).then(res => {
        if (!res || res.error) {
            showToast('删除失败: ' + ((res && res.error) || '未知错误'), 'error');
            return;
        }
        appState.keys = res.keys || appState.keys;
        currentKeyIndex = Math.min(Math.max(0, currentKeyIndex - 1), appState.keys.length - 1);
        if (currentKeyIndex < 0) currentKeyIndex = 0;
        closeModal('modal-delete');
        renderKeyList();
        selectKey(currentKeyIndex);
        showToast('密钥已删除', 'success');
    });
}

/* 供 Enter 提交（在添加/编辑模态的输入框内） */
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const addOpen = document.getElementById('modal-add-key').classList.contains('open');
    const tierOpen = document.getElementById('modal-tier').classList.contains('open');
    if (addOpen && e.target && e.target.id && e.target.id.indexOf('add-key-') === 0) {
        submitKeyForm();
    } else if (tierOpen) {
        // 档位模态有两种形态：可编辑的「月池档位」和只读的「信息提示」。
        // 旧实现在信息形态下按 Enter 也会执行 submitTier()，把全局 _modalTierValue
        // （初值 0 或上一个 Key 的档位）写进当前 Key —— 静默污染密钥配置。
        if (_tierModalReadonly) {
            closeModal('modal-tier');
            _restoreTierModal();
        } else {
            submitTier();
        }
    }
});

/* ================= 主题与系统设置控制器 ================= */
let _currentTheme = (appState && appState.theme) || 'obsidian';
function applyTheme(themeName) {
    _currentTheme = themeName || 'obsidian';
    if (!_currentTheme || _currentTheme === 'blue') {
        document.documentElement.removeAttribute('data-theme');
    } else {
        document.documentElement.setAttribute('data-theme', _currentTheme);
    }
    updateThemeModalCards();
}
applyTheme(_currentTheme);

function openSettingsModal() {
    updateThemeModalCards();
    renderPathsPanel();
    openModal('modal-settings');
}

/* ---------- 数据源路径面板 ---------- */
function _pathRow(label, obj) {
    if (!obj) return '';
    const ok = obj.found;
    const color = ok ? '#22c55e' : '#f43f5e';
    const mark = ok ? '✓ 已找到' : '✕ 未找到';
    return `<div style="display:flex;gap:6px;align-items:baseline;">
      <span style="color:${color};flex-shrink:0;">${mark}</span>
      <span style="color:#94a3b8;flex-shrink:0;">${esc(label)}</span>
      <span style="font-family:Consolas,monospace;font-size:10.5px;color:#cbd5e1;word-break:break-all;">${esc(obj.path || '(未知)')}</span>
    </div>`;
}

function renderPathsPanel() {
    const p = (appState && appState.paths) || {};
    const box = document.getElementById('paths-status');
    if (box) {
        const cm = p.cost_meter || {};
        const cmRow = cm.installed
            ? `<div style="color:#22c55e;">✓ dsh-cost-meter 插件已安装（按日费用精确）</div>`
            : `<div style="color:#f59e0b;">⚠ 未检测到 dsh-cost-meter 插件 —— 将回退到 dsh 会话缓存统计：`
              + `合计准确，但「按日」以会话创建日归属，跨天会话会整段计入创建日</div>`;
        box.innerHTML =
            cmRow +
            (p.dsh_home_warning
                ? `<div style="color:#f59e0b;">⚠ ${esc(p.dsh_home_warning)}</div>`
                : '') +
            _pathRow('DSH 账本', p.dsh_ledger) +
            _pathRow('会话缓存', p.session_cache) +
            _pathRow('Grok 会话库', p.grok_home) +
            _pathRow('opencode 库', p.opencode_db) +
            (p.env_dsh_home ? `<div style="color:#64748b;font-size:10.5px;">环境变量 DSH_HOME = ${esc(p.env_dsh_home)}</div>` : '');
    }
    const set = (id, v) => { const el = document.getElementById(id); if (el && !el.value) el.value = v || ''; };
    set('path-dsh-home', (p.dsh_home || {}).override);
    set('path-grok-home', (p.grok_home || {}).override);
    set('path-opencode-db', (p.opencode_db || {}).override);
}

function _readPathInputs() {
    const g = (id) => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
    return [g('path-dsh-home'), g('path-grok-home'), g('path-opencode-db')];
}

function saveDataPaths() {
    if (!requireBridge()) return;
    const [a, b, c] = _readPathInputs();
    apiCall('set_paths', [a, b, c], 20000).then(res => {
        if (!res || res.error) { showToast('保存失败：' + ((res && res.error) || '未知错误'), 'error'); return; }
        appState.paths = res.paths || appState.paths;
        renderPathsPanel();
        showToast('路径已保存，正在重新统计', 'success');
        loadTokenStats();
    });
}

function autoDetectPaths() {
    if (!requireBridge()) return;
    ['path-dsh-home', 'path-grok-home', 'path-opencode-db'].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = '';
    });
    apiCall('detect_paths', [], 20000).then(res => {
        if (!res || res.error) { showToast('自动探测失败：' + ((res && res.error) || '未知错误'), 'error'); return; }
        appState.paths = res.paths || appState.paths;
        renderPathsPanel();
        showToast('已恢复自动探测', 'success');
        loadTokenStats();
    });
}

function updateThemeModalCards() {
    const list = ['obsidian', 'paper', 'slate', 'violet'];
    list.forEach(th => {
        const card = document.getElementById(`theme-opt-${th}`);
        const chk = document.getElementById(`theme-chk-${th}`);
        if (card) {
            if (_currentTheme === th) card.classList.add('active');
            else card.classList.remove('active');
        }
        if (chk) {
            chk.style.display = (_currentTheme === th) ? 'inline-block' : 'none';
        }
    });
}

async function selectAppTheme(th) {
    applyTheme(th);
    if (window.pywebview && window.pywebview.api && window.pywebview.api.set_theme) {
        try {
            await window.pywebview.api.set_theme(th);
        } catch(e) {}
    }
    const names = {
        'obsidian': '黑曜极客暗黑',
        'paper': '日系暖米纸质',
        'slate': '冰海冷萃 Slate',
        'violet': '暮色星云暗紫'
    };
    showToast('已切换为：' + (names[th] || th), 'success', 1500);
}

/* ================= 密钥顺序移动 ================= */
async function moveCurrentKey(direction) {
    if (!requireBridge()) return;
    const it = (appState && appState.keys) ? appState.keys[currentKeyIndex] : null;
    const keyStr = it ? (it.key || '') : '';
    const res = await apiCall('move_key', [currentKeyIndex, direction, keyStr], 20000);
    if (res && res.success) {
        const moved = res.new_index !== currentKeyIndex;
        appState.keys = res.keys || appState.keys;
        currentKeyIndex = res.new_index;
        renderKeyList();
        selectKey(currentKeyIndex);
        if (!moved) {
            // 已在首/末位时顺序不变，不能再提示「已上移/已置顶」——那是假成功
            showToast(direction === 'down' ? '已经在最后一位了' : '已经在最前面了', 'info', 1500);
        } else {
            showToast(direction === 'up' ? '已上移密钥' : (direction === 'down' ? '已下移密钥' : '已置顶密钥'), 'success', 1200);
        }
    } else if (res && res.error) {
        showToast(res.error, 'warning', 1500);
    } else {
        showToast('移动失败：无响应', 'error');
    }
}

/* ================= 定时自动刷新 ================= */
let _autoRefreshTimer = null;
function onAutoRefreshChange(val) {
    const sec = parseInt(val, 10) || 0;
    if (_autoRefreshTimer) {
        clearInterval(_autoRefreshTimer);
        _autoRefreshTimer = null;
    }
    if (sec > 0) {
        _autoRefreshTimer = setInterval(() => {
            // doQueryAll 自带 in-flight 守卫：上一轮没跑完时本轮直接跳过，
            // 不会再出现「定时器与手动刷新叠加把按钮文案写坏」的问题。
            if (typeof doQueryAll === 'function') doQueryAll();
        }, sec * 1000);
        const min = Math.round(sec / 60);
        showToast(`已开启自动刷新：每 ${min} 分钟`, 'success', 2000);
    } else {
        showToast('已关闭自动刷新', 'info', 1500);
    }
}

/* ================= 备份与导入 ================= */
function openBackupModal() {
    const st = document.getElementById('backup-status');
    if (st) st.innerText = '';
    openModal('modal-backup');
}

async function doExportKeys() {
    if (!requireBridge()) return;
    try {
        const res = await apiCall('export_keys', [], 30000);
        if (res && res.success && res.json_str) {
            const blob = new Blob([res.json_str], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            const nowStr = new Date().toISOString().slice(0, 10);
            a.href = url;
            a.download = `opencode_keys_backup_${nowStr}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            const st = document.getElementById('backup-status');
            if (st) st.innerText = '✓ 备份文件已下载';
            showToast('密钥配置已导出', 'success');
        } else {
            showToast('导出失败: ' + ((res && res.error) || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('导出异常: ' + e, 'error');
    }
}

function handleImportFile(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (e) => {
        const content = e.target.result;
        if (!requireBridge()) return;
        try {
            const res = await apiCall('import_keys', [content], 30000);
            if (res && res.success) {
                appState.keys = res.keys || appState.keys;
                currentKeyIndex = 0;
                renderKeyList();
                selectKey(0);
                const st = document.getElementById('backup-status');
                if (st) st.innerText = `✓ 成功导入 ${res.count} 个密钥`;
                showToast(`成功导入 ${res.count} 个密钥`, 'success');
                setTimeout(() => closeModal('modal-backup'), 1200);
            } else {
                const st = document.getElementById('backup-status');
                if (st) st.innerText = '✕ 导入失败: ' + ((res && res.error) || '');
                showToast('导入失败: ' + ((res && res.error) || ''), 'error');
            }
        } catch (err) {
            showToast('导入异常: ' + err, 'error');
        }
    };
    reader.readAsText(file, 'utf-8');
    event.target.value = '';
}

// Immediately render on page load without waiting for bridge
function initImmediateUI() {
    const tg = document.getElementById('ccy-toggle');
    if (tg) tg.textContent = (_costCcy === 'USD') ? '$' : '¥';
    updateOnlyKeyButtonUI();
    renderKeyList();
    if (appState && appState.keys && appState.keys.length > 0) {
        selectKey(currentKeyIndex);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initImmediateUI);
} else {
    initImmediateUI();
}

// When pywebview bridge connects
function onBridgeConnected() {
    loadTokenStats();
}

if (window.pywebview && window.pywebview.api) {
    onBridgeConnected();
} else {
    window.addEventListener('pywebviewready', onBridgeConnected);
    let checks = 0;
    const checkTimer = setInterval(() => {
        checks++;
        if ((window.pywebview && window.pywebview.api) || checks > 40) {
            clearInterval(checkTimer);
            if (window.pywebview && window.pywebview.api) onBridgeConnected();
        }
    }, 50);
}
</script>

<!-- ============ 模态：添加密钥 ============ -->
  <div id="modal-add-key" class="modal-backdrop" onclick="if(event.target===this) closeModal('modal-add-key')">
    <div class="modal-card">
      <div class="modal-header">
        <span id="modal-key-title" class="modal-title">添加密钥</span>
        <button class="modal-x" onclick="closeModal('modal-add-key')" title="关闭">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
      </div>
      <div class="modal-body">
        <div id="add-key-error" class="modal-error"></div>

        <div>
          <label class="modal-label">名称</label>
          <input id="add-key-alias" class="modal-input" type="text" placeholder="留空自动命名（Key-1、Key-2…）" maxlength="40">
        </div>

        <div>
          <label class="modal-label">供应商</label>
          <div id="channel-go" class="channel-option selected" onclick="selectChannelInModal('opencode-go')">
            <div class="co-icon" style="background:#e0f2fe;">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#0369a1" stroke-width="2"><polyline points="16 18 22 12 16 6"></polyline><polyline points="8 6 2 12 8 18"></polyline></svg>
            </div>
            <div>
              <div class="co-title">OpenCode Go</div>
              <div class="co-desc">opencode.ai/zen/go · 5h $12 / 周 $30 / 月 $60</div>
            </div>
            <div class="co-radio"></div>
          </div>
          <div id="channel-stepfun" class="channel-option" onclick="selectChannelInModal('stepfun')">
            <div class="co-icon" style="background:#fef3c7;">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#b45309" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
            </div>
            <div>
              <div class="co-title">阶跃星辰 StepFun</div>
              <div class="co-desc">Step Plan 订阅 · 月池 Credit 按月发放</div>
            </div>
            <div class="co-radio"></div>
          </div>
          <div id="channel-commandcode" class="channel-option" onclick="selectChannelInModal('commandcode')">
            <div class="co-icon" style="background:#dcfce7;">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#047857" stroke-width="2"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
            </div>
            <div>
              <div class="co-title">Command Code</div>
              <div class="co-desc">api.commandcode.ai · 5h / 每周 / 月额度实时查询</div>
            </div>
            <div class="co-radio"></div>
          </div>
          <div id="channel-grok" class="channel-option" onclick="selectChannelInModal('grok-build')">
            <div class="co-icon" style="background:#f3e8ff;">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#7e22ce" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
            </div>
            <div>
              <div class="co-title">Grok Build (本地免密)</div>
              <div class="co-desc">~/.grok/sessions · 自动扫描本机各项目会话 · 无需 Key</div>
            </div>
            <div class="co-radio"></div>
          </div>
          <div id="channel-cline" class="channel-option" onclick="selectChannelInModal('cline')">
            <div class="co-icon" style="background:#e0e7ff;">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#4338ca" stroke-width="2"><path d="M12 2 2 7l10 5 10-5-10-5z"></path><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>
            </div>
            <div>
              <div class="co-title">Cline Pass</div>
              <div class="co-desc">api.cline.bot · 订阅/余额官方接口 · 用量取自 dsh 账本</div>
            </div>
            <div class="co-radio"></div>
          </div>
        </div>

        <div id="add-key-value-row">
          <label class="modal-label" id="add-key-value-label">API Key</label>
          <div class="input-row">
            <input id="add-key-value" class="modal-input mono" type="password" placeholder="sk-…" autocomplete="off" spellcheck="false">
            <button id="add-key-eye" class="btn-eye" onclick="toggleKeyVisibility()">显示</button>
          </div>
        </div>

        <div id="add-key-tier-row" style="display:none;">
          <label class="modal-label">月池档位</label>
          <select id="add-key-tier" class="modal-select"></select>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-action btn-action-secondary" onclick="closeModal('modal-add-key')">取消</button>
        <button id="btn-submit-key" class="btn-action btn-action-primary" onclick="submitKeyForm()">添加</button>
      </div>
    </div>
  </div>

  <!-- ============ 模态：月池档位 ============ -->
  <div id="modal-tier" class="modal-backdrop" onclick="if(event.target===this) closeModal('modal-tier')">
    <div class="modal-card" style="width:360px;">
      <div class="modal-header">
        <span class="modal-title">月池档位</span>
        <button class="modal-x" onclick="closeModal('modal-tier')" title="关闭">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
      </div>
      <div class="modal-body">
        <div id="tier-key-name" style="font-size:11.5px;color:#64748b;margin-bottom:2px;"></div>
        <div id="tier-options"></div>
      </div>
      <div class="modal-footer">
        <button class="btn-action btn-action-secondary" onclick="closeModal('modal-tier')">取消</button>
        <button class="btn-action btn-action-primary" onclick="submitTier()">保存</button>
      </div>
    </div>
  </div>

  <!-- ============ 模态：删除确认 ============ -->
  <div id="modal-delete" class="modal-backdrop" onclick="if(event.target===this) closeModal('modal-delete')">
    <div class="modal-card" style="width:340px;">
      <div class="modal-body" style="padding-top:20px;">
        <div style="display:flex;gap:12px;align-items:flex-start;">
          <div style="width:32px;height:32px;border-radius:8px;background:#fef2f2;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#e11d48" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
          </div>
          <div>
            <div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:3px;">删除密钥</div>
            <div id="delete-confirm-text" style="font-size:11.5px;color:#64748b;line-height:1.5;"></div>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-action btn-action-secondary" onclick="closeModal('modal-delete')">取消</button>
        <button class="btn-danger" onclick="confirmDeleteKey()">删除</button>
      </div>
    </div>
  </div>

  <!-- ============ 模态：备份与恢复配置 ============ -->
  <div id="modal-backup" class="modal-backdrop" onclick="if(event.target===this) closeModal('modal-backup')">
    <div class="modal-card" style="width:380px;">
      <div class="modal-header">
        <span class="modal-title">配置备份与还原</span>
        <button class="modal-x" onclick="closeModal('modal-backup')" title="关闭">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
      </div>
      <div class="modal-body">
        <div style="font-size:12px;color:#64748b;line-height:1.5;margin-bottom:14px;">
          导出当前所有密钥与通道设置；或从已有的 JSON 备份文件中一键还原全部密钥。
        </div>
        <div style="display:flex;gap:10px;margin-bottom:10px;">
          <button onclick="doExportKeys()" class="btn-action btn-action-primary" style="flex:1;justify-content:center;padding:8px 0;">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
            <span>导出备份</span>
          </button>
          <button onclick="document.getElementById('import-file-input').click()" class="btn-action btn-action-secondary" style="flex:1;justify-content:center;padding:8px 0;">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
            <span>导入还原</span>
          </button>
          <input type="file" id="import-file-input" accept=".json" style="display:none;" onchange="handleImportFile(event)">
        </div>
        <div id="backup-status" style="font-size:11.5px;color:#059669;min-height:18px;text-align:center;"></div>
      </div>
      <div class="modal-footer">
        <button class="btn-action btn-action-secondary" onclick="closeModal('modal-backup')">关闭</button>
      </div>
    </div>
  </div>

  <!-- ============ 模态：系统与主题设置 ============ -->
  <div id="modal-settings" class="modal-backdrop" onclick="if(event.target===this) closeModal('modal-settings')">
    <div class="modal-card" style="width:480px; max-width:92vw;">
      <div class="modal-header">
        <div class="flex items-center gap-2">
          <div class="w-6 h-6 rounded-md bg-blue-50 text-blue-600 flex items-center justify-center">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
          </div>
          <span class="modal-title">系统与界面设置</span>
        </div>
        <button class="modal-x" onclick="closeModal('modal-settings')" title="关闭">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
      </div>
      <div class="modal-body">
        <div style="font-size:12px;color:var(--text-secondary);line-height:1.5;margin-bottom:12px;">
          选择您心仪的主题风格，界面色彩与高明度护眼配色即时生效，并自动保存您的配置：
        </div>
        <div class="grid grid-cols-2 gap-3 mb-2">
          <!-- 风格 1：黑曜极客暗黑 -->
          <div id="theme-opt-obsidian" class="theme-select-card cursor-pointer border rounded-lg p-2.5 transition relative" onclick="selectAppTheme('obsidian')" style="background:#090d16; border-color:#1e293b;">
            <div class="flex items-center justify-between mb-1.5">
              <span style="font-size:12px;font-weight:600;color:#f8fafc;">黑曜极客暗黑</span>
              <span id="theme-chk-obsidian" class="text-xs text-sky-400 font-bold" style="display:none;">✓ 当前</span>
            </div>
            <div class="flex items-center gap-1.5 mb-1">
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#090d16;border:1px solid #334155;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#0ea5e9;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#6ee7b7;"></span>
            </div>
            <div style="font-size:10.5px;color:#94a3b8;">沉浸全黑背景 · 晶体白字 · 柔雾薄荷绿</div>
          </div>

          <!-- 风格 2：日系暖米纸质 -->
          <div id="theme-opt-paper" class="theme-select-card cursor-pointer border rounded-lg p-2.5 transition relative" onclick="selectAppTheme('paper')" style="background:#fcfbf9; border-color:#e7e2d7;">
            <div class="flex items-center justify-between mb-1.5">
              <span style="font-size:12px;font-weight:600;color:#2c2621;">日系暖米纸质</span>
              <span id="theme-chk-paper" class="text-xs text-amber-700 font-bold" style="display:none;">✓ 当前</span>
            </div>
            <div class="flex items-center gap-1.5 mb-1">
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#f5f2eb;border:1px solid #dcd5c5;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#c25e2e;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#2e7458;"></span>
            </div>
            <div style="font-size:10.5px;color:#786f66;">温暖护眼纸底 · 焙茶深字 · 雅灰绿缓存</div>
          </div>

          <!-- 风格 3：冰海冷萃 Slate -->
          <div id="theme-opt-slate" class="theme-select-card cursor-pointer border rounded-lg p-2.5 transition relative" onclick="selectAppTheme('slate')" style="background:#f0f4f8; border-color:#cbd5e1;">
            <div class="flex items-center justify-between mb-1.5">
              <span style="font-size:12px;font-weight:600;color:#0f172a;">冰海冷萃 Slate</span>
              <span id="theme-chk-slate" class="text-xs text-teal-600 font-bold" style="display:none;">✓ 当前</span>
            </div>
            <div class="flex items-center gap-1.5 mb-1">
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#f8fafc;border:1px solid #cbd5e1;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#0f766e;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#0d9488;"></span>
            </div>
            <div style="font-size:10.5px;color:#64748b;">北欧冰蓝底色 · 深海青墨字 · 冷翡绿</div>
          </div>

          <!-- 风格 4：暮色星云暗紫 -->
          <div id="theme-opt-violet" class="theme-select-card cursor-pointer border rounded-lg p-2.5 transition relative" onclick="selectAppTheme('violet')" style="background:#0f0d1b; border-color:#2a2544;">
            <div class="flex items-center justify-between mb-1.5">
              <span style="font-size:12px;font-weight:600;color:#f3f0ff;">暮色星云暗紫</span>
              <span id="theme-chk-violet" class="text-xs text-purple-400 font-bold" style="display:none;">✓ 当前</span>
            </div>
            <div class="flex items-center gap-1.5 mb-1">
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#17142b;border:1px solid #3d3566;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#8b5cf6;"></span>
              <span class="w-3.5 h-3.5 rounded-full inline-block" style="background:#7dd3fc;"></span>
            </div>
            <div style="font-size:10.5px;color:#9d95c4;">深邃夜空微紫 · 皓月白字 · 冰蓝天青</div>
          </div>
        </div>
      </div>

      <!-- ===== 数据源路径：别人的电脑上 dsh / grok / opencode 位置都不一样 ===== -->
      <div class="mb-4">
        <div class="modal-label">数据源路径</div>
        <div style="font-size:11px;color:#94a3b8;margin:-2px 0 8px;">
          留空 = 自动探测。本机用量来自这些目录，路径不对时对应面板会显示 0。
        </div>
        <div id="paths-status" style="font-size:11.5px;line-height:1.9;margin-bottom:8px;"></div>

        <label style="font-size:11px;color:#94a3b8;">DSH 数据目录（含 storages/cost-meter/ledger.json）</label>
        <input id="path-dsh-home" class="modal-input" type="text" placeholder="例如 D:\\DeepSeekHarness\\data 或 ~/.dsh" style="margin-bottom:6px;">

        <label style="font-size:11px;color:#94a3b8;">Grok 数据目录（含 sessions/ 或 auth.json）</label>
        <input id="path-grok-home" class="modal-input" type="text" placeholder="例如 ~/.grok" style="margin-bottom:6px;">

        <label style="font-size:11px;color:#94a3b8;">opencode 本地库 opencode.db 完整路径</label>
        <input id="path-opencode-db" class="modal-input" type="text" placeholder="例如 D:\\OpenCodeData\\opencode.db" style="margin-bottom:8px;">

        <div style="display:flex;gap:8px;">
          <button class="btn-action btn-action-secondary" style="flex:1;justify-content:center;padding:8px 0;" onclick="saveDataPaths()">保存路径</button>
          <button class="btn-action btn-action-secondary" style="flex:1;justify-content:center;padding:8px 0;" onclick="autoDetectPaths()">重新自动探测</button>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn-action btn-action-primary" onclick="closeModal('modal-settings')">完成</button>
      </div>
    </div>
  </div>

</body>
</html>"""

def _serialized(fn):
    """把「读-改-写整份密钥表」的操作串行化。

    add/edit/delete/move/save_tier/import 都是 load_keys() → 改 → save_keys()，
    并发执行时后写的那次会把先写的那次整份冲掉。这里加一把模块级可重入锁，
    让这些操作彼此互斥（它们都很快，不会影响查询并发）。
    """
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with _MUTATE_LOCK:
            return fn(self, *a, **kw)
    return wrapper


def js_safe(fn):
    """把 js_api 的返回值统一净化后再交给 pywebview。

    这是「查询按钮永久卡在查询中」的最后一道防线：pywebview 用
    json.dumps(allow_nan=True) 回传，只要结果里有一个 NaN / Infinity，前端
    JSON.parse 就会抛 SyntaxError，而该异常发生在回调表项被删除之后、
    resolve/reject 之前 —— Promise 永不 settle，按钮永远回不来。
    同时把未预期异常转成 {"error": ...}，避免整个调用无声消失。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return _json_safe(fn(*args, **kwargs))
        except Exception as e:
            log_line("%s 异常：%s" % (getattr(fn, "__name__", "api"), e))
            try:
                import traceback
                log_line(traceback.format_exc())
            except Exception:
                pass
            return {"error": "内部错误：%s" % e}
    wrapper._js_safe = True
    return wrapper


class DesktopAPI:
    def __init__(self, window=None):
        # 注意：pywebview 会把 js_api 对象的属性递归暴露给网页，
        # 下划线开头的属性会被跳过。窗口对象必须存成 _window，
        # 若叫 self.window，遍历器会钻进 .NET 窗口对象图无限递归（内存爆炸/未响应）
        self._window = window
        self.keys = load_keys()
        self._lock = threading.RLock()
        self._querying = False

    @js_safe
    def get_initial_state(self):
        self.keys = load_keys()
        st = load_app_settings()
        return {
            "keys": self.keys,
            "selected": self.selected_index(),
            "keys_file": str(KEYS_FILE),
            "storage_dir": str(STORAGE_DIR),
            "tiers": STEPFUN_TIERS,
            "tier_labels": STEPFUN_TIER_LABELS,
            "default_tier": DEFAULT_STEP_TIER,
            "theme": st.get("theme", "obsidian"),
            "app_version": APP_VERSION,
            "paths": self.get_paths(),
            "load_error": _LAST_LOAD_ERROR[0],
            "save_error": _LAST_SAVE_ERROR[0],
        }

    @js_safe
    def selected_index(self):
        """上次选中的 Key 索引。pywebview 会暴露所有公开方法，所以也要过净化。"""
        try:
            return _safe_int(load_app_settings().get("selected_key"), 0)
        except Exception:
            return 0

    @js_safe
    def set_selected(self, index):
        st = load_app_settings()
        st["selected_key"] = _safe_int(index)
        save_app_settings(st)
        return {"success": True}

    @js_safe
    def set_theme(self, theme_name):
        st = load_app_settings()
        st["theme"] = str(theme_name or "obsidian")
        save_app_settings(st)
        return {"success": True, "theme": st["theme"]}

    # ---------- 数据源路径（开箱即用的关键：别人机器上的目录不一样） ----------
    @js_safe
    def get_paths(self):
        """返回当前实际使用的各数据源路径 + 是否找到，供设置页展示/修改。"""
        st = load_app_settings()
        def info(p, ok):
            return {"path": str(p), "found": bool(ok)}
        ledger = dsh_ledger_path()
        grok_s = _grok_sessions_dir()
        db = get_opencode_db_path()
        out = {
            "dsh_home": dict(info(dsh_home(), _dsh_home_score(dsh_home()) > 0),
                             override=str(st.get("dsh_home") or "")),
            "grok_home": dict(info(grok_home(), grok_s.exists()),
                              override=str(st.get("grok_home") or "")),
            "opencode_db": dict(info(db, db.exists()),
                                override=str(st.get("opencode_db") or "")),
        }
        try:
            out["dsh_ledger"] = info(ledger, ledger.exists())
        except Exception:
            out["dsh_ledger"] = {"path": "", "found": False}
        try:
            _sp = dsh_home() / "storages" / "session_projcache"
            out["session_cache"] = info(_sp, _sp.exists())
        except Exception:
            out["session_cache"] = {"path": "", "found": False}
        out["cost_meter"] = {"installed": cost_meter_installed()}
        out["env_dsh_home"] = os.getenv("DSH_HOME") or os.getenv("DSH_DATA_DIR") or ""
        warn = dsh_home_warning()
        if warn:
            out["dsh_home_warning"] = warn
        return out

    @js_safe
    def set_paths(self, dsh_home="", grok_home="", opencode_db=""):
        """手动指定数据源路径。留空 = 恢复自动探测。"""
        st = load_app_settings()
        for k, v in (("dsh_home", dsh_home), ("grok_home", grok_home), ("opencode_db", opencode_db)):
            v = str(v or "").strip()
            if v:
                st[k] = v
            else:
                st.pop(k, None)
        save_app_settings(st)
        # 路径变了，所有缓存都必须失效（在各自锁内清，避免并发扫描清完立刻又填回）
        global GROK_SESSIONS_DIR
        with _LEDGER_LOCK:
            _LEDGER_CACHE.update({"path": "", "at": 0.0, "data": None, "warn": ""})
        with _KEYMAP_LOCK:
            _KEYMAP_CACHE.update({"map": None, "at": 0.0, "err": ""})
        with _GROK_LOCK:
            _GROK_CACHE["at"] = 0.0
        with _GROK_SCAN_LOCK:
            _GROK_SCAN_CACHE.clear()
        with _DSH_SESSION_LOCK:
            _DSH_SESSION_CACHE.update({"at": 0.0, "home": "", "data": None, "warn": ""})
            _DSH_SESSION_CACHE["_files"] = {}
        with _CLINE_CACHE_LOCK:
            _CLINE_USAGE_CACHE.update({"at": 0.0, "uid": "", "time_key": "", "data": None})
            _CLINE_UID_CACHE.clear()
        with _GROK_QUOTA_LOCK:
            _GROK_QUOTA_CACHE.update({"at": 0.0, "data": None})
        GROK_SESSIONS_DIR = _grok_sessions_dir()
        return {"success": True, "paths": self.get_paths()}

    @js_safe
    def detect_paths(self):
        """重新自动探测一次（清掉手动指定）。"""
        return self.set_paths("", "", "")

    def _apply_result(self, key_value, fields):
        """把一次查询结果合并进**最新**的密钥存储（按 key 值定位），返回最新列表。

        绝不能整份回写 self.keys：查询期间用户可能新增/删除了别的条目，
        整份回写会把它们冲掉（实测：刷新中新增的 Key 会从磁盘消失、
        刷新中删除的 Key 会复活）。
        """
        with self._lock:
            cur = load_keys()
            for x in cur:
                if x.get("key") == key_value:
                    x.update(fields)
                    break
            self.keys = cur
            save_keys(cur)
            return cur

    @js_safe
    def query_key(self, index, key_str=""):
        with self._lock:
            self.keys = load_keys()
            target_idx = self._resolve_index(index, key_str)
            if target_idx == -1:
                return {"error": "无效的 Key 索引或未找到该密钥", "keys": self.keys}
            it = self.keys[target_idx]
            key_value = it.get("key")
            ch = key_channel(it)
            try:
                if ch == CHANNEL_STEPFUN:
                    data, err = self._query_stepfun_backend(it)
                elif ch == CHANNEL_COMMANDCODE:
                    data, err = self._query_commandcode_backend(it)
                elif ch == CHANNEL_GROK:
                    data, err = self._query_grok_backend(it)
                elif ch == CHANNEL_CLINE:
                    data, err = self._query_cline_backend(it)
                else:
                    data, err = fetch_usage(it.get("key"))
            except Exception as e:
                import traceback
                log_line("query_key 异常：%s\n%s" % (e, traceback.format_exc()))
                data, err = None, "查询异常：%s" % e

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if err:
                cur = self._apply_result(key_value, {"last_error": str(err), "last_error_at": now_str})
                tgt = next((x for x in cur if x.get("key") == key_value), it)
                return {"error": str(err), "key": tgt, "keys": cur}
            cur = self._apply_result(key_value, {"last_result": data, "last_update": now_str,
                                                 "last_error": None})
            tgt = next((x for x in cur if x.get("key") == key_value), it)
            return {"data": data, "update_time": now_str, "key": tgt,
                    "keys": cur, "index": target_idx}

    def _resolve_index(self, index, key_str=""):
        if key_str:
            for i, k in enumerate(self.keys):
                if k.get("key") == key_str:
                    return i
        try:
            index = int(index)
        except Exception:
            index = -1
        if 0 <= index < len(self.keys):
            return index
        return -1

    def _query_grok_backend(self, it):
        quota = fetch_grok_quota()
        gdata = load_grok_data(force=False)
        daily = gdata["daily"]
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_info = daily.get(today_str, {"tokens": 0, "tokens_with_cache": 0, "cost": 0.0, "calls": 0})
        
        ks = sorted(daily.keys())
        # 真正的「最近 7 个自然日」窗口。旧实现用 ks[-7:]（有数据的最后 7 天），
        # 中间有空洞时会悄悄跨到几个月前。Grok 的日键是本地日期，故 utc=False。
        week_keys = set(_day_keys_for(ks, "近7天", utc=False))
        week_tok = sum(daily[k]["tokens_with_cache"] for k in week_keys)
        week_cost = sum(daily[k]["cost"] for k in week_keys)
        week_calls = sum(daily[k]["calls"] for k in week_keys)

        month_keys = set(_day_keys_for(ks, "本月", utc=False))
        month_tok = sum(daily[k]["tokens_with_cache"] for k in month_keys)
        month_cost = sum(daily[k]["cost"] for k in month_keys)
        month_calls = sum(daily[k]["calls"] for k in month_keys)

        total_tok = sum(daily[k]["tokens_with_cache"] for k in ks)
        total_cost = sum(daily[k]["cost"] for k in ks)
        total_calls = sum(daily[k]["calls"] for k in ks)

        data = {
            "channel": CHANNEL_GROK,
            "sessions_count": gdata["sessions_count"],
            "last_active": gdata["last_active"],
            "quota": quota,
            "today": {
                "tokens": today_info["tokens_with_cache"],
                "cost": round(today_info["cost"], 2),
                "calls": today_info["calls"]
            },
            "week": {
                "tokens": week_tok,
                "cost": round(week_cost, 2),
                "calls": week_calls
            },
            "month": {
                "tokens": month_tok,
                "cost": round(month_cost, 2),
                "calls": month_calls
            },
            "total": {
                "tokens": total_tok,
                "cost": round(total_cost, 2),
                "calls": total_calls
            },
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        # 口径标注：额度来自官方接口（账号级），用量只来自本机会话库。
        # 本机会话库被清理过 / 账号被多设备共用时，两者必然对不上——这里如实标出来。
        sc = int(gdata.get("sessions_count") or 0)
        q_used = None
        if isinstance(quota, dict) and quota.get("success"):
            try:
                q_used = float(quota.get("used_percent"))
            except Exception:
                q_used = None
        data["local_sessions"] = sc
        data["local_span_days"] = len(daily)
        data["archive_sessions"] = int(gdata.get("archive_sessions") or 0)
        data["archive_turns"] = int(gdata.get("archive_turns") or 0)
        data["archive_restored"] = int(gdata.get("archive_restored") or 0)
        data["local_scope_text"] = (
            "额度=官方接口（账号级，含该账号所有设备/成员）；"
            "用量=本机 ~/.grok/sessions 会话库 + 本地档案（只增不减）。"
            "本机会话 %d 个 / %d 天记录；档案累计 %d 个会话 / %d 条记录%s"
            % (sc, len(daily), data["archive_sessions"], data["archive_turns"],
               ("（本次从档案补回 %d 条）" % data["archive_restored"]) if data["archive_restored"] else ""))
        data["local_scope_warning"] = bool(q_used is not None and q_used >= 50 and sc <= 5)
        if data["local_scope_warning"]:
            data["local_scope_text"] += "。⚠️ 本机会话库现存记录过少（可能被清理过），与官方额度不可直接对比"
        return data, None

    def _query_stepfun_backend(self, it):
        key = it.get("key", "")
        acc, acc_err = fetch_stepfun_account(key)
        provs = providers_for_key(key)
        # provs 为空列表 = 该 Key 未在 dsh 配置中使用，数据层会返回 0 + 提示；
        # 绝不能回退成 None（那会变成全部渠道的数据，就是之前第二个 Key 显示错数据的根因）
        est = stepfun_estimate(provs)
        today = stepfun_today_tokens(provs)
        data = {
            "channel": CHANNEL_STEPFUN, "account": acc, "account_error": acc_err,
            "estimate": est, "today": today, "tier": int(it.get("step_tier") or 0),
            "providers": provs, "hint": est.get("hint") or today.get("hint"),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        if acc_err and (est.get("error") or est.get("credit_used_m", 0) <= 0):
            return data, acc_err
        return data, None

    def _query_commandcode_backend(self, it):
        """Command Code：四个账户端点（whoami/usage/credits/subscriptions），
        每个端点独立降级；再附带 dsh 本地账本的今日 tokens。"""
        key = it.get("key", "")
        rep, err = fetch_commandcode_usage(key)
        if rep is None:
            return None, err
        provs = providers_for_key(key)
        today = stepfun_today_tokens(provs)   # 复用本地账本聚合（tokens/calls/hint）
        data = {
            "channel": CHANNEL_COMMANDCODE,
            "account": rep.get("account"), "usage": rep.get("usage"),
            "credits": rep.get("credits"), "plan": rep.get("plan"),
            "failures": rep.get("failures") or [],
            "today": today, "providers": provs,
            "hint": ("；".join(rep.get("failures") or []) if rep.get("failures") else None),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return data, None

    def _query_cline_backend(self, it):
        """Cline Pass：官方订阅 / 额度窗口 / 余额 / 用量。
        用量一律取自 Cline 官方接口（官网口径，含所有客户端），**不使用本地账本**。"""
        key = it.get("key", "")
        rep, err = fetch_cline_account(key)
        if rep is None:
            return None, err
        plan = rep.get("plan") or {}
        uid = (rep.get("account") or {}).get("id") or ""
        usage = {"today": None, "period": None, "models": [], "period_start": "", "period_end": ""}
        if uid:
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            start = str(plan.get("periodStart") or "")[:10] or today_utc
            if start > today_utc:
                start = today_utc
            rows, st = fetch_cline_usage_daily(key, uid, start, today_utc)

            def _agg(rs):
                return {"tokens": sum(r["total"] for r in rs),
                        "cost": round(sum(r["cost"] for r in rs), 4)}

            models = {}
            for r in rows:
                m = models.setdefault(r["model"], {"tokens": 0, "cost": 0.0})
                m["tokens"] += r["total"]
                m["cost"] += r["cost"]
            usage = {
                "today": _agg([r for r in rows if r["date"] == today_utc]),
                "period": _agg(rows),
                "period_start": start,
                "period_end": today_utc,
                "today_date": today_utc,
                "models": [{"model": k, "tokens": v["tokens"], "cost": round(v["cost"], 4)}
                           for k, v in sorted(models.items(), key=lambda kv: -kv[1]["tokens"])],
                "api_status": st,
            }
        data = {
            "channel": CHANNEL_CLINE,
            "account": rep.get("account"), "plan": plan,
            "balance": rep.get("balance"),
            "windows": rep.get("windows") or {},
            "caps": plan.get("caps") or {},
            "usage": usage,
            "failures": rep.get("failures") or [],
            "hint": ("；".join(rep.get("failures") or []) if rep.get("failures") else None),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return data, None

    @js_safe
    def query_all(self, timeout=90):
        """并发刷新所有 Key。

        修正四处缺陷：
          1) 旧实现从 self.keys[idx] 取条目，而其它 API 调用会并发重建 self.keys，
             结果可能被写进「另一个 Key」或被整份丢弃；
          2) fut.result() 没有超时，某个渠道卡住就永远不返回；
          3) 每条失败原因放在 results 里但前端从不读，全部失败也会提示「刷新完成」；
          4) **回写必须按 Key 值合并，不能整份覆盖快照**：刷新耗时可达十几秒，期间
             用户新增的密钥会被旧快照静默删除、删除的密钥会复活（实测高危）。
        现在：查询用快照、结果按 key 值合并回「最新」存储、整体设截止时间、
        失败原因落到 last_error。
        """
        with self._lock:
            snapshot = load_keys()
            self.keys = snapshot
        n = len(snapshot)
        if n == 0:
            return {"results": [], "keys": [], "ok": 0, "failed": 0}

        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
        deadline = time.time() + max(10, _safe_int(timeout, 90))

        def _worker(it):
            ch = key_channel(it)
            if ch == CHANNEL_STEPFUN:
                return self._query_stepfun_backend(it)
            if ch == CHANNEL_COMMANDCODE:
                return self._query_commandcode_backend(it)
            if ch == CHANNEL_GROK:
                return self._query_grok_backend(it)
            if ch == CHANNEL_CLINE:
                return self._query_cline_backend(it)
            return fetch_usage(it.get("key"))

        # 结果先落到「影子」条目上，不直接改 snapshot —— snapshot 可能与用户在
        # 刷新期间新增/删除后的真实存储不一致。
        results = [None] * n
        shadow = [{} for _ in range(n)]
        # 注意：不能用 `with ThreadPoolExecutor(...)` —— 它的 __exit__ 是
        # shutdown(wait=True)，即使提前跳出也照样等到所有 worker 结束，
        # 截止时间形同虚设（实测 10s 截止跑满 25s）。
        executor = ThreadPoolExecutor(max_workers=min(8, n))
        timed_out = set()
        try:
            future_map = {executor.submit(_worker, snapshot[i]): i for i in range(n)}
            try:
                for fut in as_completed(future_map, timeout=max(1.0, deadline - time.time())):
                    i = future_map[fut]
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        data, err = fut.result(timeout=0)
                    except Exception as e:
                        results[i] = {"error": "查询异常：%s" % e, "index": i}
                        shadow[i] = {"last_error": str(e), "last_error_at": now_str}
                        continue
                    if err:
                        results[i] = {"error": str(err), "index": i}
                        shadow[i] = {"last_error": str(err), "last_error_at": now_str}
                    else:
                        results[i] = {"ok": True, "index": i, "update_time": now_str}
                        shadow[i] = {"last_result": data, "last_update": now_str, "last_error": None}
            except FuturesTimeout:
                # 到点了还有 worker 没回来：把它们标成超时并放弃等待
                for fut, i in future_map.items():
                    if results[i] is None:
                        timed_out.add(i)
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        msg = "刷新超时（整体超过 %ds）" % _safe_int(timeout, 90)
                        results[i] = {"error": msg, "index": i}
                        shadow[i] = {"last_error": msg, "last_error_at": now_str}
        finally:
            # 不等待：卡住的 worker 由它自己在网络超时后结束
            executor.shutdown(wait=False, cancel_futures=True)

        ok = sum(1 for r in results if r and r.get("ok"))
        failed = n - ok

        # 合并回最新存储：按 Key 值定位，只更新「此刻仍然存在」的条目。
        # 刷新期间新增的 Key 保留（不在快照里 → 不会被覆盖）；
        # 刷新期间删除的 Key 保持删除（在最新存储里找不到 → 不复活）。
        with self._lock:
            current = load_keys()
            by_key = {}
            for it in current:
                k = it.get("key")
                if k and k not in by_key:
                    by_key[k] = it
            applied = 0
            for i in range(n):
                if not shadow[i]:
                    continue
                tgt = by_key.get(snapshot[i].get("key"))
                if tgt is None:
                    continue          # 刷新期间被删除 —— 尊重删除
                tgt.update(shadow[i])
                applied += 1
            self.keys = current
            save_keys(current)
        return {"results": results, "keys": current, "ok": ok, "failed": failed}

    @js_safe
    @_serialized
    def add_key(self, alias, key, channel, tier):
        self.keys = load_keys()
        alias = alias.strip() if alias else f"Key-{len(self.keys)+1}"
        ch_str = str(channel or "").strip().lower()
        if ch_str in (CHANNEL_GROK, "grok", "grok-build", "grokbuild"):
            ch_code = CHANNEL_GROK
            key = "local-grok-session"
            if not alias or alias.startswith("Key-"):
                alias = "Grok Build"
        elif ch_str in (CHANNEL_COMMANDCODE, "command code", "commandcode", "cc", "command"):
            ch_code = CHANNEL_COMMANDCODE
        elif ch_str in (CHANNEL_CLINE, "cline", "cline pass", "clinepass", "cline-pass"):
            ch_code = CHANNEL_CLINE
        elif ch_str in (CHANNEL_STEPFUN, "阶跃", "阶跃星辰", "stepfun"):
            ch_code = CHANNEL_STEPFUN
        elif ch_str in (CHANNEL_OPENCODE, "go", "opencode", "opencode-go"):
            ch_code = CHANNEL_OPENCODE
        else:
            ch_code = CHANNEL_OPENCODE

        if ch_code == CHANNEL_GROK:
            if any(k.get("channel") == CHANNEL_GROK for k in self.keys):
                return {"error": "已存在 Grok Build 条目，无需重复添加"}
        else:
            key = key.strip()
            if not key:
                return {"error": "请输入 API Key"}
            if any(k.get("key") == key for k in self.keys):
                return {"error": "该 Key 已存在"}

        item = {"alias": alias, "key": key, "channel": ch_code, "last_result": None, "last_update": None}
        if ch_code == CHANNEL_STEPFUN:
            try: item["step_tier"] = int(tier)
            except: item["step_tier"] = DEFAULT_STEP_TIER
        
        self.keys.append(item)
        save_keys(self.keys)
        return {"success": True, "keys": self.keys, "new_index": len(self.keys)-1}

    @js_safe
    @_serialized
    def edit_key(self, index, alias, key, channel, tier, key_str=""):
        self.keys = load_keys()
        target_idx = self._resolve_index(index, key_str)
        if not (0 <= target_idx < len(self.keys)):
            return {"error": "无效的 Key 索引", "keys": self.keys}
        alias = alias.strip() if alias else f"Key-{target_idx+1}"
        
        ch_str = str(channel or "").strip().lower()
        if ch_str in (CHANNEL_GROK, "grok", "grok-build", "grokbuild"):
            ch_code = CHANNEL_GROK
            key = "local-grok-session"
        elif ch_str in (CHANNEL_COMMANDCODE, "command code", "commandcode", "cc", "command"):
            ch_code = CHANNEL_COMMANDCODE
        elif ch_str in (CHANNEL_CLINE, "cline", "cline pass", "clinepass", "cline-pass"):
            ch_code = CHANNEL_CLINE
        elif ch_str in (CHANNEL_STEPFUN, "阶跃", "阶跃星辰", "stepfun"):
            ch_code = CHANNEL_STEPFUN
        elif ch_str in (CHANNEL_OPENCODE, "go", "opencode", "opencode-go"):
            ch_code = CHANNEL_OPENCODE
        else:
            ch_code = CHANNEL_OPENCODE

        if ch_code != CHANNEL_GROK:
            key = key.strip()
            if not key:
                return {"error": "请输入 API Key", "keys": self.keys}
            if any(i != target_idx and k.get("key") == key for i, k in enumerate(self.keys)):
                return {"error": "该 Key 已在其他条目中存在", "keys": self.keys}
            
        it = self.keys[target_idx]
        old_key = it.get("key")
        it["alias"] = alias
        it["key"] = key
        it["channel"] = ch_code
        if ch_code == CHANNEL_STEPFUN:
            try: it["step_tier"] = int(tier)
            except: it["step_tier"] = DEFAULT_STEP_TIER
        elif "step_tier" in it and ch_code != CHANNEL_STEPFUN:
            it.pop("step_tier", None)
            
        if old_key != key:
            it["last_result"] = None
            it["last_update"] = None
            
        save_keys(self.keys)
        return {"success": True, "keys": self.keys, "index": target_idx}

    @js_safe
    @_serialized
    def delete_key(self, index, key_str=""):
        self.keys = load_keys()
        target_idx = self._resolve_index(index, key_str)
        if 0 <= target_idx < len(self.keys):
            del self.keys[target_idx]
            save_keys(self.keys)
            return {"success": True, "keys": self.keys}
        return {"error": "索引错误或未找到该密钥", "keys": self.keys}

    @js_safe
    @_serialized
    def save_tier(self, index, tier, key_str=""):
        self.keys = load_keys()
        target_idx = self._resolve_index(index, key_str)
        if 0 <= target_idx < len(self.keys):
            self.keys[target_idx]["step_tier"] = _safe_int(tier)
            save_keys(self.keys)
            return {"success": True, "keys": self.keys}
        return {"error": "索引错误", "keys": self.keys}

    @js_safe
    @_serialized
    def move_key(self, index, direction, key_str=""):
        self.keys = load_keys()
        n = len(self.keys)
        target_idx = self._resolve_index(index, key_str)
        if not (0 <= target_idx < n):
            return {"error": "无效的索引", "keys": self.keys}
        ch = key_channel(self.keys[target_idx])
        # 侧栏是按渠道分组渲染的，「上移/下移」必须在**同渠道内部**交换；
        # 旧实现交换数组相邻位，跨渠道相邻时会提示「已上移」却看不出任何变化。
        same = [i for i, k in enumerate(self.keys) if key_channel(k) == ch]
        try:
            pos = same.index(target_idx)
        except ValueError:
            pos = 0
        new_index = target_idx
        if direction == "up" and pos > 0:
            j = same[pos - 1]
            self.keys[target_idx], self.keys[j] = self.keys[j], self.keys[target_idx]
            new_index = j
        elif direction == "down" and pos < len(same) - 1:
            j = same[pos + 1]
            self.keys[target_idx], self.keys[j] = self.keys[j], self.keys[target_idx]
            new_index = j
        elif direction == "top" and pos > 0:
            it = self.keys.pop(target_idx)
            insert_at = same[0]
            self.keys.insert(insert_at, it)
            new_index = insert_at
        save_keys(self.keys)
        return {"success": True, "keys": self.keys, "new_index": new_index}

    @js_safe
    def export_keys(self):
        self.keys = load_keys()
        return {"success": True, "json_str": json.dumps(_json_safe(self.keys), ensure_ascii=False, indent=2)}

    @js_safe
    @_serialized
    def import_keys(self, json_str):
        try:
            data = _safe_json_loads(json_str)
            if not isinstance(data, list):
                if isinstance(data, dict) and "keys" in data and isinstance(data["keys"], list):
                    data = data["keys"]
                else:
                    return {"error": "备份格式不正确，应为密钥列表"}
            valid_keys = []
            for it in data:
                n = norm_key_item(it)
                if n and n.get("key"):
                    valid_keys.append(n)
            if not valid_keys:
                return {"error": "备份中未发现有效密钥条目"}
            self.keys = valid_keys
            save_keys(self.keys)
            return {"success": True, "keys": self.keys, "count": len(self.keys)}
        except Exception as e:
            return {"error": f"解析备份失败: {e}"}

    @js_safe
    def get_token_stats(self, time_key="全部", source="dsh 账本", only_key=False, key_index=0, key_str=""):
        self.keys = load_keys()
        target_idx = self._resolve_index(key_index, key_str)
        if target_idx == -1:
            target_idx = _safe_int(key_index)
        
        # 1. 勾选“仅当前 Key”：精准按当前选中的条目过滤
        if only_key:
            if 0 <= target_idx < len(self.keys):
                it = self.keys[target_idx]
                if key_channel(it) == CHANNEL_GROK:
                    try:
                        return grok_usage(time_key=time_key, force=False)
                    except Exception as e:
                        return {"per_model": [], "totals": None, "daily_series": [],
                                "error": f"读取 Grok 会话库失败: {e}",
                                "source": "grok", "db": str(GROK_SESSIONS_DIR), "days": 0}
                if key_channel(it) == CHANNEL_CLINE:
                    # Cline 用量走官方接口（官网口径，含所有客户端），不读本地账本
                    ckey = it.get("key", "")
                    try:
                        cuid = cline_user_id(ckey)
                        if cuid:
                            return cline_usage_official(ckey, cuid, time_key=time_key)
                        return {"per_model": [], "totals": None, "daily_series": [],
                                "error": "无法获取 Cline 用户 id（Key 可能无效）",
                                "source": "cline-official", "db": "api.cline.bot", "days": 0}
                    except Exception as e:
                        return {"per_model": [], "totals": None, "daily_series": [],
                                "error": f"查询 Cline 官方接口失败: {e}",
                                "source": "cline-official", "db": "api.cline.bot", "days": 0}

                use_dsh = source.startswith("dsh")
                providers = providers_for_key(it.get("key", ""))
                if use_dsh:
                    data = dsh_usage(providers, time_key)
                    if not providers and not data.get("hint"):
                        km_err = keymap_error()
                        if km_err:
                            data["hint"] = "无法解析 dsh 凭据，未能把该 Key 对应到 provider：%s" % km_err
                        elif not _dsh_credentials_path().exists():
                            data["hint"] = ("未找到 dsh 凭据文件 %s，无法把该 Key 对应到 provider。"
                                            "若你的 dsh 数据目录不在默认位置，请在「设置 → 数据源路径」里指定。"
                                            % _dsh_credentials_path())
                        else:
                            data["hint"] = "该 Key 未在 dsh 配置中使用，统计结果为 0"
                    return data
                else:
                    return query_token_stats(time_key)
            else:
                # 「仅当前 Key」但没有可选的 Key。
                # 一个 Key 都没有时退化成全渠道汇总 —— 否则新用户（还没添加任何
                # 密钥，但本机已经有 dsh 数据）永远看到 0，会以为工具坏了。
                if not self.keys:
                    allview = dsh_usage(None, time_key) if source.startswith("dsh") else query_token_stats(time_key)
                    allview["hint"] = ("还没有添加任何密钥，当前显示的是本机全部渠道用量。"
                                       "点左上角「+」添加密钥后可只看单个 Key。")
                    return allview
                return dsh_usage([], time_key) if source.startswith("dsh") else query_token_stats(time_key)

        # 2. 未勾选“仅当前 Key”：全渠道总览（深度合并 dsh 账本与 Grok Build 本地会话）
        use_dsh = source.startswith("dsh")
        if use_dsh:
            dsh_data = dsh_usage(None, time_key)
        else:
            dsh_data = query_token_stats(time_key)

        has_grok = any(key_channel(k) == CHANNEL_GROK for k in self.keys) or (GROK_SESSIONS_DIR.exists() if 'GROK_SESSIONS_DIR' in globals() else False)
        if has_grok:
            try:
                grok_data = grok_usage(time_key=time_key, force=False)
                if grok_data and (grok_data.get("totals", {}).get("tokens_with_cache", 0) > 0 or grok_data.get("per_model")):
                    return merge_usage(dsh_data, grok_data)
            except Exception as e:
                # 不再静默：Grok 合并失败时明确告诉用户「这里只有 dsh 的数据」
                import traceback
                log_line("Grok 合并失败：%s\n%s" % (e, traceback.format_exc()))
                dsh_data["hint"] = ((str(dsh_data.get("hint")) + "；") if dsh_data.get("hint") else "") + \
                    "Grok 会话库读取失败，以上只含 dsh 账本数据：%s" % e

        return dsh_data

    @js_safe
    def open_storage(self):
        try:
            if os.name == "nt": os.startfile(str(STORAGE_DIR))
            return {"success": True, "path": str(STORAGE_DIR)}
        except Exception as e:
            return {"error": str(e)}

    @js_safe
    def open_db_dir(self):
        try:
            p = get_opencode_db_path()
            if p.exists() and os.name == "nt":
                os.startfile(str(p.parent))
                return {"success": True, "path": str(p.parent)}
            return {"error": "未找到 opencode 本地库：%s" % p}
        except Exception as e:
            return {"error": str(e)}


def set_win32_window_icon(icon_path, title):
    """给窗口换上任务栏图标（pywebview 的 icon 参数在 Windows 上不生效）。"""
    if os.name != "nt":
        return
    h_small = h_big = 0
    try:
        for _ in range(100):          # 最多等 8 秒，冷启动首次拉起 WebView2 会更慢
            time.sleep(0.08)
            try:
                hwnd = ctypes.windll.user32.FindWindowW(None, title)
            except Exception:
                return
            if not hwnd:
                continue
            try:
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x00000010
                h_small = ctypes.windll.user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                h_big = ctypes.windll.user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
                if h_small:
                    ctypes.windll.user32.SendMessageW(hwnd, 128, 0, h_small)  # WM_SETICON, ICON_SMALL
                if h_big:
                    ctypes.windll.user32.SendMessageW(hwnd, 128, 1, h_big)    # WM_SETICON, ICON_BIG
            except Exception:
                pass
            break
    except Exception:
        pass
    finally:
        # 句柄用完要释放，否则每次启动都漏两个 GDI 对象
        try:
            if h_small: ctypes.windll.user32.DestroyIcon(h_small)
            if h_big: ctypes.windll.user32.DestroyIcon(h_big)
        except Exception:
            pass


def _fatal(msg):
    """EXE 是 console=False，出错必须弹原生对话框，否则用户只看到「双击没反应」。"""
    log_line("FATAL: " + str(msg))
    try:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, str(msg), APP_TITLE + " - 启动失败", 0x10)
    except Exception:
        pass


def main():
    window_title = APP_TITLE

    # Set Windows Process AppUserModelID so taskbar displays the app icon
    try:
        myappid = "OpenCode.CodingPlan.Checker.v3"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    api = DesktopAPI()
    initial_state = api.get_initial_state()
    # 关键：注入到 <script> 里的 JSON 必须转义 "<"，否则别名里出现 </script>
    # 会提前结束脚本块，整页 JS 失效。json.dumps 默认不转义 "<"。
    initial_json = (json.dumps(initial_state, ensure_ascii=False)
                    .replace("<", "\\u003c").replace(">", "\\u003e")
                    .replace("&", "\\u0026")
                    .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))

    tailwind_code = get_asset_text("tailwindcss.min.js")
    if not tailwind_code:
        log_line("警告：未找到 tailwindcss.min.js，界面将缺少样式")

    rendered_html = HTML_TEMPLATE.replace("__INITIAL_STATE_JSON__", initial_json)
    rendered_html = rendered_html.replace("__TAILWIND_SCRIPT_INLINE__", tailwind_code)

    icon_path = get_asset_path("app.ico")
    window = webview.create_window(
        title=window_title,
        html=rendered_html,
        js_api=api,
        width=1120,
        height=740,
        min_size=(1040, 680),
        background_color="#f1f5f9"
    )
    api._window = window

    # Launch icon setter thread for WM_SETICON
    if icon_path and icon_path.exists():
        t = threading.Thread(target=set_win32_window_icon, args=(icon_path, window_title), daemon=True)
        t.start()

    log_line("启动完成：storage=%s dsh_home=%s grok=%s" % (STORAGE_DIR, dsh_home(), grok_home()))
    webview.start(
        gui="edgechromium",
        debug=False,
        icon=str(icon_path) if icon_path and icon_path.exists() else None
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        log_line(tb)
        msg = ("程序启动失败：\n\n%s\n\n"
               "常见原因：\n"
               "· 缺少 WebView2 运行时（Win11 自带；Win10 需安装 Microsoft Edge WebView2 Runtime）\n"
               "· 缺少 Python 依赖（源码运行时执行：pip install -r requirements.txt）\n\n"
               "完整日志：%s" % (tb.strip().splitlines()[-1], LOG_FILE))
        _fatal(msg)
