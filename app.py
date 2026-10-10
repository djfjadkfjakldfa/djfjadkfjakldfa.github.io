import asyncio
import sys
import os
import sqlite3
import threading
import datetime
import random
import string
import json
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import requests
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# Windows 환경 이벤트 루프 충돌 방지
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 1. .env 파일 로드
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8000/callback")
MASTER_KEY = os.getenv("MASTER_KEY", "MASTERKEY123")

BACKUP_VERSION = 2

# ==========================================
# 2. 데이터베이스 초기화 (server.db, user.db)
# ==========================================
def init_databases():
    conn_server = sqlite3.connect("server.db")
    cursor_server = conn_server.cursor()
    cursor_server.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            auth_log_channel INTEGER,
            recovery_log_channel INTEGER,
            auth_panel_desc TEXT,
            recovery_panel_desc TEXT,
            auth_role_id INTEGER,
            auth_button_name TEXT DEFAULT '웹 인증하기',
            is_registered INTEGER DEFAULT 0,
            license_expire TEXT,
            master_key_used INTEGER DEFAULT 0,
            backup_data TEXT,
            registered_by INTEGER
        )
    """)
    cursor_server.execute("""
        CREATE TABLE IF NOT EXISTS license_keys (
            key TEXT PRIMARY KEY,
            days INTEGER,
            is_used INTEGER DEFAULT 0,
            key_type TEXT
        )
    """)
    cursor_server.execute("""
        CREATE TABLE IF NOT EXISTS recovery_keys (
            key TEXT PRIMARY KEY,
            guild_id INTEGER,
            max_uses INTEGER,
            used_count INTEGER DEFAULT 0
        )
    """)
    conn_server.commit()
    conn_server.close()

    conn_user = sqlite3.connect("user.db")
    cursor_user = conn_user.cursor()
    cursor_user.execute("""
        CREATE TABLE IF NOT EXISTS verified_users (
            guild_id INTEGER,
            user_id INTEGER,
            created_at TEXT,
            is_alt INTEGER,
            access_token TEXT,
            refresh_token TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    conn_user.commit()
    conn_user.close()

# ==========================================
# 3. FastAPI 웹 서버 (OAuth2 콜백 및 토큰 저장)
# ==========================================
app = FastAPI()

@app.get("/callback", response_class=HTMLResponse)
async def oauth_callback(code: str = None, state: str = None, error: str = None, error_description: str = None):
    if error:
        return f"<h3>❌ 디스코드 인증 거부/에러: {error} ({error_description})</h3>"

    if not code or not state:
        return "<h3>❌ 잘못된 접근이거나 인증 코드가 없습니다. 인증 패널을 통해 다시 시도해주세요.</h3>"

    try:
        guild_id = int(state)
    except ValueError:
        return "<h3>❌ 잘못된 서버 정보입니다.</h3>"

    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    token_res = requests.post(token_url, data=data, headers=headers)
    token_json = token_res.json()
    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token")

    if not access_token:
        return "<h3>❌ 토큰 발급 실패. 다시 시도해 주세요.</h3>"

    user_res = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"})
    user_data = user_res.json()

    user_id = int(user_data["id"])
    username = f"{user_data['username']}#{user_data.get('discriminator', '0')}"

    timestamp_ms = (user_id >> 22) + 1420070400000
    created_at_dt = datetime.datetime.utcfromtimestamp(timestamp_ms / 1000.0)
    created_at_str = created_at_dt.strftime("%Y-%m-%d %H:%M:%S")

    account_age_days = (datetime.datetime.utcnow() - created_at_dt).days
    is_alt = account_age_days < 14
    alt_status_icon = "❌" if is_alt else "✅"

    conn_server = sqlite3.connect("server.db")
    cursor_server = conn_server.cursor()
    cursor_server.execute("SELECT auth_role_id, auth_log_channel, is_registered, license_expire FROM guild_settings WHERE guild_id = ?", (guild_id,))
    row = cursor_server.fetchone()
    conn_server.close()

    if not row or row[2] != 1:
        return "<h3>❌ 이 서버는 등록되지 않았거나 라이센스가 만료되었습니다.</h3>"

    role_id, log_channel_id, _, expire_str = row

    if expire_str:
        expire_dt = datetime.datetime.strptime(expire_str, "%Y-%m-%d")
        if datetime.datetime.utcnow() > expire_dt:
            return "<h3>❌ 이 서버의 라이센스가 만료되었습니다.</h3>"

    bot_headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}

    conn_user = sqlite3.connect("user.db")
    cursor_user = conn_user.cursor()
    cursor_user.execute("""
        INSERT INTO verified_users (guild_id, user_id, created_at, is_alt, access_token, refresh_token)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET access_token = ?, refresh_token = ?
    """, (guild_id, user_id, created_at_str, 1 if is_alt else 0, access_token, refresh_token, access_token, refresh_token))
    conn_user.commit()
    conn_user.close()

    if role_id:
        role_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
        requests.put(role_url, headers=bot_headers)

    if log_channel_id:
        log_content = (
            f"🔐 **[웹 인증 성공]**\n"
            f"- **유저:** <@{user_id}> (`{username}` / ID: `{user_id}`)\n"
            f"- **계정 생성일:** `{created_at_str}`\n"
            f"- **부계정 여부:** {alt_status_icon} ({'의심됨' if is_alt else '정상'})\n"
            f"- **자동 참가 토큰:** 확보 완료 ✅"
        )
        log_url = f"https://discord.com/api/v10/channels/{log_channel_id}/messages"
        requests.post(log_url, headers=bot_headers, json={"content": log_content})

    return """
    <html>
        <head><title>인증 완료</title></head>
        <body style="font-family: Arial; text-align: center; margin-top: 50px;">
            <h2 style="color: #2ecc71;">🎉 디스코드 웹 인증이 완료되었습니다!</h2>
            <p>서버에서 역할이 지급되었습니다. 이 창을 닫으셔도 됩니다.</p>
        </body>
    </html>
    """

