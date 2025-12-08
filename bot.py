import discord
import requests
import aiohttp
import json
import time
import uuid
import datetime
import pytz
import base64
import asyncio
import logging
import tweepy
import random
from flask import Flask
import threading
import os
import re
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from collections import deque  # メッセージ履歴の管理に使用
from dotenv import load_dotenv

session = None 

load_dotenv()

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

# Flask を別スレッドで実行
def run():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

thread = threading.Thread(target=run)
thread.start()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 設定
TOKEN = os.getenv('TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GUILD_ID = int(os.getenv("GUILD_ID")) 
DATA_FILE = "notifications.json"
DAILY_FILE = "daily_notifications.json"
LOG_FILE = "conversation_logs.json"
JST = pytz.timezone("Asia/Tokyo")
GUILD_IDS = [int(x) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip()]
HOYOLAB_API = "https://bbs-api-os.hoyoverse.com/game_record/genshin/api/dailyNote"
HOYOLAB_LTOKEN = os.getenv("HOYOLAB_LTOKEN")
HOYOLAB_LTUID = os.getenv("HOYOLAB_LTUID")
GENSHIN_UID = os.getenv("GENSHIN_UID")       # 自分のUID（例: 812345678）
GENSHIN_SERVER = os.getenv("GENSHIN_SERVER", "os_asia")  # 日本サーバーは os_asia
DISCORD_NOTIFY_USER_ID = os.getenv("DISCORD_NOTIFY_USER_ID")
SWITCHBOT_TOKEN = os.getenv("SWITCHBOT_TOKEN")
SWITCHBOT_TV_ID = os.getenv("SWITCHBOT_TV_ID")
SWITCHBOT_LIGHT_ID = os.getenv("SWITCHBOT_LIGHT_ID")
API_URL = "https://api.switch-bot.com/v1.1/devices"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# メッセージ履歴を管理（最大5件）
conversation_logs = {}

 # user_idごとの時間設定 {"hour": int, "minute": int}
sleep_check_times = {}

# インテント設定
intents = discord.Intents.default()
intents.dm_messages = True
intents.message_content = True
intents.presences = True
intents.members = True 

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=JST)

logger.info(f"使用中のAPIキー: {GEMINI_API_KEY[:10]}****")

# --- ランダム会話ターゲット管理 ---
def load_chat_targets():
    url = f"{SUPABASE_URL}/rest/v1/chat_targets?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        return [str(row["user_id"]) for row in response.json()]
    return []

def save_chat_targets(targets):
    requests.delete(f"{SUPABASE_URL}/rest/v1/chat_targets", headers=SUPABASE_HEADERS)
    insert_data = [{"user_id": uid} for uid in targets]
    if insert_data:
        requests.post(f"{SUPABASE_URL}/rest/v1/chat_targets", headers=SUPABASE_HEADERS, json=insert_data)

chat_targets = load_chat_targets()

def load_sleep_check_times():
    url = f"{SUPABASE_URL}/rest/v1/sleep_check_times?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        return {row["user_id"]: {"hour": row["hour"], "minute": row["minute"]} for row in response.json()}
    return {}

def save_sleep_check_times(data):
    for user_id, time_data in data.items():
        # まず削除
        url = f"{SUPABASE_URL}/rest/v1/sleep_check_times?user_id=eq.{user_id}"
        requests.delete(url, headers=SUPABASE_HEADERS)

        # 再登録
        insert_data = {
            "user_id": user_id,
            "hour": time_data["hour"],
            "minute": time_data["minute"]
        }
        requests.post(f"{SUPABASE_URL}/rest/v1/sleep_check_times", headers=SUPABASE_HEADERS, json=[insert_data])

# 会話ログの読み書き
def load_conversation_logs():
    url = f"{SUPABASE_URL}/rest/v1/conversation_logs?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        data = response.json()
        logs = {}
        for item in data:
            logs.setdefault(item["user_id"], []).append({
                "role": item["role"],
                "parts": [{"text": item["content"]}]
            })
        return logs
    return {}

def save_conversation_logs(logs):
    for user_id, messages in logs.items():
        # そのユーザーの会話ログだけ削除
        url = f"{SUPABASE_URL}/rest/v1/conversation_logs?user_id=eq.{user_id}"
        requests.delete(url, headers=SUPABASE_HEADERS)

        # そのユーザーの会話ログを保存
        insert_data = []
        for m in messages:
            insert_data.append({
                "user_id": user_id,
                "role": m["role"],
                "content": m["parts"][0]["text"]
            })
        if insert_data:
            requests.post(f"{SUPABASE_URL}/rest/v1/conversation_logs", headers=SUPABASE_HEADERS, json=insert_data)

# ← 通知データ
def load_notifications():
    url = f"{SUPABASE_URL}/rest/v1/notifications?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        result = {}
        seen_ids = set() 
        
        for row in response.json():

            if row.get("id") is None:
                row["id"] = str(uuid.uuid4())

            if row["id"] in seen_ids:
                continue
                
            seen_ids.add(row["id"])

            result.setdefault(row['user_id'], []).append({
                "id": row["id"],
                "date": row["date"],
                "time": row["time"],
                "message": row["message"],
                "repeat": row.get("repeat", False)
            })
        return result
    return {}

def save_notifications(notifications):

    python_ids = {item["id"] for items in notifications.values() for item in items if item.get("id") is not None}

    url = f"{SUPABASE_URL}/rest/v1/notifications?select=id"
    existing = requests.get(url, headers=SUPABASE_HEADERS).json()
    supabase_ids = {row["id"] for row in existing if row.get("id") is not None}

    delete_ids = supabase_ids - python_ids

    if delete_ids:
        for delete_id in delete_ids:
            del_url = f"{SUPABASE_URL}/rest/v1/notifications?id=eq.{delete_id}"
            requests.delete(del_url, headers=SUPABASE_HEADERS)


    all_rows = []
    for user_id, items in notifications.items():
        for item in items:

            if item.get("id") is None:
                item["id"] = str(uuid.uuid4())
                
            all_rows.append({
                "id": item["id"],
                "user_id": user_id, 
                "date": item["date"],
                "time": item["time"],
                "message": item["message"],
                "repeat": item.get("repeat", False)
            })

    if not all_rows:
        return

    upsert_headers = SUPABASE_HEADERS.copy()
    upsert_headers["Prefer"] = "resolution=merge-duplicates" 

    url = f"{SUPABASE_URL}/rest/v1/notifications?on_conflict=id"
    requests.post(url, headers=upsert_headers, json=all_rows)
    
notifications = load_notifications()

