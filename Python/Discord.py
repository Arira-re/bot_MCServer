import os
import discord
from discord import app_commands
from dotenv import load_dotenv
import subprocess
import asyncio
import re
import time
from collections import deque

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
BAT_PATH = os.getenv("BAT_PATH")
LOG_PATH = os.getenv("LOG_PATH")
DISCORD_LOG_CHANNEL_ID = int(os.getenv("DISCORD_LOG_CHANNEL_ID"))
START_BAT = os.getenv("START_BAT")

log_task = None
last_start_time = 0
start_stop_lock = asyncio.Lock()

join_pattern = re.compile(
    r"\[\d{3}月\d{4} (\d{2}:\d{2}:\d{2}\.\d{3})\] \[.*?\] \[.*?\]: ([\w\d_]+) joined the game"
)
left_pattern = re.compile(
    r"\[\d{3}月\d{4} (\d{2}:\d{2}:\d{2}\.\d{3})\] \[.*?\] \[.*?\]: ([\w\d_]+) left the game"
)

def get_mc_status(log_path):
    if not os.path.exists(log_path):
        return "サーバーログがありません"

    dq = deque(maxlen=50)
    with open(log_path, encoding="cp932") as f:
        for line in f:
            dq.append(line)

    # 1. エラー・異常終了の検知
    for line in reversed(dq):
        if "Encountered an unexpected exception" in line or "Exception" in line or "ERROR" in line:
            return "サーバーがクラッシュした可能性があります"
        if "Stopping server" in line or "Goodbye!" in line:
            return "サーバーは正常に停止しました"
        if "Done (" in line and "For help, type \"help\"" in line:
            return "サーバーは起動済みです"
    # 3. 50行以上あれば「起動済み」とみなす
    if len(dq) >= 50:
        return "サーバーは起動済みです"
    # それ以外
    return "サーバー状態が判定できません"

async def tail_log(client, interval=180):
    """latest.logを差分監視し、入退出をDiscordに送信（バッファリング付き）"""
    await asyncio.sleep(5)
    last_inode = None
    last_pos = 0
    buf = []
    last_sent = time.time()
    try:
        while True:
            if not os.path.exists(LOG_PATH):
                await asyncio.sleep(2)
                continue

            stat = os.stat(LOG_PATH)
            inode = getattr(stat, 'st_ino', None)
            if inode != last_inode:
                last_pos = 0
                last_inode = inode
                buf = []

            with open(LOG_PATH, "r", encoding="cp932") as f:
                f.seek(last_pos)
                lines = f.readlines()
                last_pos = f.tell()

            msgs = []
            for line in lines:
                join_match = join_pattern.search(line)
                if join_match:
                    time_str = join_match.group(1)
                    player = join_match.group(2)
                    print(f"[JOIN] {player} at {time_str}")
                    msgs.append(f"[JOIN] {player} at {time_str}")  # ←追加！
                left_match = left_pattern.search(line)
                if left_match:
                    time_str = left_match.group(1)
                    player = left_match.group(2)
                    print(f"[LEFT] {player} at {time_str}")
                    msgs.append(f"[LEFT] {player} at {time_str}")  # ←追加！
            if msgs:
                buf.extend(msgs)

            now = time.time()
            if now - last_sent >= interval and buf:
                channel = client.get_channel(DISCORD_LOG_CHANNEL_ID)
                if channel:
                    await channel.send("\n".join(buf))
                buf = []
                last_sent = now

            await asyncio.sleep(2)
    except asyncio.CancelledError:
        print("ログ監視タスクがキャンセルされました。")
    except Exception as e:
        print(f"ログ監視エラー: {e}")

class MyClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.mc_proc = None

    async def on_ready(self):
        print(f"Logged in as {self.user}")

    async def start_log_monitor(self):
        print("ログ監視起動")
        global log_task
        if log_task is not None and not log_task.done():
            return
        log_task = asyncio.create_task(tail_log(self))

    async def stop_log_monitor(self):
        print("ログ監視停止")
        global log_task
        if log_task is not None and not log_task.done():
            log_task.cancel()
            try:
                await log_task
            except asyncio.CancelledError:
                pass
        log_task = None

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)

        @self.tree.command(guild=guild, name="start", description="Minecraftサーバーを起動します")
        async def start_server(interaction: discord.Interaction):
            global last_start_time
            async with start_stop_lock:
                if self.mc_proc is not None and self.mc_proc.poll() is None:
                    await interaction.response.send_message("すでにサーバーは起動中です。", ephemeral=True)
                    return
                self.mc_proc = subprocess.Popen(
                    [BAT_PATH],
                    shell=True,
                    cwd=os.path.dirname(BAT_PATH),
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                last_start_time = time.time()
                await interaction.response.send_message("サーバーを起動しました。", ephemeral=True)
                print("サーバー起動")
                await asyncio.sleep(30)
                status = get_mc_status(LOG_PATH)
                await interaction.followup.send(f"サーバー状態: {status}", ephemeral=True)
                await self.start_log_monitor()

        @self.tree.command(guild=guild, name="stop", description="Minecraftサーバーを停止します")
        async def stop_server(interaction: discord.Interaction):
            global last_start_time
            async with start_stop_lock:
                status = get_mc_status(LOG_PATH)
                if not status.startswith("サーバーは起動済み"):
                    await interaction.response.send_message("サーバーは起動していません。", ephemeral=True)
                    return
                now = time.time()
                interval_sec = 120
                if now < last_start_time + interval_sec:
                    wait_sec = int(last_start_time + interval_sec - now)
                    await interaction.response.send_message(f"サーバー起動から2分間は/stopできません。あと{wait_sec}秒お待ちください。", ephemeral=True)
                    return
                if self.mc_proc is not None and self.mc_proc.poll() is None:
                    try:
                        self.mc_proc.stdin.write('stop\n')
                        self.mc_proc.stdin.flush()
                    except Exception as e:
                        await interaction.response.send_message(f"サーバー停止コマンド送信時にエラー: {e}", ephemeral=True)
                        return
                else:
                    # プロセスが消えていてもログ上は起動している場合、stopコマンドを送れない
                    await interaction.response.send_message("プロセスが見つかりません。手動停止が必要かもしれません。", ephemeral=True)
                    return
                await interaction.response.send_message("サーバー停止コマンドを送信しました。サーバーの終了にはしばらく時間がかかる場合があります。", ephemeral=True)
                print("サーバー停止")
                await self.stop_log_monitor()
                async def followup_status():
                    await asyncio.sleep(10)
                    status = get_mc_status(LOG_PATH)
                    await interaction.followup.send(f"サーバー状態: {status}", ephemeral=True)
                asyncio.create_task(followup_status())

        @self.tree.command(guild=guild, name="restart", description="Minecraftサーバーを再起動します")
        async def restart_server(interaction: discord.Interaction):
            global last_start_time
            async with start_stop_lock:
                await interaction.response.send_message("サーバーを再起動します。しばらくお待ちください。", ephemeral=True)
                print("サーバー再起動")
                status = get_mc_status(LOG_PATH)
                if status.startswith("サーバーは起動済み") and self.mc_proc is not None and self.mc_proc.poll() is None:
                    try:
                        self.mc_proc.stdin.write('stop\n')
                        self.mc_proc.stdin.flush()
                    except Exception:
                        pass
                    await asyncio.sleep(15)
                    try:
                        self.mc_proc.wait(timeout=15)
                    except Exception:
                        pass
                    self.mc_proc = None
                    await self.stop_log_monitor()
                self.mc_proc = subprocess.Popen(
                    [BAT_PATH],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    shell=True,
                    text=True,
                    cwd=os.path.dirname(BAT_PATH)
                )
                last_start_time = time.time()
                await asyncio.sleep(30)
                status = get_mc_status(LOG_PATH)
                await interaction.followup.send(f"サーバー状態: {status}", ephemeral=True)
                await self.start_log_monitor()

        @self.tree.command(guild=guild, name="status", description="Minecraftサーバーの状態を確認します")
        async def status_server(interaction: discord.Interaction):
            status = get_mc_status(LOG_PATH)
            await interaction.response.send_message(status, ephemeral=True)

        @self.tree.command(guild=guild, name="bot_restart", description="Bot自体を再起動します")
        async def bot_restart(interaction: discord.Interaction):
            await interaction.response.send_message("Botを再起動します...", ephemeral=True)
            await self.stop_log_monitor()
            await self.close()
            subprocess.Popen([START_BAT], shell=True)
            await self.close()
        
        @self.tree.command(guild=guild, name="force_stop", description="Minecraftサーバーを即座に停止します（クールダウンなし）")
        async def force_stop_server(interaction: discord.Interaction):
            global last_start_time
            async with start_stop_lock:
                status = get_mc_status(LOG_PATH)
                if not status.startswith("サーバーは起動済み"):
                    await interaction.response.send_message("サーバーは起動していません。", ephemeral=True)
                    return
                if self.mc_proc is not None and self.mc_proc.poll() is None:
                    try:
                        self.mc_proc.stdin.write('stop\n')
                        self.mc_proc.stdin.flush()
                    except Exception as e:
                        await interaction.response.send_message(f"サーバー停止コマンド送信時にエラー: {e}", ephemeral=True)
                        return
                else:
                    # プロセスが消えていてもログ上は起動している場合、stopコマンドを送れない
                    await interaction.response.send_message("プロセスが見つかりません。手動停止が必要かもしれません。", ephemeral=True)
                    return
                await interaction.response.send_message("サーバー停止コマンドを送信しました。サーバーの終了にはしばらく時間がかかる場合があります。", ephemeral=True)
                async def followup_status():
                    await asyncio.sleep(10)
                    status = get_mc_status(LOG_PATH)
                    await interaction.followup.send(f"サーバー状態: {status}", ephemeral=True)
                asyncio.create_task(followup_status())

        await self.tree.sync(guild=guild)

client = MyClient()
client.run(TOKEN)