def run_web_server():
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


# ==========================================
# 4. 디스코드 봇 설정 및 가드
# ==========================================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def check_guild_registered(guild_id: int) -> bool:
    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_registered, license_expire, master_key_used FROM guild_settings WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or row[0] != 1:
        return False
    if row[2] == 1:
        return True
    if row[1]:
        expire_dt = datetime.datetime.strptime(row[1], "%Y-%m-%d")
        if datetime.datetime.utcnow() > expire_dt:
            return False
    return True


# ==========================================
# 5. UI 컴포넌트 (Views & Modals)
# ==========================================
class AuthView(discord.ui.View):
    def __init__(self, button_name: str, oauth_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=button_name, style=discord.ButtonStyle.link, url=oauth_url))

class LicenseExtendModal(discord.ui.Modal, title="라이센스 연장"):
    extend_key = discord.ui.TextInput(label="연장키 입력", placeholder="발급받은 연장키를 입력하세요", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.extend_key.value.strip()
        guild_id = interaction.guild.id

        conn = sqlite3.connect("server.db")
        cursor = conn.cursor()
        cursor.execute("SELECT days, is_used, key_type FROM license_keys WHERE key = ?", (key,))
        k_row = cursor.fetchone()

        if not k_row or k_row[1] == 1 or k_row[2] != 'extend':
            conn.close()
            await interaction.followup.send("❌ 유효하지 않거나 이미 사용된 연장키입니다.", ephemeral=True)
            return

        days = k_row[0]
        cursor.execute("SELECT license_expire FROM guild_settings WHERE guild_id = ?", (guild_id,))
        g_row = cursor.fetchone()

        current_expire = g_row[0] if g_row else None
        now = datetime.datetime.utcnow()

        if current_expire:
            exp_dt = datetime.datetime.strptime(current_expire, "%Y-%m-%d")
            base_dt = exp_dt if exp_dt > now else now
        else:
            base_dt = now

        new_expire = (base_dt + datetime.timedelta(days=days)).strftime("%Y-%m-%d")

        cursor.execute("UPDATE guild_settings SET license_expire = ? WHERE guild_id = ?", (new_expire, guild_id))
        cursor.execute("UPDATE license_keys SET is_used = 1 WHERE key = ?", (key,))
        conn.commit()
        conn.close()

        await interaction.followup.send(f"✅ 라이센스가 연장되었습니다! (새 만료일: `{new_expire}`)", ephemeral=True)

class RecoveryModal(discord.ui.Modal, title="서버 인원 복구 (자동 참가)"):
    recovery_key = discord.ui.TextInput(label="복구키 입력", placeholder="발급받은 복구키를 입력하세요", required=True)
    invite_code = discord.ui.TextInput(label="새로운 서버 초대코드 (뒷자리 또는 링크)", placeholder="예: r9YGA8Epb 또는 전체 링크", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.recovery_key.value.strip()
        invite_input = self.invite_code.value.strip()
        guild_id = interaction.guild.id

        invite_code = invite_input.split("/")[-1]

        bot_headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
        invite_res = requests.get(f"https://discord.com/api/v10/invites/{invite_code}", headers=bot_headers)

        if invite_res.status_code != 200:
            await interaction.followup.send("❌ 유효하지 않거나 만료된 초대코드입니다. 정확한 초대코드 뒷자리를 입력해주세요.", ephemeral=True)
            return

        invite_data = invite_res.json()
        target_guild_id = invite_data.get("guild", {}).get("id")

        if not target_guild_id:
            await interaction.followup.send("❌ 해당 초대코드로 서버 정보를 불러올 수 없습니다.", ephemeral=True)
            return

        conn = sqlite3.connect("server.db")
        cursor = conn.cursor()
        cursor.execute("SELECT max_uses, used_count, guild_id FROM recovery_keys WHERE key = ?", (key,))
        r_row = cursor.fetchone()

        if not r_row:
            conn.close()
            await interaction.followup.send("❌ 존재하지 않는 복구키입니다.", ephemeral=True)
            return

        max_uses, used_count, db_guild_id = r_row
        if db_guild_id != guild_id:
            conn.close()
            await interaction.followup.send("❌ 이 서버에서 발행된 복구키가 아닙니다.", ephemeral=True)
            return

        if used_count >= max_uses:
            conn.close()
            await interaction.followup.send("❌ 이미 사용 횟수가 소진된 복구키입니다.", ephemeral=True)
            return

        cursor.execute("UPDATE recovery_keys SET used_count = used_count + 1 WHERE key = ?", (key,))
        cursor.execute("SELECT recovery_log_channel FROM guild_settings WHERE guild_id = ?", (guild_id,))
        l_row = cursor.fetchone()
        conn.commit()
        conn.close()

        conn_user = sqlite3.connect("user.db")
        cursor_user = conn_user.cursor()
        cursor_user.execute("SELECT user_id, access_token FROM verified_users WHERE guild_id = ?", (guild_id,))
        users = cursor_user.fetchall()
        conn_user.close()

        success_count = 0
        fail_count = 0

        for u_id, token in users:
            if not token:
                fail_count += 1
                continue

            add_member_url = f"https://discord.com/api/v10/guilds/{target_guild_id}/members/{u_id}"
            payload = {"access_token": token}
            res = requests.put(add_member_url, headers=bot_headers, json=payload)

            if res.status_code in [201, 204]:
                success_count += 1
            else:
                fail_count += 1

        if l_row and l_row[0]:
            log_channel = interaction.guild.get_channel(l_row[0])
            if log_channel:
                await log_channel.send(
                    f"🔄 **[서버 인원 자동 복구 실행 결과]**\n"
                    f"- **실행자:** {interaction.user.mention}\n"
                    f"- **대상 서버 ID:** `{target_guild_id}`\n"
                    f"- **성공적으로 가입 처리된 유저:** {success_count}명\n"
                    f"- **실패/토큰만료 유저:** {fail_count}명"
                )

        await interaction.followup.send(
            f"✅ 복구 작업이 완료되었습니다!\n"
            f"- **성공:** {success_count}명\n"
            f"- **실패:** {fail_count}명",
            ephemeral=True
        )

class RecoveryPanelView(discord.ui.View):
    def __init__(self, is_owner_or_master: bool):
        super().__init__(timeout=None)
        self.is_owner_or_master = is_owner_or_master

    @discord.ui.button(label="복구키 사용하기", style=discord.ButtonStyle.secondary, custom_id="use_recovery_key_btn")
    async def handle_recovery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RecoveryModal())

    @discord.ui.button(label="라이센스 연장하기", style=discord.ButtonStyle.secondary, custom_id="extend_license_btn")
    async def handle_extend(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.is_owner_or_master:
            await interaction.response.send_message("❌ 마스터키 서버 또는 소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(LicenseExtendModal())


# ==========================================
# 6. 백그라운드 태스크 (만료 임박 알림)
# ==========================================
@tasks.loop(hours=24)
async def license_expire_check_task():
    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("SELECT guild_id, license_expire, registered_by, master_key_used FROM guild_settings WHERE is_registered = 1 AND master_key_used = 0")
    rows = cursor.fetchall()
    conn.close()

    now = datetime.datetime.utcnow()
    for guild_id, expire_str, reg_by, _ in rows:
        if not expire_str or not reg_by:
            continue
        expire_dt = datetime.datetime.strptime(expire_str, "%Y-%m-%d")
        remaining_days = (expire_dt - now).days

        if remaining_days == 3:
            try:
                user = await bot.fetch_user(reg_by)
                if user:
                    guild = bot.get_guild(guild_id)
                    guild_name = guild.name if guild else "서버"
                    await user.send(f"⚠️ **[라이센스 만료 경고]**\n관리하시는 **{guild_name}** 서버의 복구봇 라이센스가 **3일 뒤({expire_str})** 만료됩니다!")
            except Exception:
                pass


# ==========================================
# 7. 슬래시 명령어 정의
# ==========================================

@bot.tree.command(name="복구봇등록", description="라이센스 키 또는 마스터키를 입력해 해당 서버에 복구봇 기능을 활성화합니다.")
@app_commands.describe(키="발급받은 라이센스 키 또는 마스터키 입력")
async def register_bot(interaction: discord.Interaction, 키: str):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    is_master = (키 == MASTER_KEY)

    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()

    if is_master:
        cursor.execute("""
            INSERT INTO guild_settings (guild_id, is_registered, master_key_used, registered_by)
            VALUES (?, 1, 1, ?)
            ON CONFLICT(guild_id) DO UPDATE SET is_registered = 1, master_key_used = 1, registered_by = ?
        """, (guild_id, user_id, user_id))
        conn.commit()
        conn.close()
        await interaction.followup.send("👑 마스터키가 인증되어 복구봇 기능이 무제한 활성화되었습니다!", ephemeral=True)
    else:
        cursor.execute("SELECT days, is_used, key_type FROM license_keys WHERE key = ?", (키,))
        k_row = cursor.fetchone()

        if not k_row or k_row[1] == 1 or k_row[2] != 'license':
            conn.close()
            await interaction.followup.send("❌ 유효하지 않거나 이미 사용된 라이센스 키입니다.", ephemeral=True)
            return

        days = k_row[0]
        expire_date = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).strftime("%Y-%m-%d")

        cursor.execute("""
            INSERT INTO guild_settings (guild_id, is_registered, license_expire, registered_by)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET is_registered = 1, license_expire = ?, registered_by = ?
        """, (guild_id, expire_date, user_id, expire_date, user_id))

        cursor.execute("UPDATE license_keys SET is_used = 1 WHERE key = ?", (키,))
        conn.commit()
        conn.close()

        await interaction.followup.send(f"✅ 라이센스가 등록되었습니다! (만료일: `{expire_date}`)", ephemeral=True)


@bot.tree.command(name="라이센스발급", description="[Owner 전용] 라이센스 키를 생성합니다.")
@app_commands.describe(일수="라이센스 유효 일수")
async def generate_license(interaction: discord.Interaction, 일수: int):
    await interaction.response.defer(ephemeral=True)
    if not await bot.is_owner(interaction.user):
        await interaction.followup.send("❌ 봇 소유자만 사용할 수 있습니다.", ephemeral=True)
        return

    rand_key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO license_keys (key, days, key_type) VALUES (?, ?, 'license')", (rand_key, 일수))
    conn.commit()
    conn.close()

    await interaction.followup.send(f"🎟️ **라이센스 키:** `{rand_key}` ({일수}일)", ephemeral=True)


@bot.tree.command(name="연장키발급", description="[Owner 전용] 연장키를 생성합니다.")
@app_commands.describe(일수="연장될 일수")
async def generate_extend_key(interaction: discord.Interaction, 일수: int):
    await interaction.response.defer(ephemeral=True)
    if not await bot.is_owner(interaction.user):
        await interaction.followup.send("❌ 봇 소유자만 사용할 수 있습니다.", ephemeral=True)
        return

    rand_key = 'EXT-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO license_keys (key, days, key_type) VALUES (?, ?, 'extend')", (rand_key, 일수))
    conn.commit()
    conn.close()

    await interaction.followup.send(f"⏳ **연장키:** `{rand_key}` ({일수}일)", ephemeral=True)


@bot.tree.command(name="복구키", description="복구키를 생성합니다.")
@app_commands.describe(사용가능인원="사용할 수 있는 최대 인원")
async def generate_recovery_key(interaction: discord.Interaction, 사용가능인원: int):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id

    if not check_guild_registered(guild_id):
        await interaction.followup.send("❌ 이 서버는 등록되지 않았습니다.", ephemeral=True)
        return

    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("SELECT registered_by FROM guild_settings WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0] != interaction.user.id and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ 등록자 또는 관리자만 생성할 수 있습니다.", ephemeral=True)
        return

    if 사용가능인원 <= 0:
        await interaction.followup.send("❌ 인원은 1명 이상이어야 합니다.", ephemeral=True)
        return

    # DB에 저장된 인증 유저 수 확인
    conn_user = sqlite3.connect("user.db")
    cursor_user = conn_user.cursor()
    cursor_user.execute("SELECT COUNT(*) FROM verified_users WHERE guild_id = ?", (guild_id,))
    verified_count = cursor_user.fetchone()[0]
    conn_user.close()

    if verified_count < 사용가능인원:
        await interaction.followup.send(
            f"❌ 복구키를 발급할 수 없습니다.\n"
            f"- **요청 인원:** {사용가능인원}명\n"
            f"- **DB에 저장된 인증 유저:** {verified_count}명\n"
            f"저장된 유저 수 이하의 인원으로만 발급할 수 있습니다.",
            ephemeral=True
        )
        return

    rand_key = 'REC-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO recovery_keys (key, guild_id, max_uses) VALUES (?, ?, ?)", (rand_key, guild_id, 사용가능인원))
    conn.commit()
    conn.close()

    await interaction.followup.send(f"🔑 **복구키:** `{rand_key}` (최대 {사용가능인원}명)", ephemeral=True)


@bot.tree.command(name="설정", description="서버 설정을 일괄 지정합니다.")
@app_commands.describe(
    인증로그채널="인증 로그 채널",
    복구로그채널="복구 로그 채널",
    인증패널설명="인증 패널 설명",
    복구패널설명="복구 패널 설명",
    인증역할="지급할 역할",
    버튼이름="인증 버튼 이름"
)
async def settings_command(
    interaction: discord.Interaction,
    인증로그채널: discord.TextChannel,
    복구로그채널: discord.TextChannel,
    인증패널설명: str,
    복구패널설명: str,
    인증역할: discord.Role,
    버튼이름: str = "웹 인증하기"
):
    await interaction.response.defer(ephemeral=True)
    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE guild_settings SET
            auth_log_channel = ?,
            recovery_log_channel = ?,
            auth_panel_desc = ?,
            recovery_panel_desc = ?,
            auth_role_id = ?,
            auth_button_name = ?
        WHERE guild_id = ?
    """, (인증로그채널.id, 복구로그채널.id, 인증패널설명, 복구패널설명, 인증역할.id, 버튼이름, guild_id))
    conn.commit()
    conn.close()

    await interaction.followup.send("✅ 설정이 저장되었습니다!", ephemeral=True)


@bot.tree.command(name="인증패널", description="웹 인증 패널을 생성합니다.")
async def auth_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("SELECT auth_role_id, auth_panel_desc, auth_button_name FROM guild_settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if not row or not row[0]:
        await interaction.followup.send("❌ 먼저 `/설정`을 통해 인증 역할을 설정해주세요.", ephemeral=True)
        return

    _, desc, button_name = row

    oauth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify%20guilds.join"
        f"&state={interaction.guild.id}"
    )

    view = AuthView(button_name, oauth_url)
    await interaction.channel.send(embed=discord.Embed(title="🔐 서버 웹 인증", description=desc, color=discord.Color.blue()), view=view)
    await interaction.followup.send("✅ 인증 패널이 생성되었습니다.", ephemeral=True)


@bot.tree.command(name="복구패널", description="복구 패널을 생성합니다.")
async def recovery_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    is_owner = await bot.is_owner(interaction.user)
    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("SELECT master_key_used, recovery_panel_desc FROM guild_settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    master_used = row[0] if row else 0
    desc = row[1] if row and row[1] else "버튼을 눌러 복구키를 입력하세요."

    is_owner_or_master = is_owner or (master_used == 1)

    view = RecoveryPanelView(is_owner_or_master)
    await interaction.channel.send(embed=discord.Embed(title="🔄 서버 복구 센터", description=desc, color=discord.Color.green()), view=view)
    await interaction.followup.send("✅ 복구 패널이 생성되었습니다.", ephemeral=True)


@bot.tree.command(name="인원확인", description="인증된 유저 통계를 확인합니다.")
async def check_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    conn = sqlite3.connect("user.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM verified_users WHERE guild_id = ?", (interaction.guild.id,))
    total_verified = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM verified_users WHERE guild_id = ? AND is_alt = 1", (interaction.guild.id,))
    total_alts = cursor.fetchone()[0]
    conn.close()

    embed = discord.Embed(title="📊 백업 및 인증 통계", color=discord.Color.gold())
    embed.add_field(name="총 인증 유저 (복구 가능 대상)", value=f"{total_verified}명", inline=True)
    embed.add_field(name="부계정 의심 유저", value=f"{total_alts}명", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ------------------------------------------
# 서버 구조 백업 / 붙여넣기
# ------------------------------------------
def _overwrites_to_list(channel):
    """채널의 역할 권한 덮어쓰기를 저장 가능한 형태로 변환합니다. (멤버 개인 권한은 제외)"""
    result = []
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Role):
            continue
        allow, deny = overwrite.pair()
        result.append({
            "role": target.name,
            "everyone": target.is_default(),
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


def _build_overwrites(items, role_map, guild):
    result = {}
    for item in items or []:
        role = guild.default_role if item.get("everyone") else role_map.get(item.get("role"))
        if role is None:
            continue
        result[role] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(item.get("allow", 0)),
            discord.Permissions(item.get("deny", 0)),
        )
    return result


@bot.tree.command(name="서버저장", description="서버의 역할/카테고리/채널/권한 구조를 백업합니다.")
async def server_save(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        await interaction.followup.send("❌ 서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    guild = interaction.guild

    roles_data = [
        {
            "name": r.name,
            "color": r.color.value,
            "hoist": r.hoist,
            "mentionable": r.mentionable,
            "permissions": r.permissions.value,
            "position": r.position,
        }
        for r in guild.roles
        if not r.managed and not r.is_default()
    ]

    channels_data = []
    for c in guild.channels:
        channels_data.append({
            "id": c.id,
            "name": c.name,
            "type": str(c.type),
            "position": c.position,
            "category_id": getattr(c, "category_id", None),
            "overwrites": _overwrites_to_list(c),
            "topic": getattr(c, "topic", None),
            "nsfw": bool(getattr(c, "nsfw", False)),
            "slowmode_delay": getattr(c, "slowmode_delay", 0) or 0,
            "bitrate": getattr(c, "bitrate", None),
            "user_limit": getattr(c, "user_limit", None),
        })

    backup_dict = {
        "version": BACKUP_VERSION,
        "everyone_permissions": guild.default_role.permissions.value,
        "roles": roles_data,
        "channels": channels_data,
    }
    backup_json = json.dumps(backup_dict, ensure_ascii=False)

    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE guild_settings SET backup_data = ? WHERE guild_id = ?", (backup_json, guild.id))
    conn.commit()
    conn.close()

    n_cat = sum(1 for c in channels_data if c["type"] == "category")
    await interaction.followup.send(
        f"💾 서버 구조가 백업되었습니다!\n"
        f"- 역할 {len(roles_data)}개\n"
        f"- 카테고리 {n_cat}개\n"
        f"- 채널 {len(channels_data) - n_cat}개 (권한 포함)",
        ephemeral=True
    )


@bot.tree.command(name="섭붙여넣기", description="/서버저장으로 백업한 구조를 이 서버에 그대로 만듭니다.")
@app_commands.describe(기존채널삭제="True면 이 서버의 기존 채널을 모두 지우고 붙여넣습니다. (현재 채널 제외)")
async def server_paste(interaction: discord.Interaction, 기존채널삭제: bool = False):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        await interaction.followup.send("❌ 서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("SELECT backup_data FROM guild_settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if not row or not row[0]:
        await interaction.followup.send("❌ 백업 데이터가 없습니다. 먼저 `/서버저장`을 실행해주세요.", ephemeral=True)
        return

    try:
        data = json.loads(row[0])
    except Exception:
        await interaction.followup.send("❌ 백업 데이터가 손상되었습니다. `/서버저장`을 다시 실행해주세요.", ephemeral=True)
        return

    if data.get("version") != BACKUP_VERSION:
        await interaction.followup.send(
            "❌ 예전 방식으로 저장된 백업이라 복원할 수 없습니다.\n"
            "원본 서버에서 `/서버저장`을 다시 실행한 뒤 시도해주세요.",
            ephemeral=True
        )
        return

    guild = interaction.guild
    failed = []
    stats = {"roles": 0, "categories": 0, "channels": 0, "deleted": 0}

    # 0. 기존 채널 삭제 (선택)
    if 기존채널삭제:
        for ch in list(guild.channels):
            if ch.id == interaction.channel_id:
                continue
            try:
                await ch.delete(reason="섭붙여넣기: 기존 채널 삭제")
                stats["deleted"] += 1
            except Exception as e:
                failed.append(f"삭제 실패: #{ch.name} ({type(e).__name__})")

    # 1. @everyone 권한
    try:
        await guild.default_role.edit(
            permissions=discord.Permissions(data.get("everyone_permissions", 0)),
            reason="섭붙여넣기"
        )
    except Exception as e:
        failed.append(f"@everyone 권한 복원 실패 ({type(e).__name__})")

    # 2. 역할 (높은 순위부터 만들어야 순서가 유지됨)
    role_map = {}
    existing_roles = {r.name: r for r in guild.roles if not r.managed and not r.is_default()}

    for r in sorted(data.get("roles", []), key=lambda x: x["position"], reverse=True):
        if r["name"] in existing_roles:
            role_map[r["name"]] = existing_roles[r["name"]]
            continue
        try:
            role_map[r["name"]] = await guild.create_role(
                name=r["name"],
                color=discord.Color(r["color"]),
                hoist=r["hoist"],
                mentionable=r["mentionable"],
                permissions=discord.Permissions(r["permissions"]),
                reason="섭붙여넣기"
            )
            stats["roles"] += 1
        except Exception as e:
            failed.append(f"역할 '{r['name']}' ({type(e).__name__})")

    # 3. 카테고리
    channels = data.get("channels", [])
    category_map = {}

    for c in sorted([x for x in channels if x["type"] == "category"], key=lambda x: x["position"]):
        try:
            category_map[c["id"]] = await guild.create_category(
                name=c["name"],
                overwrites=_build_overwrites(c["overwrites"], role_map, guild),
                reason="섭붙여넣기"
            )
            stats["categories"] += 1
        except Exception as e:
            failed.append(f"카테고리 '{c['name']}' ({type(e).__name__})")

    # 4. 채널
    for c in sorted([x for x in channels if x["type"] != "category"], key=lambda x: x["position"]):
        category = category_map.get(c.get("category_id"))
        overwrites = _build_overwrites(c["overwrites"], role_map, guild)
        ctype = c["type"]

        try:
            if ctype in ("text", "news"):
                kwargs = dict(
                    name=c["name"],
                    category=category,
                    topic=c.get("topic") or None,
                    nsfw=c.get("nsfw", False),
                    slowmode_delay=c.get("slowmode_delay", 0),
                    overwrites=overwrites,
                    reason="섭붙여넣기"
                )
                if ctype == "news":
                    try:
                        await guild.create_text_channel(news=True, **kwargs)
                    except Exception:
                        await guild.create_text_channel(**kwargs)
                else:
                    await guild.create_text_channel(**kwargs)

            elif ctype == "voice":
                await guild.create_voice_channel(
                    name=c["name"],
                    category=category,
                    bitrate=min(c.get("bitrate") or 64000, guild.bitrate_limit),
                    user_limit=c.get("user_limit") or 0,
                    overwrites=overwrites,
                    reason="섭붙여넣기"
                )

            elif ctype == "stage_voice":
                await guild.create_stage_channel(
                    name=c["name"],
                    category=category,
                    overwrites=overwrites,
                    reason="섭붙여넣기"
                )

            elif ctype == "forum":
                await guild.create_forum(
                    name=c["name"],
                    category=category,
                    topic=c.get("topic") or None,
                    nsfw=c.get("nsfw", False),
                    overwrites=overwrites,
                    reason="섭붙여넣기"
                )

            else:
                failed.append(f"채널 '{c['name']}' (지원하지 않는 종류: {ctype})")
                continue

            stats["channels"] += 1

        except Exception as e:
            failed.append(f"채널 '{c['name']}' ({type(e).__name__})")

    msg = (
        f"✅ 붙여넣기 완료!\n"
        f"- 역할 {stats['roles']}개 생성\n"
        f"- 카테고리 {stats['categories']}개 생성\n"
        f"- 채널 {stats['channels']}개 생성"
    )
    if 기존채널삭제:
        msg += f"\n- 기존 채널 {stats['deleted']}개 삭제"
    if failed:
        shown = "\n".join(f"  · {f}" for f in failed[:15])
        more = f"\n  … 외 {len(failed) - 15}건" if len(failed) > 15 else ""
        msg += f"\n\n⚠️ 실패 {len(failed)}건\n{shown}{more}"

    await interaction.followup.send(msg[:1990], ephemeral=True)


# ==========================================
# 8. 봇 실행 이벤트
# ==========================================
@bot.event
async def on_ready():
    init_databases()
    if not license_expire_check_task.is_running():
        license_expire_check_task.start()

    print(f"로그인 완료: {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")
    except Exception as e:
        print(f"명령어 동기화 오류: {e}")

if __name__ == "__main__":
    if not TOKEN or not CLIENT_ID or not CLIENT_SECRET:
        print("❌ 오류: .env 파일 설정을 확인해주세요.")
    else:
        init_databases()
        web_thread = threading.Thread(target=run_web_server, daemon=True)
        web_thread.start()
        print("🌐 웹 인증 콜백 서버가 http://localhost:8000 에서 실행되었습니다.")

        bot.run(TOKEN)
