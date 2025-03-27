import discord
import datetime
import time
import uuid
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

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

client = discord.Client(intents=intents)


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
    now = datetime.datetime.now()
    name = member.display_name
    now = datetime.datetime.now(JST)
    timestamp = now.strftime("%Y/%m/%d %H:%M:%S")

    # 出勤
    if not before.channel and after.channel:
        clock_in_times[name] = now
        msg = f"{name} が「{after.channel.name}」に出勤しました。\n出勤時間\n{timestamp}"
        send_slack_message(msg)

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
        msg = f"{name} が「{before.channel.name}」を退出しました。\n退勤時間\n{timestamp}\n\n勤務時間\n{work_duration}]"
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


client.run(DISCORD_TOKEN)

from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route('/')
def health_check():
    return 'OK'

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# Flaskを別スレッドで起動
Thread(target=run_flask).start()