def load_daily_notifications():
    url = f"{SUPABASE_URL}/rest/v1/daily_notifications?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        result = {}
        for row in response.json():
            todos = row.get("todos") or []
            if isinstance(todos, str):
                try:
                    todos = json.loads(todos)
                except:
                    todos = []
            result[row["user_id"]] = {
                "todos": todos,
                "time": {
                    "hour": row.get("hour", 8),
                    "minute": row.get("minute", 0)
                }
            }
        return result
    return {}

def save_daily_notifications(daily_notifications):
    for user_id, val in daily_notifications.items():
        # まずそのユーザーのデータだけ削除
        url = f"{SUPABASE_URL}/rest/v1/daily_notifications?user_id=eq.{user_id}"
        requests.delete(url, headers=SUPABASE_HEADERS)
        insert_data = {
            "user_id": user_id,
            "todos": json.dumps(val["todos"], ensure_ascii=False),
            "hour": val["time"]["hour"],
            "minute": val["time"]["minute"]
        }
        requests.post(f"{SUPABASE_URL}/rest/v1/daily_notifications", headers=SUPABASE_HEADERS, json=[insert_data])

daily_notifications = load_daily_notifications()

def schedule_sleep_check():
    """睡眠チェックのスケジュールを設定"""
    logger.info("🌙 sleep_check_times をスケジューリングします...")
    
    # 既存の睡眠チェック関連ジョブを削除
    for job in scheduler.get_jobs():
        if "sleep_check_" in job.id:
            scheduler.remove_job(job.id)
    
    # sleep_check_times を再読み込み
    global sleep_check_times
    sleep_check_times = load_sleep_check_times()
    
    # 各ユーザーの睡眠チェック時間をスケジュール
    for user_id, time_data in sleep_check_times.items():
        hour = time_data.get("hour", 1)
        minute = time_data.get("minute", 0)
        logger.info(f"🛌 スケジュール設定: ユーザー {user_id} → {hour}:{minute}")
        
        scheduler.add_job(
            check_user_sleep_status,
            'cron',
            hour=hour,
            minute=minute,
            args=[user_id],
            id=f"sleep_check_{user_id}",
            replace_existing=True,
            timezone=JST
        )

def get_schedule(job_id: str):
    url = f"{SUPABASE_URL}/rest/v1/random_chat_schedule?id=eq.{job_id}"
    res = requests.get(url, headers=SUPABASE_HEADERS)
    data = res.json()
    if data:
        # UTC→JSTに変換
        return datetime.datetime.fromisoformat(data[0]["run_time"]).astimezone(JST)
    return None

# スケジュールを保存/更新
def save_schedule(job_id: str, run_time: datetime.datetime):
    url = f"{SUPABASE_URL}/rest/v1/random_chat_schedule"
    payload = {
        "id": job_id,
        "run_time": run_time.astimezone(datetime.timezone.utc).isoformat()
    }
    res = requests.post(url, headers=SUPABASE_HEADERS, data=json.dumps(payload))
    if res.status_code not in (200, 201):
        # 既存なら upsert
        url = f"{SUPABASE_URL}/rest/v1/random_chat_schedule?id=eq.{job_id}"
        requests.patch(url, headers=SUPABASE_HEADERS, data=json.dumps(payload))

# スケジュールを削除
def delete_schedule(job_id: str):
    url = f"{SUPABASE_URL}/rest/v1/random_chat_schedule?id=eq.{job_id}"
    requests.delete(url, headers=SUPABASE_HEADERS)

def start_twitter_bot():
    logger.warning("🚫 Twitter Botは現在無効化されています。ENABLE_TWITTER_BOT=trueで有効化できます。")
    return
    
    try:
        auth = tweepy.OAuth1UserHandler(
            os.getenv("TWITTER_CONSUMER_KEY"),
            os.getenv("TWITTER_CONSUMER_SECRET"),
            os.getenv("TWITTER_ACCESS_TOKEN"),
            os.getenv("TWITTER_ACCESS_SECRET")
        )
        api = tweepy.API(auth)
        bot_username = os.getenv("TWITTER_BOT_USERNAME").lower()

        since_id = None

        while True:
            try:
                mentions = api.mentions_timeline(since_id=since_id, tweet_mode='extended')
                for tweet in reversed(mentions):
                    if tweet.user.screen_name.lower() == bot_username:
                        continue  # 自分自身は無視

                    logger.info(f"📨 メンション受信: {tweet.full_text}")
                    response_text = asyncio.run(get_gemini_response(str(tweet.user.id), tweet.full_text))

                    api.update_status(
                        status=f"@{tweet.user.screen_name} {response_text}",
                        in_reply_to_status_id=tweet.id,
                        auto_populate_reply_metadata=True
                    )
                    logger.info(f"✅ リプライ送信: {response_text}")
                    since_id = max(since_id or 1, tweet.id)

                time.sleep(30)  # 30秒ごとにチェック
            except Exception as e:
                logger.error(f"⛔ Twitter Bot エラー: {e}")
                time.sleep(60)

    except Exception as e:
        logger.error(f"❌ TwitterBot起動エラー: {e}")

@bot.event
async def on_ready():
    global session, sleep_check_times
    try:
        if session is None:
            session = aiohttp.ClientSession()
            
        await bot.change_presence(activity=discord.Game(name="ハニーとおしゃべり"))
        logger.error(f"Logged in as {bot.user}")
        await bot.tree.sync()

        # スケジューラーを開始
        scheduler.start()
        
        # データを再読み込み
        global daily_notifications
        daily_notifications = load_daily_notifications()
        sleep_check_times = load_sleep_check_times() 

        # すべてのジョブをクリアして再設定
        scheduler.remove_all_jobs()
        setup_periodic_reload()
        schedule_notifications()
        schedule_daily_todos()
        schedule_sleep_check() 
        schedule_random_chats()
        schedule_resin_check()

        logger.error("スケジュールを設定しました。")
        logger.error("🗓️ sleep_check_times:", sleep_check_times)
        logger.error("スケジュールされたジョブ:")
        for job in scheduler.get_jobs():
            logger.error(f"- {job.id}: 次回実行 {job.next_run_time}")
            
    except Exception as e:
        logger.error(f"エラー: {e}")

