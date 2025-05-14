import discord
import datetime
import time
import uuid
from flask import Flask
from threading import Thread
import requests
import pytz
import os
from dotenv import load_dotenv

# タイムゾーン設定
JST = pytz.timezone('Asia/Tokyo')

load_dotenv()

# 環境変数
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
SLACK_CHANNEL_ID = os.getenv('SLACK_CHANNEL_ID')
DAILY_REPORT_CHANNEL_ID = os.getenv('DAILY_REPORT_CHANNEL_ID')

# 状態管理用変数
last_sheet_events = {}      # ユーザーごとの最終送信時刻
clock_in_times = {}         # ユーザーの出勤時刻
rest_start_times = {}       # 休憩開始時刻
rest_durations = {}         # 累積休憩時間（秒）
last_events = {}            # メモリ内イベント履歴

# イベントハッシュ生成（多重発火防止）
import hashlib

def generate_event_hash(user_id, event_type, channel_name, timestamp):
    raw = f"{user_id}-{event_type}-{channel_name}-{timestamp.strftime('%Y%m%d%H%M%S%f')}"
    return hashlib.md5(raw.encode()).hexdigest()

# 時間フォーマット
def format_duration(seconds):
    minutes = int(seconds // 60)
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"

# 名前正規化
def normalize(name):
    if not name:
        return ""
    return name.lower().replace('　', ' ').replace('・', ' ').strip()

# 通知対象ユーザー
ALLOWED_USERS = [
    normalize("井上 璃久 / Riku Inoue"),
    normalize("平井 悠喜 / Yuki Hirai"),
    normalize("松岡満貴 / Maki Matsuoka"),
]

# Slackユーザーキャッシュ
slack_user_cache = {}

def build_slack_user_cache():
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get("https://slack.com/api/users.list", headers=headers).json()
    for member in resp.get("members", []):
        if member.get("deleted"): continue
        uid = member.get("id")
        profile = member.get("profile", {})
        rn = normalize(profile.get("real_name", ""))
        dn = normalize(profile.get("display_name", ""))
        slack_user_cache[rn] = uid
        slack_user_cache[dn] = uid

# Slack送信関数
def send_slack_message(text, mention_user_id=None, thread_ts=None, use_daily_channel=False):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    channel = DAILY_REPORT_CHANNEL_ID if use_daily_channel else SLACK_CHANNEL_ID
    msg = f"<@{mention_user_id}>\n{text}" if mention_user_id else text
    payload = {"channel": channel, "text": msg}
    if thread_ts: payload["thread_ts"] = thread_ts
    resp = requests.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)
    data = resp.json()
    return data.get("ts") if data.get("ok") else None

# Discordクライアント設定
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
client = discord.Client(intents=intents)

