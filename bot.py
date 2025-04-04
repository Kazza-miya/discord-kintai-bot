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
import hashlib

# 🔒 多重発火防止: イベント内容のハッシュを作る
def generate_event_hash(user_id, event_type, channel_name, timestamp):
    raw = f"{user_id}-{event_type}-{channel_name}-{timestamp.strftime('%Y%m%d%H%M%S%f')}"
    return hashlib.md5(raw.encode()).hexdigest()

def format_duration(seconds):
    minutes = int(seconds // 60)
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"

# def send_to_spreadsheet(name, status, clock_in=None, clock_out=None, work_duration=None, rest_duration=None):
#     print(f"[SEND] Spreadsheet: {name} - {status}")
#     webhook_url = WEBHOOK_URLS.get(name)
#     if not webhook_url:
#         print(f"Webhook URL が未設定: {name}")
#         return
#     try:
#         payload = {
#             "date": datetime.datetime.now(JST).strftime("%Y-%m-%d"),
#             "status": status,
#             "clock_in": clock_in.strftime("%H:%M:%S") if clock_in else "",
#             "clock_out": clock_out.strftime("%H:%M:%S") if clock_out else "",
#             "work_duration": work_duration or "",
#             "rest_duration": format_duration(rest_duration) if isinstance(rest_duration, (int, float)) else rest_duration or "",
#         }
#         response = requests.post(webhook_url, json=payload)
#         print(f"Webhook送信: {response.status_code} → {name}")
#     except Exception as e:
#         print(f"スプレッドシート送信失敗 → {name}: {e}")

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

client = discord.Client(intents=intents)

last_events = {}  # ← これを def normalize() の上などに追加

def normalize(name):
    if not name:
        return ""
    return name.lower().replace('　', ' ').replace('・', ' ').strip()

ALLOWED_USERS = [
    normalize("井上 璃久 / Riku Inoue"),
    normalize("平井 悠喜 / Yuki Hirai"),
    normalize("松岡満貴 / Maki Matsuoka"),
]

slack_user_cache = {}

def build_slack_user_cache():
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    response = requests.get("https://slack.com/api/users.list", headers=headers).json()
    
    for member in response.get("members", []):
        if member.get("deleted"):
            continue
        user_id = member.get("id")
        profile = member.get("profile", {})
        real_name = normalize(profile.get("real_name", ""))
        display_name = normalize(profile.get("display_name", ""))
        
        # 名前でマッピング
        slack_user_cache[real_name] = user_id
        slack_user_cache[display_name] = user_id

def send_slack_message(text,
                       mention_user_id=None,
                       thread_ts=None,
                       use_daily_channel=False):
    print(f"[SEND] Slack message: {text[:50]}...")  # 長文は切る
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
    if normalize(name) not in ALLOWED_USERS:
        print(f"[SKIP] 通知対象外ユーザー: {name}")
        return
    timestamp = now.strftime("%Y/%m/%d %H:%M:%S")
    print(f"[LOG] Voice state update: {name}")
    print(f"[LOG] Before channel: {before.channel.name if before.channel else 'None'}")
    print(f"[LOG] After channel: {after.channel.name if after.channel else 'None'}")

    # イベント種別を判定
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
    
    # 🔒 多重通知防止ロジック
    event_key = f"{member.id}-{event_type}"
    channel_name = after.channel.name if after.channel else (before.channel.name if before.channel else "None")
    event_hash = generate_event_hash(member.id, event_type, channel_name, now)
    
    last_record = last_events.get(event_key)
    if last_record:
        delta = (now - last_record["timestamp"]).total_seconds()
        if delta < 3 and last_record["event_hash"] == event_hash:
            print(f"[SKIP] 多重通知防止: {event_key} within {delta:.2f}s")
            return

    
    # 記録更新
    last_events[event_key] = {
        "timestamp": now,
        "channel": channel_name,
        "event_hash": event_hash
    }


    # 休憩室に入ったら、開始時間を記録（何もしない）
    if after.channel and after.channel.name == "休憩室":
        rest_start_times[name] = now

    # 休憩室から出たら、累積休憩時間に加算
    if before.channel and before.channel.name == "休憩室":
        start = rest_start_times.pop(name, None)
        if start:
            duration = (now - start).total_seconds()
            rest_durations[name] = rest_durations.get(name, 0) + duration


    # 出勤
    if event_type == "clock_in":
        if name not in clock_in_times and after.channel.name != "休憩室":
            clock_in_times[name] = now
            last_key = f"{name}-出勤"
            last_sent = last_sheet_events.get(last_key)
            if not last_sent or (now - last_sent).total_seconds() >= 60:
                msg = f"{name} が「{after.channel.name}」に出勤しました。\n出勤時間\n{timestamp}"
                send_slack_message(msg)
                # send_to_spreadsheet(
                #     name=name,
                #     status="出勤",
                #     clock_in=now
                # )
                last_sheet_events[last_key] = now
        
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

    # 移動（休憩室含む）
    elif event_type == "move":
        if name in clock_in_times and before.channel != after.channel:
            # 多重通知防止（移動にも適用）
            last_key = f"{name}-move"
            last_sent = last_sheet_events.get(last_key)
            if last_sent and (now - last_sent).total_seconds() < 3:
                print(f"[SKIP] 移動の重複通知をスキップ: {name}")
                return
            last_sheet_events[last_key] = now
    
            msg = f"{name} が「{after.channel.name}」に移動しました。"
            send_slack_message(msg)

    # 退勤
    if event_type == "clock_out" and name in clock_in_times:
        clock_out = now
        clock_in = clock_in_times.get(name)
        rest_sec = rest_durations.pop(name, 0)
        rest_duration = 0
        work_duration = "不明（出勤情報なし）"
    
        if clock_in:
            delta = clock_out - clock_in
            work_sec = int(delta.total_seconds() - rest_sec)
            work_duration = max(work_sec, 0)
            rest_duration = rest_sec
    
        # 通知
        msg = f"{name} が「{before.channel.name}」を退出しました。\n退勤時間\n{timestamp}"
        if isinstance(work_duration, (int, float)):
            msg += f"\n\n勤務時間\n{format_duration(work_duration)}"
        
        # Slackに投稿 → ts を取得
        result_ts = send_slack_message(msg)
        
        # スプレッドシート
        # send_to_spreadsheet(
        #     name=name,
        #     status="退勤",
        #     clock_in=clock_in,
        #     clock_out=clock_out,
        #     work_duration=format_duration(work_duration) if isinstance(work_duration, (int, float)) else (work_duration or ""),
        #     rest_duration=format_duration(rest_duration) if isinstance(rest_duration, (int, float)) else (rest_duration or "")
        # )
                
        # 日報テンプレートは退勤時だけ送る
        if result_ts:
            time.sleep(2)
            slack_user_id = get_slack_user_id(name)
            thread_msg = (f"<@{slack_user_id}>\n"
                          f"以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                          "◆日報一言テンプレート\n"
                          "やったこと\n・\n次にやること\n・\nひとこと\n・")
            send_slack_message(thread_msg, thread_ts=result_ts, use_daily_channel=False)
        
                
        # 出勤記録削除
        clock_in_times.pop(name, None)


def get_slack_user_id(discord_name):
    normalized_name = normalize(discord_name)

    # ① キャッシュから探す
    if normalized_name in slack_user_cache:
        return slack_user_cache[normalized_name]

    # ② 見つからなければAPI問い合わせ
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    response = requests.get("https://slack.com/api/users.list", headers=headers).json()

    for member in response.get("members", []):
        if member.get("deleted"):
            continue
        user_id = member.get("id")
        profile = member.get("profile", {})
        display_name = normalize(profile.get("display_name", ""))
        real_name = normalize(profile.get("real_name", ""))

        # キャッシュにも登録しておく（今後のため）
        slack_user_cache[display_name] = user_id
        slack_user_cache[real_name] = user_id

        if (normalized_name in display_name
                or display_name in normalized_name
                or normalized_name in real_name
                or real_name in normalized_name):
            return user_id

    return None
import asyncio

async def monitor_voice_channels():
    await client.wait_until_ready()
    while not client.is_closed():
        for guild in client.guilds:
            for member in guild.members:
                name = member.display_name
                if normalize(name) not in ALLOWED_USERS:
                    continue

                # 出勤中かつ現在どのチャンネルにもいない場合 = 強制退出と判断
                if name in clock_in_times and not member.voice:
                    now = datetime.datetime.now(JST)
                    timestamp = now.strftime("%Y/%m/%d %H:%M:%S")
                    clock_out = now
                    clock_in = clock_in_times.get(name)
                    rest_sec = rest_durations.pop(name, 0)
                    work_duration = "不明（出勤情報なし）"

                    if clock_in:
                        delta = clock_out - clock_in
                        work_sec = int(delta.total_seconds() - rest_sec)
                        work_duration = max(work_sec, 0)

                    msg = f"{name} の接続が切れました（強制退勤と見なします）。\n退勤時間\n{timestamp}"
                    if isinstance(work_duration, (int, float)):
                        msg += f"\n\n勤務時間\n{format_duration(work_duration)}"

                    result_ts = send_slack_message(msg)
                    if result_ts:
                        time.sleep(2)
                        slack_user_id = get_slack_user_id(name)
                        thread_msg = (f"<@{slack_user_id}>\n"
                                      f"以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                                      "◆日報一言テンプレート\n"
                                      "やったこと\n・\n次にやること\n・\nひとこと\n・")
                        send_slack_message(thread_msg, thread_ts=result_ts, use_daily_channel=False)

                    # 出勤情報削除
                    clock_in_times.pop(name, None)

        await asyncio.sleep(15)  # 15秒ごとにチェック

from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route('/')
def health_check():
    return 'OK'

def run_discord_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(monitor_voice_channels())
    loop.run_until_complete(client.start(DISCORD_TOKEN))

if __name__ == '__main__':
    build_slack_user_cache()
    # Discord Bot を別スレッドで起動
    Thread(target=run_discord_bot).start()

    # Flask（Render用のWebサーバ）
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

