import discord
import datetime
import time
import uuid
from flask import Flask
from threading import Thread
import requests
import pytz

JST = pytz.timezone('Asia/Tokyo')
import os
from dotenv import load_dotenv

load_dotenv()
last_sheet_events = {}  # ユーザーごとに最後に送信した時刻を記録

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
SLACK_CHANNEL_ID = os.getenv('SLACK_CHANNEL_ID')
DAILY_REPORT_CHANNEL_ID = os.getenv('DAILY_REPORT_CHANNEL_ID')
clock_in_times = {}  # ユーザーの出勤時刻を一時保存
# スプレッドシート用Webhookマッピング　ここにドンドン増やしていく
WEBHOOK_URLS = {
    "宮内 和貴 / Kazuki Miyauchi": "https://script.google.com/macros/s/AKfycbzle9GzA0nC_1v1S4M6rha85UCOoLsLNz0P7E4b6i44ItzIb4pMWHGmEzQtH2wQ7Gxm7A/exec",
    "井上 璃久": "https://script.google.com/macros/s/AKfycbwKC8IH3tbN1cmaKjCsQCvqMiI3Fuf5XDarB3djgX1LsWpco8a8x-sTpnpve50pAHYBpg/exec"
}
def format_duration(seconds):
    minutes = int(seconds // 60)
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"

def send_to_spreadsheet(name, status, clock_in=None, clock_out=None, work_duration=None, rest_duration=None):
    webhook_url = WEBHOOK_URLS.get(name)
    if not webhook_url:
        print(f"Webhook URL が未設定: {name}")
        return
    try:
        payload = {
            "date": datetime.datetime.now(JST).strftime("%Y-%m-%d"),
            "status": status,
            "clock_in": clock_in.strftime("%H:%M:%S") if clock_in else "",
            "clock_out": clock_out.strftime("%H:%M:%S") if clock_out else "",
            "work_duration": work_duration or "",
            "rest_duration": format_duration(rest_duration) if isinstance(rest_duration, (int, float)) else rest_duration or "",
        }
        response = requests.post(webhook_url, json=payload)
        print(f"Webhook送信: {response.status_code} → {name}")
    except Exception as e:
        print(f"スプレッドシート送信失敗 → {name}: {e}")

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

client = discord.Client(intents=intents)

last_events = {}  # ← これを def normalize() の上などに追加

def normalize(name):
    if not name:
        return ""
    return name.lower().replace('　', ' ').replace('・', ' ').strip()


def send_slack_message(text,
                       mention_user_id=None,
                       thread_ts=None,
                       use_daily_channel=False):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }

    channel = DAILY_REPORT_CHANNEL_ID if use_daily_channel else SLACK_CHANNEL_ID

    message_text = f"<@{mention_user_id}>\n{text}" if mention_user_id else text

    payload = {"channel": channel, "text": message_text}

    if thread_ts:
        payload["thread_ts"] = thread_ts  # スレッド内投稿

    response = requests.post("https://slack.com/api/chat.postMessage",
                             headers=headers,
                             json=payload)
    data = response.json()
    print("Slack通知送信:", data)
    return data.get("ts") if data.get("ok") else None


def debug_slack_users():
    headers = {"Authorization": f"Bearer " + SLACK_BOT_TOKEN}
    response = requests.get("https://slack.com/api/users.list",
                            headers=headers)
    users = response.json().get("members", [])
    print("\n--- [Slackユーザー一覧] ---")
    for user in users:
        if user.get("deleted"):
            continue
        profile = user.get("profile", {})
        print(
            f"real_name: {profile.get('real_name', '')} | display_name: {profile.get('display_name', '')}"
        )
    print("--- [ここまで] ---\n")

rest_start_times = {}   # 休憩室に入った時刻
rest_durations = {}     # 休憩の累計時間（秒）

@client.event
async def on_voice_state_update(member, before, after):
    now = datetime.datetime.now(JST)
    name = member.display_name
    timestamp = now.strftime("%Y/%m/%d %H:%M:%S")