# ボイスステート更新イベント
@client.event
async def on_voice_state_update(member, before, after):
    now = datetime.datetime.now(JST)
    name = member.display_name
    if normalize(name) not in ALLOWED_USERS:
        return
    # イベント判定
    event_type = None
    if not before.channel and after.channel:
        event_type = "clock_in"
    elif before.channel and not after.channel:
        event_type = "clock_out"
    elif before.channel and after.channel and before.channel != after.channel:
        event_type = "move"
    if not event_type: return
    # 多重通知防止
    key = f"{member.id}-{event_type}"
    channel_name = after.channel.name if after.channel else before.channel.name
    ehash = generate_event_hash(member.id, event_type, channel_name, now)
    prev = last_events.get(key)
    if prev and (now - prev['timestamp']).total_seconds() < 3 and prev['event_hash'] == ehash:
        return
    last_events[key] = {'timestamp': now, 'event_hash': ehash}
    # 休憩管理
    if after.channel and after.channel.name == "休憩室":
        rest_start_times[name] = now
    if before.channel and before.channel.name == "休憩室":
        start = rest_start_times.pop(name, None)
        if start:
            rest_durations[name] = rest_durations.get(name, 0) + (now - start).total_seconds()
    # 出勤処理
    if event_type == "clock_in":
        if name not in clock_in_times and after.channel.name != "休憩室":
            clock_in_times[name] = now
            ts = send_slack_message(f"{name} が「{after.channel.name}」に出勤しました。\n出勤時間\n{now.strftime('%Y/%m/%d %H:%M:%S')}")
            last_sheet_events[f"{name}-出勤"] = now
    # 移動処理
    elif event_type == "move" and name in clock_in_times:
        last = last_sheet_events.get(f"{name}-move")
        if not last or (now - last).total_seconds() >= 3:
            last_sheet_events[f"{name}-move"] = now
            send_slack_message(f"{name} が「{after.channel.name}」に移動しました。")
    # 退勤処理
    if event_type == "clock_out" and name in clock_in_times:
        clock_out = now
        clock_in = clock_in_times.get(name)
        rest_sec = rest_durations.pop(name, 0)
        if clock_in:
            work_sec = int((clock_out - clock_in).total_seconds() - rest_sec)
        else:
            work_sec = 0
        msg = f"{name} が「{before.channel.name}」を退出しました。\n退勤時間\n{now.strftime('%Y/%m/%d %H:%M:%S')}"
        msg += f"\n\n勤務時間\n{format_duration(work_sec)}"
        ts = send_slack_message(msg)
        if ts:
            time.sleep(2)
            uid = get_slack_user_id(name)
            thread_msg = (f"<@{uid}>\n以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                          "◆日報一言テンプレート\nやったこと\n・\n次にやること\n・\nひとこと\n・")
            send_slack_message(thread_msg, thread_ts=ts)
        clock_in_times.pop(name, None)

# 強制退出検知用タスク
async def monitor_voice_channels():
    # ログイン完了を安全に待機
    while not client.is_ready():
        await asyncio.sleep(1)
    while not client.is_closed():
        for guild in client.guilds:
            for member in guild.members:
                name = member.display_name
                if normalize(name) not in ALLOWED_USERS:
                    continue
                if name in clock_in_times and not member.voice:
                    now = datetime.datetime.now(JST)
                    clock_out = now
                    clock_in = clock_in_times.get(name)
                    rest_sec = rest_durations.pop(name, 0)
                    work_sec = int((clock_out - clock_in).total_seconds() - rest_sec) if clock_in else 0
                    msg = (f"{name} の接続が切れました（強制退勤と見なします）。\n"
                           f"退勤時間\n{now.strftime('%Y/%m/%d %H:%M:%S')}\n\n勤務時間\n{format_duration(work_sec)}")
                    ts = send_slack_message(msg)
                    if ts:
                        time.sleep(2)
                        uid = get_slack_user_id(name)
                        thread_msg = (f"<@{uid}>\n以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                                      "◆日報一言テンプレート\nやったこと\n・\n次にやること\n・\nひとこと\n・")
                        send_slack_message(thread_msg, thread_ts=ts)
                    clock_in_times.pop(name, None)
        await asyncio.sleep(15)

# SlackユーザーID取得
def get_slack_user_id(discord_name):
    norm = normalize(discord_name)
    if norm in slack_user_cache:
        return slack_user_cache[norm]
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get("https://slack.com/api/users.list", headers=headers).json()
    for member in resp.get("members", []):
        if member.get("deleted"): continue
        uid = member.get("id")
        profile = member.get("profile", {})
        dn = normalize(profile.get("display_name", ""))
        rn = normalize(profile.get("real_name", ""))
        slack_user_cache[dn] = uid
        slack_user_cache[rn] = uid
        if norm in dn or dn in norm or norm in rn or rn in norm:
            return uid
    return None

# Flaskアプリケーション
app = Flask(__name__)
@app.route('/')
def health_check():
    return 'OK'

# Discord Bot 起動関数
import asyncio

def run_discord_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(monitor_voice_channels())
    loop.run_until_complete(client.start(DISCORD_TOKEN))

# メイン
if __name__ == '__main__':
    # Slackユーザーキャッシュ構築
    build_slack_user_cache()

    # Discord Bot をデーモンスレッドで起動
    Thread(target=run_discord_bot, daemon=True).start()

    # 本番向け WSGI サーバーで Flask を起動
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    serve(app, host='0.0.0.0', port=port)