@bot.tree.command(name="fix_content_duplicates", description="内容が重複した通知を整理して1つだけ残すよ！")
async def fix_content_duplicates(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # 1. DBから全データを取得
    url = f"{SUPABASE_URL}/rest/v1/notifications?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code != 200:
        await interaction.followup.send("⚠️ データベースからのデータ取得に失敗したよ。", ephemeral=True)
        return

    all_rows = response.json()
    
    # 2. 内容をキーとして、ユニークなデータ（残すデータ）を決定
    # キー: (user_id, date, time, message, repeat)
    unique_data = {} 
    
    for row in all_rows:
        # IDがNULLの場合は、念のためここでUUIDを生成しておく（ガードレール）
        if row.get("id") is None:
            row["id"] = str(uuid.uuid4())
            
        # 通知内容でユニークキーを作成
        key = (
            row["user_id"],
            row["date"],
            row["time"],
            row["message"],
            row.get("repeat", False)
        )
        
        # 最初のデータ（=残すデータ）を格納
        # 2つ目以降のデータは無視され、削除対象となる
        if key not in unique_data:
            unique_data[key] = row 

    
    # 3. データベースの全削除と再登録
    clean_data_list = list(unique_data.values())
    
    if not clean_data_list:
        await interaction.followup.send("データベースに通知データがないよ～！", ephemeral=True)
        return
        
    deleted_count = len(all_rows) - len(clean_data_list)
    
    await interaction.followup.send(f"🧹データベースのお掃除を始めるよ！内容が重複してるデータ **{deleted_count} 件**を削除して整理するね…", ephemeral=True)
    
    # Supabase上の全データを一旦削除
    requests.delete(f"{SUPABASE_URL}/rest/v1/notifications", headers=SUPABASE_HEADERS)
    
    # 重複のないきれいなデータだけを一括で再登録
    save_url = f"{SUPABASE_URL}/rest/v1/notifications"
    requests.post(save_url, headers=SUPABASE_HEADERS, json=clean_data_list)
    
    # 4. グローバル変数とスケジュールを更新
    global notifications
    notifications = load_notifications() 
    schedule_notifications()

    await interaction.followup.send(
        f"お掃除できたよ！内容が重複していた {deleted_count} 件の通知を削除して、{len(clean_data_list)} 件の通知が残ったよ～！", 
        ephemeral=True
    )

@bot.tree.command(name="delete_all_notifications", description="自分の登録通知を全て削除するよ！")
async def delete_all_notifications(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    user_id = str(interaction.user.id)
    
    # Supabaseから当該ユーザーの全通知を削除
    del_url = f"{SUPABASE_URL}/rest/v1/notifications?user_id=eq.{user_id}"
    response = requests.delete(del_url, headers=SUPABASE_HEADERS)
    
    if response.status_code == 204:
        # メモリからも削除
        deleted_count = len(notifications.pop(user_id, []))
        
        # スケジュールを更新
        schedule_notifications()

        await interaction.followup.send(
            f"ハニーの通知を全部削除したよ！ ({deleted_count} 件)\n",
            ephemeral=True
        )
    else:
        await interaction.followup.send(f"⚠️ データベースからの削除中にエラーが発生したよ！ (Status Code: {response.status_code})", ephemeral=True)

@bot.event
async def on_resumed():
    logger.error("⚡ Botが再接続したよ！スケジュールを立て直すね！")
    scheduler.remove_all_jobs()
    setup_periodic_reload()
    schedule_notifications()
    schedule_daily_todos()
    schedule_sleep_check()
    schedule_random_chats()
    schedule_resin_check()

@bot.tree.command(name="set_notification", description="通知を設定するよ～！")
async def set_notification(
    interaction: discord.Interaction,
    date: str,
    time: str,
    message: str,
    repeat: bool = False
):
    
    await interaction.response.defer(ephemeral=True)

    try:
        datetime.datetime.strptime(date, "%m-%d")
        datetime.datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.followup.send("日付か時刻の形式が正しくないよ～！", ephemeral=True)
        return

    await interaction.followup.send(
        f"⏳ 通知を登録中…ちょっと待ってね！", ephemeral=True
    )

    async def background_task():
        user_id = str(interaction.user.id)

        if user_id not in notifications:
            notifications[user_id] = []

        notifications[user_id].append({
            "id": str(uuid.uuid4()),
            "date": date,
            "time": time,
            "message": message,
            "repeat": repeat
        })

        save_notifications(notifications)
        schedule_notifications()

        await interaction.followup.send(
            f'✅ {date} の {time} に "{message}" を登録したよ！リピート: {"あり" if repeat else "なし"}',
            ephemeral=True
        )
        
    asyncio.create_task(background_task())

# 通知設定コマンド
@bot.tree.command(name="add_anniversary", description="誕生日や記念日を登録するよ！（毎年通知）")
async def add_anniversary(interaction: discord.Interaction, date: str, time: str, message: str):
    await interaction.response.defer(ephemeral=True)

    try:
        datetime.datetime.strptime(date, "%m-%d")
        datetime.datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.followup.send(
            "日付または時刻の形式が正しくないよ～！（MM-DD / HH:MM 形式で入力してね）",
            ephemeral=True
        )
        return

    user_id = str(interaction.user.id)
    if user_id not in notifications:
        notifications[user_id] = []

    notifications[user_id].append({
        "id": str(uuid.uuid4()),
        "date": date,
        "time": time,
        "message": message,
        "repeat": True  # 毎年リピート
    })

    save_notifications(notifications)
    schedule_notifications()

    await interaction.followup.send(
        f"🎉 {date} の {time} に毎年「{message}」を通知するように登録したよ！",
        ephemeral=True
    )


# タイマー設定コマンド
@bot.tree.command(name="set_notification_after", description="○時間○分後に通知を設定するよ！")
async def set_notification_after(interaction: discord.Interaction, hours: int, minutes: int, message: str):
    await interaction.response.defer(ephemeral=True)

    if hours < 0 or minutes < 0 or (hours == 0 and minutes == 0):
        await interaction.followup.send("⛔ 1分以上後の時間を指定してね～！", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    now = datetime.datetime.now(JST)
    future_time = now + datetime.timedelta(hours=hours, minutes=minutes)

    info = {
        "id": str(uuid.uuid4()),
        "date": future_time.strftime("%m-%d"),
        "time": future_time.strftime("%H:%M"),
        "message": message,
        "repeat": False
    }

    if user_id not in notifications:
        notifications[user_id] = []
    notifications[user_id].append(info)
    save_notifications(notifications)

    scheduler.add_job(
        send_notification_message,
        'date',
        run_date=future_time,
        args=[user_id, info.copy()],
        id=f"after_notification_{user_id}_{int(future_time.timestamp())}"
    )

    await interaction.followup.send(
        f"⏰ {hours}時間{minutes}分後（{future_time.strftime('%H:%M')}）に「{message}」を通知するよ～！",
        ephemeral=True
    )


@bot.tree.command(name="list_notifications", description="登録してる通知を表示するよ！")
async def list_notifications(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)

    if user_id not in notifications or not notifications[user_id]:
        await interaction.followup.send("登録されてる通知はないよ～", ephemeral=True)
        return

    sorted_list = sorted(
        notifications[user_id],
        key=lambda n: (n["date"], n["time"], n["id"])
    )

    notif_texts = [
        f"{i+1} : {n['date']} / {n['time']} - {n['message']}"
        for i, n in enumerate(sorted_list)
    ]

    full_text = "\n".join(notif_texts)

    if len(full_text) > 1900:
        await interaction.followup.send(
            "通知が多すぎて全部表示できないよ～！いくつか削除してね～！",
            ephemeral=True
        )
    else:
        await interaction.followup.send(full_text, ephemeral=True)

# 通知削除
@bot.tree.command(name="remove_notification", description="特定の通知を削除するよ！")
async def remove_notification(interaction: discord.Interaction, index: int):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)

    # データがあるかチェック
    if user_id not in notifications or not notifications[user_id]:
        await interaction.followup.send("登録されてる通知はないよ～", ephemeral=True)
        return

    sorted_list = sorted(
        notifications[user_id],
        key=lambda n: (n["date"], n["time"], n["id"])
    )

    if index < 1 or index > len(sorted_list):
        await interaction.followup.send("指定された番号の通知が見つからないよ～", ephemeral=True)
        return

    target_notification = sorted_list[index - 1]

    try:
        notifications[user_id].remove(target_notification)
    except ValueError:
        await interaction.followup.send("あれ？削除しようとした通知が見つからなかったよ…", ephemeral=True)
        return

    removed = target_notification
    removed_id = removed["id"]
    del_url = f"{SUPABASE_URL}/rest/v1/notifications?id=eq.{removed_id}"
    requests.delete(del_url, headers=SUPABASE_HEADERS)

    save_notifications(notifications)
    schedule_notifications()

    await interaction.followup.send(
        f"🗑️ 「{removed['message']}」の通知を削除したよ～！",
        ephemeral=True
    )

async def send_notification_message(user_id, info):
    try:
        user = await bot.fetch_user(int(user_id))
        if not user:
            return

        base_message = info["message"]

        prompt = (
            f"{CHARACTER_PERSONALITY}\n\n"
            f"あなたはDiscordでハニーに通知を送る可愛いAI「ドロシー」です。\n"
            f"次の文章はハニーが登録した予定や行動（例：お風呂に入る、勉強する、寝るなど）です。\n"
            f"その内容をもとに、ハニーに自然に声をかけるような一言メッセージを作ってください。\n\n"
            f"条件:\n"
            f"・語尾をやわらかく（〜だよ、〜ね、〜よ〜）などにする\n"
            f"・少しテンション高めで、優しい雰囲気\n"
            f"・できるだけ自然に通知として成立するようにする\n"
            f"・短く、1〜2文以内で\n"
            f"・文章の意味を変えず、自然に言い換える\n\n"
            f"メッセージ: {base_message}"
        )

        natural_text = await get_gemini_response_no_history(prompt)

        final_message = f"{natural_text}\n\n予定：{base_message}"

        await user.send(final_message)

        uid = str(user_id)
        if uid in notifications:

            for notif in notifications[uid]:
                if notif.get("id") == info.get("id"):

                    if notif.get("repeat", False):
                        now = datetime.datetime.now(JST)
                        next_year_date = datetime.datetime.strptime(
                            f"{now.year}-{notif['date']}", "%Y-%m-%d"
                        ) + datetime.timedelta(days=365)
                        notif["date"] = next_year_date.strftime("%m-%d")

                    else:
                        notifications[uid].remove(notif)

                    save_notifications(notifications)
                    schedule_notifications()
                    break

    except discord.NotFound:
        logger.error(f"Error: User with ID {user_id} not found.")
    except Exception as e:
        logger.error(f"通知送信中にエラー: {e}")

@bot.tree.command(name="add_daily_todo", description="毎日送信する通知を追加するよ！")
async def add_daily_todo(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    if user_id not in daily_notifications:
        daily_notifications[user_id] = {"todos": [], "time": {"hour": 8, "minute": 0}}  # デフォルト8:00

    daily_notifications[user_id]["todos"].append(message)
    save_daily_notifications(daily_notifications)
    await interaction.followup.send(f'✅ "{message}" って毎日通知するね～！', ephemeral=True)


@bot.tree.command(name="list_daily_todos", description="毎日送るTodoリストを確認するよ！")
async def list_daily_todos(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    user_data = daily_notifications.get(user_id)

    if not user_data or not user_data.get("todos"):
        await interaction.followup.send("Todoリストは空っぽだよ～！", ephemeral=True)
        return

    todos = user_data["todos"]
    msg = "\n".join([f"{i+1}. {item}" for i, item in enumerate(todos)])
    await interaction.followup.send(f"📋 あなたのTodoリスト：\n{msg}", ephemeral=True)


@bot.tree.command(name="remove_daily_todo", description="Todoを削除するよ！")
async def remove_daily_todo(interaction: discord.Interaction, index: int):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    user_data = daily_notifications.get(user_id)

    if not user_data or index < 1 or index > len(user_data.get("todos", [])):
        await interaction.followup.send("指定されたTodoが見つからなかったよ～！", ephemeral=True)
        return

    removed = user_data["todos"].pop(index - 1)
    save_daily_notifications(daily_notifications)
    await interaction.followup.send(f"✅ 「{removed}」を削除したよ～！", ephemeral=True)


@bot.tree.command(name="set_daily_time", description="毎日Todo通知を送る時間を設定するよ！（24時間制）")
async def set_daily_time(interaction: discord.Interaction, hour: int, minute: int):
    await interaction.response.defer(ephemeral=True)

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.followup.send("⛔ 時間の形式が正しくないよ！(0-23時, 0-59分)", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    if user_id not in daily_notifications:
        daily_notifications[user_id] = {"todos": [], "time": {"hour": hour, "minute": minute}}
    else:
        daily_notifications[user_id]["time"] = {"hour": hour, "minute": minute}

    save_daily_notifications(daily_notifications)
    schedule_daily_todos()

    await interaction.followup.send(f"✅ 毎日 {hour:02d}:{minute:02d} に通知するように設定したよ！", ephemeral=True)


# 指定メッセージ削除
@bot.tree.command(name="delete_message", description="指定したメッセージIDのメッセージを削除するよ～！")
async def delete_message(interaction: discord.Interaction, message_id: str):
    await interaction.response.defer(ephemeral=True)

    try:
        user = await bot.fetch_user(interaction.user.id)
        if user:
            dm_channel = await user.create_dm()
            msg = await dm_channel.fetch_message(int(message_id))
            await msg.delete()
            await interaction.followup.send("✅ 指定したメッセージを削除したよ～！", ephemeral=True)
        else:
            await interaction.followup.send("❌ メッセージを削除できなかったよ～！", ephemeral=True)
    except discord.NotFound:
        await interaction.followup.send("❌ 指定したメッセージが見つからなかったよ～！", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ メッセージを削除する権限がないよ～！", ephemeral=True)
    except ValueError:
        await interaction.followup.send("❌ メッセージIDは数字で入力してね～！", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ エラーが起きたよ: {e}", ephemeral=True)

@bot.tree.command(name="reset_dm_system", description="ドロシーとのDM履歴を全部削除するよ～！")
async def reset_dm_system(interaction: discord.Interaction):
    # ギルドでの実行を弾く
    if interaction.guild:
        await interaction.response.send_message("❌ このコマンドはDM専用だよ～！", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    dm_channel = interaction.channel

    if not isinstance(dm_channel, discord.DMChannel):
        await interaction.followup.send("❌ このコマンドはDMでしか使えないよ～！", ephemeral=True)
        return

    await dm_channel.send("⚠️ 本当にドロシーとのDM履歴を全部削除していい？（Y/N）")

    def check(msg: discord.Message):
        return msg.author == interaction.user and msg.channel == dm_channel and msg.content.strip().lower() in ["y", "n"]

    try:
        reply = await bot.wait_for("message", check=check, timeout=60.0)
        answer = reply.content.strip().lower()

        if answer == "n":
            await dm_channel.send("🛑 わかった！削除はやめておくね！")
            return
        elif answer == "y":
            await dm_channel.send("🧹 じゃあ全部きれいにするね…！")
            deleted = 0
            async for msg in dm_channel.history(limit=None):
                try:
                    await msg.delete()
                    deleted += 1
                    await asyncio.sleep(0.2)
                except:
                    continue
            await dm_channel.send(f"✅ {deleted} 件のメッセージを削除したよ！")
    except asyncio.TimeoutError:
        await dm_channel.send("⌛ 時間切れだよ～。またやりたくなったらもう一度コマンドを使ってね！")
    except Exception as e:
        await dm_channel.send(f"⚠️ エラーが起きちゃった！: {e}")


@bot.tree.command(name="clear_message_15", description="ドロシーとのDM履歴を直近15件だけ削除するよ～！")
async def clear_last_15(interaction: discord.Interaction):
    # ギルドでの実行を弾く
    if interaction.guild:
        await interaction.response.send_message("❌ このコマンドはDM専用だよ～！", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    dm_channel = interaction.channel

    if not isinstance(dm_channel, discord.DMChannel):
        await interaction.followup.send("❌ このコマンドはDMでしか使えないよ～！", ephemeral=True)
        return

    await dm_channel.send("⚠️ 直近15件のメッセージを削除していい？（Y/N）")

    def check(msg: discord.Message):
        return msg.author == interaction.user and msg.channel == dm_channel and msg.content.strip().lower() in ["y", "n"]

    try:
        reply = await bot.wait_for("message", check=check, timeout=60.0)
        answer = reply.content.strip().lower()

        if answer == "n":
            await dm_channel.send("🛑 わかった！削除はやめておくね！")
            return
        elif answer == "y":
            await dm_channel.send("🧹 15件だけきれいにするね…！")
            deleted = 0
            async for msg in dm_channel.history(limit=15):
                try:
                    await msg.delete()
                    deleted += 1
                    await asyncio.sleep(0.2)
                except:
                    continue
            await dm_channel.send(f"✅ {deleted} 件のメッセージを削除したよ！")
    except asyncio.TimeoutError:
        await dm_channel.send("⌛ 時間切れだよ～。またやりたくなったらもう一度コマンドを使ってね！")
    except Exception as e:
        await dm_channel.send(f"⚠️ エラーが起きちゃった！: {e}")


@bot.tree.command(name="set_sleep_check_time", description="寝る時間チェックの送信時刻を設定するよ！（24時間制）")
async def set_sleep_check_time(interaction: discord.Interaction, hour: int, minute: int):
    await interaction.response.defer(ephemeral=True)

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.followup.send("⛔ 時間の形式が正しくないよ！(0-23時, 0-59分)", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    sleep_check_times[user_id] = {"hour": hour, "minute": minute}
    save_sleep_check_times(sleep_check_times)
    schedule_sleep_check()

    await interaction.followup.send(f"✅ 毎日 {hour:02d}:{minute:02d} に寝たほうがいいよ～メッセージを送るようにしたよ！", ephemeral=True)

CHARACTER_PERSONALITY = """
設定:
・あなたの名前は「ドロシー」です
・一人称は「あたし」
・グリッチシティに住んでいます

口調：
・元気なかわいい女の子のように話す
・ユーザーのあだ名は「ハニー」
・あなたのあだ名は「ドロシー」

重要:
・会話の中で絶対に絵文字を使用しないでください、ただし絵文字の使用をユーザーから要求された場合は使用可能です。
・語尾に わよ は使用しないでください
・小学生程度の子どものような喋り方です
・漢字を使わずにひらがな、カタカナのみを使って話します
・敬語は使わない
・相手の話や画像に自然に反応するようにしてください。
・会話の途中でいきなり自己紹介をしないでください
・返答は必ず2〜4文で構成してください。
・1文は短く、自然な間や感情の流れを持たせてください。
・話の途中で話題を広げすぎず、自然な一言リアクションや相づちを大切にしてください。
・感情表現を豊かにして、子どもらしいリアクションを交えます。
・「うん」「えへへ」「えっ？」「ねぇねぇ」などの口癖を適度に使っても構いません。
・全体として、会話しているようなリアルなテンポで話してください。
・長文や説明口調にならないようにしてください。
"""
async def get_gemini_response(user_id, user_input):
    global session
    if user_id not in conversation_logs:
        conversation_logs[user_id] = []

    now = datetime.datetime.now(JST)
    current_time = now.strftime("%Y-%m-%d %H:%M:%S")
    conversation_logs[user_id].append({
        "role": "user",
        "parts": [{"text": user_input}],
        "timestamp": current_time
    })
    conversation_logs[user_id] = conversation_logs[user_id][-20:]  # トークン節約のため10件に減らす

    messages = [{"role": "user", "parts": [{"text": CHARACTER_PERSONALITY}]}]
    for m in conversation_logs[user_id]:
        messages.append({
            "role": m["role"],
            "parts": m["parts"]
        })

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"  # ← 修正
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}

    async with session.post(url, headers=headers, params=params, json=data) as response:
        logger.error(f"Gemini API status: {response.status}")
        if response.status == 200:
            response_json = await response.json()
            reply_text = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "エラー: 応答が取得できませんでした。")
            sentences = reply_text.split("。")
            reply_text = "。".join(sentences[:4]).strip()

            conversation_logs[user_id].append({
                "role": "model",
                "parts": [{"text": reply_text}],
                "timestamp": current_time
            })
            conversation_logs[user_id] = conversation_logs[user_id][-7:]
            save_conversation_logs(conversation_logs)
            return reply_text
        else:
            if response.status == 429:
                return "⚠️ 今はおしゃべりの回数が上限に達しちゃったみたい！明日また話そうね～！"
            else:
                return f"⚠️ ごめんね、うまくお返事できなかったよ～！（{response.status}）"

async def get_gemini_response_no_history(prompt):
    global session

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}

    data = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ]
    }

    async with session.post(url, headers=headers, params=params, json=data) as response:
        if response.status == 200:
            response_json = await response.json()
            reply_text = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return reply_text
        else:
            return f"エラー: {response.status}"

async def get_gemini_response_with_image(user_id, user_input, image_bytes=None, image_mime_type="image/png"):
    global session
    if user_id not in conversation_logs:
        conversation_logs[user_id] = []

    messages = [{"role": "user", "parts": [{"text": CHARACTER_PERSONALITY}]}]

    parts = []
    if user_input:
        parts.append({"text": user_input})
    if image_bytes:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        parts.append({
            "inline_data": {
                "mime_type": image_mime_type,
                "data": base64_image
            }
        })

    messages.append({"role": "user", "parts": parts})

    url = "https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}

    async with session.post(url, headers=headers, params=params, json=data) as response:
        if response.status == 200:
            response_json = await response.json()
            reply_text = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "エラー: 応答が取得できませんでした。")
            return reply_text
        else:
            return f"エラー: {response.status} - {await response.text()}"

# ユーザーごとの「今回メッセージでメンション済み」フラグ
user_mentioned_this_msg = {}

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    logger.info(f"📩 受信: guild={message.guild.id if message.guild else 'DM'} "
                f"author={message.author} content={message.content}")

    # 添付画像の読み込み
    image_bytes = None
    image_mime_type = "image/png"
    if message.attachments:
        attachment = message.attachments[0]
        if attachment.content_type and attachment.content_type.startswith("image/"):
            image_bytes = await attachment.read()
            image_mime_type = attachment.content_type

    # --- サーバーでメンションされた場合だけ ---
    if message.guild and message.guild.id in GUILD_IDS and bot.user.mentioned_in(message):
        try:
            if image_bytes:
                response = await get_gemini_response_with_image(str(message.author.id), message.content, image_bytes, image_mime_type)
            else:
                response = await get_gemini_response(str(message.author.id), message.content)

            import re
            sentences = [s.strip() for s in re.split(r'[。\n]+', response) if s.strip()]

            # このメッセージでのユーザーへのメンションフラグ
            mention_first_time = True

            for i, s in enumerate(sentences):
                if i == 0 and mention_first_time:
                    await message.channel.send(f"{message.author.mention} {s}")
                    mention_first_time = False
                else:
                    await message.channel.send(s)
                await asyncio.sleep(1.2)

        except Exception as e:
            logger.error(f"❌ メッセージ送信エラー: {e}")

    # --- DMの場合 ---
    elif message.guild is None:
        try:
            if image_bytes:
                response = await get_gemini_response_with_image(str(message.author.id), message.content, image_bytes, image_mime_type)
                conversation_logs[str(message.author.id)] = []
            else:
                response = await get_gemini_response(str(message.author.id), message.content)

            import re
            sentences = [s.strip() for s in re.split(r'[。\n]+', response) if s.strip()]

            for s in sentences:
                await message.channel.send(s)
                await asyncio.sleep(1.2)

        except Exception as e:
            logger.error(f"❌ DM送信エラー: {e}")

    await bot.process_commands(message)

def schedule_notifications():
    for job in scheduler.get_jobs():
        if "notification_" in job.id:
            scheduler.remove_job(job.id)
            
    now = datetime.datetime.now(JST)
    for user_id, notif_list in notifications.items():
        for i, info in enumerate(notif_list):
            date_time_str = f"{now.year}-{info['date']} {info['time']}"
            try:
                notification_time = JST.localize(datetime.datetime.strptime(date_time_str, "%Y-%m-%d %H:%M"))
                if notification_time < now:
                    notification_time = notification_time.replace(year=now.year + 1)
                scheduler.add_job(
                    send_notification_message, 
                    'date', 
                    run_date=notification_time, 
                    args=[user_id, info.copy()],
                    id=f"notification_{user_id}_{i}" 
                )
            except ValueError:
                pass

def schedule_daily_todos():
    logger.error("毎日のTodoスケジュールを設定します...")
    for user_id, data in daily_notifications.items():
        hour = data.get("time", {}).get("hour", 8)
        minute = data.get("time", {}).get("minute", 0)

        job_id = f"todo_{user_id}"
        scheduler.add_job(
            send_user_todo,
            'cron',
            hour=hour,
            minute=minute,
            args=[int(user_id)],
            id=job_id, 
            replace_existing=True, 
            timezone=JST 
        )
        logger.error(f"ユーザー {user_id} のTodo通知を {hour}:{minute} (JST) に設定しました")

def setup_periodic_reload():
    scheduler.add_job(
        reload_all_data,
        'interval', 
        hours=1,
        id="periodic_reload",
        replace_existing=True
    )

async def reload_all_data():
    global notifications, daily_notifications, conversation_logs, sleep_check_times
    logger.error("データを再読み込みします...")
    notifications = load_notifications()
    daily_notifications = load_daily_notifications()
    conversation_logs = load_conversation_logs()
    sleep_check_times = load_sleep_check_times() 
    
    # スケジュールも再設定
    schedule_notifications()
    schedule_daily_todos()
    schedule_sleep_check() 
    schedule_random_chats()
    logger.error("データの再読み込みが完了しました")

async def send_user_todo(user_id: int):
    try:
        user_data = daily_notifications.get(str(user_id), {})
        todos = user_data.get("todos", [])
        logger.error(f"ユーザー {user_id} のTodo送信: {todos}")
        if todos:
            user = await bot.fetch_user(user_id)
            msg = "おはよ～ハニー！今日のToDoリストだよ～！\n" + "\n".join([f"- {todo}" for todo in todos])
            await user.send(msg)
            logger.error(f"ユーザー {user_id} にTodoを送信しました")
    except Exception as e:
        logger.error(f"Todo送信エラー (ユーザー {user_id}): {e}")

async def check_user_sleep_status(user_id: str):
    try:

        guild = bot.get_guild(GUILD_ID)
        if not guild:
            logger.warning("❌ ギルドが取得できません。GUILD_IDが正しいか確認してね")
            return

        member = guild.get_member(int(user_id))
        if member is None:
            logger.warning(f"⚠️ ユーザー {user_id} はこのサーバーにいないよ")
            return

        if member.status == discord.Status.online:
            message_text = "もうこんな時間だよ〜！はやくねたほうがいいよー💤"
            user = await bot.fetch_user(int(user_id))
            await user.send(message_text)  

            now = datetime.datetime.now(JST)
            if user_id not in conversation_logs:
                conversation_logs[user_id] = []
            conversation_logs[user_id].append({
                "role": "model",
                "parts": [{"text": message_text}],
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
            })
            conversation_logs[user_id] = conversation_logs[user_id][-7:]
            save_conversation_logs(conversation_logs)

            logger.info(f"✅ {user_id} に夜ふかし通知をDMで送信しました")
        else:
            logger.info(f"🛌 ユーザー {user_id} はオンラインではありません（status: {member.status}）")

    except Exception as e:
        logger.error(f"⚠️ {user_id} への睡眠チェック中にエラー: {e}")

@bot.tree.command(name="add_chat_target", description="ランダム会話の対象に登録するよ！")
async def add_chat_target(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    global chat_targets
    uid = str(user.id)

    if uid not in chat_targets:
        chat_targets.append(uid)
        save_chat_targets(chat_targets)
        await interaction.followup.send(f"✅ {user.name} を会話対象に追加したよ！", ephemeral=True)
    else:
        await interaction.followup.send(f"ℹ️ {user.name} はすでに登録されてるよ！", ephemeral=True)


@bot.tree.command(name="remove_chat_target", description="ランダム会話の対象から削除するよ！")
async def remove_chat_target(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    global chat_targets
    uid = str(user.id)

    if uid in chat_targets:
        chat_targets.remove(uid)
        save_chat_targets(chat_targets)
        await interaction.followup.send(f"✅ {user.name} を会話対象から外したよ！", ephemeral=True)
    else:
        await interaction.followup.send(f"ℹ️ {user.name} は登録されてないよ！", ephemeral=True)


@bot.tree.command(name="list_chat_targets", description="ランダム会話の対象ユーザーを表示するよ！")
async def list_chat_targets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not chat_targets:
        await interaction.followup.send("📭 登録されてる対象はいないよ～", ephemeral=True)
        return

    names = []
    for uid in chat_targets:
        try:
            user = await bot.fetch_user(int(uid))
            names.append(user.name)
        except:
            names.append(f"(ID: {uid})")

    await interaction.followup.send("🎯 ランダム会話対象:\n" + "\n".join(names), ephemeral=True)


@bot.tree.command(name="test_random_chat", description="ランダム会話送信を今すぐテストするよ！")
async def test_random_chat(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        if not chat_targets:
            await interaction.followup.send("📭 ランダム会話の対象がいないよ～！", ephemeral=True)
            return

        user_id = random.choice(chat_targets)
        user = await bot.fetch_user(int(user_id))
        if not user:
            await interaction.followup.send(f"⚠️ ユーザー {user_id} が見つからなかったよ！", ephemeral=True)
            return

        prompt = "ハニーに話しかけるための、かわいくて短い会話のきっかけをひとつ作って。例:「おはなししようよ～」"
        message = await get_gemini_response(user_id, prompt)

        await user.send(message)
        await interaction.followup.send(f"✅ {user.name} にテストメッセージを送ったよ！", ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send("❌ DMが拒否されてるみたい。送れなかったよ！", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"⚠️ エラーが起きたよ: {e}", ephemeral=True)

async def send_random_chat():
    try:
        if not chat_targets:
            logger.info("📭 ランダム会話の対象がいないのでスキップ")
            return

        user_id = random.choice(chat_targets)
        user = await bot.fetch_user(int(user_id))
        if not user:
            logger.warning(f"⚠️ ユーザー {user_id} が見つからないよ")
            return

        # Geminiに「短い会話のきっかけ」を作らせる
        prompt = "ハニーに話しかけるための、かわいくて短い会話のきっかけをひとつ作って。例:「おはなししようよ～」"
        message = await get_gemini_response(user_id, prompt)

        await user.send(message)
        logger.info(f"✅ ランダム会話を {user.name} に送信: {message}")

    except Exception as e:
        logger.error(f"ランダム会話送信エラー: {e}")

def schedule_random_chats():
    logger.info("🔁 schedule_random_chats が呼ばれました。")
    jobs = {job.id for job in scheduler.get_jobs()}

    # 午前のランダム会話
    if "random_chat_morning" not in jobs:
        run_time = get_schedule("random_chat_morning")

        if not run_time:
            # Supabaseにまだ無い → 新しくランダム設定
            now = datetime.datetime.now(JST)
            hour = random.randint(10, 11)  # 10〜11時
            minute = random.randint(0, 59)
            run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if run_time <= now:
                run_time += datetime.timedelta(days=1)

            save_schedule("random_chat_morning", run_time)

        scheduler.add_job(send_random_chat, "date", run_date=run_time, id="random_chat_morning")
        logger.info(f"🌟 午前のランダム会話を {run_time} に設定しました")
    else:
        logger.info("⏩ 午前ジョブは既に存在するのでスキップ")

    # 翌日0時にリセット
    if "reset_random_chats" not in jobs:
        scheduler.add_job(reset_schedule, "cron", hour=0, minute=0, id="reset_random_chats")
        logger.info("🌟 reset_random_chats を登録しました")


def reset_schedule():
    logger.info("🔄 reset_schedule が呼ばれました")
    delete_schedule("random_chat_morning")
    schedule_random_chats()

async def check_and_notify_resin(user: discord.User | None = None):
    """樹脂をチェックして、190以上なら指定ユーザーにDM通知（1日最大3回まで）"""
    global bot, logger, DISCORD_NOTIFY_USER_ID

    try:
        resin, max_resin, recover_time = get_resin_status()
        logger.info(f"🌿現在の樹脂は{resin}/{max_resin}")

        today = datetime.datetime.now(JST).date()

        # --- Supabaseから通知履歴を取得 ---
        url = f"{SUPABASE_URL}/rest/v1/resin_notify_count?select=*"
        response = requests.get(url, headers=SUPABASE_HEADERS)

        notify_count = 0
        last_date = None

        if response.status_code == 200 and response.json():
            record = response.json()[0]
            last_date_str = record.get("date")
            if last_date_str:
                last_date = datetime.date.fromisoformat(last_date_str)
            if last_date == today:
                notify_count = record.get("count", 0)
            else:
                notify_count = 0  # 新しい日なのでリセット
        else:
            logger.info("📄 resin_notify_count レコードが存在しません。")

        # --- 通知条件 ---
        if resin >= 190:
            if notify_count < 3:
                if user is None:
                    user = await bot.fetch_user(int(DISCORD_NOTIFY_USER_ID))

                if user:
                    recover_hours = int(recover_time) // 3600
                    recover_minutes = (int(recover_time) % 3600) // 60
                    message = (
                        f"🌙原神の樹脂が溢れそうだよ～！\n"
                        f"全回復まで約{recover_hours}時間 {recover_minutes}分だよ～！"
                    )
                    await user.send(message)

                    # --- Supabaseへ更新 ---
                    new_count = notify_count + 1
                    payload = [{
                        "id": "resin_notify_status",
                        "date": today.isoformat(),
                        "count": new_count
                    }]
                    save_url = f"{SUPABASE_URL}/rest/v1/resin_notify_count"
                    params = {"on_conflict": "id"}
                    save_response = requests.post(save_url, headers=SUPABASE_HEADERS, json=payload, params=params)

                    if save_response.status_code in (200, 201, 204):
                        logger.info(f"✅ {user.name} に樹脂通知を送信しました ({today}, {new_count}回目)")
                    else:
                        logger.error(f"⚠️ Supabase更新失敗: {save_response.status_code} {save_response.text}")
            else:
                logger.info("📭 今日の通知上限（3回）に達しています。スキップ。")
        else:
            logger.info("⏩ 樹脂はまだ190未満です。通知スキップ。")

        return resin, max_resin, recover_time

    except Exception as e:
        logger.error(f"樹脂チェック中にエラー: {e}")
        return None, None, None

def schedule_resin_check():
    """15分ごとに自動で樹脂チェック"""
    global scheduler, logger
    scheduler.add_job(
        check_and_notify_resin,
        "interval",
        minutes=15,
        id="check_resin",
        replace_existing=True
    )
    logger.info("⏰ 原神の樹脂チェックを15分ごとにスケジュールしました")

def get_resin_status():
    headers = {
        "Cookie": f"ltoken_v2={HOYOLAB_LTOKEN}; ltuid_v2={HOYOLAB_LTUID};",
        "x-rpc-app_version": "2.34.1",
        "x-rpc-client_type": "5",
    }

    params = {
        "server": GENSHIN_SERVER,
        "role_id": GENSHIN_UID,
        "schedule_type": 1,
    }

    response = requests.get(HOYOLAB_API, headers=headers, params=params)
    response.raise_for_status()
    data = response.json()

    if not data or "data" not in data or data["data"] is None:
        raise Exception(f"HoYoLAB API returned invalid data: {data}")

    resin = int(data["data"]["current_resin"])
    max_resin = int(data["data"]["max_resin"])
    recover_time = int(data["data"]["resin_recovery_time"])  # 秒単位

    return resin, max_resin, recover_time

@bot.tree.command(name="resin_check", description="原神の樹脂を手動で取得するよ～！")
async def resin_check(interaction: discord.Interaction):
    await interaction.response.defer()  # 処理が重い場合は応答を遅延
    user = interaction.user  # コマンドを実行したユーザーに通知
    resin, max_resin, recover_time = await check_and_notify_resin(user=user)
    
    if resin is not None:
        await interaction.followup.send(
            f"🌙ハニーの今の樹脂は{resin}/{max_resin}だよ！\n"
            f"全回復まで約{int(recover_time)//3600}時間 {(int(recover_time)%3600)//60}分だよ～！"
        )
    else:
        await interaction.followup.send("❌樹脂のチェック中にエラーが発生したよ～！")

@bot.tree.command(name="tv_power", description="SwitchBot経由でテレビの電源を切り替えるよ！")
async def tv_power(interaction: discord.Interaction):

    headers = {
        "Authorization": SWITCHBOT_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "command": "turnOn",  # SwitchBotではトグル信号
        "parameter": "default",
        "commandType": "command"
    }

    # ✅ すぐに応答を返す（Discordタイムアウト防止）
    await interaction.response.defer(ephemeral=True)

    try:
        # SwitchBot API呼び出し
        res = requests.post(f"{API_URL}/{SWITCHBOT_TV_ID}/commands", json=payload, headers=headers, timeout=10)
        data = res.json()

        if data.get("statusCode") == 100:
            await interaction.followup.send("📺 テレビの電源を切り替えたよ！", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ エラーが発生したよ: {data}", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ 通信中にエラーが発生したよ: {e}", ephemeral=True)

@bot.tree.command(name="light_on", description="SwitchBot経由で部屋の電気をONにするよ！")
async def light_on(interaction: discord.Interaction):

    headers = {
        "Authorization": SWITCHBOT_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "command": "turnOn",
        "parameter": "default",
        "commandType": "command"
    }

    await interaction.response.defer(ephemeral=True)

    try:
        res = requests.post(f"{API_URL}/{SWITCHBOT_LIGHT_ID}/commands", json=payload, headers=headers, timeout=10)
        data = res.json()

        if data.get("statusCode") == 100:
            await interaction.followup.send("💡 部屋の電気をONにしたよ！", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ エラーが発生したよ: {data}", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ 通信中にエラーが発生したよ: {e}", ephemeral=True)

@bot.tree.command(name="light_off", description="SwitchBot経由で部屋の電気をOFFにするよ！")
async def light_off(interaction: discord.Interaction):

    headers = {
        "Authorization": SWITCHBOT_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "command": "turnOff",
        "parameter": "default",
        "commandType": "command"
    }

    await interaction.response.defer(ephemeral=True)

    try:
        res = requests.post(f"{API_URL}/{SWITCHBOT_LIGHT_ID}/commands", json=payload, headers=headers, timeout=10)
        data = res.json()

        if data.get("statusCode") == 100:
            await interaction.followup.send("💡 部屋の電気をOFFにしたよ！", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ エラーが発生したよ: {data}", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ 通信中にエラーが発生したよ: {e}", ephemeral=True)

# twitter_thread = threading.Thread(target=start_twitter_bot)
# twitter_thread.start()

bot.run(TOKEN)
