import os
import random
import string
import colorama
import tls_client
import discord
from discord import app_commands
from discord.ext import commands

# 초기화 설정
colorama.init(convert=True)

# 봇 설정 (인텐트 설정)
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# 환경 변수에서 설정 불러오기
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID_ENV = os.getenv("LOG_CHANNEL_ID")

# 로그 채널 ID 정수 변환 (설정되지 않았거나 숫자가 아니면 None 처리)
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_ENV) if LOG_CHANNEL_ID_ENV and LOG_CHANNEL_ID_ENV.isdigit() else None

# 간단한 키 저장소
VALID_KEYS = {}  # 형식: { "키": 부스트개수 }

def log(text):
    print(f"{text}{colorama.Fore.RESET}")

class NitroBooster:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "en-US",
            "authorization": token,
            "referer": "https://discord.com/channels/@me",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9007 Chrome/91.0.4472.164 Electron/13.6.6 Safari/537.36",
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": "en-US",
            "x-super-properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiRGlzY29yZCBDbGllbnQiLCJyZWxlYXNlX2NoYW5uZWwiOiJzdGFibGUiLCJjbGllbnRfdmVyc2lvbiI6IjEuMC45MDA3Iiwib3NfdmVyc2lvbiI6IjEwLjAuMTkwNDMiLCJvc19hcmNoIjoieDY0Iiwic3lzdGVtX2xvY2FsZSI6ImVuLVVTIiwiY2xpZW50X2J1aWxkX251bWJlciI6MTYxODQyLCJjbGllbnRfZXZlbnRfc291cmNlIjpudWxsf",
        }
        self.session = tls_client.Session(client_identifier="chrome_107")
        self.sub_ids = []

    def removeTokenFromTxt(self):
        try:
            if not os.path.exists("tokens.txt"):
                return
            with open("tokens.txt", "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open("tokens.txt", "w", encoding="utf-8") as f:
                for line in lines:
                    if line.strip("\n") != self.token:
                        f.write(line)
        except Exception as e:
            print(f"토큰 파일 수정 중 오류 발생: {e}")

    def hasNitro(self):
        response = self.session.get(
            "https://discord.com/api/v9/users/@me/guilds/premium/subscription-slots",
            headers=self.headers,
        )
        
        if response.status_code == 401:
            log(f"{colorama.Fore.RED}Token is invalid (401 Unauthorized), removing.")
            self.removeTokenFromTxt()
            return False
        
        if response.status_code == 403:
            return False

        try:
            data = response.json()
            if isinstance(data, list):
                for sub in data:
                    self.sub_ids.append(sub["id"])
        except Exception as e:
            print(e)

        if len(self.sub_ids) == 0:
            return False
            
        return True

    def boostServer(self, guildID):
        boosted_count = 0
        for i in range(len(self.sub_ids)):
            self.headers["Content-Type"] = "application/json"
            r = self.session.put(
                url=f"https://discord.com/api/v9/guilds/{guildID}/premium/subscriptions",
                headers=self.headers,
                json={
                    "user_premium_guild_subscription_slot_ids": [f"{self.sub_ids[i]}"]
                },
            )
            if r.status_code == 201:
                boosted_count += 1
                log(f"{colorama.Fore.GREEN}Boosted {i + 1} of {len(self.sub_ids)} from {self.token[25:]}")
            elif r.status_code == 400:
                log(f"{colorama.Fore.YELLOW}Boost already used {i + 1} of {len(self.sub_ids)} from {self.token[25:]}")
        
        self.removeTokenFromTxt()
        return boosted_count


# 부스트 정보를 입력받는 팝업(Modal) 클래스
class BoostModal(discord.ui.Modal, title="서버 부스트 사용하기"):
    server_link = discord.ui.TextInput(
        label="서버 링크 또는 초대 코드",
        placeholder="https://discord.gg/xxxxxx 또는 초대 코드",
        required=True,
        max_length=100
    )
    boost_key = discord.ui.TextInput(
        label="부스트 키",
        placeholder="발급받은 부스트 키를 입력하세요",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        key = self.boost_key.value.strip()
        link = self.server_link.value.strip()

        if key not in VALID_KEYS:
            await interaction.followup.send("❌ 유효하지 않거나 이미 사용된 부스트 키입니다.", ephemeral=True)
            return

        required_boosts = VALID_KEYS[key]

        guild_id = None
        invite_code = link.split("/")[-1]
        
        try:
            invite = await bot.fetch_invite(invite_code)
            guild_id = str(invite.guild.id)
            guild_name = invite.guild.name
        except Exception:
            await interaction.followup.send("❌ 올바르지 않은 서버 링크이거나 봇이 서버 정보를 불러올 수 없습니다.", ephemeral=True)
            return

        if not os.path.exists("tokens.txt"):
            await interaction.followup.send("❌ tokens.txt 파일을 찾을 수 없습니다.", ephemeral=True)
            return

        with open("tokens.txt", "r", encoding="utf-8") as f:
            tokens = f.read().splitlines()

        if not tokens:
            await interaction.followup.send("❌ 사용 가능한 토큰이 없습니다.", ephemeral=True)
            return

        total_boosted = 0
        used_tokens_count = 0

        for token in tokens:
            if not token.strip():
                continue
            if total_boosted >= required_boosts:
                break

            booster = NitroBooster(token.strip())
            if booster.hasNitro():
                count = booster.boostServer(guild_id)
                total_boosted += count
                used_tokens_count += 1

        del VALID_KEYS[key]

        await interaction.followup.send(f"✅ 부스트 작업이 완료되었습니다! (총 {total_boosted}개 부스트 완료)", ephemeral=True)

        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                embed = discord.Embed(title="🚀 서버 부스트 사용 로그", color=discord.Color.blurple())
                embed.add_field(name="사용자", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=False)
                embed.add_field(name="대상 서버", value=f"{guild_name} (`{guild_id}`)", inline=False)
                embed.add_field(name="사용된 부스트 키", value=key, inline=True)
                embed.add_field(name="성공한 부스트 개수", value=f"{total_boosted}개", inline=True)
                embed.set_footer(text=f"사용 시각: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
                await log_channel.send(embed=embed)


class BoostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="부스트 사용하기", style=discord.ButtonStyle.green, custom_id="open_boost_modal")
    async def boost_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BoostModal())


@bot.event
async def on_ready():
    log(f"{colorama.Fore.GREEN}Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        log(f"{colorama.Fore.CYAN}Synced {len(synced)} command(s).")
    except Exception as e:
        print(e)


@bot.tree.command(name="부스트", description="서버 부스트 사용 버튼을 출력합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def boost_command(interaction: discord.Interaction):
    view = BoostView()
    await interaction.response.send_message("아래 버튼을 눌러 서버 부스트를 진행하세요.", view=view)


@bot.tree.command(name="키발급", description="지정한 개수(짝수)만큼 부스트가 가능한 키를 발급합니다.")
@app_commands.describe(개수="발급할 부스트 개수 (반드시 짝수 입력)")
@app_commands.checks.has_permissions(administrator=True)
async def generate_key(interaction: discord.Interaction, 개수: int):
    if 개수 % 2 != 0:
        await interaction.response.send_message("❌ 부스트 개수는 반드시 **짝수**여야 합니다.", ephemeral=True)
        return

    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    key = f"BOOST-{random_str}"

    VALID_KEYS[key] = 개수

    embed = discord.Embed(title="🔑 부스트 키 발급 완료", color=discord.Color.green())
    embed.add_field(name="발급된 키", value=f"`{key}`", inline=False)
    embed.add_field(name="부스트 수량", value=f"{개수}개", inline=False)
    embed.set_footer(text="이 키는 1회 사용 시 자동 소멸됩니다.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ 오류: DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")
    else:
        bot.run(DISCORD_TOKEN)
