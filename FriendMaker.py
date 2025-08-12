import discord
from discord import app_commands
from discord.ui import Select, View, Modal, TextInput, Button
from discord.ext import tasks
from datetime import datetime, time, date, timezone, timedelta
import asyncio
import os
import sqlite3
import json

BOT_TOKEN = "" 
GUILD_ID = 
DB_NAME = "FriendMaker.db"

# [추가] 한국 시간대(KST) 정의
KST = timezone(timedelta(hours=9))

# 인텐트 설정
intents = discord.Intents.default()
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

active_civil_wars = {} 
next_war_id = 1

# --- 게임 추가 ---
PREDEFINED_GAMES = [
    "리그 오브 레전드", "발로란트", "마인크래프트", "문명", 
    "DJMAX RESPECT V", "오버워치 2", "배틀그라운드", "이터널 리턴"
]

# --- 데이터베이스 초기화 및 헬퍼 함수 ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS civil_wars (
        war_id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL, start_datetime TEXT NOT NULL,
        games_list TEXT NOT NULL, description TEXT, message_id INTEGER, channel_id INTEGER,
        recruitment_end_datetime TEXT, is_recruiting INTEGER NOT NULL DEFAULT 1
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        war_id INTEGER NOT NULL, user_id INTEGER NOT NULL, game_name TEXT NOT NULL,
        FOREIGN KEY (war_id) REFERENCES civil_wars(war_id) ON DELETE CASCADE,
        PRIMARY KEY (war_id, user_id, game_name)
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS absent_participants (
        war_id INTEGER NOT NULL, user_id INTEGER NOT NULL, game_name TEXT NOT NULL, reason TEXT,
        FOREIGN KEY (war_id) REFERENCES civil_wars(war_id) ON DELETE CASCADE,
        PRIMARY KEY (war_id, user_id, game_name)
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reminder_sent (
        war_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        FOREIGN KEY (war_id) REFERENCES civil_wars(war_id) ON DELETE CASCADE,
        PRIMARY KEY (war_id, user_id)
    )""")
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_NAME)

# --- [수정된 부분] 시간 파싱 함수 ---
def parse_time_string(time_str: str) -> time | None:
    try:
        # strptime으로 생성된 naive time 객체에 KST 시간대 정보를 추가
        return datetime.strptime(time_str, "%H:%M").time().replace(tzinfo=KST)
    except ValueError:
        try:
            if "시" in time_str:
                parts = time_str.replace("오후", "").replace("오전", "").replace(" ", "").split("시")
                hour = int(parts[0])
                minute = 0
                if "분" in parts[1]:
                    minute = int(parts[1].replace("분", ""))
                
                if "오후" in time_str and hour < 12:
                    hour += 12
                elif "오전" in time_str and hour == 12: 
                    hour = 0
                # time 객체 생성 시 KST 시간대 정보를 직접 포함
                return time(hour, minute, tzinfo=KST)
            elif ":" in time_str:
                 hour, minute = map(int, time_str.split(':'))
                 # time 객체 생성 시 KST 시간대 정보를 직접 포함
                 return time(hour, minute, tzinfo=KST)
        except Exception:
            return None
    return None

def parse_time_string_to_datetime(time_str: str, reference_date: date | None = None) -> datetime | None:
    parsed_time = parse_time_string(time_str)
    if parsed_time:
        # 현재 날짜도 KST 기준으로 가져옴
        today_kst = datetime.now(KST).date()
        target_date = reference_date if reference_date else today_kst
        
        # 이제 parsed_time이 aware 객체이므로, dt_obj도 aware 객체가 됨
        dt_obj = datetime.combine(target_date, parsed_time)
        
        # 비교 대상인 datetime.now(KST)도 aware 객체이므로 정상적으로 비교 가능
        if not reference_date and dt_obj < datetime.now(KST):
            dt_obj += timedelta(days=1)
        return dt_obj
    return None

async def create_civil_war_games_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = []
    current_typed_games = [game.strip() for game in current.split(',') if game.strip()]
    last_typed_segment = ""
    if current.endswith(','):
        last_typed_segment = ""
    elif current_typed_games:
        last_typed_segment = current_typed_games[-1]
    else:
        last_typed_segment = current
    for game_name in PREDEFINED_GAMES:
        if last_typed_segment.lower() in game_name.lower():
            choices.append(app_commands.Choice(name=game_name, value=game_name))
    return choices[:25]


class CivilWarInfo:
    def __init__(self, war_id, host_id, start_datetime: datetime, games_list, description, 
                 message_id, channel_id, recruitment_end_datetime: datetime | None, 
                 is_recruiting: bool = True):
        self.war_id = war_id
        self.host_id = host_id
        self.start_datetime = start_datetime
        self.games_list = games_list 
        self.description = description
        self.message_id = message_id
        self.channel_id = channel_id
        self.participants = {} 
        self.absent_participants = {}
        self.message: discord.Message | None = None
        self.is_recruiting = is_recruiting 
        self.recruitment_end_datetime = recruitment_end_datetime
        self.reminder_sent_users = set()

    async def load_participants_from_db(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, game_name FROM participants WHERE war_id = ?", (self.war_id,))
        for row in cursor.fetchall():
            user_id, game_name = row
            if user_id not in self.participants:
                self.participants[user_id] = set()
            self.participants[user_id].add(game_name)
        
        cursor.execute("SELECT user_id, game_name, reason FROM absent_participants WHERE war_id = ?", (self.war_id,))
        for row in cursor.fetchall():
            user_id, game_name, reason = row
            if user_id not in self.absent_participants:
                self.absent_participants[user_id] = {}
            self.absent_participants[user_id][game_name] = reason

        cursor.execute("SELECT user_id FROM reminder_sent WHERE war_id = ?", (self.war_id,))
        for row in cursor.fetchall():
            self.reminder_sent_users.add(row[0])
        conn.close()

    def get_participant_count_for_game(self, game_name_to_check: str) -> int:
        count = 0
        for user_id, selected_games_set in self.participants.items():
            user_absences = self.absent_participants.get(user_id, {})
            if game_name_to_check in selected_games_set and game_name_to_check not in user_absences:
                count += 1
        return count

    def get_total_unique_participants(self) -> int:
        count = 0
        for user_id in self.participants:
            user_participated_games = self.participants.get(user_id, set())
            user_absent_games = self.absent_participants.get(user_id, {}).keys()
            if user_participated_games - set(user_absent_games):
                count +=1
        return count
        
    def get_embed(self, bot_client: discord.Client):
        host_user = bot_client.get_user(self.host_id)
        host_display = host_user.mention if host_user else f"주최자 (ID: {self.host_id})"
        
        current_time = datetime.now(KST)
        is_currently_recruiting = self.is_recruiting and (not self.recruitment_end_datetime or self.recruitment_end_datetime > current_time)

        title_suffix = ""
        if not is_currently_recruiting:
            title_suffix = " (모집 종료)"
        elif self.recruitment_end_datetime:
             end_time_display = self.recruitment_end_datetime.strftime("%m월 %d일 %H시 %M분")
             title_suffix += f" (모집 마감: {end_time_display})"

        embed = discord.Embed(
            title=f"🎮 내전 공지 안내{title_suffix}",
            description=f"**주최자:** {host_display}",
            color=discord.Color.gold() if is_currently_recruiting else discord.Color.dark_grey()
        )
        embed.add_field(name="⏰ 내전 시작 시간", value=self.start_datetime.strftime("%Y-%m-%d %H:%M"), inline=False)
        if self.recruitment_end_datetime:
             embed.add_field(name="⏳ 모집 마감 시간", value=self.recruitment_end_datetime.strftime("%Y-%m-%d %H:%M"), inline=False)
        
        if not self.games_list:
            embed.add_field(name="🎮 게임 목록", value="선택된 게임이 없습니다.", inline=False)
        else:
            embed.add_field(name="🎮 게임 목록", value=", ".join(self.games_list), inline=False)
            for game_name_in_list in self.games_list:
                participant_names = []
                for user_id, user_selected_games_set in self.participants.items():
                    user_absences_for_this_war = self.absent_participants.get(user_id, {})
                    if game_name_in_list in user_selected_games_set and game_name_in_list not in user_absences_for_this_war:
                        user = bot_client.get_user(user_id)
                        participant_names.append(user.display_name if user else f"유저ID({user_id})")
                participant_count = len(participant_names)
                value_str = ", ".join(participant_names) if participant_names else "아직 참여자가 없습니다."
                embed.add_field(name=f"➥ {game_name_in_list} 참여자 ({participant_count}명)", value=value_str, inline=True)
        
        embed.add_field(name="📝 상세 설명", value=self.description, inline=False)
        
        absent_display_list = []
        for user_id, absent_games_dict in self.absent_participants.items():
            if absent_games_dict: 
                user_obj = bot_client.get_user(user_id)
                user_display = user_obj.display_name if user_obj else f"유저ID({user_id})"
                game_reasons = []
                for game, reason in absent_games_dict.items():
                    game_reasons.append(f"**{game}**") 
                if game_reasons: 
                    absent_display_list.append(f"{user_display} / {', '.join(game_reasons)}")
        
        if absent_display_list: 
            embed.add_field(name="😥 불참자 명단", value="\n".join(absent_display_list), inline=False)
        
        footer_text = f"내전 ID: {self.war_id}"
        if is_currently_recruiting:
            footer_text += " | 아래 버튼으로 참여, 불참은 /내전불참 명령어 사용"
        else:
            footer_text += " | 모집이 종료되었습니다."
        embed.set_footer(text=footer_text)
        return embed

class CivilWarActionView(View):
    def __init__(self, war_info: CivilWarInfo):
        super().__init__(timeout=None)
        for game_name in war_info.games_list:
            button = Button(
                label=game_name + " 내전 참여하기",
                style=discord.ButtonStyle.primary, emoji='🕹️',
                custom_id=f"join_toggle:{war_info.war_id}:{game_name}"
            )
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        custom_id_parts = interaction.data['custom_id'].split(':')
        war_id_from_button = int(custom_id_parts[1])
        game_name = custom_id_parts[2]

        live_war_info = active_civil_wars.get(war_id_from_button)
        
        current_time = datetime.now(KST)
        recruitment_ended = live_war_info and live_war_info.recruitment_end_datetime and live_war_info.recruitment_end_datetime <= current_time

        if not live_war_info or not live_war_info.is_recruiting or recruitment_ended:
            if live_war_info and live_war_info.is_recruiting:
                live_war_info.is_recruiting = False
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("UPDATE civil_wars SET is_recruiting = 0 WHERE war_id = ?", (live_war_info.war_id,))
                conn.commit()
                conn.close()
                print(f"참여 시도 중 내전 ID {live_war_info.war_id}의 모집 상태를 종료로 수정했습니다.")
            
            await interaction.response.send_message("모집이 종료되었거나 만료된 내전입니다.", ephemeral=True)
            
            try: 
                view = View.from_message(interaction.message)
                if view:
                    for child in view.children:
                        if isinstance(child, Button): child.disabled = True
                    updated_embed = live_war_info.get_embed(interaction.client) 
                    await interaction.message.edit(embed=updated_embed, view=view)
            except Exception: pass
            return

        user_id = interaction.user.id
        
        if user_id not in live_war_info.participants:
            live_war_info.participants[user_id] = set()
        if user_id not in live_war_info.absent_participants:
            live_war_info.absent_participants[user_id] = {}

        is_absent = game_name in live_war_info.absent_participants.get(user_id, {})
        is_participating = game_name in live_war_info.participants.get(user_id, set())
        
        feedback_message = ""
        made_change = False

        conn = get_db_connection()
        cursor = conn.cursor()

        if is_absent:
            del live_war_info.absent_participants[user_id][game_name]
            if not live_war_info.absent_participants[user_id]:
                del live_war_info.absent_participants[user_id]
            cursor.execute("DELETE FROM absent_participants WHERE war_id=? AND user_id=? AND game_name=?", (live_war_info.war_id, user_id, game_name))
            live_war_info.participants[user_id].add(game_name)
            cursor.execute("INSERT OR IGNORE INTO participants (war_id, user_id, game_name) VALUES (?,?,?)", (live_war_info.war_id, user_id, game_name))
            feedback_message = f"'{game_name}' 게임 불참을 취소하고 다시 참여했습니다 ☺️"
            made_change = True
        
        elif is_participating:
            await interaction.response.send_message(f"이미 '{game_name}' 내전에 참여 중입니다. 참여를 취소하려면 `/내전불참` 명령어를 사용해주세요.", ephemeral=True)
        
        else:
            live_war_info.participants[user_id].add(game_name)
            cursor.execute("INSERT OR IGNORE INTO participants (war_id, user_id, game_name) VALUES (?,?,?)", (live_war_info.war_id, user_id, game_name))
            feedback_message = f"'{game_name}' 내전에 참여의사를 밝혔습니다 😊"
            made_change = True

        conn.commit()
        conn.close()

        if made_change:
            try:
                updated_embed = live_war_info.get_embed(interaction.client)
                await interaction.message.edit(embed=updated_embed)
            except Exception as e:
                print(f"버튼 콜백에서 임베드 업데이트 실패 (war_id: {live_war_info.war_id}): {e}")
        if feedback_message:
            await interaction.response.send_message(feedback_message, ephemeral=True)

# --- 명령어 정의 ---
@tree.command(name="내전생성", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    시작시간="내전 시작 시간 (예: 21:00 또는 오후 9시)",
    모집종료시간="모집 종료 시간 (예: 23:50 또는 오후 11시 50분)",
    게임목록="플레이할 게임 (쉼표로 구분, 예: 리그 오브 레전드, 발로란트)",
    상세설명="내전 규칙, 참가 조건 등 상세 내용"
)
@app_commands.autocomplete(게임목록=create_civil_war_games_autocomplete)
async def create_civil_war(interaction: discord.Interaction, 시작시간: str, 모집종료시간: str, 게임목록: str, 상세설명: str):
    if not 게임목록:
        await interaction.response.send_message("하나 이상의 게임을 입력해야 합니다.", ephemeral=True)
        return
    parsed_start_datetime = parse_time_string_to_datetime(시작시간)
    if not parsed_start_datetime:
        await interaction.response.send_message(f"(!) 시작 시간 형식이 올바르지 않습니다. (입력값: {시작시간})", ephemeral=True)
        return
    parsed_recruitment_end_datetime = parse_time_string_to_datetime(모집종료시간)
    if not parsed_recruitment_end_datetime:
        await interaction.response.send_message(f"(!) 모집 종료 시간 형식이 올바르지 않습니다. (입력값: {모집종료시간})", ephemeral=True)
        return

    input_games_original_case = [game.strip() for game in 게임목록.split(',') if game.strip()]
    input_games_lower_case_set = {game.lower() for game in input_games_original_case}
    if not input_games_original_case:
        await interaction.response.send_message("유효한 게임 이름이 하나 이상 포함되어야 합니다.", ephemeral=True)
        return

    conflicting_original_games = set()
    for existing_war_id, existing_war_info in active_civil_wars.items():
        if not existing_war_info.is_recruiting:
            continue
        existing_games_lower_set = {g.lower() for g in existing_war_info.games_list}
        intersection = input_games_lower_case_set.intersection(existing_games_lower_set)
        if intersection:
            for conflict_lower in intersection:
                for orig_game in input_games_original_case: 
                    if orig_game.lower() == conflict_lower:
                        conflicting_original_games.add(orig_game)
                        break 
    if conflicting_original_games:
        games_str = ", ".join(list(conflicting_original_games))
        await interaction.response.send_message(f"(!) 다음 게임에 대한 내전이 이미 모집 중입니다: **{games_str}**", ephemeral=True)
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    global next_war_id 
    current_war_id = next_war_id
    cursor.execute("""
        INSERT INTO civil_wars (war_id, host_id, start_datetime, games_list, description, message_id, channel_id, recruitment_end_datetime, is_recruiting)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (current_war_id, interaction.user.id, parsed_start_datetime.isoformat(), json.dumps(input_games_original_case), 상세설명, 
          None, interaction.channel_id, parsed_recruitment_end_datetime.isoformat() if parsed_recruitment_end_datetime else None, 1))
    conn.commit()
    
    war_info = CivilWarInfo(
        war_id=current_war_id, host_id=interaction.user.id, start_datetime=parsed_start_datetime, 
        games_list=input_games_original_case, description=상세설명, message_id=None,
        channel_id=interaction.channel_id, recruitment_end_datetime=parsed_recruitment_end_datetime, is_recruiting=True 
    )
    active_civil_wars[war_info.war_id] = war_info
    next_war_id +=1

    view = CivilWarActionView(war_info)
    initial_embed = war_info.get_embed(interaction.client)
    await interaction.response.send_message(
        content="@everyone", embed=initial_embed, 
        allowed_mentions=discord.AllowedMentions(everyone=True), view=view
    )
    original_message = await interaction.original_response()
    
    war_info.message_id = original_message.id
    war_info.message = original_message
    
    cursor.execute("UPDATE civil_wars SET message_id = ? WHERE war_id = ?", (original_message.id, war_info.war_id))
    conn.commit()
    conn.close()
    print(f"내전 생성됨 (DB 저장): ID {war_info.war_id}, 게임: {input_games_original_case}")

