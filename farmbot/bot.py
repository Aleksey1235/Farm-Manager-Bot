from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, time, date

import discord
from discord.ext import commands, tasks

from .calc import format_duration, week_bounds, percent_change, latest_report_week_start
from .views import MainPanelView, AdminPanelView, ContentReviewView, PaymentReviewView, PendingAmountView, SessionReminderView


log = logging.getLogger("farmbot")


class FarmBot(commands.Bot):
    def __init__(self, config, db):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True  # нужен только для приёма фото в канале подтверждений
        super().__init__(command_prefix="__disabled__", intents=intents)
        self.config = config
        self.db = db
        self._ready_once = False
        self.pending_payment_uploads: dict[int, str] = {}

    async def setup_hook(self):
        await self.db.init()

        # Никаких slash-команд: удаляем старые команды от прошлых версий.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        guild_obj = discord.Object(id=self.config.guild_id)
        self.tree.clear_commands(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)

        self.add_view(MainPanelView(self))
        self.add_view(AdminPanelView(self))
        for row in await self.db.pending_content_reviews():
            self.add_view(ContentReviewView(self, row["id"], row["user_id"]))
        for row in await self.db.pending_payment_reviews():
            self.add_view(PaymentReviewView(self, row["user_id"], row["week_start"]))
        for row in await self.db.pending_daily_reports():
            self.add_view(PendingAmountView(self, row["user_id"], row["day"]))
        for row in await self.db.unresolved_session_reminders():
            self.add_view(SessionReminderView(self, row["user_id"], row["segment_started_at"]))

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self._ready_once:
            self._ready_once = True
            await self.sync_farm_roster()
            await self.ensure_panels()
            self.panel_loop.start()
            self.session_watch_loop.start()
            self.weekly_report_loop.start()
            self.roster_sync_loop.start()

    async def ensure_panels(self):
        await self._ensure_panel("main", self.config.panel_channel_id, self.main_panel_embed(), MainPanelView(self))
        await self._ensure_panel("admin", self.config.admin_channel_id, self.admin_panel_embed(), AdminPanelView(self))

    async def _ensure_panel(self, name, channel_id, embed, view):
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.error("Panel channel %s not found", channel_id)
            return
        saved = await self.db.get_panel(name)
        msg = None
        if saved:
            try:
                msg = await channel.fetch_message(saved["message_id"])
            except discord.HTTPException:
                msg = None
        if msg:
            await msg.edit(embed=embed, view=view)
        else:
            msg = await channel.send(embed=embed, view=view)
            await self.db.save_panel(name, channel.id, msg.id)

    def main_panel_embed(self):
        e = discord.Embed(
            title="🌾 FARM • Панель отдела",
            description=(
                "Все действия выполняются кнопками.\n\n"
                "🟢 **Начать фарм** — запускает учёт чистого времени.\n"
                "🔴 **Завершить фарм** — закрывает текущий отрезок.\n"
                "🎯 **Уехать на контент** — ставит фарм на паузу.\n"
                "↩️ **Вернуться с контента** — возобновляет фарм и требует фото участия.\n\n"
                "Время на контенте **никогда не входит** во время фарма."
            )
        )
        e.set_footer(text="FARM Manager Bot v2.3")
        return e

    def admin_panel_embed(self):
        return discord.Embed(
            title="⚙️ FARM • Руководство",
            description=(
                "Персональные суммы назначаются прямо под отчётами игроков. "
                "Здесь доступны карточки игроков, оплаты, сравнение недель, "
                "недельный отчёт и настройки."
            )
        )

    async def sync_farm_roster(self):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            return
        now = datetime.now(self.config.timezone)
        active_ids = set()
        for member in guild.members:
            if member.bot:
                continue
            if any(role.id == self.config.farm_role_id for role in member.roles):
                active_ids.add(member.id)
                await self.db.ensure_member(member.id, member.display_name, now)
        await self.db.sync_active_members(active_ids)

    async def refresh_panels(self):
        await self.refresh_main_panel()

    async def refresh_main_panel(self):
        saved = await self.db.get_panel("main")
        if not saved:
            return
        channel = self.get_channel(saved["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            msg = await channel.fetch_message(saved["message_id"])
        except discord.HTTPException:
            return
        now = datetime.now(self.config.timezone)
        rows = await self.db.active_states()
        lines = []
        for r in rows:
            if r["state"] == "farming":
                sec = int((now - datetime.fromisoformat(r["farming_started_at"])).total_seconds())
                lines.append(f"🟢 <@{r['user_id']}> — фармит `{format_duration(sec)}`")
            else:
                sec = int((now - datetime.fromisoformat(r["content_started_at"])).total_seconds())
                lines.append(f"🎯 <@{r['user_id']}> — на контенте `{format_duration(sec)}`")
        desc = "\n".join(lines) if lines else "Сейчас никто не фармит и не находится на контенте."
        embed = self.main_panel_embed()
        embed.add_field(name=f"Активность сейчас • {len(rows)}", value=desc[:1024], inline=False)
        try:
            await msg.edit(embed=embed, view=MainPanelView(self))
        except discord.HTTPException:
            pass

    @tasks.loop(seconds=15)
    async def panel_loop(self):
        await self.refresh_main_panel()

    @panel_loop.before_loop
    async def before_panel_loop(self):
        await self.wait_until_ready()

    async def notify_pending_amount(self, member: discord.Member, day: str, seconds: int):
        ch = self.get_channel(self.config.report_channel_id)
        if not isinstance(ch, discord.TextChannel):
            return

        threshold = await self.db.setting_int("farm_threshold_minutes")

        embed = discord.Embed(
            title="💵 Требуется назначить сумму",
            description=(
                f"Игрок: {member.mention}\n"
                f"Дата: **{day}**\n"
                f"Чистый фарм за день: **{format_duration(seconds)}**\n"
                f"Порог: **{threshold//60}ч {threshold%60}м**\n\n"
                f"Игрок завершил фарм ниже порога. "
                f"Руководству нужно назначить персональную сумму."
            ),
        )
        embed.set_footer(text="Нажмите кнопку ниже — вводить ID или дату не нужно.")

        existing = await self.db.daily_report_notification(member.id, day)
        if existing:
            return

        view = PendingAmountView(self, member.id, day)
        self.add_view(view)
        message = await ch.send(embed=embed, view=view)
        await self.db.save_daily_report_notification(member.id, day, ch.id, message.id)

    async def member_stats_embed(self, user_id: int, display_name: str):
        now = datetime.now(self.config.timezone)
        s = await self.db.member_week_summary(user_id, now.date(), self.config.timezone, now)
        e = discord.Embed(title=f"📊 {display_name} • неделя")
        e.add_field(name="Фарм", value=f"**{format_duration(s['total_seconds'])}**", inline=True)
        e.add_field(name="Контенты", value=f"**{s['approved_contents']}**", inline=True)
        e.add_field(name="Скидка", value=f"**−{s['discount_total']:,}$**".replace(",", " "), inline=True)
        lines = []
        for d in s["days"]:
            if d["status"] == "inactive":
                amount = "⚪ не фармил"
            elif d["status"] in ("farming", "open"):
                amount = "🟢 фарм не завершён"
            elif d["status"] == "pending":
                amount = "⏳ руководство назначает сумму"
            else:
                amount = f"{d['amount']:,}$".replace(",", " ")
            lines.append(f"**{d['day'].strftime('%d.%m')}** — `{format_duration(d['seconds'])}` → {amount}")
        e.add_field(name="По дням", value="\n".join(lines)[:1024] or "Нет данных", inline=False)
        due = "⏳ ещё не рассчитан: есть дни без суммы" if s["due"] is None else f"**{s['due']:,}$**".replace(",", " ")
        e.add_field(name="Предварительный недельный взнос", value=due, inline=False)
        e.set_footer(text=f"Скидка за 1 подтверждённый контент: {s['content_discount']:,}$".replace(",", " "))
        return e

    async def department_stats_embed(self):
        now = datetime.now(self.config.timezone)
        rows = await self.db.department_week_stats(now.date(), self.config.timezone, now)
        e = discord.Embed(title="📊 FARM • Статистика недели")
        if not rows:
            e.description = "Пока нет участников в базе."
            return e
        total_sec = sum(x["total_seconds"] for x in rows)
        total_contents = sum(x["approved_contents"] for x in rows)
        ready_due = sum(x["due"] for x in rows if x["due"] is not None)
        pending = sum(1 for x in rows if x["due"] is None)
        e.add_field(name="Общее время", value=format_duration(total_sec), inline=True)
        e.add_field(name="Контенты", value=str(total_contents), inline=True)
        e.add_field(name="Рассчитано взносов", value=f"{ready_due:,}$".replace(",", " "), inline=True)
        top = "\n".join(
            f"{i}. <@{r['user_id']}> — `{format_duration(r['total_seconds'])}` • 🎯 {r['approved_contents']}"
            for i, r in enumerate(rows[:10], 1)
        )
        e.add_field(name="ТОП по чистому фарму", value=top[:1024], inline=False)
        if pending:
            e.add_field(name="Ожидают расчёт", value=f"У **{pending}** участников есть день без назначенной суммы.", inline=False)
        return e

    async def my_day_embed(self, user_id: int, display_name: str):
        now = datetime.now(self.config.timezone)
        today = now.date()
        state = await self.db.get_state(user_id)
        work_day = today
        if state.get("state") in ("farming", "content") and state.get("work_day"):
            work_day = date.fromisoformat(state["work_day"])

        seconds = await self.db.farm_seconds_for_day(user_id, work_day, self.config.timezone, now)
        threshold = await self.db.setting_int("farm_threshold_minutes")
        auto_amount = await self.db.setting_int("auto_daily_amount")
        summary = await self.db.member_week_summary(user_id, today, self.config.timezone, now)

        today_row = next((x for x in summary["days"] if x["day"] == work_day), None)
        contents_today = 0
        start_dt = datetime.combine(today, datetime.min.time(), tzinfo=self.config.timezone)
        end_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        async with self.db.connect() as db:
            row = await (await db.execute("""
                SELECT COUNT(*) AS c FROM content_sessions
                WHERE user_id=? AND status='approved' AND started_at>=? AND started_at<?
            """, (user_id, start_dt.isoformat(), end_dt.isoformat()))).fetchone()
            contents_today = int(row["c"])

        remaining = max(0, threshold * 60 - seconds)
        day_label = "Сегодня" if work_day == today else f"Рабочий день {work_day.strftime('%d.%m')}"
        e = discord.Embed(title=f"📋 {display_name} • {day_label}")
        state_text = {
            "idle": "⚪ Не фармит",
            "farming": "🟢 Фармит сейчас",
            "content": "🎯 На контенте — фарм на паузе",
        }.get(state.get("state"), "⚪ Не фармит")
        e.add_field(name="Статус", value=state_text, inline=False)
        e.add_field(name="Чистый фарм", value=f"**{format_duration(seconds)}**", inline=True)
        e.add_field(name="Контенты", value=f"**{contents_today}** подтверждено", inline=True)

        if seconds >= threshold * 60:
            amount_text = f"✅ Порог достигнут\n**{auto_amount:,}$**".replace(",", " ")
            progress = "✅ Выполнено"
        else:
            progress = f"Осталось **{format_duration(remaining)}**"
            if today_row and today_row["status"] == "manual":
                amount_text = f"Руководство назначило: **{today_row['amount']:,}$**".replace(",", " ")
            elif today_row and today_row["status"] == "pending":
                amount_text = "⏳ Ожидает решения руководства"
            elif state.get("state") in ("farming", "content"):
                amount_text = "Пока не зафиксировано — фарм не завершён"
            else:
                amount_text = "Сумма появится после завершения фарма"

        e.add_field(name="До порога", value=progress, inline=True)
        e.add_field(name="Начисление за день", value=amount_text, inline=False)
        return e

    async def player_card_embed(self, user_id: int, display_name: str):
        now = datetime.now(self.config.timezone)
        s = await self.db.member_week_summary(user_id, now.date(), self.config.timezone, now)
        status = await self.db.payment_status(user_id, s["week_start"].isoformat())

        e = discord.Embed(
            title=f"👤 {display_name} • карточка FARM",
            description=f"Неделя **{s['week_start'].strftime('%d.%m')}–{s['week_end'].strftime('%d.%m.%Y')}**"
        )
        e.add_field(name="Чистый фарм", value=format_duration(s["total_seconds"]), inline=True)
        e.add_field(name="Активных дней", value=str(s["active_days"]), inline=True)
        e.add_field(name="Контентов", value=str(s["approved_contents"]), inline=True)
        e.add_field(name="До скидок", value=f"{s['gross']:,}$".replace(",", " "), inline=True)
        e.add_field(name="Скидка", value=f"−{s['discount_total']:,}$".replace(",", " "), inline=True)
        due_text = "⏳ не рассчитан" if s["due"] is None else f"{s['due']:,}$".replace(",", " ")
        e.add_field(name="Итоговый взнос", value=due_text, inline=True)

        payment_text = {
            "paid": "✅ Оплачен",
            "pending": "⏳ На проверке",
            "rejected": "❌ Отчёт отклонён",
            "open": "❌ Не оплачен",
        }.get(status, status)
        if s["due"] == 0:
            payment_text = "✅ Оплата не требуется"
        e.add_field(name="Оплата", value=payment_text, inline=False)

        lines = []
        for d in s["days"]:
            if d["status"] == "inactive":
                val = "⚪ не фармил"
            elif d["status"] == "pending":
                val = "⏳ ждёт сумму"
            elif d["status"] in ("farming", "open"):
                val = "🟢 не завершён"
            else:
                val = f"{d['amount']:,}$".replace(",", " ")
            day_names = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
            lines.append(f"**{day_names[d['day'].weekday()]} {d['day'].strftime('%d.%m')}** • `{format_duration(d['seconds'])}` → {val}")
        e.add_field(name="Дни недели", value="\n".join(lines)[:1024] or "Нет данных", inline=False)
        return e

    async def payment_category_embed(self, category: str, title: str):
        now = datetime.now(self.config.timezone)
        wb = week_bounds(now.date())
        overview = await self.db.payment_overview(wb.start, wb.end, self.config.timezone, now)
        rows = overview.get(category, [])
        e = discord.Embed(title=f"💰 FARM • {title}")
        if not rows:
            e.description = "В этой категории никого нет."
            return e

        lines = []
        for r in rows[:30]:
            if r["due"] is None:
                amount = "сумма не рассчитана"
            else:
                amount = f"{r['due']:,}$".replace(",", " ")
            lines.append(f"• <@{r['user_id']}> — **{amount}**")
        e.description = "\n".join(lines)[:4000]
        if len(rows) > 30:
            e.set_footer(text=f"Показано 30 из {len(rows)}")
        return e

    async def week_comparison_embed(self):
        now = datetime.now(self.config.timezone)
        current = week_bounds(now.date())
        prev_start = current.start - timedelta(days=7)
        prev_end = current.start - timedelta(days=1)

        cur = await self.db.department_snapshot(current.start, current.end, self.config.timezone, now)
        prev = await self.db.department_snapshot(prev_start, prev_end, self.config.timezone, now)

        def change(cur_val, prev_val, unit=""):
            pct = percent_change(cur_val, prev_val)
            if pct is None:
                delta = "новое значение" if cur_val else "0%"
            else:
                arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
                delta = f"{arrow} {abs(pct):.1f}%"
            return delta

        e = discord.Embed(
            title="📈 FARM • Эта неделя vs прошлая",
            description=(
                f"Текущая: **{current.start.strftime('%d.%m')}–{current.end.strftime('%d.%m')}**\n"
                f"Прошлая: **{prev_start.strftime('%d.%m')}–{prev_end.strftime('%d.%m')}**"
            )
        )
        e.add_field(
            name="⏱ Чистый фарм",
            value=f"{format_duration(cur['total_seconds'])}\nпрошлая: {format_duration(prev['total_seconds'])}\n**{change(cur['total_seconds'], prev['total_seconds'])}**",
            inline=True
        )
        e.add_field(
            name="👥 Активные",
            value=f"{cur['active_members']}\nпрошлая: {prev['active_members']}\n**{change(cur['active_members'], prev['active_members'])}**",
            inline=True
        )
        e.add_field(
            name="🎯 Контенты",
            value=f"{cur['contents']}\nпрошлая: {prev['contents']}\n**{change(cur['contents'], prev['contents'])}**",
            inline=True
        )
        e.add_field(
            name="⏱ Среднее на активного",
            value=f"{format_duration(cur['avg_seconds'])}\nпрошлая: {format_duration(prev['avg_seconds'])}\n**{change(cur['avg_seconds'], prev['avg_seconds'])}**",
            inline=True
        )
        e.add_field(
            name="🏷 Скидки",
            value=f"{cur['discounts']:,}$\nпрошлая: {prev['discounts']:,}$\n**{change(cur['discounts'], prev['discounts'])}**".replace(",", " "),
            inline=True
        )
        e.add_field(
            name="💰 Рассчитано к оплате",
            value=f"{cur['ready_due']:,}$\nпрошлая: {prev['ready_due']:,}$\n**{change(cur['ready_due'], prev['ready_due'])}**".replace(",", " "),
            inline=True
        )
        return e

    async def weekly_report_embed(self, week_start=None):
        now = datetime.now(self.config.timezone)
        if week_start is None:
            wb = week_bounds(now.date())
            week_start, week_end = wb.start, wb.end
        else:
            week_end = week_start + timedelta(days=6)

        snap = await self.db.department_snapshot(week_start, week_end, self.config.timezone, now)
        e = discord.Embed(
            title="🧾 FARM • Итоги недели",
            description=f"**{week_start.strftime('%d.%m.%Y')} — {week_end.strftime('%d.%m.%Y')}**"
        )
        e.add_field(name="👥 Участников", value=str(snap["members"]), inline=True)
        e.add_field(name="🌾 Фармили", value=str(snap["active_members"]), inline=True)
        e.add_field(name="⏱ Всего фарма", value=format_duration(snap["total_seconds"]), inline=True)
        e.add_field(name="🎯 Контентов", value=str(snap["contents"]), inline=True)
        e.add_field(name="💵 До скидок", value=f"{snap['gross']:,}$".replace(",", " "), inline=True)
        e.add_field(name="🏷 Скидки", value=f"−{snap['discounts']:,}$".replace(",", " "), inline=True)
        e.add_field(name="💰 Рассчитано к оплате", value=f"{snap['ready_due']:,}$".replace(",", " "), inline=True)
        e.add_field(name="✅ Оплатили", value=str(snap["paid"]), inline=True)
        e.add_field(name="⏳ На проверке", value=str(snap["payment_pending"]), inline=True)
        e.add_field(name="❌ Не оплатили", value=str(snap["unpaid"]), inline=True)
        e.add_field(name="⚠️ Не рассчитано", value=str(snap["unresolved"]), inline=True)

        top = "\n".join(
            f"{i}. <@{r['user_id']}> — `{format_duration(r['total_seconds'])}` • 🎯 {r['approved_contents']}"
            for i, r in enumerate(snap["rows"][:10], 1)
        )
        if top:
            e.add_field(name="ТОП по чистому фарму", value=top[:1024], inline=False)
        return e

    async def send_weekly_report_if_due(self):
        if not await self.db.setting_int("weekly_report_enabled"):
            return False
        now = datetime.now(self.config.timezone)
        weekday = await self.db.setting_int("weekly_report_weekday")
        hour = await self.db.setting_int("weekly_report_hour")
        minute = await self.db.setting_int("weekly_report_minute")
        candidate = latest_report_week_start(now, weekday, hour, minute)

        # Не отправляем недели до появления бота/участников без необходимости.
        if await self.db.report_was_sent(candidate):
            return False

        # Если ещё идёт фарм/контент, начатый в отчётной неделе,
        # не отправляем преждевременный итог. После завершения сессии
        # следующий проход цикла отправит полный отчёт один раз.
        candidate_end = candidate + timedelta(days=6)

        # Финальный недельный отчёт нельзя фиксировать до конца воскресенья:
        # иначе фарм, начатый после настроенного времени отчёта, потеряется.
        if now.date() <= candidate_end:
            return False

        active_rows = await self.db.active_states()
        for row in active_rows:
            work_day_raw = row.get("work_day")
            if not work_day_raw:
                continue
            active_work_day = date.fromisoformat(work_day_raw)
            if candidate <= active_work_day <= candidate_end:
                return False

        scheduled_day = candidate + timedelta(days=weekday)
        scheduled = datetime.combine(
            scheduled_day,
            time(hour=hour, minute=minute),
            tzinfo=self.config.timezone
        )
        if now < scheduled:
            return False

        snap = await self.db.department_snapshot(
            candidate, candidate + timedelta(days=6), self.config.timezone, now
        )
        has_meaningful_data = any((
            snap["total_seconds"],
            snap["contents"],
            snap["gross"],
            snap["ready_due"],
            snap["paid"],
            snap["payment_pending"],
        ))
        if not has_meaningful_data:
            await self.db.mark_report_sent(candidate, now, None)
            return False

        channel = self.get_channel(self.config.report_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        embed = await self.weekly_report_embed(candidate)
        msg = await channel.send(embed=embed)
        await self.db.mark_report_sent(candidate, now, msg.id)
        await self.db.audit(now, "weekly_report_sent", details=f"week={candidate.isoformat()}")
        return True

    @tasks.loop(seconds=60)
    async def weekly_report_loop(self):
        try:
            await self.send_weekly_report_if_due()
        except Exception:
            log.exception("Weekly report loop failed")

    @weekly_report_loop.before_loop
    async def before_weekly_report_loop(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=60)
    async def session_watch_loop(self):
        try:
            now = datetime.now(self.config.timezone)
            reminder_minutes = await self.db.setting_int("session_reminder_minutes")
            if reminder_minutes <= 0:
                return
            rows = await self.db.active_states()
            for r in rows:
                if r["state"] != "farming" or not r["farming_started_at"]:
                    continue
                started = datetime.fromisoformat(r["farming_started_at"])
                elapsed = (now - started).total_seconds()
                if elapsed < reminder_minutes * 60:
                    continue
                if await self.db.session_reminder_exists(r["user_id"], r["farming_started_at"]):
                    continue
                created = await self.db.create_session_reminder(
                    r["user_id"], r["farming_started_at"], now
                )
                if not created:
                    continue
                view = SessionReminderView(self, r["user_id"], r["farming_started_at"])
                self.add_view(view)
                text = (
                    f"⚠️ Вы фармите без перерыва уже **{format_duration(int(elapsed))}**.\n"
                    f"Сессия всё ещё активна?"
                )
                guild = self.get_guild(self.config.guild_id)
                member = guild.get_member(r["user_id"]) if guild else None
                sent = False
                if member:
                    try:
                        await member.send(text, view=view)
                        sent = True
                    except discord.HTTPException:
                        sent = False
                if not sent:
                    ch = self.get_channel(self.config.panel_channel_id)
                    if isinstance(ch, discord.TextChannel):
                        await ch.send(f"<@{r['user_id']}>\n{text}", view=view)
        except Exception:
            log.exception("Session watch loop failed")

    @session_watch_loop.before_loop
    async def before_session_watch_loop(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=5)
    async def roster_sync_loop(self):
        try:
            await self.sync_farm_roster()
        except Exception:
            log.exception("Roster sync loop failed")

    @roster_sync_loop.before_loop
    async def before_roster_sync_loop(self):
        await self.wait_until_ready()

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.attachments:
            return
        images = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
        if not images:
            return

        now = datetime.now(self.config.timezone)
        await self.db.ensure_member(
            message.author.id,
            getattr(message.author, "display_name", message.author.name),
            now
        )

        if message.channel.id == self.config.content_proof_channel_id:
            try:
                cid = await self.db.set_content_proof(message.author.id, images[0].url, message.id)
            except ValueError:
                return
            await self.db.audit(now, "content_proof", message.author.id, message.author.id, f"content_id={cid}")
            report_ch = self.get_channel(self.config.report_channel_id)
            if isinstance(report_ch, discord.TextChannel):
                embed = discord.Embed(
                    title="🎯 Контент • подтверждение участия",
                    description=f"Игрок: {message.author.mention}\\nКонтент ID: **#{cid}**\\nФото ожидает проверки руководством."
                )
                embed.set_image(url=images[0].url)
                view = ContentReviewView(self, cid, message.author.id)
                self.add_view(view)
                await report_ch.send(embed=embed, view=view)
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            return

        if message.channel.id == self.config.payment_proof_channel_id:
            week_start = self.pending_payment_uploads.get(message.author.id)
            if not week_start:
                return
            summary = await self.db.member_week_summary(
                message.author.id, now.date(), self.config.timezone, now
            )
            if summary["week_start"].isoformat() != week_start or summary["due"] is None:
                self.pending_payment_uploads.pop(message.author.id, None)
                return
            try:
                await self.db.set_payment_proof(
                    message.author.id, week_start, int(summary["due"]), images[0].url
                )
            except ValueError:
                self.pending_payment_uploads.pop(message.author.id, None)
                return
            self.pending_payment_uploads.pop(message.author.id, None)
            await self.db.audit(now, "payment_proof", message.author.id, message.author.id, f"week={week_start}")
            report_ch = self.get_channel(self.config.report_channel_id)
            if isinstance(report_ch, discord.TextChannel):
                embed = discord.Embed(
                    title="💰 Недельный взнос • проверка",
                    description=(
                        f"Игрок: {message.author.mention}\\n"
                        f"Неделя: **{week_start}**\\n"
                        f"Сумма: **{summary['due']:,}$**"
                    ).replace(",", " ")
                )
                embed.set_image(url=images[0].url)
                view = PaymentReviewView(self, message.author.id, week_start)
                self.add_view(view)
                await report_ch.send(embed=embed, view=view)
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
