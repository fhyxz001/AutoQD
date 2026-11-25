"""AutoQD 签到管理平台: 调度 tasks/ 下所有签到任务并提供 Web 管理界面."""
import html
import importlib.util
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.getenv("PORT", "8080"))
_HERE = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.getenv("TASKS_DIR", os.path.abspath(os.path.join(_HERE, "..", "tasks")))
DATA_DIR = os.getenv("DATA_DIR", os.path.abspath(os.path.join(_HERE, "..", "data")))
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
MAX_RECORDS = 200
DEFAULT_CONFIG = {
    "enabled": True,
    "run_at": "08:30",
    "max_retries": 3,
    "retry_interval": 600,
    "settings": {},
}


def log(message):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def now():
    return datetime.now(TZ)


def atomic_write_json(path, data):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Task:
    def __init__(self, name, module):
        self.name = name
        self.module = module
        self.config_path = os.path.join(TASKS_DIR, name, "config.json")
        self.records_path = os.path.join(DATA_DIR, f"{name}_records.json")
        self.lock = threading.RLock()
        self.event = threading.Event()
        self.actions = []
        self.config = self.load_config()
        self.records = self.load_records()
        self.next_run = "未启动"
        self.running = False

    # ---------- 配置 ----------
    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                cfg = dict(DEFAULT_CONFIG)
                cfg.update({k: data[k] for k in DEFAULT_CONFIG if k in data})
                if not isinstance(cfg["settings"], dict):
                    cfg["settings"] = {}
                return cfg
        except (OSError, ValueError):
            pass
        return dict(DEFAULT_CONFIG)

    def save_config(self, cfg):
        with self.lock:
            self.config = cfg
            atomic_write_json(self.config_path, cfg)

    def get_config(self):
        with self.lock:
            return json.loads(json.dumps(self.config))  # 深拷贝快照

    # ---------- 动作 (run / config) ----------
    def push_action(self, action):
        with self.lock:
            self.actions.append(action)
        self.event.set()

    def take_action(self):
        with self.lock:
            return self.actions.pop(0) if self.actions else None

    # ---------- 记录 ----------
    def load_records(self):
        try:
            with open(self.records_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def add_record(self, result, attempt, manual):
        entry = {
            "time": f"{now():%Y-%m-%d %H:%M:%S}",
            "ok": bool(result.get("ok")),
            "detail": str(result.get("detail", ""))[:500],
            "attempt": attempt,
            "manual": bool(manual),
        }
        with self.lock:
            self.records.insert(0, entry)
            del self.records[MAX_RECORDS:]
            try:
                atomic_write_json(self.records_path, self.records)
            except OSError as exc:
                log(f"[{self.name}] 保存记录失败: {exc}")
        log(f"[{self.name}] {'成功' if entry['ok'] else '失败'}: {entry['detail']}")

    def snapshot_records(self, limit):
        with self.lock:
            return self.records[:limit]

    def success_today(self):
        today = f"{now():%Y-%m-%d}"
        with self.lock:
            return any(r.get("ok") and str(r.get("time", "")).startswith(today)
                       for r in self.records)


# ---------- 调度 ----------
def next_run_at(run_at):
    hour, minute = (int(part) for part in str(run_at).split(":"))
    target = now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now():
        target += timedelta(days=1)
    return target


def wait_with_interrupt(task, seconds):
    """等待指定秒数, 期间有新的运行/配置操作则提前返回 False"""
    for _ in range(seconds):
        if task.event.is_set():
            return False
        time.sleep(1)
    return True


def run_with_retries(task, cfg, manual=False):
    max_retries = int(cfg.get("max_retries", 3))
    interval = max(int(cfg.get("retry_interval", 600)), 1)
    attempt = 0
    label = "手动" if manual else "定时"
    while True:
        attempt += 1
        log(f"[{task.name}] {label}第 {attempt} 次尝试")
        task.running = True
        try:
            result = task.module.run(cfg)
        except Exception as exc:  # 任务代码异常不应影响调度
            result = {"ok": False, "detail": f"任务异常: {exc}"}
        task.running = False
        if not isinstance(result, dict) or ("ok" not in result and not result.get("skip")):
            result = {"ok": False, "detail": f"任务返回无效: {result!r}"}
        if result.get("skip"):
            # 任务要求推迟执行(如站点限制两次签到间隔), 不计入重试次数
            detail = str(result.get("detail", "任务跳过"))
            try:
                wait = int(result.get("retry_after", interval))
            except (TypeError, ValueError):
                wait = interval
            wait = min(max(wait, 30), 86400)
            if manual:
                task.add_record({"ok": False, "detail": f"跳过: {detail}"}, attempt, manual)
                break
            attempt -= 1
            log(f"[{task.name}] 跳过: {detail}, {wait} 秒后重试")
            task.next_run = f"等待中, {now() + timedelta(seconds=wait):%Y-%m-%d %H:%M:%S} 重试"
            if not wait_with_interrupt(task, wait):
                log(f"[{task.name}] 跳过等待被中断")
                break
            continue
        task.add_record(result, attempt, manual)
        if result.get("ok"):
            break
        detail = str(result.get("detail", ""))
        if detail.startswith("未配置"):
            log(f"[{task.name}] 配置缺失, 不重试")
            break
        if attempt > max_retries:
            log(f"[{task.name}] 已达最大重试次数({max_retries}), 本轮放弃")
            break
        task.next_run = f"重试等待中, {interval} 秒后第 {attempt + 1} 次尝试"
        if wait_with_interrupt(task, interval):  # 新操作到来会中断重试等待
            continue
        log(f"[{task.name}] 重试等待被中断")
        break


def task_worker(task):
    log(f"[{task.name}] 任务线程启动")
    first = True
    while True:
        cfg = task.get_config()
        if first:
            first = False
            if cfg.get("enabled", True):
                if task.success_today():
                    log(f"[{task.name}] 启动检查: 今日已签到, 等待下次定时")
                else:
                    log(f"[{task.name}] 启动检查: 今日未签到, 立即执行")
                    run_with_retries(task, cfg)
            continue

        # 计算目标时间并等待
        if cfg.get("enabled", True):
            target = next_run_at(cfg.get("run_at", "08:30"))
            task.next_run = f"{target:%Y-%m-%d %H:%M:%S}"
        else:
            target = None
            task.next_run = "已禁用"

        run_now = False
        while True:
            if target is not None:
                remaining = (target - now()).total_seconds()
                if remaining <= 0:
                    break
                fired = task.event.wait(min(remaining, 30))
            else:
                fired = task.event.wait()  # 禁用时只等操作
            if fired:
                task.event.clear()
                action = task.take_action()
                if action == "run":
                    run_now = True
                    break
                if action == "config":
                    break  # 重新计算计划
        if run_now:
            run_with_retries(task, cfg, manual=True)
            continue
        if not cfg.get("enabled", True):
            continue
        if task.success_today():
            log(f"[{task.name}] 到达定时但今日已签到, 跳过")
        else:
            run_with_retries(task, cfg)


def discover_tasks():
    tasks = {}
    if not os.path.isdir(TASKS_DIR):
        return tasks
    for name in sorted(os.listdir(TASKS_DIR)):
        task_file = os.path.join(TASKS_DIR, name, "task.py")
        if not os.path.isfile(task_file):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"autoqd_task_{name}", task_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            tasks[name] = Task(name, module)
        except Exception as exc:
            log(f"加载任务 {name} 失败: {exc}")
    return tasks


# ---------- Web ----------
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoQD 签到管理平台</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 16px;
    background: #f4f6fb; color: #1c2434;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
      "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 920px; margin: 0 auto; }
  h1 { margin: 0 0 6px; font-size: 26px; letter-spacing: -.02em; }
  .sub { margin: 0 0 24px; color: #6b7485; font-size: 14px; }
  .task {
    background: #fff; border: 1px solid #e6eaf2; border-radius: 16px;
    padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(16,24,40,.04);
  }
  .task-head { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  .task-head h2 { margin: 0; font-size: 17px; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .badge.ok { background: #e6f6ec; color: #067647; }
  .badge.fail { background: #feeceb; color: #b42318; }
  .badge.muted { background: #eef1f6; color: #667085; }
  .meta { color: #6b7485; font-size: 13px; margin: 4px 0 14px; line-height: 1.7; }
  .btn {
    border: 1px solid #d0d7e2; background: #fff; color: #1c2434;
    border-radius: 8px; padding: 7px 16px; font-size: 13px; cursor: pointer;
  }
  .btn:hover { background: #f4f6fb; }
  .btn.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
  .btn.primary:hover { background: #1d4ed8; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 9px 10px; text-align: left; border-bottom: 1px solid #f1f4f9; }
  th { color: #7a8394; font-weight: 500; background: #fafbfd; white-space: nowrap; }
  td.detail { color: #5b6474; word-break: break-all; }
  tr:last-child td { border-bottom: 0; }
  .empty { text-align: center; color: #98a2b3; padding: 24px 0; }
  .overlay {
    display: none; position: fixed; inset: 0; background: rgba(16,24,40,.4);
    align-items: center; justify-content: center; z-index: 10; padding: 16px;
  }
  .overlay.show { display: flex; }
  .modal {
    background: #fff; border-radius: 16px; padding: 24px; width: 100%; max-width: 460px;
    max-height: 90vh; overflow-y: auto;
  }
  .modal h3 { margin: 0 0 16px; font-size: 16px; }
  .field { margin-bottom: 12px; }
  .field label { display: block; font-size: 13px; color: #475467; margin-bottom: 5px; }
  .field input, .field select {
    width: 100%; border: 1px solid #d0d7e2; border-radius: 8px;
    padding: 8px 10px; font-size: 14px; font-family: inherit;
  }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .check { display: flex; align-items: center; gap: 8px; font-size: 14px; margin-bottom: 14px; }
  .modal-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
  .hint { color: #98a2b3; font-size: 12px; margin-top: 4px; }
  .toast {
    position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%);
    background: #1c2434; color: #fff; border-radius: 8px; padding: 10px 18px;
    font-size: 13px; opacity: 0; pointer-events: none; transition: opacity .3s;
  }
  .toast.show { opacity: .95; }
  @media (max-width: 720px) { body { padding: 20px 10px; } }
</style>
</head>
<body>
<div class="wrap">
  <h1>AutoQD 签到管理平台</h1>
  <p class="sub">统一管理签到任务 · 当前时间 <span id="now">__NOW__</span> · 页面每 10 秒自动刷新 · tasks/ 新增任务目录自动接管</p>
  <div id="tasks">加载中...</div>
</div>

<div class="overlay" id="overlay">
  <div class="modal">
    <h3>编辑任务: <span id="mTitle"></span></h3>
    <div class="check"><input type="checkbox" id="mEnabled"><label for="mEnabled">启用自动签到</label></div>
    <div class="row2">
      <div class="field"><label>每日签到时刻</label><input type="time" id="mRunAt"></div>
      <div class="field"><label>失败重试次数</label><input type="number" id="mRetries" min="0" max="20"></div>
    </div>
    <div class="field"><label>重试间隔（秒）</label><input type="number" id="mInterval" min="30" max="86400"></div>
    <div id="mSettings"></div>
    <p class="hint">任务设置字段由任务脚本定义, 如账号密码 / Cookie 等</p>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="saveConfig()">保存</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let state = null;
let editName = null;

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function refresh() {
  try {
    state = await (await fetch('/api/state')).json();
    document.getElementById('now').textContent = state.now;
    render();
  } catch (e) { /* 忽略瞬时错误 */ }
}

function render() {
  const box = document.getElementById('tasks');
  if (!state.tasks.length) { box.innerHTML = '<div class="task empty">tasks 目录下没有任务, 放入 &lt;任务名&gt;/task.py 后会自动发现</div>'; return; }
  box.innerHTML = state.tasks.map(t => {
    const last = t.records[0];
    const lastBadge = last
      ? `<span class="badge ${last.ok ? 'ok' : 'fail'}">${last.ok ? '最近成功' : '最近失败'}</span> ${esc(last.time)}`
      : '<span class="badge muted">暂无记录</span>';
    const rows = t.records.slice(0, 10).map(r =>
      `<tr><td style="white-space:nowrap">${esc(r.time)}</td>` +
      `<td><span class="badge ${r.ok ? 'ok' : 'fail'}">${r.ok ? '成功' : '失败'}</span></td>` +
      `<td class="detail">${esc(r.detail)}</td></tr>`).join('');
    return `<div class="task">
      <div class="task-head">
        <h2>${esc(t.name)}</h2>
        ${t.enabled ? '<span class="badge ok">启用中</span>' : '<span class="badge muted">已禁用</span>'}
        ${t.running ? '<span class="badge fail">运行中</span>' : ''}
        ${t.success_today ? '<span class="badge ok">今日已完成</span>' : ''}
      </div>
      <div class="meta">
        下次运行: ${esc(t.next_run)}<br>
        每天 ${esc(t.run_at)} 执行, 失败重试 ${t.max_retries} 次 / 间隔 ${t.retry_interval} 秒<br>
        最近: ${lastBadge}
      </div>
      <div style="margin-bottom:14px; display:flex; gap:10px;">
        <button class="btn primary" onclick="runNow('${esc(t.name)}')" ${t.running ? 'disabled' : ''}>立即运行</button>
        <button class="btn" onclick="openModal('${esc(t.name)}')">编辑设置</button>
      </div>
      <table>
        <thead><tr><th>时间</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="3" class="empty">暂无签到记录</td></tr>'}</tbody>
      </table>
    </div>`;
  }).join('');
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

async function runNow(name) {
  const resp = await fetch('/api/run/' + encodeURIComponent(name), {method: 'POST'});
  const data = await resp.json();
  toast(data.message || '已触发');
  setTimeout(refresh, 800);
}

function openModal(name) {
  const t = state.tasks.find(x => x.name === name);
  if (!t) return;
  editName = name;
  document.getElementById('mTitle').textContent = name;
  document.getElementById('mEnabled').checked = t.enabled;
  document.getElementById('mRunAt').value = t.run_at;
  document.getElementById('mRetries').value = t.max_retries;
  document.getElementById('mInterval').value = t.retry_interval;
  const box = document.getElementById('mSettings');
  const keys = Object.keys(t.settings || {});
  box.innerHTML = keys.map(k => {
    const isSecret = /password|cookie|token/i.test(k);
    return `<div class="field"><label>${esc(k)}</label>` +
      `<input type="${isSecret ? 'password' : 'text'}" data-setting="${esc(k)}" value="${esc(t.settings[k])}"></div>`;
  }).join('') || '<p class="hint">此任务没有可配置项</p>';
  document.getElementById('overlay').classList.add('show');
}

function closeModal() { document.getElementById('overlay').classList.remove('show'); }

async function saveConfig() {
  const body = {
    enabled: document.getElementById('mEnabled').checked,
    run_at: document.getElementById('mRunAt').value,
    max_retries: parseInt(document.getElementById('mRetries').value, 10),
    retry_interval: parseInt(document.getElementById('mInterval').value, 10),
    settings: {}
  };
  document.querySelectorAll('[data-setting]').forEach(el => {
    body.settings[el.dataset.setting] = el.value;
  });
  const resp = await fetch('/api/config/' + encodeURIComponent(editName), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await resp.json();
  if (resp.ok) { toast('已保存并生效'); closeModal(); refresh(); }
  else toast(data.message || '保存失败');
}

document.getElementById('overlay').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeModal();
});
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE_TEMPLATE.replace("__NOW__", f"{now():%Y-%m-%d %H:%M:%S}").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._send_json(200, build_state())
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        run_match = re.fullmatch(r"/api/run/([^/]+)", path)
        cfg_match = re.fullmatch(r"/api/config/([^/]+)", path)
        if run_match:
            task = TASKS.get(run_match.group(1))
            if not task:
                self._send_json(404, {"ok": False, "message": "任务不存在"})
                return
            task.push_action("run")
            self._send_json(200, {"ok": True, "message": "已触发, 请稍后刷新查看结果"})
        elif cfg_match:
            task = TASKS.get(cfg_match.group(1))
            if not task:
                self._send_json(404, {"ok": False, "message": "任务不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                self._send_json(400, {"ok": False, "message": "请求体格式错误"})
                return
            ok, message = update_config(task, body)
            self._send_json(200 if ok else 400, {"ok": ok, "message": message})
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # 屏蔽访问日志


def build_state():
    data = {"now": f"{now():%Y-%m-%d %H:%M:%S}", "tasks": []}
    with TASKS_LOCK:  # 后台扫描会新增任务, 取快照避免遍历时字典被改动
        snapshot = sorted(TASKS.values(), key=lambda t: t.name)
    for task in snapshot:
        cfg = task.get_config()
        data["tasks"].append({
            "name": task.name,
            "enabled": bool(cfg.get("enabled", True)),
            "run_at": cfg.get("run_at", "08:30"),
            "max_retries": cfg.get("max_retries", 3),
            "retry_interval": cfg.get("retry_interval", 600),
            "settings": cfg.get("settings", {}),
            "next_run": task.next_run,
            "running": task.running,
            "success_today": task.success_today(),
            "records": task.snapshot_records(20),
        })
    return data


def update_config(task, body):
    cfg = task.get_config()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "run_at" in body:
        value = str(body["run_at"]).strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", value):
            return False, "签到时刻格式应为 HH:MM"
        hour, minute = (int(p) for p in value.split(":"))
        if hour > 23 or minute > 59:
            return False, "签到时刻超出范围"
        cfg["run_at"] = f"{hour:02d}:{minute:02d}"
    if "max_retries" in body:
        try:
            retries = int(body["max_retries"])
        except (TypeError, ValueError):
            return False, "重试次数必须是数字"
        if not 0 <= retries <= 20:
            return False, "重试次数应在 0-20 之间"
        cfg["max_retries"] = retries
    if "retry_interval" in body:
        try:
            interval = int(body["retry_interval"])
        except (TypeError, ValueError):
            return False, "重试间隔必须是数字"
        if not 30 <= interval <= 86400:
            return False, "重试间隔应在 30-86400 秒之间"
        cfg["retry_interval"] = interval
    if "settings" in body:
        if not isinstance(body["settings"], dict):
            return False, "settings 格式错误"
        merged = dict(cfg.get("settings", {}))
        for key, value in body["settings"].items():
            merged[str(key)] = str(value)
        cfg["settings"] = merged
    task.save_config(cfg)
    task.push_action("config")
    log(f"[{task.name}] 配置已更新: 每天 {cfg['run_at']}, "
        f"重试 {cfg['max_retries']} 次 / {cfg['retry_interval']} 秒, "
        f"{'启用' if cfg['enabled'] else '禁用'}")
    return True, "已保存"


TASKS = {}
TASKS_LOCK = threading.RLock()
RESCAN_INTERVAL = max(int(os.getenv("RESCAN_INTERVAL", "30")), 5)  # 秒, 定期发现 tasks/ 下新增任务


def register_new_tasks():
    """扫描 tasks/ 目录, 为尚未加载的任务建线程, 返回新增任务名列表"""
    discovered = discover_tasks()
    added = []
    with TASKS_LOCK:
        for name, task in discovered.items():
            if name not in TASKS:
                TASKS[name] = task
                added.append(task)
    for task in added:
        log(f"加载任务: {task.name}")
        threading.Thread(target=task_worker, args=(task,), daemon=True).start()
    return [task.name for task in added]


def rescan_worker(interval):
    """新增任务目录后无需重启, 后台定期扫描并自动接管"""
    while True:
        time.sleep(interval)
        try:
            added = register_new_tasks()
        except Exception as exc:
            log(f"扫描新任务失败: {exc}")
            continue
        if added:
            log(f"新任务已接管: {', '.join(added)}")


def main():
    register_new_tasks()
    threading.Thread(target=rescan_worker, args=(RESCAN_INTERVAL,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"平台启动, 管理页面: http://0.0.0.0:{PORT} (每 {RESCAN_INTERVAL} 秒扫描新任务)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