@tree.command(name="내전삭제", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(내전id="삭제할 내전의 ID")
async def delete_civil_war(interaction: discord.Interaction, 내전id: int):
    war_info = active_civil_wars.get(내전id)
    if not war_info:
        await interaction.response.send_message(f"ID '{내전id}' 내전을 찾을 수 없습니다.", ephemeral=True)
        return
    if war_info.host_id != interaction.user.id:
        await interaction.response.send_message("자신이 생성한 내전만 삭제할 수 있습니다.", ephemeral=True)
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM civil_wars WHERE war_id = ?", (내전id,))
        conn.commit()
        conn.close()
        if war_info.message:
            deleted_embed = discord.Embed(title=f"ID {war_info.war_id} 내전 - 삭제됨", description="이 내전은 주최자에 의해 삭제되었습니다.", color=discord.Color.dark_red())
            await war_info.message.edit(content="내전 삭제됨.", embed=deleted_embed, view=None)
        if 내전id in active_civil_wars:
            del active_civil_wars[내전id]
        await interaction.response.send_message(f"ID '{내전id}' 내전이 삭제되었습니다.", ephemeral=True)
        print(f"내전 삭제됨: ID {내전id} by {interaction.user}")
    except Exception as e:
        await interaction.response.send_message(f"내전 삭제 중 오류: {e}", ephemeral=True)
        print(f"내전 삭제 중 오류 (ID: {내전id}): {e}")

@tree.command(name="내전불참", guild=discord.Object(id=GUILD_ID))
async def leave_civil_war_games(interaction: discord.Interaction):
    user_id = interaction.user.id 
    eligible_wars_for_absence_select = []
    for war_id, war_info in active_civil_wars.items():
        if not war_info.is_recruiting:
            continue
        user_participated_games = war_info.participants.get(user_id, set())
        user_absent_games_in_this_war = war_info.absent_participants.get(user_id, {}).keys()
        if user_participated_games - set(user_absent_games_in_this_war): 
            eligible_wars_for_absence_select.append(war_info)
    if not eligible_wars_for_absence_select:
        await interaction.response.send_message("불참 처리할 수 있는 내전이 없습니다.", ephemeral=True)
        return
    war_absence_select_view = View()
    war_absence_select_view.add_item(WarForAbsenceSelect(interaction.client, user_id))
    await interaction.response.send_message("불참 처리할 내전을 선택하세요:", view=war_absence_select_view, ephemeral=True)

class WarForAbsenceSelect(Select):
    def __init__(self, bot_client: discord.Client, user_id: int):
        self.bot_client = bot_client
        self.user_id = user_id
        options = []
        for war_id, war_info in active_civil_wars.items():
            current_time = datetime.now(KST)
            is_currently_recruiting = war_info.is_recruiting and (not war_info.recruitment_end_datetime or war_info.recruitment_end_datetime > current_time)
            if not is_currently_recruiting:
                continue

            user_participated_games = war_info.participants.get(user_id, set())
            user_absent_games_in_this_war = war_info.absent_participants.get(user_id, {}).keys()
            eligible_games_for_absence = user_participated_games - set(user_absent_games_in_this_war)
            if eligible_games_for_absence:
                label_games = f"({', '.join(list(eligible_games_for_absence)[:2])} 등)" if eligible_games_for_absence else ""
                label = f"ID {war_id}: {', '.join(war_info.games_list[:2])} 등 {label_games}"
                options.append(discord.SelectOption(label=label[:100], value=str(war_id)))
        super().__init__(placeholder="불참 처리할 내전을 선택하세요.", min_values=1, max_values=1, 
                         options=options if options else [discord.SelectOption(label="불참 처리할 (모집중인) 참여 내전 없음", value="_no_wars_", disabled=True)])
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "_no_wars_":
            await interaction.response.edit_message(content="불참 처리할 참여 중인 내전이 없습니다.", view=None)
            return
        selected_war_id = int(self.values[0])
        war_info = active_civil_wars.get(selected_war_id)
        if not war_info:
            await interaction.response.edit_message(content="선택한 내전을 찾을 수 없습니다.", view=None) 
            return
        
        current_time = datetime.now(KST)
        if war_info.recruitment_end_datetime and war_info.recruitment_end_datetime <= current_time:
            await interaction.response.edit_message(content=f"ID {war_info.war_id} 내전은 모집이 종료되어 불참 처리할 수 없습니다.", view=None)
            return

        view = View()
        view.add_item(GamesToAbsentSelect(war_info, self.bot_client, self.user_id))
        await interaction.response.edit_message(content="불참할 게임을 선택하세요:", view=view)

class GamesToAbsentSelect(Select):
    def __init__(self, war_info: CivilWarInfo, bot_client: discord.Client, user_id: int):
        self.war_info = war_info
        self.bot_client = bot_client
        self.user_id = user_id
        options = []
        user_participated_games = war_info.participants.get(user_id, set())
        user_absent_games_in_this_war = war_info.absent_participants.get(user_id, {}).keys()
        selectable_games_for_absence = user_participated_games - set(user_absent_games_in_this_war)
        if not selectable_games_for_absence:
            options.append(discord.SelectOption(label="불참 가능한 게임 없음", value="_no_games_", disabled=True))
        else:
            for game_name in selectable_games_for_absence:
                options.append(discord.SelectOption(label=game_name, value=game_name))
        super().__init__(placeholder="불참할 게임을 선택하세요. (다중 선택 가능)", min_values=1, 
                         max_values=len(options) if options and options[0].value != "_no_games_" else 1, options=options)
    
    async def callback(self, interaction: discord.Interaction):
        if self.values and self.values[0] == "_no_games_":
            await interaction.response.edit_message(content="불참 가능한 게임이 없습니다.", view=None)
            return
        
        live_war_info = active_civil_wars.get(self.war_info.war_id)
        current_time = datetime.now(KST)
        if live_war_info and live_war_info.recruitment_end_datetime and live_war_info.recruitment_end_datetime <= current_time:
             await interaction.response.edit_message(content=f"ID {self.war_info.war_id} 내전은 모집이 종료되어 불참 처리할 수 없습니다.", view=None)
             return

        games_to_make_absent = set(self.values) 
        absence_modal = AbsenseReasonModal(self.war_info, games_to_make_absent)
        await interaction.response.send_modal(absence_modal)
        await interaction.edit_original_response(content="불참 사유를 입력해주세요...", view=None)

class AbsenseReasonModal(Modal):
    def __init__(self, war_info: CivilWarInfo, games_to_absent: set[str]):
        super().__init__(title=f"ID {war_info.war_id} 게임 불참 사유")
        self.war_info = war_info
        self.games_to_absent = games_to_absent 
        games_str = ", ".join(games_to_absent)
        self.reason = TextInput(label=f"'{games_str}' 불참 사유 (최대 200자)", placeholder="개인 사정입니다.", required=True, max_length=200, style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        reason_text = self.reason.value
        user_id = interaction.user.id
        live_war_info = active_civil_wars.get(self.war_info.war_id)
        if not live_war_info or not live_war_info.is_recruiting:
            await interaction.response.send_message("모집이 종료되었거나 만료된 내전입니다.", ephemeral=True)
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        
        if user_id not in live_war_info.absent_participants:
            live_war_info.absent_participants[user_id] = {}
            
        changed_games_count = 0
        for game_name in self.games_to_absent:
            live_war_info.absent_participants[user_id][game_name] = reason_text
            cursor.execute("""
                INSERT OR REPLACE INTO absent_participants (war_id, user_id, game_name, reason)
                VALUES (?, ?, ?, ?)
            """, (live_war_info.war_id, user_id, game_name, reason_text))
            if user_id in live_war_info.participants:
                live_war_info.participants[user_id].discard(game_name)
            cursor.execute("""
                DELETE FROM participants WHERE war_id = ? AND user_id = ? AND game_name = ?
            """, (live_war_info.war_id, user_id, game_name))
            changed_games_count += 1
        
        conn.commit()
        conn.close()
        feedback_msg = f"선택한 {changed_games_count}개 게임에 대한 불참(사유: {reason_text})이 등록되었습니다."
        try:
            original_message = live_war_info.message
            if not original_message and live_war_info.message_id:
                try:
                    channel = client.get_channel(live_war_info.channel_id)
                    if channel:
                        original_message = await channel.fetch_message(live_war_info.message_id)
                        live_war_info.message = original_message
                except (discord.NotFound, discord.Forbidden): pass
            if original_message:
                updated_embed = live_war_info.get_embed(client) 
                await original_message.edit(embed=updated_embed)
            await interaction.response.send_message(content=feedback_msg, ephemeral=True)
        except Exception as e:
            print(f"불참 처리 중 오류 (war_id: {live_war_info.war_id}): {e}")
            await interaction.response.send_message(content=f"{feedback_msg}\n(공지 업데이트 중 오류 발생)", ephemeral=True)

# --- 자동 작업들 ---
@tasks.loop(seconds=60.0)
async def check_recruitment_end_task():
    now = datetime.now(KST)
    for war_id, war_info in list(active_civil_wars.items()): 
        if war_info.is_recruiting and war_info.recruitment_end_datetime:
            if now >= war_info.recruitment_end_datetime:
                war_info.is_recruiting = False
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("UPDATE civil_wars SET is_recruiting = 0 WHERE war_id = ?", (war_id,))
                conn.commit()
                conn.close()
                print(f"내전 ID {war_id} 모집 자동 종료 (DB 업데이트됨).")
                if war_info.message:
                    try:
                        updated_embed = war_info.get_embed(client)
                        view = View.from_message(war_info.message)
                        if view:
                            for item in view.children:
                                item.disabled = True
                        await war_info.message.edit(embed=updated_embed, view=view) 
                    except Exception as e:
                        print(f"내전 ID {war_id} 공지(모집종료) 업데이트 중 오류: {e}")

@tasks.loop(seconds=60.0)
async def check_war_start_reminders():
    now = datetime.now(KST)
    for war_id, war_info in list(active_civil_wars.items()):
        if war_info.start_datetime: 
            time_until_start = war_info.start_datetime - now
            if timedelta(seconds=0) < time_until_start <= timedelta(minutes=10):
                for user_id, participated_games_set in war_info.participants.items():
                    if user_id not in war_info.reminder_sent_users:
                        user_absences = war_info.absent_participants.get(user_id, {})
                        actual_participating_games = participated_games_set - user_absences.keys()
                        if actual_participating_games: 
                            try:
                                user = await client.fetch_user(user_id) 
                                if user:
                                    games_str = ", ".join(list(actual_participating_games))
                                    dm_message = (f"{user.mention}님, 잠시 후 **{war_info.start_datetime.strftime('%H시 %M분')}**에\n"
                                                  f"{games_str} 내전이 시작될 예정입니다! \n잊지 말고 참여해주세요! 😘")
                                    await user.send(dm_message)
                                    war_info.reminder_sent_users.add(user_id)
                                    conn = get_db_connection()
                                    cursor = conn.cursor()
                                    cursor.execute("INSERT OR IGNORE INTO reminder_sent (war_id, user_id) VALUES (?, ?)", (war_id, user_id))
                                    conn.commit()
                                    conn.close()
                                    print(f"DM 알림 발송 성공 (DB 기록): {user.name} (내전 ID: {war_id})")
                            except Exception as e:
                                print(f"DM 알림 발송 중 오류: User ID {user_id}, 내전 ID {war_id} - {e}")

@client.event
async def on_ready():
    global next_war_id, active_civil_wars
    init_db() 
    print("데이터베이스 초기화 완료.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT war_id, host_id, start_datetime, games_list, description, message_id, channel_id, recruitment_end_datetime, is_recruiting FROM civil_wars")
    loaded_wars_count = 0
    max_db_war_id = 0
    for row in cursor.fetchall():
        war_id, host_id, start_dt_str, games_json, desc, msg_id, chan_id, rec_end_dt_str, is_rec = row
        
        start_dt = datetime.fromisoformat(start_dt_str).astimezone(KST)
        rec_end_dt = datetime.fromisoformat(rec_end_dt_str).astimezone(KST) if rec_end_dt_str else None
        
        games = json.loads(games_json)
        
        current_time = datetime.now(KST)
        actual_is_recruiting = bool(is_rec)
        if actual_is_recruiting and rec_end_dt and rec_end_dt <= current_time:
            actual_is_recruiting = False

        war = CivilWarInfo(war_id, host_id, start_dt, games, desc, msg_id, chan_id, rec_end_dt, actual_is_recruiting)
        await war.load_participants_from_db() 
        if war.message_id and war.channel_id:
            try:
                channel = client.get_channel(war.channel_id)
                if channel:
                    war.message = await channel.fetch_message(war.message_id)
            except (discord.NotFound, discord.Forbidden): pass
            except Exception as e:
                print(f"내전 ID {war.war_id} 메시지 로드 중 오류: {e}")
        
        active_civil_wars[war_id] = war
        if war.message_id:
            view = CivilWarActionView(war)
            if not war.is_recruiting:
                for item in view.children:
                    item.disabled = True
            client.add_view(view, message_id=war.message_id)

        loaded_wars_count += 1
        if war_id > max_db_war_id:
            max_db_war_id = war_id
            
    next_war_id = max_db_war_id + 1 if loaded_wars_count > 0 else 1
    print("SQLite DB 로드를 완료했습니다! 🚀🚀")
    print(f"{loaded_wars_count}개의 내전 정보를 DB에서 로드했습니다. 다음 내전 ID: {next_war_id}")
    conn.close()

    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f'{client.user} (ID: {client.user.id})으로 로그인했습니다.')
        print(f'명령어가 서버 ID {GUILD_ID}에 동기화되었습니다.')
        if not check_recruitment_end_task.is_running(): 
            check_recruitment_end_task.start() 
            print("모집 종료 자동 체크 작업 시작됨.")
        if not check_war_start_reminders.is_running(): 
            check_war_start_reminders.start()
            print("내전 시작 10분 전 알림 작업 시작됨.")
        print('봇이 준비되었습니다!')
    except Exception as e:
        print(f"동기화 중 오류 발생: {e}")

if __name__ == "__main__":
    try:
        client.run(BOT_TOKEN)
    except discord.errors.LoginFailure:
        print("CRITICAL: 봇 토큰이 유효하지 않습니다. 디스코드 개발자 포털에서 토큰을 확인해주세요.")
    except Exception as e:
        print(f"봇 실행 중 오류 발생: {e}")
