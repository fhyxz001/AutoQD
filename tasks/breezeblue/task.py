import json
import os
from datetime import datetime, timedelta, timezone

import requests

API_URL = "https://breezeblue.ai/api/credits/daily-claim"
CLAIM_INTERVAL = timedelta(hours=24)  # 站点要求两次领取间隔 24 小时以上
SAFETY_MARGIN = timedelta(minutes=5)  # 额外余量, 避免服务端计时误差导致过早领取
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, 与平台记录时间一致
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.abspath(os.path.join(_HERE, "..", "..", "data")))
RECORDS_PATH = os.path.join(DATA_DIR, "breezeblue_records.json")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def last_success_time():
    """从平台签到记录中读取最近一次成功领取的时间"""
    try:
        with open(RECORDS_PATH, "r", encoding="utf-8") as file:
            records = json.load(file)
    except (OSError, ValueError):
        return None
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict) or not record.get("ok"):
            continue
        try:
            naive = datetime.strptime(str(record.get("time", "")), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        return naive.replace(tzinfo=TZ)
    return None


def run(config):
    """执行一次每日领取尝试, 返回 {"ok": bool, "detail": str}
    未满足 24 小时间隔时返回 {"skip": True, "retry_after": 秒数}, 由平台推迟执行"""
    settings = config.get("settings", {})
    cookie = (settings.get("cookie") or "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    if not cookie:
        return {"ok": False, "detail": "未配置 Cookie, 请在平台编辑任务设置"}

    last_success = last_success_time()
    if last_success is not None:
        earliest = last_success + CLAIM_INTERVAL + SAFETY_MARGIN
        remaining = (earliest - datetime.now(TZ)).total_seconds()
        if remaining > 0:
            return {
                "ok": False,
                "skip": True,
                "retry_after": int(remaining) + 1,
                "detail": f"距上次成功不足 24 小时, 最早 {earliest:%Y-%m-%d %H:%M} 才可再次领取",
            }

    user_agent = (settings.get("user_agent") or "").strip() or DEFAULT_UA

    headers = {
        "Cookie": cookie,
        "User-Agent": user_agent,
        "Origin": "https://breezeblue.ai",
        "Referer": "https://breezeblue.ai/zh/app/home",
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        response = requests.post(API_URL, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "detail": f"请求异常: {exc}"}

    try:
        payload = response.json()
        detail = json.dumps(payload, ensure_ascii=False)
    except ValueError:
        payload = None
        detail = response.text[:500]

    ok = response.status_code == 200 and not (
        isinstance(payload, dict) and payload.get("ok") is False
    )
    return {"ok": ok, "detail": detail[:500]}
