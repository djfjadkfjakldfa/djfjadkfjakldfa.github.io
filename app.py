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

    # user.db: 유저 토큰(access_token, refresh_token) 컬럼 추가
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
    is_bot = user_data.get("bot", False)

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

    # 유저 정보 및 토큰 저장 (중복 시 토큰 갱신)
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
            f"🔐 **[웹 인증 성공 및 토큰 확보 로그]**\n"
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

        # 1. 초대코드에서 순수 코드 추출 (예: discord.gg/abcde -> abcde)
        invite_code = invite_input.split("/")[-1]

        # 2. 디스코드 API로 초대코드 정보(어떤 서버인지) 조회
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

        # 3. 복구키 유효성 검사
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

        # 4. user.db에서 인증된 유저들의 토큰 가져오기
        conn_user = sqlite3.connect("user.db")
        cursor_user = conn_user.cursor()
        cursor_user.execute("SELECT user_id, access_token FROM verified_users WHERE guild_id = ?", (guild_id,))
        users = cursor_user.fetchall()
        conn_user.close()

        success_count = 0
        fail_count = 0

        # 5. 새로운 복구 서버(target_guild_id)로 유저들 자동 참가시키기
        for u_id, token in users:
            if not token:
                fail_count += 1
                continue
            
            add_member_url = f"https://discord.com/api/v10/guilds/{target_guild_id}/members/{u_id}"
            payload = {"access_token": token}
            res = requests.put(add_member_url, headers=bot_headers, json=payload)
            
            # 201 Created 또는 204 No Content면 성공 (이미 가입된 경우도 성공 처리될 수 있음)
            if res.status_code in [201, 204]:
                success_count += 1
            else:
                fail_count += 1

        # 6. 로그 채널 전송
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

    @discord.ui.button(label="복구키 사용하기", style=discord.ButtonStyle.green, custom_id="use_recovery_key_btn")
    async def handle_recovery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RecoveryModal())

    @discord.ui.button(label="라이센스 연장하기", style=discord.ButtonStyle.blurple, custom_id="extend_license_btn")
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
    
    # 💡 중요: guilds.join 스코프를 포함하여 유저 토큰으로 서버에 자동 가입시킬 수 있는 권한을 요청합니다.
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


@bot.tree.command(name="서버저장", description="서버 구조를 백업합니다.")
async def server_save(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    guild = interaction.guild
    channels_data = [{"name": c.name, "type": str(c.type), "position": c.position} for c in guild.channels]
    roles_data = [{"name": r.name, "color": r.color.value} for r in guild.roles if not r.managed]

    backup_dict = {"channels": channels_data, "roles": roles_data}
    backup_json = json.dumps(backup_dict, ensure_ascii=False)

    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE guild_settings SET backup_data = ? WHERE guild_id = ?", (backup_json, guild.id))
    conn.commit()
    conn.close()

    await interaction.followup.send("💾 서버 구조가 백업되었습니다!", ephemeral=True)


@bot.tree.command(name="섭붙여넣기", description="서버 구조 데이터를 불러오는 기본 틀 기능입니다.")
async def server_paste(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not check_guild_registered(interaction.guild.id):
        await interaction.followup.send("❌ 먼저 `/복구봇등록`을 진행해주세요.", ephemeral=True)
        return

    conn = sqlite3.connect("server.db")
    cursor = conn.cursor()
    cursor.execute("SELECT backup_data FROM guild_settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if not row or not row[0]:
        await interaction.followup.send("❌ 백업 데이터가 없습니다.", ephemeral=True)
        return

    await interaction.followup.send("📂 백업된 서버 구조 데이터를 불러왔습니다.", ephemeral=True)


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
        web_thread = threading.Thread(target=run_web_server, daemon=True)
        web_thread.start()
        print("🌐 웹 인증 콜백 서버가 http://localhost:8000 에서 실행되었습니다.")

        bot.run(TOKEN)