# イベント種別を判定
    event_type = None
    if not before.channel and after.channel:
        event_type = "clock_in"
    elif before.channel and not after.channel:
        event_type = "clock_out"
    elif before.channel and after.channel and before.channel != after.channel:
        event_type = "move"
    
    if not event_type:
        return
    
    # voice_state_updateの多重発火対策（全体で5秒制限）
    event_key = f"{member.id}-{event_type}"
    if event_key in last_events:
        if (now - last_events[event_key]).total_seconds() < 5:
            return
    last_events[event_key] = now


    # ↓↓↓ ここが重複防止（5秒以内の同一ユーザー＆イベントは無視）
    key = f"{name}-{event_type}"
    last_time = last_events.get(key)
    if last_time and (now - last_time).total_seconds() < 5:
        return  # スキップ
    last_events[key] = now  # 実行記録を保存

    # 休憩室に入ったら、開始時間を記録（何もしない）
    if after.channel and after.channel.name == "休憩室":
        rest_start_times[name] = now
        return

    # 休憩室から出たら、累積休憩時間に加算
    if before.channel and before.channel.name == "休憩室":
        start = rest_start_times.pop(name, None)
        if start:
            duration = (now - start).total_seconds()
            rest_durations[name] = rest_durations.get(name, 0) + duration


    # 出勤
    if not before.channel and after.channel:
        clock_in_times[name] = now
        msg = f"{name} が「{after.channel.name}」に出勤しました。\n出勤時間\n{timestamp}"
        send_slack_message(msg)
        
        last_key = f"{name}-出勤"
        last_sent = last_sheet_events.get(last_key)
        if last_sent and (now - last_sent).total_seconds() < 60:
            print("スプレッドシートへの重複送信をスキップ（出勤）:", name)
        # else:
        #     send_to_spreadsheet(
        #         name=name,
        #         status="出勤",
        #         clock_in=now
        #     )
            last_sheet_events[last_key] = now

    # 移動
    elif before.channel and after.channel and before.channel != after.channel:
        msg = f"{name} が「{after.channel.name}」に移動しました。"
        send_slack_message(msg)

    # 退勤
    if before.channel and not after.channel:
        now = datetime.datetime.now(JST)  # ← 必須
        name = member.display_name        # ← 必須
        clock_out = now
        clock_in = clock_in_times.get(name)
        rest_sec = rest_durations.pop(name, 0)
        rest_duration = 0
        work_duration = "不明（出勤情報なし）"

        if clock_in:
            delta = clock_out - clock_in
            work_sec = int(delta.total_seconds() - rest_sec)
            work_duration = max(work_sec, 0)  # 秒数として保持
            rest_duration = rest_sec
        # 退勤処理の最後で送信前にチェック
        last_key = f"{name}-退勤"
        last_sent = last_sheet_events.get(last_key)
        if last_sent and (now - last_sent).total_seconds() < 60:
            print("スプレッドシートへの重複送信をスキップ:", name)
        else:
            send_to_spreadsheet(
                name=name,
                status="退勤",
                clock_in=clock_in,
                clock_out=clock_out,
                work_duration=format_duration(work_duration) if isinstance(work_duration, (int, float)) else (work_duration or ""),
                rest_duration=format_duration(rest_duration) if isinstance(rest_duration, (int, float)) else (rest_duration or "")
            )
            last_sheet_events[last_key] = now
        
        # 退勤メッセージ作成（ここで msg を定義）
        msg = f"{name} が「{before.channel.name}」を退出しました。\n退勤時間\n{timestamp}"
        if work_duration != "不明（出勤情報なし）":
            msg += f"\n\n勤務時間\n{work_duration}"
    
        # Slackに通知
        result = send_slack_message(msg, mention_user_id=None)
    
        # スレッド返信（Slack投稿反映待ち）
        time.sleep(1.5)
        slack_user_id = get_slack_user_id(name)
        thread_msg = (f"<@{slack_user_id}>\n"
                      f"以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                      "◆日報一言テンプレート\n"
                      "やったこと\n・\n次にやること\n・\nひとこと\n・")
    
        if result:
            send_slack_message(thread_msg, thread_ts=result, use_daily_channel=False)
        else:
            send_slack_message(thread_msg, use_daily_channel=False)
    
        # 出勤記録削除
        clock_in_times.pop(name, None)

def get_slack_user_id(discord_name):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    response = requests.get("https://slack.com/api/users.list",
                            headers=headers).json()
    normalized_discord_name = normalize(discord_name)

    for member in response.get("members", []):
        if member.get("deleted"):
            continue
        profile = member.get("profile", {})
        display_name = normalize(profile.get("display_name", ""))
        real_name = normalize(profile.get("real_name", ""))
        if (normalized_discord_name in display_name
                or display_name in normalized_discord_name
                or normalized_discord_name in real_name
                or real_name in normalized_discord_name):
            return member.get("id")
    return None

from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route('/')
def health_check():
    return 'OK'

def run_discord_bot():
    client.run(DISCORD_TOKEN)

if __name__ == '__main__':
    # Discord Bot を別スレッドで起動
    Thread(target=run_discord_bot).start()

    # Flask（Render用のWebサーバ）
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

