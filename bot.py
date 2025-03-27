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

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
SLACK_CHANNEL_ID = os.getenv('SLACK_CHANNEL_ID')
DAILY_REPORT_CHANNEL_ID = os.getenv('DAILY_REPORT_CHANNEL_ID')
clock_in_times = {}  # ユーザーの出勤時刻を一時保存
# スプレッドシート用Webhookマッピング　ここにドンドン増やしていく
WEBHOOK_URLS = {
    "宮内 和貴": "https://script.google.com/macros/s/AKfycbzle9GzA0nC_1v1S4M6rha85UCOoLsLNz0P7E4b6i44ItzIb4pMWHGmEzQtH2wQ7Gxm7A/exec",
    "井上 璃久": "https://script.google.com/macros/s/AKfycbwKC8IH3tbN1cmaKjCsQCvqMiI3Fuf5XDarB3djgX1LsWpco8a8x-sTpnpve50pAHYBpg/exec"
}

def send_to_spreadsheet(name, status, clock_in=None, clock_out=None, work_duration=None):
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
            "work_duration": work_duration or ""
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

    # ↓↓↓ ここが重複防止（5秒以内の同一ユーザー＆イベントは無視）
    key = f"{name}-{event_type}"
    last_time = last_events.get(key)
    if last_time and (now - last_time).total_seconds() < 5:
        return  # スキップ
    last_events[key] = now  # 実行記録を保存


    # 出勤
    if not before.channel and after.channel:
        clock_in_times[name] = now
        msg = f"{name} が「{after.channel.name}」に出勤しました。\n出勤時間\n{timestamp}"
        send_slack_message(msg)
        send_to_spreadsheet(name, status="出勤", clock_in=now)
    # 移動
    elif before.channel and after.channel and before.channel != after.channel:
        msg = f"{name} が「{after.channel.name}」に移動しました。"
        send_slack_message(msg)

    # 退勤
    elif before.channel and not after.channel:
        clock_out = now
        clock_in = clock_in_times.get(name)
        work_duration = ""
        if clock_in:
            delta = clock_out - clock_in
            work_duration = str(
                datetime.timedelta(seconds=int(delta.total_seconds())))
        else:
            work_duration = "不明（出勤情報なし）"

            # メンションせず、普通の退勤メッセージを投稿（親スレッド）
        unique_id = str(uuid.uuid4())[:8]  # 投稿をユニークにするID
        msg = f"{name} が「{before.channel.name}」を退出しました。\n退勤時間\n{timestamp}\n\n勤務時間\n{work_duration}"
        result = send_slack_message(msg, mention_user_id=None)

        # 少し待ってからスレッド返信（Slackが投稿を反映するのを待つ）
        time.sleep(1.5)

        # SlackのユーザーIDを取得して、スレッド側でメンション付きテンプレを作成
        slack_user_id = get_slack_user_id(name)
        thread_msg = (f"<@{slack_user_id}>\n"
                      f"以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                      "◆日報一言テンプレート\n"
                      "やったこと\n・\n次にやること\n・\nひとこと\n・")

        # スレッド投稿（Bot名義）
        if result:
            send_slack_message(thread_msg,
                               thread_ts=result,
                               use_daily_channel=False)
        else:
            # 念のため通常投稿（スレッドなし）に fallback
            send_slack_message(thread_msg, use_daily_channel=False)

        # 出勤記録を削除
        clock_in_times.pop(name, None)
        send_to_spreadsheet(name, status="退勤", clock_in=clock_in, clock_out=clock_out, work_duration=work_duration)

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

